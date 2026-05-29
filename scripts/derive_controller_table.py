#!/usr/bin/env python3
"""One-time helper: scan data/*.hsp and report observed FS/EXP controller sources.

Not invoked at runtime. Used to derive the values pasted into
src/helixgen/controllers.py:CONTROLLER_SOURCE_IDS.

Usage:
    python scripts/derive_controller_table.py [data_dir]

Prints a frequency table of (source_id, controller_type) tuples seen across
all .hsp files in the given directory (default: ./data).
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from helixgen.hsp import read_hsp


def main(argv: list[str]) -> int:
    data_dir = Path(argv[1] if len(argv) > 1 else "data")
    if not data_dir.is_dir():
        print(f"No such directory: {data_dir}", file=sys.stderr)
        return 1

    counts: Counter = Counter()
    for fp in sorted(data_dir.glob("*.hsp")):
        try:
            d = read_hsp(fp)
        except Exception as e:
            print(f"skip {fp.name}: {e}", file=sys.stderr)
            continue
        flow = d.get("preset", {}).get("flow") or []
        for path in flow:
            if not isinstance(path, dict):
                continue
            for bkey, block in path.items():
                if not isinstance(block, dict) or not bkey.startswith("b"):
                    continue
                slots = block.get("slot") or []
                for slot in slots:
                    if not isinstance(slot, dict):
                        continue
                    enabled = block.get("@enabled")
                    if isinstance(enabled, dict) and isinstance(enabled.get("controller"), dict):
                        c = enabled["controller"]
                        counts[(c.get("source"), c.get("type", ""))] += 1
                    for pname, pval in (slot.get("params") or {}).items():
                        if isinstance(pval, dict) and isinstance(pval.get("controller"), dict):
                            c = pval["controller"]
                            counts[(c.get("source"), c.get("type", ""))] += 1

    print(f"{'source':>12}  {'hex':>12}  {'type':<15}  count")
    for (src, ctype), n in sorted(counts.items()):
        hex_str = f"0x{src:08x}" if isinstance(src, int) else str(src)
        print(f"{src!s:>12}  {hex_str:>12}  {ctype:<15}  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
