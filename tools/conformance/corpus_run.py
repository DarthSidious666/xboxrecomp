"""Compile a C corpus, lift the machine code back, and compare the two."""

import re

BASE = 0x00200000

# A listing operand that names a symbol rather than a register, an immediate or
# an [reg+disp] address. In an unlinked .obj those bytes are still zero, so
# lifting them produces confident nonsense -- refuse the function instead.
_SYMBOLIC = re.compile(
    r"\bOFFSET\b|__real@|__xmm@|\?\?_C@"      # literals parked in .rdata
    r"|\bcall\b\s+_"                          # a call out to anything else
    r"|\bPTR\s+_[A-Za-z0-9_]+\b(?!\$)",       # a global -- but `_a$[ebp]` is
    re.I)                                     # a stack slot, not a relocation

_PROC = re.compile(r"^(\S+)\s+PROC\b")
_ENDP = re.compile(r"^(\S+)\s+ENDP\b")
# address, byte column, then the disassembled text
_CODE = re.compile(
    r"^\s*([0-9a-fA-F]{5,})\t([0-9a-fA-F]{2}(?: [0-9a-fA-F]{2})*)(?:\t(.*))?$")
_CONT = re.compile(r"^\t([0-9a-fA-F]{2}(?: [0-9a-fA-F]{2})*)(?:\t(.*))?$")


def function_bytes(cod_text, name):
    """Every code byte in one PROC, plus the operand text for the guard."""
    want, inside = "_" + name, False
    chunks, text = [], []
    for line in cod_text.replace(chr(13), "").splitlines():
        m = _PROC.match(line)
        if m:
            inside = (m.group(1) == want)
            continue
        if _ENDP.match(line):
            if inside:
                break
            continue
        if not inside:
            continue
        m = _CODE.match(line) or _CONT.match(line)
        if not m:
            continue
        groups = m.groups()
        chunks.append(groups[-2].strip() if len(groups) == 3 else groups[0].strip())
        if groups[-1]:
            text.append(groups[-1])
    if not chunks:
        raise RuntimeError(f"{name}: no code found in the listing")
    return bytes.fromhex(" ".join(chunks).replace(" ", "")), "\n".join(text)


_CONFIG_GLOBALS = (
    "_SECTIONS", "SECTIONS", "_configured_from",
    "TEXT_VA_START", "TEXT_VA_END", "RDATA_VA_START", "RDATA_VA_END",
    "DATA_VA_START", "DATA_VA_END", "KERNEL_THUNK_ADDR", "ENTRY_POINT",
)


def lift_function(code, name):
    """Run the real recompiler over these bytes: FunctionTranslator, not just
    the lifter, so this exercises frame handling, labels and block layout too.

    The section map is process-global, so it is saved and put back. Leaving a
    synthetic one installed silently changes what every later caller in the
    same process thinks the address space looks like -- under pytest that is
    the rest of the suite.
    """
    from tools.recomp import config
    from tools.recomp.translator import FunctionTranslator
    saved = {k: getattr(config, k) for k in _CONFIG_GLOBALS
             if hasattr(config, k)}
    try:
        config._install(
            [config.Section(".text", BASE, len(code), 0x0000, len(code), True)],
            entry_point=BASE, kernel_thunk_addr=BASE,
            origin="conformance-corpus")
        info = {"start": f"0x{BASE:08X}", "end": BASE + len(code),
                "_addr": BASE, "size": len(code), "name": f"lifted_{name}"}
        return FunctionTranslator(code,
                                  {BASE: info}).translate_function(BASE, info)
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


# ── marshalling ─────────────────────────────────────────────────────────────
# Argument widths in stack slots, and how the result comes back, per __cdecl.

_SIG = {
    "ii->i": {"ctype": "int",       "arg": "int",      "push": "i", "ret": "eax"},
    "uu->u": {"ctype": "unsigned",  "arg": "unsigned", "push": "i", "ret": "eax"},
    "ll->l": {"ctype": "long long", "arg": "long long","push": "l", "ret": "edx:eax"},
    "dd->d": {"ctype": "double",    "arg": "double",   "push": "d", "ret": "st0"},
    "ff->f": {"ctype": "float",     "arg": "float",    "push": "f", "ret": "st0"},
}

_PUSH = {
    "i": "    { uint32_t _v = (uint32_t)a{n}; sp -= 4; memcpy(sp, &_v, 4); }",
    "l": "    { uint64_t _v = (uint64_t)a{n}; sp -= 8; memcpy(sp, &_v, 8); }",
    "d": "    { double   _v = a{n};           sp -= 8; memcpy(sp, &_v, 8); }",
    "f": "    { float    _v = a{n};           sp -= 4; memcpy(sp, &_v, 4); }",
}

_READ = {
    "eax":     "(int)g_eax",
    "edx:eax": "(long long)(((uint64_t)g_edx << 32) | g_eax)",
    "st0":     "g_fp_stack[g_fp_top & 7]",
}


_PREAMBLE = """/* generated -- whole-function corpus: compiled C vs lifted C */
#define RECOMP_GENERATED_CODE 1
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
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

/* The guest stack. Guest addresses are host addresses (g_xbox_mem_offset stays
   0), so the lifted code's MEM32(esp) reads these bytes directly. */
static unsigned char g_stack[256 * 1024];
#define GUEST_SP_TOP (g_stack + sizeof(g_stack) - 4096)

static int g_total, g_fail;

/* A lifted function is void(void) and takes its arguments off the guest stack,
   right to left, under a return address -- __cdecl, which is what the
   recompiler's own generated code assumes. Setting that up here is the point:
   it exercises the real calling path rather than a special one. */
static void enter(void) {
    g_fp_top = 0; g_fp_cmp = 0; g_fp_control_word = 0x027Fu;
    memset(g_fp_stack, 0, sizeof g_fp_stack);
    g_eax = g_ecx = g_edx = g_ebx = g_esi = g_edi = 0;
    g_seh_ebp = g_ebp = 0;
}
"""

_REPORT = """
static void fail(const char *name, const char *why, int *shown, int vec) {
    if (!*shown) printf("FAIL %s  (%s)@NL@", name, why);
    (*shown)++;
    g_fail++;
    (void)vec;
}

static int same_d(double a, double b, double tol) {
    if (memcmp(&a, &b, sizeof a) == 0) return 1;
    if (isnan(a) && isnan(b)) return 1;
    if (tol > 0.0 && !isnan(a) && !isnan(b)) {
        double d = fabs(a - b), s = fabs(a) > fabs(b) ? fabs(a) : fabs(b);
        return d <= tol * (s > 1.0 ? s : 1.0);
    }
    return 0;
}
"""


def harness_source(prepared):
    """prepared: list of (fn, lifted_c)."""
    out = [_PREAMBLE]
    for fn, _ in prepared:
        s = _SIG[fn["sig"]]
        out.append(f'extern {s["ctype"]} __cdecl {fn["name"]}'
                   f'({s["arg"]}, {s["arg"]});')
    out.append("")
    for _, lifted in prepared:
        out.append(lifted)
    out.append(_REPORT)

    for fn, _ in prepared:
        s = _SIG[fn["sig"]]
        pushes = "\n".join(_PUSH[s["push"]].replace("{n}", str(i))
                           for i in (1, 0))
        out.append(f"""
static void run_{fn['name']}({s['arg']} a0, {s['arg']} a1, int vec, int *shown) {{
    {s['ctype']} want, got;
    unsigned char *sp = GUEST_SP_TOP;
    want = {fn['name']}(a0, a1);
    enter();
{pushes}
    {{ uint32_t _ret = 0xDEADBEEFu; sp -= 4; memcpy(sp, &_ret, 4); }}
    g_esp = (uint32_t)(uintptr_t)sp;
    lifted_{fn['name']}();
    got = ({s['ctype']}){_READ[s['ret']]};
    g_total++;
    if ({_agree(fn, s)}) return;
    fail("{fn['name']}", "{fn['why']}", shown, vec);
    if (*shown <= 3) {_printf(fn, s)}
}}""")

    out.append("int main(void) {\n    int shown;")
    out.append("    /* Match the model: it holds the x87 stack as C doubles, so\n"
               "       the hardware is put in double-precision mode too. */\n"
               "    { unsigned short cw = 0x027F; __asm { fldcw cw } }")
    for fn, _ in prepared:
        out.append(f"    shown = 0;   /* {fn['name']} */")
        for vec, (a, b) in enumerate(fn["args"]):
            out.append(f"    run_{fn['name']}({_lit(a, fn['sig'])}, "
                       f"{_lit(b, fn['sig'])}, {vec}, &shown);")
        out.append('    if (shown > 3) printf("       ... and %d more@NL@", '
                   'shown - 3);')
    out.append('    printf("@NL@%d function vectors, %d mismatches@NL@", '
               'g_total, g_fail);\n    return g_fail != 0;\n}')
    return "\n".join(out).replace("@NL@", chr(92) + "n")


def _agree(fn, s):
    if s["ret"] == "st0":
        return f"same_d((double)want, (double)got, {fn['tol']!r})"
    return "want == got"


def _printf(fn, s):
    if s["ret"] == "st0":
        return ('printf("       vec %-3d native=%.17g  lifted=%.17g@NL@",'
                ' vec, (double)want, (double)got);')
    if s["ret"] == "edx:eax":
        return ('printf("       vec %-3d native=%lld  lifted=%lld@NL@",'
                ' vec, (long long)want, (long long)got);')
    return ('printf("       vec %-3d native=%d (%08X)  lifted=%d (%08X)@NL@",'
            ' vec, (int)want, (unsigned)want, (int)got, (unsigned)got);')


def _lit(v, sig):
    if sig in ("dd->d",):
        return repr(float(v))
    if sig in ("ff->f",):
        return repr(float(v)) + "f"
    if sig == "ll->l":
        return f"{v}LL"
    if sig == "uu->u":
        return f"0x{v & 0xFFFFFFFF:08X}u"
    return f"({v})" if v < 0 else str(v)
