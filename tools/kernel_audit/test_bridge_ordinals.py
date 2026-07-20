"""
Cross-check the kernel bridge's ordinal routing against the export table.

Run: py -3 tools/kernel_audit/test_bridge_ordinals.py

src/kernel/kernel_bridge.c maps ordinals to bridge_<Name> wrappers by hand.
Nothing tied those numbers to the real kernel export list, and a block of them
had drifted by one -- so ordinal 24 (ExQueryNonVolatileSetting) dispatched to
ExQueryPoolBlockSize, and 67 (IoCreateSymbolicLink) to IoCreateFile. Both
"worked" in that they returned a plausible value, which is what made it hard
to see: the game got a pool size where it wanted EEPROM settings, and a
file-not-found where it wanted a drive mount.

KERNEL_EXPORTS in tools/xbe_parser is the reference. It is ordinal-indexed and
matches the published Xbox kernel export table.
"""

import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

BRIDGE_C = os.path.join(ROOT, "src", "kernel", "kernel_bridge.c")
PARSER_PY = os.path.join(ROOT, "tools", "xbe_parser", "xbe_parser.py")


def load_exports():
    with open(PARSER_PY, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    m = re.search(r"KERNEL_EXPORTS\s*[:=][^{]*\{(.*?)\n\}", src, re.S)
    assert m, "could not locate KERNEL_EXPORTS in tools/xbe_parser"
    pairs = re.findall(r"(\d+)\s*:\s*[\"']([A-Za-z_][A-Za-z0-9_]*)", m.group(1))
    exports = {int(o): n for o, n in pairs}
    assert len(exports) > 300, f"only parsed {len(exports)} exports"
    return exports


def load_routes():
    with open(BRIDGE_C, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    routes = re.findall(
        r"case\s+(\d+):\s*return\s+bridge_([A-Za-z_][A-Za-z0-9_]*)\s*;", src)
    assert routes, "no bridge routes found"
    return [(int(o), n) for o, n in routes]


def test_every_route_matches_its_ordinal():
    exports = load_exports()
    bad = []
    for ordinal, name in load_routes():
        expected = exports.get(ordinal)
        if expected is None:
            bad.append(f"ordinal {ordinal} -> bridge_{name}, but no such export")
        elif expected != name:
            bad.append(
                f"ordinal {ordinal} -> bridge_{name}, but ordinal {ordinal} "
                f"is {expected}")

    # KeSetTimerEx is knowingly served by the KeSetTimer bridge; the extra
    # Period argument is dropped. Tracked, not silently accepted.
    known = {"ordinal 150 -> bridge_KeSetTimer, but ordinal 150 is KeSetTimerEx"}
    bad = [b for b in bad if b not in known]

    assert not bad, "misrouted kernel ordinals:\n  " + "\n  ".join(bad)
    print(f"ok  every_route_matches_its_ordinal ({len(load_routes())} routes)")


def test_no_duplicate_ordinals():
    seen, dupes = set(), []
    for ordinal, name in load_routes():
        if ordinal in seen:
            dupes.append(f"ordinal {ordinal} routed twice (last: bridge_{name})")
        seen.add(ordinal)
    assert not dupes, "\n  ".join(dupes)
    print("ok  no_duplicate_ordinals")


def test_arg_size_comments_match_their_ordinal():
    """The stdcall arg table names each function in a comment; check it.

    Routing and argument sizes are two separate tables, and fixing one does not
    fix the other. Ordinal 67 was routed correctly to IoCreateSymbolicLink while
    its arg entry still said `40; /* IoCreateFile(10) */` - so every call popped
    40 bytes instead of 8 and walked esp 32 bytes off. That does not fail where
    it happens: it surfaces later as a function returning with ebx/esi/edi
    restored from the wrong stack slots, which is about as far from "wrong
    stdcall size" as a symptom can look.
    """
    exports = load_exports()
    with open(BRIDGE_C, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    m = re.search(r"stdcall_args_for_ordinal.*?\n\}", src, re.S)
    assert m, "could not locate stdcall_args_for_ordinal"

    bad = []
    for ordinal, _bytes, named in re.findall(
            r"case\s+(\d+):\s*return\s+(\d+);\s*/\*\s*([A-Za-z_][A-Za-z0-9_]*)",
            m.group(0)):
        ordinal = int(ordinal)
        expected = exports.get(ordinal)
        if expected and expected != named:
            bad.append(f"ordinal {ordinal} arg entry is commented {named}, "
                       f"but ordinal {ordinal} is {expected}")

    assert not bad, "arg-size table disagrees with the export table:\n  " + \
                    "\n  ".join(bad)
    print("ok  arg_size_comments_match_their_ordinal")


def test_every_routed_ordinal_has_an_arg_size():
    """A bridged ordinal with no arg entry falls through to `default: return 0`.

    The comment-matching test above cannot see this: there is no entry, so
    there is no comment to disagree with. Adding a bridge and forgetting the
    arg size is one edit, and it leaks the whole argument list on every call.

    Ordinal 47 (HalRegisterShutdownNotification, 2 args) was added this way.
    D3D's CMiniport::InitHardware calls it once, so esp came back 8 bytes low
    and the epilogue's pop edi/esi/ebx each read one slot too far down: `this`
    for the next call became 1, the DMA channel was initialised against
    garbage, and the title hung in a push-buffer wait several calls later with
    nothing to connect it back to a missing stdcall size.

    An ordinal that genuinely takes no arguments needs an explicit `return 0;`
    entry, so that "zero" is a decision on the record rather than a fallthrough.
    """
    exports = load_exports()
    with open(BRIDGE_C, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    m = re.search(r"stdcall_args_for_ordinal.*?\n\}", src, re.S)
    assert m, "could not locate stdcall_args_for_ordinal"
    sized = {int(o) for o in
             re.findall(r"case\s+(\d+):\s*return\s+\d+;", m.group(0))}

    missing = [
        f"ordinal {o} routes to bridge_{n} but has no arg-size entry "
        f"({exports.get(o, '?')})"
        for o, n in load_routes() if o not in sized
    ]
    assert not missing, ("bridged ordinals fall through to `default: return 0` "
                         "and leak their args:\n  " + "\n  ".join(missing))
    print(f"ok  every_routed_ordinal_has_an_arg_size ({len(sized)} sized)")


def test_arg_sizes_are_dword_multiples():
    """stdcall cleanup pops whole dwords; an odd size corrupts the stack."""
    with open(BRIDGE_C, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    m = re.search(r"stdcall_args_for_ordinal.*?\n\}", src, re.S)
    assert m, "could not locate stdcall_args_for_ordinal"
    bad = [
        f"ordinal {o} pops {b} bytes"
        for o, b in re.findall(r"case\s+(\d+):\s*return\s+(\d+);", m.group(0))
        if int(b) % 4
    ]
    assert not bad, "\n  ".join(bad)
    print("ok  arg_sizes_are_dword_multiples")


if __name__ == "__main__":
    test_every_route_matches_its_ordinal()
    test_no_duplicate_ordinals()
    test_arg_size_comments_match_their_ordinal()
    test_every_routed_ordinal_has_an_arg_size()
    test_arg_sizes_are_dword_multiples()
    print("\nall passed")
