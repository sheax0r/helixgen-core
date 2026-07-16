"""Per-snapshot OUTPUT-endpoint gain through the `.hsp` -> device transcoder
(loudness phase 2, backlog #62).

The normalize loop writes per-snapshot trims onto the path output block's
`gain` (dB-native). These tests pin the delivery path: `bridge.hsp_to_paths`
lifts the b13 `snapshots` arrays as `output_snap_params`, and the transcoder
emits a param snapshot target keyed by the OutputMatrix endpoint's instance
id, with the parm leaf bound (`snap=True, tid_=<trg>`), exactly like a
user-block snapshot param.

NOTE (phase-0 hardware findings, spec 2026-07-14): every meter-grid tap sits
UPSTREAM of the output block's gain, so these trims are dB-exact but can
never be confirmed via `device measure` — the loop trusts the math.
"""
from __future__ import annotations

import pytest

pytest.importorskip("msgpack")

from helixgen.device import bridge, content, defs, transcode  # noqa: E402

OUTPUT_MATRIX = 783  # P35_OutputMatrix


def _tamv_map(snap):
    tamv = snap["tamv"]
    return {tamv[i]: tamv[i + 1] for i in range(0, len(tamv), 2)}


def _output_gain_hsp_body(*, snapshots=True, flow1=False):
    """A minimal authored `.hsp` whose path-0 output gain carries a
    per-snapshot trim array (dense, base 0.0, snapshot 1 trimmed -3 dB)."""
    gain = {"value": 0.0}
    if snapshots:
        gain["snapshots"] = [0.0, -3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    def _flow(g):
        return {
            "b00": {"@enabled": {"value": True},
                    "slot": [{"model": "P35_InputInst1", "params": {}}]},
            "b01": {"@enabled": {"value": True},
                    "slot": [{"model": "HD2_DistMinotaurMono",
                              "params": {"Gain": {"value": 0.4}}}]},
            "b13": {"@enabled": {"value": True}, "type": "output",
                    "slot": [{"model": "P35_OutputMatrix",
                              "params": {"gain": g,
                                         "pan": {"value": 0.5}}}]},
        }
    flows = [_flow(gain)]
    if flow1:
        flows.append(_flow({"value": 0.0,
                            "snapshots": [0.0, 0.0, -6.0, 0.0,
                                          0.0, 0.0, 0.0, 0.0]}))
    snaps = [{"name": "A"}, {"name": "B"}] + [
        {"name": f"S{i}"} for i in range(3, 9)]
    return {"meta": {"device_id": "stadium_xl"},
            "preset": {"flow": flows, "snapshots": snaps}}


def _output_endpoint(doc, flow=0):
    for b in doc["sfg_"]["flow"][flow]["blks"]:
        if isinstance(b, dict):
            mid = (b.get("mdls") or [{}])[0].get("id__")
            if mid == OUTPUT_MATRIX:
                return b
    return None


def test_bridge_lifts_output_snap_params():
    paths = bridge.hsp_to_paths(_output_gain_hsp_body())
    assert paths[0]["output_snap_params"] == {
        "gain": [0.0, -3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]}


def test_bridge_omits_output_snap_params_without_array():
    paths = bridge.hsp_to_paths(_output_gain_hsp_body(snapshots=False))
    assert "output_snap_params" not in paths[0]


def test_output_gain_snapshots_tracked_and_bound():
    doc = content.decode_any(transcode.hsp_to_sbepgsm(_output_gain_hsp_body()))
    entt = doc["cg__"]["entt"]
    out = _output_endpoint(doc)
    assert out is not None and out["id__"] == 13  # base 0 + gridpos 13
    pid = defs.param_id_for(OUTPUT_MATRIX, "gain")
    trgs = [t for t in entt["trgs"]
            if t.get("eID_") == out["id__"] and t.get("pid_") == pid]
    assert len(trgs) == 1, entt["trgs"]
    trg = trgs[0]
    assert trg["type"] == 2 and trg["enty"] == 3 and trg["mmid"] == OUTPUT_MATRIX
    # the gain parm leaf is snapshot-bound
    leaf = next(p for p in out["mdls"][0]["parm"] if p["pid_"] == pid)
    assert leaf["snap"] is True and leaf["tid_"] == trg["id__"]
    # stid + ptid registration and per-snapshot tamv values
    assert trg["id__"] in entt["ctm_"]["stid"]
    ptid = entt["ctm_"]["ptid"]
    assert dict(zip(ptid[::2], ptid[1::2]))[
        (out["id__"] << 16) | pid] == trg["id__"]
    snps = sorted(entt["snps"], key=lambda s: s["si__"])
    row = [_tamv_map(s)[trg["id__"]] for s in snps]
    assert row == [0.0, -3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_output_gain_snapshots_ignored_without_variation():
    body = _output_gain_hsp_body()
    body["preset"]["flow"][0]["b13"]["slot"][0]["params"]["gain"][
        "snapshots"] = [0.0] * 8
    doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
    entt = doc["cg__"]["entt"]
    out = _output_endpoint(doc)
    assert not any(t.get("eID_") == out["id__"] for t in entt["trgs"])
    assert out["mdls"][0]["parm"][0]["snap"] is False


def test_flow1_output_gain_uses_nonzero_eid():
    doc = content.decode_any(transcode.hsp_to_sbepgsm(
        _output_gain_hsp_body(flow1=True)))
    entt = doc["cg__"]["entt"]
    out1 = _output_endpoint(doc, flow=1)
    assert out1 is not None and out1["id__"] == 41  # base 28 + gridpos 13
    pid = defs.param_id_for(OUTPUT_MATRIX, "gain")
    trg = next(t for t in entt["trgs"]
               if t.get("eID_") == 41 and t.get("pid_") == pid)
    snps = sorted(entt["snps"], key=lambda s: s["si__"])
    row = [_tamv_map(s)[trg["id__"]] for s in snps]
    assert row == [0.0, 0.0, -6.0, 0.0, 0.0, 0.0, 0.0, 0.0]
