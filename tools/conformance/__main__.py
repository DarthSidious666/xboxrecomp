"""Differential conformance: execute each snippet on the real CPU, then lift it
and execute the lifted C, and compare.

    py -3 -m tools.conformance            # run everything
    py -3 -m tools.conformance -k neg     # just the cases matching a substring

Why this works here and not on other recomp projects: ps3recomp has to build an
independent *model* of PowerPC to check its lifter against, because the host is
not a PPC. We target x86 and run on x86, so the host CPU is the reference
implementation -- an oracle no model can be wrong about.

The flow, per case:

  1. MSVC assembles the snippet (we never hand-encode), bracketed by nop
     markers, and /FAc hands back the exact bytes it produced.
  2. Those bytes go through our real Disassembler + Lifter.
  3. A harness runs both versions over the same inputs and compares eax.

Needs a 32-bit MSVC (vcvars32). Guest addresses map 1:1 onto host addresses
here (g_xbox_mem_offset = 0), so a memory operand reads the same bytes on both
sides.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

from .cases import CASES

_WHY = {c["name"]: c["why"] for c in CASES}

MARK = ["nop", "nop", "nop"]
_MARK_BYTES = "90 90 90"

# "  00009\t3c ff\t\t cmp\t al, -1"  ->  addr, bytes.
#
# The byte column holds at most five bytes and then WRAPS, with the remainder
# on the next line under a leading tab and no address:
#
#     00009\t8b 0d 00 00 00
#   \t00\t\t mov\t ecx, DWORD PTR _g_in_b
#
# Missing the continuation silently drops every instruction longer than five
# bytes -- which is every 32-bit immediate -- and a dropped instruction is the
# one failure mode this whole tool exists to catch.
_COD_LINE = re.compile(
    r"^\s*([0-9a-fA-F]{5,})\t([0-9a-fA-F]{2}(?: [0-9a-fA-F]{2})*)(?:\t|$)")
_COD_CONT = re.compile(
    r"^\t([0-9a-fA-F]{2}(?: [0-9a-fA-F]{2})*)(?:\t|$)")
_COD_PROC = re.compile(r"^(\S+)\s+PROC\b")
_COD_ENDP = re.compile(r"^(\S+)\s+ENDP\b")


def _find_vcvars():
    for root in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("ProgramFiles", r"C:\Program Files")):
        for ed in ("Community", "Professional", "Enterprise", "BuildTools"):
            p = os.path.join(root, "Microsoft Visual Studio", "2022", ed,
                             "VC", "Auxiliary", "Build", "vcvars32.bat")
            if os.path.exists(p):
                return p
    return None


def _cl(vcvars, workdir, args):
    """Run cl.exe under a 32-bit toolchain environment."""
    cmd = f'"{vcvars}" >nul 2>&1 && cl /nologo {args}'
    return subprocess.run(cmd, cwd=workdir, shell=True, capture_output=True,
                          text=True)


def _native_source(cases):
    out = ["/* generated -- native side of the conformance run */",
           "unsigned int g_in_a, g_in_b;", ""]
    for c in cases:
        body = "\n".join(f"        {ins}" for ins in
                         MARK + c["asm"] + MARK)
        out.append(f"""unsigned int nat_{c['name']}(void) {{
    unsigned int r;
    __asm {{
        mov eax, g_in_a
        mov ecx, g_in_b
        xor edx, edx
{body}
        mov r, eax
    }}
    return r;
}}""")

    return "\n".join(out)


def _bytes_from_listing(cod_text, name):
    """Pull the bytes between the two nop markers out of one function."""
    want, inside, chunks = f"_nat_{name}", False, []
    for line in cod_text.replace(chr(13), "").splitlines():
        m = _COD_PROC.match(line)
        if m:
            inside = (m.group(1) == want)
            continue
        if _COD_ENDP.match(line):
            if inside:
                break
            continue
        if not inside:
            continue
        m = _COD_LINE.match(line)
        if m:
            chunks.append(m.group(2).strip())
            continue
        m = _COD_CONT.match(line)
        if m and chunks:
            chunks.append(m.group(1).strip())
    stream = " ".join(chunks)
    first = stream.find(_MARK_BYTES)
    last = stream.rfind(_MARK_BYTES)
    if first < 0 or last <= first:
        raise RuntimeError(f"{name}: could not find both nop markers")
    mid = stream[first + len(_MARK_BYTES):last].strip()
    return bytes.fromhex(mid.replace(" ", ""))


def _lift(code_bytes):
    """Lift raw bytes through the real pipeline, exactly as recomp would.

    Via lift_basic_block, not lift_instruction: the flag peephole that turns
    `cmp` + `jcc`/`setcc`/`cmovcc` into a real condition lives at block level.
    Lifting one instruction at a time would test a path recomp never uses, and
    would report a stale `_flags` that the block pass never emits.
    """
    from tools.recomp.disasm import BasicBlock, Disassembler
    from tools.recomp.lifter import Lifter, lift_basic_block
    d = Disassembler()
    insns, mnemonics = [], []
    for insn in d._cs.disasm(code_bytes, 0x00100000):
        mnemonics.append(insn.mnemonic)
        insns.append(d._decode_instruction(insn))
    lifter = Lifter()
    # FunctionTranslator sets this per function, and the lifter only bothers
    # producing CF when something consumes it. Without it the snippet would be
    # lifted differently from how recomp would lift the same bytes.
    lifter.needs_cf = any(i.mnemonic in ("sbb", "adc") for i in insns)
    lines, _ = lift_basic_block(lifter, BasicBlock(start=0x00100000,
                                                   instructions=insns))
    return list(lines), mnemonics


def _harness_source(prepared):
    out = ["""/* generated -- both sides, same inputs, compared */
#define RECOMP_GENERATED_CODE 1
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <math.h>
#include "recomp_types.h"

RECOMP_TLS uint32_t g_eax, g_ecx, g_edx, g_esp, g_ebx, g_esi, g_edi;
RECOMP_TLS uint32_t g_seh_ebp, g_ebp;
RECOMP_TLS double g_fp_stack[8]; RECOMP_TLS int g_fp_top;
RECOMP_TLS uint16_t g_fp_control_word = 0x037Fu; RECOMP_TLS int g_fp_cmp;
RECOMP_TLS RecompXmm g_xmm0,g_xmm1,g_xmm2,g_xmm3,g_xmm4,g_xmm5,g_xmm6,g_xmm7;
volatile uint32_t g_icall_trace[16]; volatile uint32_t g_icall_trace_idx;
volatile uint64_t g_icall_count;
ptrdiff_t g_xbox_mem_offset;
void recomp_icall_fail_log(uint32_t va) { (void)va; }

unsigned int g_in_a, g_in_b;
static unsigned char g_guest_stack[64 * 1024];
"""]
    for name, _, _ in prepared:
        out.append(f"unsigned int nat_{name}(void);")
    out.append("")
    for name, lines, _ in prepared:
        body = "\n".join(f"    {l}" for l in lines) or "    /* nothing */"
        # Same preamble FunctionTranslator emits: the lifter names these
        # temporaries and the enclosing function is expected to declare them.
        out.append(f"""static unsigned int lif_{name}(void) {{
    uint32_t ebp = 0; int _cf = 0; int _flags = 0;
    uint32_t _fa = 0, _fb = 0; int32_t _fas = 0, _fbs = 0;
    (void)ebp; (void)_cf; (void)_flags;
    (void)_fa; (void)_fb; (void)_fas; (void)_fbs;
    g_eax = g_in_a; g_ecx = g_in_b; g_edx = 0;
    g_esp = (uint32_t)(uintptr_t)(g_guest_stack + sizeof(g_guest_stack) / 2);
{body}
    return g_eax;
}}""")
    out.append("""
typedef unsigned int (*fn_t)(void);
static int g_total, g_fail;

/* Runs one case over its whole input vector and reports it as a unit: a
   lifting bug is a property of the snippet, not of one input, so a per-case
   line with a couple of examples is more use than 60 near-identical rows. */
static void run_case(const char *name, const char *why, fn_t nat, fn_t lif,
                     const unsigned int (*in)[2], int n) {
    int i, fails = 0, shown = 0;
    for (i = 0; i < n; i++) {
        unsigned int want, got;
        g_in_a = in[i][0]; g_in_b = in[i][1]; want = nat();
        g_in_a = in[i][0]; g_in_b = in[i][1]; got  = lif();
        g_total++;
        if (want != got) {
            fails++;
            if (shown < 3) {
                if (!shown)
                    printf("FAIL %s  (%s)\\n", name, why);
                printf("       a=%08X b=%08X  native=%08X  lifted=%08X\\n",
                       in[i][0], in[i][1], want, got);
                shown++;
            }
        }
    }
    if (fails) {
        if (fails > 3) printf("       ... %d of %d vectors\\n", fails, n);
        g_fail += fails;
    }
}

int main(void) {""")
    for name, _, inputs in prepared:
        vec = ", ".join(f"{{0x{a:08X}u,0x{b:08X}u}}" for a, b in inputs)
        out.append(f"    {{ static const unsigned int v[][2] = {{{vec}}};")
        out.append(f"      run_case(\"{name}\", \"{_WHY[name]}\", nat_{name}, "
                   f"lif_{name}, v, {len(inputs)}); }}")
    out.append("""    printf("\\n%d vectors, %d mismatches\\n", g_total, g_fail);
    return g_fail != 0;
}""")
    return "\n".join(out)


def main_with_args(argv):
    ap = argparse.ArgumentParser(prog="python -m tools.conformance",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("-k", metavar="SUBSTR", help="only cases whose name matches")
    ap.add_argument("--keep", action="store_true",
                    help="keep the generated C and listing for inspection")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    vcvars = _find_vcvars()
    if not vcvars:
        print("ERROR: no 32-bit MSVC found (looked for vcvars32.bat under "
              "Visual Studio 2022).", file=sys.stderr)
        return 2

    cases = [c for c in CASES if not args.k or args.k in c["name"]]
    if not cases:
        print(f"no cases match {args.k!r}", file=sys.stderr)
        return 2

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runtime_inc = os.path.join(os.path.dirname(root), "templates", "runtime")

    workdir = tempfile.mkdtemp(prefix="xboxrecomp-conf-")
    if args.verbose or args.keep:
        print(f"workdir: {workdir}")

    # 1. MSVC assembles the snippets and tells us the exact bytes.
    with open(os.path.join(workdir, "native.c"), "w") as f:
        f.write(_native_source(cases))
    r = _cl(vcvars, workdir, "/c /FAc /Fanative.cod native.c")
    if r.returncode != 0:
        print("native assembly failed:\n" + r.stdout + r.stderr, file=sys.stderr)
        return 1
    with open(os.path.join(workdir, "native.cod"), errors="replace") as f:
        cod = f.read()

    # 2. Lift those bytes with the real pipeline.
    prepared, unlifted = [], []
    for c in cases:
        code = _bytes_from_listing(cod, c["name"])
        lines, mnemonics = _lift(code)
        dropped = [l for l in lines if l.strip().startswith("/*")]
        if dropped:
            unlifted.append((c["name"], mnemonics, dropped))
        prepared.append((c["name"], lines, c["inputs"]))
        if args.verbose:
            print(f"  {c['name']:<20} {code.hex():<28} {len(lines)} C lines")

    # 3. Run both and compare.
    with open(os.path.join(workdir, "harness.c"), "w") as f:
        f.write(_harness_source(prepared))
    r = _cl(vcvars, workdir,
            f'/W3 /I"{runtime_inc}" harness.c native.obj /Feharness.exe')
    if r.returncode != 0:
        print("harness build failed:\n" + r.stdout + r.stderr, file=sys.stderr)
        return 1
    run = subprocess.run([os.path.join(workdir, "harness.exe")],
                         capture_output=True, text=True)
    print(run.stdout.strip())
    if run.returncode < 0 or run.returncode > 1:
        # A snippet faulted on the native side (idiv overflow, a bad memory
        # operand). Say so -- otherwise the run looks like a silent pass.
        print(f"\nharness terminated abnormally: exit {run.returncode} "
              f"(0x{run.returncode & 0xFFFFFFFF:08X}). The case that faulted is "
              f"the one after the last line printed above.", file=sys.stderr)
        if run.stderr.strip():
            print(run.stderr.strip(), file=sys.stderr)
        return 1

    if unlifted:
        print("\nInstructions that lifted to a comment (silently no-ops, so the "
              "comparison above cannot see them):")
        for name, mnemonics, dropped in unlifted:
            print(f"  {name:<20} {' '.join(mnemonics)}")
            for d in dropped:
                print(f"      {d.strip()}")

    if not args.keep:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    return run.returncode or (1 if unlifted else 0)


def main():
    return main_with_args(None)


if __name__ == "__main__":
    sys.exit(main())
