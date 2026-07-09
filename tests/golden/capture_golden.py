#!/usr/bin/env python3
"""One-shot script that (re)captures the golden `.hsp` corpus.

Run this on the CURRENT (pre-rewrite) pipeline whenever a corpus recipe is
added or intentionally changed:

    PYTHONPATH=$PWD/src python tests/golden/capture_golden.py

For every `tests/golden/corpus/<name>.recipe.json`, builds a fresh
synthetic library (see `harness.build_corpus_library`), runs it through
`harness.run_current_pipeline`, and overwrites `tests/golden/corpus/<name>.hsp`
with the result.

This script is NOT run by pytest. `test_golden_parity` only *reads* the
committed corpus/*.hsp files it produces; re-running this script is a
deliberate, manual act of updating the pinned behavior (which should only
happen for a reason worth explaining in the commit message).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from tests.golden import harness  # noqa: E402


def capture_all() -> list[str]:
    names = sorted(
        p.name.removesuffix(".recipe.json") for p in harness.CORPUS_DIR.glob("*.recipe.json")
    )
    if not names:
        print("No *.recipe.json files found in tests/golden/corpus/.", file=sys.stderr)
        return []

    captured = []
    with tempfile.TemporaryDirectory(prefix="helixgen-golden-capture-") as tmp:
        library = harness.build_corpus_library(Path(tmp) / "lib")
        for name in names:
            recipe = harness.load_recipe(name)
            raw = harness.run_current_pipeline(recipe, library)
            out_path = harness.CORPUS_DIR / f"{name}.hsp"
            out_path.write_bytes(raw)
            captured.append(name)
            print(f"captured {out_path.relative_to(harness.CORPUS_DIR.parents[1])} "
                  f"({len(raw)} bytes)")
    return captured


if __name__ == "__main__":
    captured = capture_all()
    print(f"\n{len(captured)} golden(s) captured.")
