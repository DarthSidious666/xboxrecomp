# Reuniting Burnout 3 with xboxrecomp

Burnout 3 was the first static recompilation of an original Xbox title with no
prior recompiler to lean on. The core of xboxrecomp was written while getting it
to boot, then extracted into a reusable toolkit and hardened against Halo,
Crimson Skies and Steel Battalion. The two have since diverged. This is the plan
to bring Burnout 3 back onto the shared toolkit — and, more importantly, an
honest account of why it is a *reconciliation*, not a *swap*.

## The one-line finding

**The toolkit halves reunited cleanly; the runtime halves did not.** After the
extraction, xboxrecomp's `tools/` and Burnout 3's `tools/` diverged in one
direction (xboxrecomp got every fix, the fork kept one feature), but the
*runtime libraries* diverged in **both** directions — each grew mechanisms its
own game layer now depends on. The runtime cannot be swapped without merging
those.

## What is already done

**Toolkit (`tools/`): reunited.** Burnout 3's fork had exactly one thing
xboxrecomp lacked — `--exclude-manual`, which reads `recomp_manual.c` for the
functions it defines by hand and skips generating their bodies. That is now
upstreamed (`manual_scan.py` + the `--exclude-manual` flag). Proven end to end:
xboxrecomp's full pipeline on Burnout 3's XBE — correct export table, call-target
realignment, transcendentals, real return addresses, flat dispatch — generates
22,893 / 23,010 functions, 0 failed, excluding 117 hand-written overrides and
wrapping 2. **Burnout 3's recompiled code can be produced entirely by
xboxrecomp's tools today.**

The x87 transcendentals were also ported directly into Burnout 3's lifter fork
as an interim fix (its own commit on the `linux-port` branch), which measurably
eliminated its FP exceptions (10 → 0). That is redundant once the toolkit swap
lands, but it is a real correctness fix in the meantime.

## Why the runtime is not a swap

Measured, file by file. Every library diverged substantially — no library is a
clean stale copy:

| library | BO3 vs xboxrecomp | nature of divergence |
|---|---|---|
| `src/kernel` | 17/17 files differ | **architectural** (threading model) |
| `src/d3d` | 9/12 files differ | co-tuned with BO3's nv2a pipeline |
| `src/nv2a` | game-coupled | 2 BO3-only bridge headers |
| `src/audio` | 768 diff-lines | independent evolution |
| `src/input` | 488 diff-lines | independent evolution |

### The hard gate: two opposite threading models

This is the blocker, and it is not staleness — it is two valid designs that each
co-evolved with their game layer.

- **Burnout 3**: `xbox_PsCreateSystemThreadEx` creates a **real Win32 thread**
  (`CreateThread`). Its `main.c` frame-pump drives the game tick on a claimed
  worker-stack slice (`xbox_worker_stack_alloc`, 16 × 256 KB), and its watchdog
  samples the game thread through `xbox_thread_debug_handle`. Recent commits —
  "Make game_frame_pump the only frame driver", "Give the host loop a stack so it
  can drive the game tick" — built this. The game loop *requires* real concurrency.

- **xboxrecomp**: `bridge_PsCreateSystemThreadEx` runs the start routine
  **synchronously** through `RECOMP_ICALL` ("Must run synchronously…"). This is
  what Halo and Crimson need; their loops are not built on a worker frame-pump.

Swapping Burnout 3 onto xboxrecomp's kernel would replace real threads with
synchronous execution and its frame pump would never be driven. Swapping the
other way would break Halo. **Neither kernel is a superset of the other.**

The routing tables tell the same story from the correctness side: Burnout 3's
kernel is built on a **shifted export table** — 50 of its 147 imports are
misrouted (ordinal 17 `ExFreePool` handled as `ExEventObjectType`, 67
`IoCreateSymbolicLink` as `IoCreateFile`, and so on — the exact drift
`test_bridge_ordinals.py` was written to catch). It survives because the errors
are mostly benign (a `free` that reads a data address and drops it → a leak, not
a crash). xboxrecomp's kernel has the correct table. So the kernel is where the
*most* correctness is waiting and where the swap is *hardest*.

## The plan

Sequenced so each step is independently testable and nothing regresses a working
title.

**Phase 0 — toolkit. Mostly done.** `--exclude-manual` upstreamed; xboxrecomp's
tools generate Burnout 3's code. To *retire* the `tools/` fork — point Burnout 3's
regen at xboxrecomp's `tools/` and delete its copy — two small loose ends remain,
both low-risk:

- Burnout 3's `gen_dangling_stubs.py` is **redundant** with xboxrecomp's built-in
  `recomp_stubs_unresolved.c` (the translator emits stubs for unresolved call
  targets inline; the realignment fix shrinks that set further). It is dropped,
  not ported.
- Burnout 3's `recomp_types.h` is a stale copy that predates flat dispatch, so it
  lacks the `recomp_dispatch_init` declaration xboxrecomp's generated code
  references. Replace it with xboxrecomp's template (re-applying any Burnout 3
  game-local additions). `merge_names` and `xdk_names.json` already work with
  xboxrecomp's `func_id`; those stay as Burnout 3 config.

Completing Phase 0 is a regen + full rebuild (the giant chunks are slow) and does
not touch the runtime, so it is safe — just not instant.

**Phase 1 — threading reconciliation. Done.** xboxrecomp's kernel now supports
both driving models, defaulting to the one Halo and Crimson Skies use so they are
untouched.

- `XBOX_WORKER_STACK_*` region + `xbox_worker_stack_alloc`/`free` (verbatim from
  the Burnout 3 fork): a guest stack for the host's own thread to call recompiled
  code from, which is what a host-tick-driven title needs to pump its frame tick.
- `xbox_thread_debug_handle` + `g_game_thread`, recorded when the bridge spawns a
  thread, so a host watchdog can sample the game thread.
- `xbox_SetThreadMode(XBOX_THREAD_MODE_SPAWN)`: in SPAWN mode every
  `PsCreateSystemThreadEx` is a real thread so the title's entry can return and
  the host can drive. Default is `INLINE` — the first call runs the game in place,
  the historical behavior.

All of it is additive: a default-model title calls none of it, so the region is
unused address space and the code is never reached. Verified: Halo builds and
runs **byte-identically** (same exit, same milestones, same
`render_cameras.c:458`), the allocator passes a standalone correctness check (16
distinct slices, exhaustion, free/reuse, aligned stride), and Burnout 3's exact
`main.c` threading API compiles against xboxrecomp's headers. What remains is
Phase 2 wiring it up under a real swap — including confirming empirically whether
Burnout 3's init thread must genuinely run concurrently (SPAWN) or whether INLINE
suffices.

**Phase 2 — kernel swap.** With threading reconciled, Burnout 3 uses xboxrecomp's
kernel. This is where the 50 misroutings get fixed and where the correct export
table finally reaches Burnout 3. Expect behaviour changes: functions that were
benign no-ops (e.g. `ExFreePool`) start doing real work, which can surface latent
issues the wrong routing was hiding. Regenerate `gen/` with xboxrecomp's tools in
the same step (gen reaches the kernel only through `RECOMP_ICALL` by ordinal, so
it does not hard-depend on names — but the two should move together).

**Phase 3 — nv2a / d3d.** The hardest, most coupled. Burnout 3's game speaks to
its nv2a through two local bridge headers (`d3d_device_snapshot.h`,
`render_input_snapshot.h`) that xboxrecomp's nv2a does not expose. This is
Burnout 3's proven end-to-end pushbuffer pipeline and the last thing to risk.
Options, in order of preference: (a) upstream Burnout 3's snapshot interface into
xboxrecomp's nv2a; (b) keep Burnout 3's nv2a local indefinitely and swap only the
rest. Decide after Phase 2.

**Phase 4 — audio / input.** Peripheral, not on the frame-pump or render path.
Diff, reconcile, swap. Lowest risk, do last or opportunistically.

## The honest summary

"Get Burnout 3 running on the new xboxrecomp" is the right goal, and half of it is
already true — the toolkit. The runtime half is gated on reconciling two threading
architectures that were each correct for their title. That is real design work on
a path two shipping titles depend on, not a `CMakeLists` edit. The sequence above
gets there without a flag-day rewrite, and without throwing away the worker-stack
frame-pump Burnout 3 earned the hard way.
