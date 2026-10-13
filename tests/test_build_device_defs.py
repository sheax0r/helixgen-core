"""``tools/build_device_defs.py`` picks the newest ``p35md-*.bin`` itself, so a
firmware/app update only needs a re-run, not an edit (1.4 shipped 828 models
while the script still pointed at the 1.3 file)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "build_device_defs.py"
_spec = importlib.util.spec_from_file_location("build_device_defs", _TOOL)
bdd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bdd)


def test_picks_highest_version_numerically(tmp_path):
    md = tmp_path / "modeldefs"
    md.mkdir()
    for v in ("1_2_1_0", "1_4_0_0", "1_10_0_0", "1_3_0_0"):
        (md / f"p35md-{v}.bin").write_bytes(b"")
    (md / "p37md-9_0_0_0.bin").write_bytes(b"")  # other product: ignored
    assert bdd.find_modeldefs(tmp_path).name == "p35md-1_10_0_0.bin"


def test_no_modeldefs_raises(tmp_path):
    (tmp_path / "modeldefs").mkdir()
    try:
        bdd.find_modeldefs(tmp_path)
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")
