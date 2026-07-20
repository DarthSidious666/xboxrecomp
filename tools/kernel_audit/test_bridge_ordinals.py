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
    test_arg_sizes_are_dword_multiples()
    print("\nall passed")
