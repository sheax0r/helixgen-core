"""Unit tests for the authoring bridge chain-mapping logic (no device).

Uses real device model ids (resolved via the vendored defs) so
``device_category`` works, but never touches hardware.
"""
from __future__ import annotations

import pytest

pytest.importorskip("msgpack")

from helixgen.device import bridge, content as C, defs  # noqa: E402

# real device model ids with known categories (from the vendored defs)
DIST = 310   # HD2_DistScream808Mono   -> distortion
AMP = 695    # HD2_AmpDasBenzinMega    -> amp
DELAY = 87   # HD2_DelaySimpleDelayStereo -> delay
REVERB = 63  # HD2_ReverbPlateStereo   -> reverb
INPUT = 769  # P35_InputInst1_2        -> input
OUTPUT = 783  # P35_OutputMatrix       -> output


def _blk(model_id):
    return {"enbl": 1, "mdls": [{"id__": model_id, "parm": []}]}


def _template():
    # input, distortion, distortion, amp, delay, reverb, output
    blks = []
    for i, mid in enumerate([INPUT, DIST, DIST, AMP, DELAY, REVERB, OUTPUT]):
        blks.append(i)      # the flat list alternates int, dict
        blks.append(_blk(mid))
    return {"cg__": {}, "pm__": [], "sfg_": {"flow": [{"blks": blks}]}}


def test_device_category_resolves():
    assert bridge.device_category(DIST) == "distortion"
    assert bridge.device_category(AMP) == "amp"
    assert bridge.device_category(REVERB) == "reverb"
    assert bridge.device_category(999999999) is None


def test_build_parm_from_defs_with_override():
    parm = bridge.build_parm(DIST, {"Gain": 0.75})
    gain_pid = defs.param_id_for(DIST, "Gain")
    by_pid = {p["pid_"]: p["valu"] for p in parm}
    assert by_pid[gain_pid] == pytest.approx(0.75)
    # every entry carries the model id and is sorted by pid
    assert all(p["mid_"] == DIST for p in parm)
    assert [p["pid_"] for p in parm] == sorted(p["pid_"] for p in parm)


def test_author_chain_assigns_by_category_and_bypasses_rest():
    doc = _template()
    chain = [(DIST, {"Gain": 0.7}), (AMP, {}), (REVERB, {"Mix": 0.3})]
    bridge.author_chain(doc, chain)
    slots = bridge._user_blocks(doc)
    state = {bridge.device_category(b["mdls"][0]["id__"]): b for _p, b in slots}
    enabled = [(bridge.device_category(b["mdls"][0]["id__"]), b["mdls"][0]["id__"])
               for _p, b in slots if b.get("enbl") == 1]
    # one distortion, the amp, and the reverb are enabled...
    assert (("distortion", DIST) in enabled)
    assert (("amp", AMP) in enabled)
    assert (("reverb", REVERB) in enabled)
    # ...the second distortion slot and the delay slot are bypassed
    disabled_cats = [bridge.device_category(b["mdls"][0]["id__"])
                     for _p, b in slots if b.get("enbl") == 0]
    assert "delay" in disabled_cats
    assert disabled_cats.count("distortion") == 1  # only the extra one


def test_author_chain_raises_without_matching_slot():
    doc = _template()  # has no modulation slot
    MOD = defs.model_id_for("HD2_ModChorusStereo") or 0
    with pytest.raises(ValueError):
        bridge.author_chain(doc, [(REVERB, {}), (REVERB, {})])  # only one reverb slot


def test_content_from_template_roundtrips_to_stored_blob():
    doc = _template()
    blob = C.encode_content(doc)  # _sbepgsm edit-buffer form
    out = bridge.content_from_template(blob, [(DIST, {"Gain": 0.5})])
    assert out[:8] == C.CONTENT_DATA_MAGIC        # stored-content form
    back = C.decode_any(out)
    assert isinstance(back["sfg_"]["flow"][0]["blks"], list)
