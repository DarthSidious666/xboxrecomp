# Conformance Testing

The unit tests in `tools/recomp/test_lifter_*.py` compare the lifter's **output
text** against an expected string. That catches a rewrite that changes the
emitted form, and it catches nothing else. It cannot tell you whether the C it
emitted computes what the instruction computes, because nothing in the loop ever
executes either one.

Every bug in the v0.6.0 changelog was of that second kind: the generated C
compiled, linked, ran, and was quietly wrong. `repe cmpsb` reporting "equal"
regardless of input, `movaps` moving 4 of 16 bytes, a signed compare evaluated
at the wrong width — all of them pass a string-comparison test suite.

`tools/conformance` closes that gap by executing both sides.

## The idea, borrowed

This is [ps3recomp](https://github.com/sp00nznet/ps3recomp)'s methodology, which
is worth stating plainly since the two projects share a maintainer and the
approach transfers almost intact:

1. **Check the decoder against an oracle you did not write.** ps3recomp runs the
   SDK's `ppu-lv2-objdump` over a binary and diffs mnemonic-per-address against
   its own decoder, with an explicit alias table for the simplified mnemonics
   objdump prefers.
2. **Check the lifter by executing it.** `lift_selftest.py` compiles a corpus of
   C with the real PPU compiler, lifts the resulting PowerPC, compiles the
   lifted C on the host, runs both, and requires bit-exact agreement.
3. **Whitelist known divergences** so the audit sits at zero noise and any *new*
   divergence is a failure rather than another line in a long report.
4. **Assert the observable thing.** Its CI checks the presented pixel, not the
   process exit code.

## What changes for x86

Pillar 1 mostly does not apply to us: we do not have our own decoder to audit.
We use Capstone, which is already the independent implementation. The layer
worth auditing here is our *operand model* on top of it, not the decode.

Pillar 2 gets much stronger, and this is the important difference. ps3recomp has
to reason about a PowerPC while running on an x86 host, so its oracle is a
*model* — the SDK toolchain, an independent decoder, a hand-built PPC
interpreter. A model can be wrong, and when it is, it is wrong in the same
places your lifter is.

We target x86 and run on x86. **The host CPU is the reference implementation.**
There is no model to disagree with: we execute the actual instruction bytes on
actual silicon and compare that against the lifted C. Nothing else in the
recompilation pipeline gets an oracle that good.

## How it works

Each case in `tools/conformance/cases.py` is a short x86 sequence whose net
effect lands in `eax`, written as assembly *text*:

```python
Case("neg_sbb_separated",
     "neg's carry into a separated sbb -- the real codegen shape",
     ["neg eax", "push edx", "mov edx, 1", "pop edx",
      "sbb ecx, ecx", "mov eax, ecx"], _PAIRS),
```

Per case, the runner:

1. Emits that text into a C function as an MSVC `__asm` block, bracketed by
   three-`nop` markers, and compiles with `/FAc`. **MSVC is the assembler** —
   we never hand-encode, so the bytes cannot drift from the intent.
2. Reads the exact bytes back out of the `.cod` listing, between the markers.
3. Lifts those bytes through the real `Disassembler` + `lift_basic_block`.
4. Generates a harness holding both the native function and the lifted C,
   runs each over the same input vectors, and compares `eax`.

Inputs cluster on the boundaries that separate a correct implementation from a
plausible one: `0x7F`/`0x80`/`0xFF` at byte width, `0x7FFF`/`0x8000` at word
width, `0x80000000`, `0xFFFFFFFF`, zero. A bug that evaluates an 8-bit operand
at 32 bits is invisible at `0x00000005` and obvious at `0x000000FF`.

```
py -3 -m tools.conformance          # everything
py -3 -m tools.conformance -k neg   # one family
py -3 -m tools.conformance --keep   # keep the generated C to read
```

It also reports separately on any instruction that lifted to a bare comment.
Those are **invisible to the comparison** — an instruction that lifts to nothing
usually leaves `eax` untouched, so the two sides can agree while the lift is
doing nothing at all. That listing is the one part of the output that is not a
pass/fail.

## Lifting the way recomp lifts

Two details matter, and both were got wrong on the first attempt:

- Lift through **`lift_basic_block`**, not `lift_instruction`. The peephole that
  turns `cmp` + `jcc`/`setcc`/`cmovcc` into a real condition lives at block
  level. Lifting one instruction at a time reports a stale `_flags` that the
  block pass never emits, and produces a spectacular run of false failures.
- Set **`needs_cf`** the way `FunctionTranslator` does. The lifter only produces
  carry when something downstream consumes it, so a snippet lifted without it is
  not the code recomp would generate.

The rule underneath both: if the harness does not drive the lifter exactly as
the recompiler does, it is testing a path that does not ship.

## What it found

On the first complete run, one genuine gap: `stc` / `clc` / `cmc` were
unhandled and lifted to `/* TODO: stc */`, so the carry flag a following
`adc`/`sbb` read kept whatever the last arithmetic had left in it. MSVC emits
these around multi-word arithmetic and the "return a bool in CF" idiom.

It also found two bugs in *itself* before finding that one, which is worth
recording because both would have produced confident false reports:

- Lifting per-instruction instead of per-block, as above.
- The `.cod` byte column wraps after five bytes onto a continuation line with no
  address. The first parser missed the continuation, silently dropping every
  instruction longer than five bytes — that is, every 32-bit immediate. A
  dropped instruction is precisely the failure this tool exists to catch, so
  having it in the harness was worse than not having the harness.

## Extending it

Add a case to `cases.py`. The two directions worth going next:

- **x87 and SSE.** The mechanism is the same, but the harness has to capture
  and compare the FPU stack and XMM registers rather than just `eax`. This is
  where the v0.6.0 merge made the most semantic decisions with the least
  execution behind them.
- **Whole functions.** Compile a corpus of C with a 32-bit MSVC, lift the
  result, and compare against the same source compiled natively — ps3recomp's
  `lift_selftest.py` shape. That exercises what an optimising compiler actually
  emits (register allocation, branch layout, CRT idioms) rather than sequences
  someone thought to write down.
