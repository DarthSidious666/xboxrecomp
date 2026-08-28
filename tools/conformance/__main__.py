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
from .harness import (MARK, _MARK_BYTES, harness_source, native_source)
from . import corpus_run
from .corpus import CORPUS

_WHY = {c["name"]: c["why"] for c in CASES}
_TOL = {c["name"]: c.get("tol", 0.0) for c in CASES}


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



def main_with_args(argv):
    ap = argparse.ArgumentParser(prog="python -m tools.conformance",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("-k", metavar="SUBSTR", help="only cases whose name matches")
    ap.add_argument("--keep", action="store_true",
                    help="keep the generated C and listing for inspection")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--only", choices=("snippets", "corpus"),
                    help="run only one of the two phases (default: both)")
    args = ap.parse_args(argv)

    vcvars = _find_vcvars()
    if not vcvars:
        print("ERROR: no 32-bit MSVC found (looked for vcvars32.bat under "
              "Visual Studio 2022).", file=sys.stderr)
        return 2

    cases = [c for c in CASES if not args.k or args.k in c["name"]]
    if not cases and args.only != "corpus":
        print(f"no cases match {args.k!r}", file=sys.stderr)
        return 2

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runtime_inc = os.path.join(os.path.dirname(root), "templates", "runtime")

    workdir = tempfile.mkdtemp(prefix="xboxrecomp-conf-")
    if args.verbose or args.keep:
        print(f"workdir: {workdir}")

    if args.only == "corpus":
        rc = _run_corpus(vcvars, workdir, runtime_inc, args)
        if not args.keep:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        return rc

    # 1. MSVC assembles the snippets and tells us the exact bytes.
    with open(os.path.join(workdir, "native.c"), "w") as f:
        f.write(native_source(cases))
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
        prepared.append((c["name"], c["kind"], lines, c["inputs"]))
        if args.verbose:
            print(f"  {c['name']:<20} {code.hex():<28} {len(lines)} C lines")

    # 3. Run both and compare.
    with open(os.path.join(workdir, "harness.c"), "w") as f:
        f.write(harness_source(prepared, _WHY, _TOL))
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

    rc = run.returncode or (1 if unlifted else 0)

    if args.only != "snippets":
        rc = _run_corpus(vcvars, workdir, runtime_inc, args) or rc

    if not args.keep:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    return rc




def _run_corpus(vcvars, workdir, runtime_inc, args):
    """Phase two: real C functions, compiled, lifted, and compared."""
    fns = [f for f in CORPUS if not args.k or args.k in f["name"]]
    if not fns:
        return 0
    src = "\n".join(f["source"] for f in fns)
    with open(os.path.join(workdir, "corpus.c"), "w") as f:
        f.write("/* corpus: compiled with the target compiler, then lifted "
                "back */\n" + src + "\n")
    # /O2 on purpose: the point is to lift what the optimiser really emits.
    #
    # /arch:IA32 because the Xbox CPU is a Pentium III -- SSE1, no SSE2. Modern
    # MSVC defaults to SSE2 and puts doubles in XMM, which no real Xbox binary
    # contains, so without this the corpus would test instructions the target
    # cannot execute and skip the x87 paths that every Xbox title actually uses.
    r = _cl(vcvars, workdir, "/c /O2 /GS- /arch:IA32 /FAc /Facorpus.cod corpus.c")
    if r.returncode != 0:
        print("corpus compile failed:\n" + r.stdout + r.stderr, file=sys.stderr)
        return 1
    with open(os.path.join(workdir, "corpus.cod"), errors="replace") as f:
        cod = f.read()

    prepared, skipped = [], []
    for fn in fns:
        code, text = corpus_run.function_bytes(cod, fn["name"])
        sym = corpus_run._SYMBOLIC.search(text)
        if sym:
            # An unlinked .obj still has zero where the linker will patch, so
            # these bytes cannot be lifted honestly. Say so rather than compare
            # against nonsense.
            skipped.append((fn["name"], sym.group(0).strip()))
            continue
        prepared.append((fn, corpus_run.lift_function(code, fn["name"])))
        if args.verbose:
            print(f"  {fn['name']:<14} {len(code):>4} bytes  "
                  f"{len(prepared[-1][1].splitlines()):>4} C lines")

    if skipped:
        print("\nSkipped (needs a relocation the .obj has not resolved):",
              file=sys.stderr)
        for name, why in skipped:
            print(f"  {name:<14} references {why}", file=sys.stderr)

    if not prepared:
        return 1
    with open(os.path.join(workdir, "corpus_harness.c"), "w") as f:
        f.write(corpus_run.harness_source(prepared))
    r = _cl(vcvars, workdir,
            f'/W3 /I"{runtime_inc}" corpus_harness.c corpus.obj '
            f'/Fecorpus_harness.exe')
    if r.returncode != 0:
        print("corpus harness build failed:\n" + r.stdout + r.stderr,
              file=sys.stderr)
        return 1
    run = subprocess.run([os.path.join(workdir, "corpus_harness.exe")],
                         capture_output=True, text=True)
    print(run.stdout.strip())
    if run.returncode < 0 or run.returncode > 1:
        print(f"\ncorpus harness terminated abnormally: exit {run.returncode} "
              f"(0x{run.returncode & 0xFFFFFFFF:08X})", file=sys.stderr)
        return 1
    return run.returncode


def main():
    return main_with_args(None)


if __name__ == "__main__":
    sys.exit(main())
