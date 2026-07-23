#!/usr/bin/env python3
"""
Kernel coverage for one title: what it imports vs what the bridge routes.

    py -3 -m tools.kernel_audit.coverage <analysis.json>

Answers the question you actually have when standing up a new title -- "how much
kernel work is left?" -- and, more usefully, splits the remainder by how much
work each piece is:

  A. xbox_* exists, needs a bridge_ wrapper   mechanical
  B. data export                              needs a value, not a function
  C. no implementation at all                 real work

The headline "N imports missing" number on its own is misleading in both
directions. An XBE imports whatever its statically-linked XDK references, so
much of the list is never called; and an unrouted ordinal is not necessarily
fatal, because kernel_bridge.c's argument-size table lets the generic stub clean
the right number of bytes off the simulated stack and return 0.

Data exports are called out separately because they are a different mechanism:
the thunk holds a pointer to a variable, not a function, so routing one to a
bridge wrapper produces a call through a data address.
"""

import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BRIDGE_C = os.path.join(ROOT, "src", "kernel", "kernel_bridge.c")

# Kernel exports whose thunk holds a variable rather than a function.
# Routing these to a bridge wrapper would call through a data address.
DATA_EXPORTS = {
    "ExEventObjectType", "ExMutantObjectType", "ExSemaphoreObjectType",
    "ExTimerObjectType", "IoCompletionObjectType", "IoDeviceObjectType",
    "IoFileObjectType", "PsThreadObjectType", "ObDirectoryObjectType",
    "ObSymbolicLinkObjectType",
    "HalDiskCachePartitionCount", "HalDiskModelNumber", "HalDiskSerialNumber",
    "HalBootSMCVideoMode", "IdexChannelObject",
    "KeTickCount", "KeTimeIncrement", "KeInterruptTime", "KeSystemTime",
    "LaunchDataPage", "XboxHardwareInfo", "XboxKrnlVersion", "XboxHDKey",
    "XboxSignatureKey", "XboxLANKey", "XboxAlternateSignatureKeys",
    "XePublicKeyData", "XeImageFileName", "XcKeyTable",
}


def bridge_routes():
    src = open(BRIDGE_C, encoding="utf-8", errors="replace").read()
    return {int(o) for o in re.findall(
        r"case\s+(\d+):\s*return\s+bridge_[A-Za-z_]", src)}


def bridge_arg_sizes():
    src = open(BRIDGE_C, encoding="utf-8", errors="replace").read()
    return {int(o) for o in re.findall(
        r"case\s+(\d+):\s*return\s+\d+;\s*/\*", src)}


def thunk_routes():
    """Ordinals kernel_thunks.c resolves directly.

    Data exports live here rather than in the bridge: their thunk holds a
    pointer into emulated kernel data, so there is no function to wrap and no
    stack arguments to size. Checking only the bridge reports every one of them
    as a gap, which is how this tool first claimed Crimson Skies was missing 18
    data exports that were already handled.
    """
    path = os.path.join(ROOT, "src", "kernel", "kernel_thunks.c")
    if not os.path.exists(path):
        return set()
    src = open(path, encoding="utf-8", errors="replace").read()
    return {int(o) for o in re.findall(r"case\s+(\d+):\s*return\s+", src)}


def kernel_impls():
    """{name: file} for every xbox_<Name>() defined outside the bridge."""
    out = {}
    for path in glob.glob(os.path.join(ROOT, "src", "kernel", "*.c")):
        if "kernel_bridge" in path:
            continue
        src = open(path, encoding="utf-8", errors="replace").read()
        for name in re.findall(r"\bxbox_([A-Za-z_][A-Za-z0-9_]*)\s*\(", src):
            out.setdefault(name, os.path.basename(path))
    return out


def classify(imports):
    routed, sizes, impls = bridge_routes(), bridge_arg_sizes(), kernel_impls()
    thunks = thunk_routes()
    covered, wrap, data, todo = [], [], [], []
    for imp in sorted(imports, key=lambda i: i["ordinal"]):
        o, n = imp["ordinal"], imp["name"]
        if o in routed or (n in DATA_EXPORTS and o in thunks):
            covered.append(imp)
        elif n in DATA_EXPORTS:
            data.append(imp)
        elif n in impls:
            wrap.append((imp, impls[n]))
        else:
            todo.append(imp)
    return covered, wrap, data, todo, sizes


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tools.kernel_audit.coverage",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("analysis", help="analysis.json from tools.xbe_parser")
    ap.add_argument("--list", action="store_true",
                    help="also list the ordinals already routed")
    args = ap.parse_args(argv)

    data_in = json.load(open(args.analysis))
    imports = data_in["kernel_imports"]
    title = data_in.get("title", os.path.basename(args.analysis))
    covered, wrap, data, todo, sizes = classify(imports)
    n = len(imports)

    print("%s -- %d kernel imports" % (title, n))
    print("=" * 66)
    print("  handled (bridge or thunk)   %4d  (%.1f%%)" % (len(covered), 100.0 * len(covered) / n))
    print("  xbox_* exists, needs a wrap %4d  mechanical" % len(wrap))
    print("  data exports                %4d  need a value, not a function" % len(data))
    print("  no implementation           %4d  real work" % len(todo))
    print()

    _routed, _thunks = bridge_routes(), thunk_routes()
    unsafe = [i for i in imports
              if i["ordinal"] not in _routed and i["ordinal"] not in sizes
              and not (i["name"] in DATA_EXPORTS and i["ordinal"] in _thunks)]
    print("  of the %d unhandled, %d have no argument-size entry either --" %
          (n - len(covered), len(unsafe)))
    print("  those cannot be stack-safely stubbed and will corrupt the")
    print("  simulated stack if the title ever calls them:")
    for i in unsafe:
        print("      %3d  %s" % (i["ordinal"], i["name"]))
    if not unsafe:
        print("      (none -- every unrouted import can be stubbed safely)")
    print()

    if wrap:
        print("-- A. needs a bridge_ wrapper around an existing xbox_* --")
        for i, where in wrap:
            print("   %3d  %-36s %s" % (i["ordinal"], i["name"], where))
        print()
    if data:
        print("-- B. data exports --")
        for i in data:
            print("   %3d  %s" % (i["ordinal"], i["name"]))
        print()
    if todo:
        print("-- C. no implementation --")
        for i in todo:
            print("   %3d  %s" % (i["ordinal"], i["name"]))
        print()
    if args.list:
        print("-- routed --")
        for i in covered:
            print("   %3d  %s" % (i["ordinal"], i["name"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
