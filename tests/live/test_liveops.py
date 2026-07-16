"""Live-ops verbs (mutate the ACTIVE tone): snapshot / bypass / model /
live set-param.

Safety procedure (as validated live 2026-07-15): FIRST `device install` +
`device load` an HGTEST preset into an empty slot, so every live-op mutates
only the HGTEST tone's edit buffer. The mutations are volatile (never saved)
and the preset is deleted afterwards. Residual: the module necessarily leaves
the (volatile) edit buffer on the deleted HGTEST tone — the device has no
"which preset was active before?" query to restore from.
"""
from __future__ import annotations

import json

import pytest

from .conftest import HGTEST, delete_preset, find_user_preset, install_preset

pytestmark = [pytest.mark.live, pytest.mark.liveops]


@pytest.fixture(scope="module")
def live_tone(helix, hgtest_hsp, amp_blocks):
    """Install + LOAD an HGTEST tone; yields its edit-buffer amp coordinate."""
    code, out, err = helix("device", "list", "--json")
    assert code == 0, err or out
    occupied = {m.get("posi") for m in json.loads(out)}
    free = [p for p in range(127, -1, -1) if p not in occupied]
    if not free:
        pytest.skip("no empty user slot for the liveops tone")
    name = f"{HGTEST} LiveOps"
    cid = install_preset(helix, hgtest_hsp, name, free[0])
    try:
        code, out, err = helix("device", "load", cid)
        assert code == 0, f"device load failed: {err or out}"
        code, out, err = helix("device", "blocks", "--json")
        assert code == 0, err or out
        blocks = json.loads(out)
        amp = next((b for b in blocks if "Amp" in (b.get("model") or "")), None)
        assert amp is not None, f"no amp block in loaded edit buffer: {blocks}"
        yield {"cid": cid, "amp": amp, "blocks": blocks}
    finally:
        delete_preset(helix, cid)
        assert find_user_preset(helix, name) is None, \
            f"cleanup failed: {name!r} still on device"


def test_live_set_param_amp_pid1(helix, live_tone):
    """Live `device set-param` against an AMP block, pid 1 — the combination
    validated working on real hardware 2026-07-15. Do NOT use the help text's
    output-block gain pid 2 example: it fails deterministically on fw 1.3.2
    (pids 0 and 2 rejected at Output Matrix — backlog #67)."""
    amp = live_tone["amp"]
    code, out, err = helix("device", "set-param",
                           amp["path"], amp["block"], 1, 0.5)
    assert code == 0, err or out


def test_bypass_off_then_on(helix, live_tone):
    amp = live_tone["amp"]
    code, out, err = helix("device", "bypass", amp["path"], amp["block"], "off")
    assert code == 0, err or out
    code, out, err = helix("device", "bypass", amp["path"], amp["block"], "on")
    assert code == 0, err or out


def test_model_swap_same_category(helix, live_tone, amp_blocks):
    """Volatile live model swap to a different amp (same category — the
    device rejects cross-category swaps)."""
    amp = live_tone["amp"]
    current = amp.get("model")
    other = next((b["model_id"] for b in amp_blocks
                  if b["model_id"] != current), None)
    if other is None:
        pytest.skip("library has no second amp model to swap to")
    code, out, err = helix("device", "model", amp["path"], amp["block"], other)
    assert code == 0, err or out
    # swap back for tidiness (still volatile either way)
    if current:
        code, out, err = helix("device", "model",
                               amp["path"], amp["block"], current)
        assert code == 0, err or out


def test_snapshot_recall(helix, live_tone):
    code, out, err = helix("device", "snapshot", 0)
    assert code == 0, err or out


def test_snapshot_rejects_out_of_range(helix, live_tone):
    code, out, err = helix("device", "snapshot", 99)
    assert code != 0
