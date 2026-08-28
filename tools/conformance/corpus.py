"""Whole functions: compile C with a 32-bit MSVC, lift the machine code back,
and require the lifted C to agree with the original.

The snippet cases in cases.py test instructions someone thought to write down.
This tests whatever the optimiser actually emitted -- its register allocation,
its branch layout, the idioms it reaches for -- which is the code a real title
is made of.

Constraints, and why:

  * **Leaf functions only, and no globals or floating-point constants.** A
    reference to anything outside the function becomes a relocation, and the
    bytes in the .obj hold zero until the linker fills them in. Lifting those
    bytes gives confident nonsense. MSVC parks float literals in .rdata, so the
    float cases take their constants as arguments. The runner refuses any
    function whose listing shows a symbolic operand rather than trusting this
    comment to stay true.

  * **__cdecl.** Arguments on the stack right to left, caller cleans up,
    result in eax / edx:eax / st(0). That is the ABI the recompiler's generated
    code already assumes, so the harness exercises the real calling path rather
    than a special one.

Signatures drive how the harness marshals arguments and reads the result:

    ii->i   int (int, int)          uu->u   unsigned (unsigned, unsigned)
    ll->l   long long (ll, ll)      dd->d   double (double, double)
    ff->f   float (float, float)
"""

_INT_ARGS = [
    (0, 0), (1, 1), (5, 3), (-7, 2), (100, -100), (-1, -1),
    (123456, -98765), (-2147483647 - 1, 5), (2147483647, 1),
    (0x7F, 0x80), (-32768, 32767), (3, 0), (0, 3), (-5, -9),
]

_UINT_ARGS = [
    (0, 1), (1, 1), (0xFFFFFFFF, 1), (0xFFFFFFFF, 0xFFFFFFFF),
    (0x80000000, 3), (12345, 67), (0xDEADBEEF, 0x10), (7, 0x7FFFFFFF),
]

_LONG_ARGS = [
    (0, 0), (3, 5), (-7, 11), (0x100000001, 0x30), (-1, -1),
    (0x7FFFFFFFFFFFFFFF, 2), (-0x8000000000000000, 3), (0xFFFFFFFF, 0xFFFFFFFF),
]

_DBL_ARGS = [
    (1.5, 2.0), (-3.25, 0.5), (100.0, 7.0), (2.0, 1.0), (0.0, 1.0),
    (-0.0, 2.0), (1e10, 3.0), (1e-10, 7.0), (-1.0, -1.0), (0.5, 0.5),
]

_FLT_ARGS = [
    (1.5, 2.0), (-3.25, 0.5), (10.0, 4.0), (1.0, 3.0), (0.0, 2.0),
    (-7.5, -1.25), (1e5, 3.0),
]


def Fn(name, sig, why, source, args, tol=0.0):
    return {"name": name, "sig": sig, "why": why, "source": source,
            "args": args, "tol": tol}


CORPUS = [
    Fn("c_arith", "ii->i",
       "mixed integer arithmetic -- whatever /O2 does with it",
       "int __cdecl c_arith(int a, int b){ int x = a*3 + (b<<2) - (a>>1);"
       " x ^= (a&b); x += (a>b)?a:b; return x*2 - 7; }", _INT_ARGS),

    Fn("c_branches", "ii->i",
       "every signed and unsigned comparison, as the compiler lays them out",
       "int __cdecl c_branches(int a, int b){ int r = 0;"
       " if(a<b) r+=1; if(a<=b) r+=2; if(a==b) r+=4; if(a>=b) r+=8;"
       " if(a>b) r+=16; if((unsigned)a<(unsigned)b) r+=32;"
       " if((unsigned)a>=(unsigned)b) r+=64; return r; }", _INT_ARGS),

    Fn("c_shifts", "ii->i",
       "the rotate idiom, arithmetic vs logical shift, and the &31 masking",
       "int __cdecl c_shifts(int a, int b){ unsigned u = a; int n = b & 31;"
       " return (int)((u<<n)|(u>>((32-n)&31))) + (a>>(n&15)) - (int)(u>>n); }",
       _INT_ARGS),

    Fn("c_loop", "ii->i",
       "a counted loop: a back edge, so more than one basic block",
       "int __cdecl c_loop(int a, int b){ int i, acc = a;"
       " for(i = 0; i < 16; i++){ acc = acc*2 + (b^i); if(acc > 1000000)"
       " acc >>= 3; } return acc; }", _INT_ARGS),

    Fn("c_nested", "ii->i",
       "nested conditionals inside a loop -- branch layout the optimiser picks",
       "int __cdecl c_nested(int a, int b){ int i, r = 0;"
       " for(i = 0; i < 8; i++){ if(((a>>i)&1) != 0){ r += b + i; }"
       " else if(((b>>i)&1) != 0){ r -= a - i; } else { r ^= i; } }"
       " return r; }", _INT_ARGS),

    Fn("c_minmax", "ii->i",
       "min/max and abs -- where the compiler reaches for cmov or setcc",
       "int __cdecl c_minmax(int a, int b){ int lo = a<b?a:b, hi = a<b?b:a;"
       " int d = hi - lo; if(d < 0) d = -d; return lo + hi*3 + d; }",
       _INT_ARGS),

    Fn("c_udiv", "uu->u",
       "unsigned divide and modulo, including the by-one and by-max cases",
       "unsigned __cdecl c_udiv(unsigned a, unsigned b){ unsigned d = b|1u;"
       " return (a/d) + (a%d)*3u + (a>>1)/((d&0xFFu)|1u); }", _UINT_ARGS),

    Fn("c_bits", "uu->u",
       "bit twiddling: the compiler's shift/mask lowering",
       "unsigned __cdecl c_bits(unsigned a, unsigned b){"
       " unsigned x = a ^ (b<<16) ^ (b>>16);"
       " x = ((x & 0x55555555u) << 1) | ((x >> 1) & 0x55555555u);"
       " x = ((x & 0x00FF00FFu) << 8) | ((x >> 8) & 0x00FF00FFu);"
       " return x + (a & ~b); }", _UINT_ARGS),

    Fn("c_mul64", "ll->l",
       "64-bit multiply and shift -- edx:eax pairs and the CRT helper shapes",
       "long long __cdecl c_mul64(long long a, long long b){"
       " return a*b - (a>>3) + (b<<5); }", _LONG_ARGS),

    Fn("c_add64", "ll->l",
       "64-bit add/subtract: add/adc and sub/sbb across the halves",
       "long long __cdecl c_add64(long long a, long long b){"
       " long long s = a + b, d = a - b; return s ^ (d << 1); }", _LONG_ARGS),

    Fn("c_cmp64", "ll->l",
       "64-bit comparison, which lowers to a two-step compare on the halves",
       "long long __cdecl c_cmp64(long long a, long long b){ long long r = 0;"
       " if(a < b) r |= 1; if(a == b) r |= 2; if(a > b) r |= 4;"
       " if((unsigned long long)a < (unsigned long long)b) r |= 8;"
       " return r; }", _LONG_ARGS),

    Fn("c_dwork", "dd->d",
       "double arithmetic through the x87 stack",
       "double __cdecl c_dwork(double a, double b){"
       " return a*b + a/b - b*b + a; }", _DBL_ARGS),

    Fn("c_dmix", "dd->d",
       "doubles with a comparison and a branch -- fcom feeding a real jump",
       "double __cdecl c_dmix(double a, double b){ double r = a;"
       " if(a > b) r = a - b; else if(a < b) r = b - a; else r = a + b;"
       " return r*a - b; }", _DBL_ARGS),

    Fn("c_fwork", "ff->f",
       "float arithmetic -- single precision through the same x87 stack",
       "float __cdecl c_fwork(float a, float b){"
       " return a*b + b - a/b + a*a; }", _FLT_ARGS),

    Fn("c_int2dbl", "ii->i",
       "int to double and back: the fld/fistp conversion pair",
       "int __cdecl c_int2dbl(int a, int b){ double x = (double)a;"
       " double y = (double)(b|1); return (int)(x*y) + (int)(x/y); }",
       _INT_ARGS),

    Fn("c_bittest_loop", "ii->i",
       "the minimal shape that broke: a loop over bit tests. /O2 unrolls it "
       "into `test dl, 1<<i` + `je`, and the last iteration becomes "
       "`test dl, dl` + `jns` -- an 8-bit sign test",
       "int __cdecl c_bittest_loop(int a, int b){ int i, r = 0;"
       " for(i = 0; i < 8; i++){ if(((a>>i)&1) != 0){ r += b + i; } }"
       " return r; }", _INT_ARGS),
]
