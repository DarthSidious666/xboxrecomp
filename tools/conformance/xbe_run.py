"""Conformance against a real title: lift the game's own code and run both.

Xbox code is 32-bit x86 and this harness is a 32-bit x86 process, so the game's
machine code is not merely liftable, it is *executable*. Map the XBE where it
was linked for, call one of its functions directly, then run the lifted C over
the same arguments and compare. The oracle is the shipped binary itself.

That is only safe for functions we can show are self-contained, so candidates
are chosen mechanically rather than by hand:

  * a standard `push ebp; mov ebp, esp` prologue, and a plain `ret` -- not
    `ret imm16`, because a stdcall callee cleans a number of argument bytes we
    would have to guess, and guessing wrong corrupts the caller's stack;
  * no `call`, so nothing reaches the kernel, the CRT or an unmapped address;
  * every memory operand either esp/ebp-relative (its own frame and arguments)
    or an absolute address inside a mapped section, so it cannot dereference
    an argument we invented;
  * no `fs:` access (SEH), no string operations (they walk esi/edi as
    pointers), no privileged or I/O instructions;
  * and nothing that lifts to a bare comment, since an unhandled instruction is
    a silent no-op that the comparison cannot see.

Arguments are small integers. A function that treated one as a pointer would
fault -- but the operand rule above already excludes those, and the native call
runs under an exception guard regardless.
"""

import re

from . import image

# push ebp ; mov ebp, esp -- the frame prologue MSVC emits for a function that
# is not FPO-optimised. Not every function looks like this, and that is fine:
# we need a supply of verifiable functions, not all of them.
_PROLOGUE = bytes((0x55, 0x8B, 0xEC))

_MAX_INSNS = 120
_MIN_BYTES, _MAX_BYTES = 12, 400

_BANNED = {
    "call", "int", "int1", "into", "in", "out", "hlt", "iret", "iretd",
    "sysenter", "sysexit", "cpuid", "rdtsc", "rdmsr", "wrmsr", "lgdt", "lidt",
    "invd", "wbinvd", "ltr", "lldt", "arpl", "bound",
}
_BANNED_PREFIX = ("rep", "lods", "stos", "movs", "scas", "cmps", "ins", "outs")


def load(path):
    """(raw image bytes, [image.Section], base VA)."""
    from tools.xbe_parser.xbe_parser import XBEParser
    xbe = XBEParser(path).parse()
    sections = [
        image.Section(s.name, s.virtual_addr, max(s.virtual_size, s.raw_size),
                      s.raw_addr, s.raw_size, bool(s.flags & 0x4))
        for s in xbe.sections if s.raw_size
    ]
    if not sections:
        raise RuntimeError("no sections with raw data")
    return xbe.raw_data, sections, min(s.va for s in sections)


def _mapped(sections, va):
    return any(s.va <= va < s.va + s.vsize for s in sections)


def _self_contained(insns, start, end, sections):
    """Whether this function only touches its own frame and mapped memory."""
    for insn in insns:
        m = insn.mnemonic
        if m in _BANNED or any(m.startswith(p) for p in _BANNED_PREFIX):
            return False
        text = insn.op_str
        if "fs:" in text or "gs:" in text:
            return False
        # An indirect branch or call goes somewhere we cannot vouch for.
        if m.startswith("j") and not text.startswith("0x"):
            return False
        for op in insn.operands:
            if getattr(op, "type", None) != "mem":
                continue
            base = (op.mem_base or "").lower()
            index = (op.mem_index or "").lower()
            if base in ("esp", "ebp") and not index:
                continue          # its own frame, or an argument
            if not base and not index:
                if _mapped(sections, op.mem_disp or 0):
                    continue      # a global in a section we map
                return False
            return False          # a pointer we would have to invent
        # A jump must stay inside the function.
        if m.startswith("j") and text.startswith("0x"):
            try:
                target = int(text, 16)
            except ValueError:
                return False
            if not (start <= target < end):
                return False
    return True


def find_candidates(data, sections, limit, cs):
    """Scan the code sections for functions that are safe to call."""
    found = []
    for sec in sections:
        if not sec.is_code or sec.name != ".text":
            continue
        blob = data[sec.raw_off:sec.raw_off + sec.raw_size]
        pos = 0
        while len(found) < limit:
            pos = blob.find(_PROLOGUE, pos)
            if pos < 0:
                break
            start = sec.va + pos
            pos += 1
            insns, end, ok = [], None, False
            for insn in cs.disasm(blob[pos - 1:pos - 1 + _MAX_BYTES], start):
                insns.append(insn)
                if insn.mnemonic in ("ret", "retn"):
                    # plain `ret` only: a `ret imm16` callee cleans arguments,
                    # and we do not know how many.
                    ok = insn.op_str.strip() == ""
                    end = insn.address + insn.size
                    break
                if len(insns) >= _MAX_INSNS:
                    break
            if not ok or end is None or end - start < _MIN_BYTES:
                continue
            if not _self_contained(insns, start, end, sections):
                continue
            found.append((start, end - start,
                          blob[start - sec.va:end - sec.va]))
    return found


def lift(data, sections, candidates):
    """Lift each candidate through the real FunctionTranslator."""
    from tools.recomp import config
    from tools.recomp.translator import FunctionTranslator
    from .corpus_run import _CONFIG_GLOBALS

    func_db = {va: {"start": f"0x{va:08X}", "end": va + size, "_addr": va,
                    "size": size, "name": f"lifted_{va:08X}"}
               for va, size, _ in candidates}
    saved = {k: getattr(config, k) for k in _CONFIG_GLOBALS if hasattr(config, k)}
    out, rejected = [], []
    try:
        config._install(
            [config.Section(s.name, s.va, s.vsize, s.raw_off, s.raw_size,
                            s.is_code) for s in sections],
            entry_point=min(s.va for s in sections if s.is_code),
            kernel_thunk_addr=0, origin="conformance-xbe")
        tr = FunctionTranslator(data, func_db)
        for va, size, _ in candidates:
            body = tr.translate_function(va, func_db[va])
            gaps = [l.strip() for l in body.splitlines()
                    if l.strip().startswith(("/* TODO", "/* FPU:"))]
            if gaps:
                # An unhandled instruction is a silent no-op: the comparison
                # would be meaningless, so say which one and move on.
                rejected.append((va, gaps))
                continue
            if re.search(r"\bsub_[0-9A-Fa-f]{8}\s*\(", body):
                rejected.append((va, ["calls a function outside the set"]))
                continue
            out.append((va, size, body))
    finally:
        for k, v in saved.items():
            setattr(config, k, v)
    return out, rejected


_HARNESS = '''/* generated -- a real title's own code, lifted and run against itself */
#define RECOMP_GENERATED_CODE 1
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include <windows.h>
#include "recomp_types.h"

RECOMP_TLS uint32_t g_eax, g_ecx, g_edx, g_esp, g_ebx, g_esi, g_edi;
RECOMP_TLS uint32_t g_seh_ebp, g_ebp;
RECOMP_TLS double g_fp_stack[8]; RECOMP_TLS int g_fp_top;
RECOMP_TLS uint16_t g_fp_control_word = 0x027F; RECOMP_TLS int g_fp_cmp;
RECOMP_TLS RecompXmm g_xmm0,g_xmm1,g_xmm2,g_xmm3,g_xmm4,g_xmm5,g_xmm6,g_xmm7;
volatile uint32_t g_icall_trace[16]; volatile uint32_t g_icall_trace_idx;
volatile uint64_t g_icall_count;
ptrdiff_t g_xbox_mem_offset;
void recomp_icall_fail_log(uint32_t va) { (void)va; }
typedef void (*recomp_func_t)(void);
recomp_func_t recomp_lookup(uint32_t va) { (void)va; return 0; }
recomp_func_t recomp_lookup_manual(uint32_t va) { (void)va; return 0; }
recomp_func_t recomp_lookup_kernel(uint32_t va) { (void)va; return 0; }

static unsigned char g_stack[256 * 1024];
static int g_total, g_fail, g_faulted;

/* The image is mapped where the XBE was linked for, so a guest address is a
   host address: the lifted code's MEM32() and the original machine code read
   the very same bytes. Executable, because the original code is *run*. */
static int commit(uintptr_t lo, uintptr_t hi) {
    /* Reserve in 64 KB granules. The low address space is not one contiguous
       free block, and adjacent XBE sections often share a granule, so a whole-
       image reservation fails while a per-granule one succeeds. */
    uintptr_t a;
    for (a = lo & ~(uintptr_t)0xFFFF; a < hi; a += 0x10000) {
        MEMORY_BASIC_INFORMATION mbi;
        if (VirtualAlloc((LPVOID)a, 0x10000, MEM_RESERVE | MEM_COMMIT,
                         PAGE_EXECUTE_READWRITE) == (LPVOID)a)
            continue;
        if (VirtualQuery((LPCVOID)a, &mbi, sizeof mbi)
                && mbi.State == MEM_COMMIT)
            continue;                   /* already ours from a shared granule */
        return 0;
    }
    return 1;
}

static int map_image(const char *path) {
    FILE *f = fopen(path, "rb");
    size_t i;
    int mapped = 0;
    if (!f) { printf("cannot open %s@NL@", path); return 0; }
    for (i = 0; i < sizeof g_secs / sizeof g_secs[0]; i++) {
        uintptr_t lo = g_secs[i].va;
        uintptr_t hi = (uintptr_t)g_secs[i].va + g_secs[i].raw_size;
        if (!commit(lo, hi)) {
            printf("  note: %08X..%08X unavailable, section skipped@NL@",
                   (unsigned)lo, (unsigned)hi);
            continue;
        }
        if (fseek(f, (long)g_secs[i].raw_off, SEEK_SET) != 0) continue;
        if (fread((void *)lo, 1, g_secs[i].raw_size, f) != g_secs[i].raw_size)
            continue;
        mapped++;
    }
    fclose(f);
    if (!mapped) { printf("mapped nothing@NL@"); return 0; }
    g_xbox_mem_offset = 0;
    return 1;
}

/* Call the title's own code. pushad/popad around it because a function that
   fails to restore a callee-saved register would otherwise corrupt this
   harness rather than merely fail its comparison. */
static uint32_t g_nat_eax, g_nat_edx;

/* RECOMP_GENERATED_CODE maps the guest register names onto globals -- `eax`
   really is `g_eax` in this translation unit. That rewrite reaches into inline
   assembly too, so the real register names have to be uncovered here and put
   back afterwards for the lifted bodies. */
#undef eax
#undef ecx
#undef edx
#undef esp
#undef ebx
#undef esi
#undef edi

static void *g_fn;
static const uint32_t *g_args_p;
static double g_fp_arg;

static void call_native(void *fn, const uint32_t *args) {
    /* Both operands go through globals rather than parameters. MSVC addresses
       parameters relative to esp in a frameless function, and the four pushes
       below move esp -- so `call dword ptr fn` would dispatch through the
       wrong slot, which is a fault every time. */
    g_fn = fn;
    g_args_p = args;
    __asm {
        pushad
        mov  esi, g_args_p
        push dword ptr [esi+12]
        push dword ptr [esi+8]
        push dword ptr [esi+4]
        push dword ptr [esi]
        /* Enter with the same register state the lifted side starts from.
           Without this a function that never writes eax -- a void one -- comes
           back holding whatever the caller left there, and compares unequal
           against a lifted side that began at zero. The call goes indirectly
           through the stack slot so that eax can be zeroed as well. */
        finit
        fld  qword ptr g_fp_arg
        xor  eax, eax
        xor  ecx, ecx
        xor  edx, edx
        xor  ebx, ebx
        xor  esi, esi
        xor  edi, edi
        call dword ptr g_fn
        add  esp, 16
        mov  g_nat_eax, eax
        mov  g_nat_edx, edx
        popad
    }
}

#define eax g_eax
#define ecx g_ecx
#define edx g_edx
#define esp g_esp
#define ebx g_ebx
#define esi g_esi
#define edi g_edi
'''


# Small integers only. The operand rule already rejects any function that
# dereferences an argument, so these cannot be mistaken for pointers -- and
# the native call runs guarded anyway.
ARG_VECTORS = [
    (0, 0, 0, 0),
    (1, 2, 3, 4),
    (0xFFFFFFFF, 1, 0, 2),
    (0x7FFFFFFF, 0x80000000, 0xFF, 0x10),
    (3, 0, 0xFFFF, 0x7F),
    (0x40000000, 0x3F800000, 2, 1),   # also plausible float bit patterns
    (0x80000000, 0xFFFFFFFF, 0x8000, 0),
]

# A value in st(0) as well. Plenty of this era's maths helpers take their
# argument on the x87 stack rather than the call stack -- and leaving the stack
# empty is not neutral: the hardware yields the indefinite value where the
# model yields 0.0, so the two sides disagree over an input neither was given.
FP_ARGS = [0.0, 1.0, -1.0, 3.5, -2.25, 1e10, 0.5]


def harness_source(xbe_path, sections, lifted):
    # VirtualAlloc places a reservation on a 64 KB boundary, and .text starts
    # at 0x11000 -- round out to the granularity or the request is refused.
    lo = min(s.va for s in sections) & ~0xFFFF
    hi = (max(s.va + s.vsize for s in sections) + 0xFFFF) & ~0xFFFF
    out = [f"#define IMAGE_LO 0x{lo:08X}u", f"#define IMAGE_HI 0x{hi:08X}u", ""]
    out.append("static const struct { unsigned va, raw_off, raw_size; } "
               "g_secs[] = {")
    for s in sections:
        out.append(f"    {{ 0x{s.va:08X}u, 0x{s.raw_off:08X}u, "
                   f"0x{s.raw_size:08X}u }},   /* {s.name} */")
    out.append("};")
    out.append(f'#define XBE_PATH {_c_string(xbe_path)}')
    out.append(_HARNESS)
    for va, _, _ in lifted:
        out.append(f"void lifted_{va:08X}(void);")
    for _, _, body in lifted:
        out.append(body)

    out.append("static const uint32_t g_funcs[] = {")
    for va, _, _ in lifted:
        out.append(f"    0x{va:08X}u,")
    out.append("};")
    out.append("static recomp_func_t lookup_lifted(uint32_t va) {")
    out.append("    switch (va) {")
    for va, _, _ in lifted:
        out.append(f"    case 0x{va:08X}u: return lifted_{va:08X};")
    out.append("    default: return 0; }")
    out.append("}")
    out.append("static const uint32_t g_args[][4] = {")
    for vec in ARG_VECTORS:
        out.append("    { " + ", ".join(f"0x{v & 0xFFFFFFFF:08X}u"
                                        for v in vec) + " },")
    out.append("};")
    out.append("static const double g_fp_args[] = {"
               + ", ".join(repr(float(x)) for x in FP_ARGS) + "};")

    out.append('''
static void run_one(uint32_t va, const uint32_t *args, int vec, int *shown) {
    uint32_t nat_eax, nat_edx;
    unsigned char *sp = g_stack + sizeof(g_stack) - 8192;
    int i;
    recomp_func_t lifted = lookup_lifted(va);

    g_faulted = 0;
    __try { call_native((void *)(uintptr_t)va, args); }
    __except (EXCEPTION_EXECUTE_HANDLER) { g_faulted = 1; }
    if (g_faulted) return;          /* not callable with these arguments */
    nat_eax = g_nat_eax; nat_edx = g_nat_edx;

    g_fp_cmp = 0; g_fp_control_word = 0x027Fu;
    memset(g_fp_stack, 0, sizeof g_fp_stack);
    /* one value on the x87 stack, matching the native side's fld */
    g_fp_top = 7; g_fp_stack[7] = g_fp_arg;
    g_eax = g_ecx = g_edx = g_ebx = g_esi = g_edi = 0;
    g_seh_ebp = g_ebp = 0;
    for (i = 3; i >= 0; i--) { sp -= 4; memcpy(sp, &args[i], 4); }
    { uint32_t ret = 0xDEADBEEFu; sp -= 4; memcpy(sp, &ret, 4); }
    g_esp = (uint32_t)(uintptr_t)sp;

    __try { lifted(); }
    __except (EXCEPTION_EXECUTE_HANDLER) { g_faulted = 2; }
    g_total++;
    if (g_faulted == 2) {
        if (!(*shown)++) printf("FAIL sub_%08X  (lifted code faulted)@NL@", va);
        g_fail++;
        return;
    }
    if (nat_eax == g_eax && nat_edx == g_edx) return;
    if (!(*shown)++)
        printf("FAIL sub_%08X@NL@", va);
    if (*shown <= 3)
        printf("       vec %d  args %08X %08X %08X %08X@NL@"
               "         native eax=%08X edx=%08X   lifted eax=%08X edx=%08X@NL@",
               vec, args[0], args[1], args[2], args[3],
               nat_eax, nat_edx, g_eax, g_edx);
    g_fail++;
}

int main(int argc, char **argv) {
    int i, v, shown;
    const char *path = argc > 1 ? argv[1] : XBE_PATH;
    if (!map_image(path)) return 2;
    { unsigned short cw = 0x027F; __asm { fldcw cw } }
    for (i = 0; i < (int)(sizeof g_funcs / sizeof g_funcs[0]); i++) {
        shown = 0;
        if (getenv("XBE_TRACE")) {
            printf("[trying sub_%08X]@NL@", g_funcs[i]); fflush(stdout);
        }
        for (v = 0; v < (int)(sizeof g_args / sizeof g_args[0]); v++) {
            g_fp_arg = g_fp_args[v];
            run_one(g_funcs[i], g_args[v], v, &shown);
        }
        if (shown > 3) printf("       ... and %d more@NL@", shown - 3);
    }
    printf("@NL@%d function vectors from the title, %d mismatches@NL@",
           g_total, g_fail);
    return g_fail != 0;
}''')
    return "\n".join(out).replace("@NL@", chr(92) + "n")


def _c_string(s):
    return '"' + s.replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"') + '"'
