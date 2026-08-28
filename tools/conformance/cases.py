"""Snippets whose net effect lands in eax.

Each case is a short x86 sequence, written as MSVC inline-assembly text so the
assembler -- not us -- decides the encoding. The same bytes are then executed
natively and lifted to C, and the two eax values are compared.

Inputs are (eax, ecx) pairs, chosen for the edges that matter: sign-bit
boundaries at each operand width, zero, and -1.
"""

Case = lambda name, why, asm, inputs: {
    "name": name, "why": why, "asm": asm, "inputs": inputs}

# Values that sit on a boundary at one width but not another. A bug that
# evaluates an 8- or 16-bit operand at 32 bits shows up here and nowhere else.
_EDGES = [0x00000000, 0x00000001, 0x0000007F, 0x00000080, 0x000000FF,
          0x00007FFF, 0x00008000, 0x0000FFFF, 0x7FFFFFFF, 0x80000000,
          0xFFFFFFFF, 0x12345678]

_PAIRS = [(a, b) for a in _EDGES for b in (0x00000001, 0x0000007F, 0x00000080,
                                           0x000000FF, 0xFFFFFFFF)]

CASES = [
    # -- signed compare width (the RECOMP_SIGNED vs SXV decision) -------------
    Case("setl_i8", "cmp at byte width, then jl's condition",
         ["cmp al, cl", "setl al", "movzx eax, al"], _PAIRS),
    Case("setl_i16", "cmp at word width",
         ["cmp ax, cx", "setl al", "movzx eax, al"], _PAIRS),
    Case("setl_i32", "cmp at dword width",
         ["cmp eax, ecx", "setl al", "movzx eax, al"], _PAIRS),
    Case("setle_i8", "<= at byte width",
         ["cmp al, cl", "setle al", "movzx eax, al"], _PAIRS),
    Case("setg_i16", "> at word width",
         ["cmp ax, cx", "setg al", "movzx eax, al"], _PAIRS),
    Case("setb_i32", "unsigned below, for contrast with the signed forms",
         ["cmp eax, ecx", "setb al", "movzx eax, al"], _PAIRS),

    # -- test / sign flag ----------------------------------------------------
    Case("tests_i8", "SF from an 8-bit test",
         ["test al, cl", "sets al", "movzx eax, al"], _PAIRS),
    Case("testz_i16", "ZF from a 16-bit test",
         ["test ax, cx", "setz al", "movzx eax, al"], _PAIRS),

    # -- neg carry into a dependent sbb (PR #8) ------------------------------
    Case("neg_sbb_adjacent", "neg sets CF = (operand != 0); sbb consumes it",
         ["neg eax", "sbb ecx, ecx", "mov eax, ecx"], _PAIRS),
    Case("neg_sbb_separated",
         "same, with flag-safe instructions in between -- the real codegen shape",
         ["neg eax", "push edx", "mov edx, 1", "pop edx",
          "sbb ecx, ecx", "mov eax, ecx"], _PAIRS),
    Case("neg_adc", "neg's carry into adc",
         ["neg eax", "adc ecx, 0", "mov eax, ecx"], _PAIRS),

    # -- sign/zero extension -------------------------------------------------
    Case("movsx_8_32", "sign-extend byte to dword",
         ["movsx eax, al"], _PAIRS),
    Case("movsx_16_32", "sign-extend word to dword",
         ["movsx eax, ax"], _PAIRS),
    Case("movzx_8_32", "zero-extend byte to dword",
         ["movzx eax, al"], _PAIRS),
    # Divisor forced odd and positive: idiv raises #DE both on a zero divisor
    # and on INT32_MIN / -1, and a trapping case tells us nothing about lifting.
    Case("cdq_idiv", "cdq's sign-extend feeding a signed divide",
         ["or ecx, 1", "and ecx, 07FFFFFFFh", "cdq", "idiv ecx"], _PAIRS),

    # -- shifts --------------------------------------------------------------
    Case("shl_cl", "variable shift left, including the &31 masking",
         ["shl eax, cl"], _PAIRS),
    Case("sar_cl", "arithmetic right shift keeps the sign",
         ["sar eax, cl"], _PAIRS),
    Case("shr_cl", "logical right shift does not",
         ["shr eax, cl"], _PAIRS),
    Case("rol_cl", "rotate left",
         ["rol eax, cl"], _PAIRS),
    Case("shld", "double-precision shift",
         ["shld eax, ecx, 5"], _PAIRS),

    # -- arithmetic ----------------------------------------------------------
    Case("imul_32", "signed multiply, low half",
         ["imul eax, ecx"], _PAIRS),
    Case("add_adc_64", "the 64-bit add idiom: add produces carry, adc consumes",
         ["add eax, ecx", "adc edx, 0", "mov eax, edx"], _PAIRS),
    Case("sub_sbb_64", "the 64-bit subtract idiom",
         ["sub eax, ecx", "sbb edx, 0", "mov eax, edx"], _PAIRS),
    Case("inc_dec", "inc/dec leave CF alone -- a classic place to get flags wrong",
         ["stc", "inc eax", "adc ecx, 0", "mov eax, ecx"], _PAIRS),
    Case("bswap", "byte swap", ["bswap eax"], _PAIRS),
    Case("not_and", "bitwise", ["not eax", "and eax, ecx"], _PAIRS),
]
