"""Transcode fidelity for signal-flow params (parity #18): input-endpoint
params, output level/pan, impedance ints, and split/join params must survive
``.hsp -> _sbepgsm`` (``device/transcode.py``). Offline, synthetic bodies.
"""
import pytest

pytest.importorskip("msgpack")

from helixgen.device import bridge, content, transcode  # noqa: E402


def _wrap(params):
    return {k: {"value": v} for k, v in params.items()}


def _hsp_body(b00_model, b00_params, b13_params=None, extra_blocks=None,
              preset_params=None):
    flow0 = {
        "b00": {"type": "input", "position": 0, "path": 0,
                "slot": [{"model": b00_model, "params": b00_params}]},
        "b13": {"type": "output", "position": 13, "path": 0,
                "slot": [{"model": "P35_OutputMatrix",
                          "params": _wrap(b13_params or {})}]},
    }
    flow0.update(extra_blocks or {})
    return {
        "meta": {"name": "t", "device_id": 2490368},
        "preset": {"flow": [flow0], "params": dict(preset_params or {})},
    }


def _blocks_by_type(doc, flow=0):
    out = {}
    for b in doc["sfg_"]["flow"][flow]["blks"]:
        if isinstance(b, dict):
            out.setdefault(b.get("type"), []).append(b)
    return out


def _parm_by_pid(block):
    return {p["pid_"]: p["valu"] for p in block["mdls"][0]["parm"]}


class TestBridgeLift:
    def test_mono_input_params_lifted(self):
        body = _hsp_body("P35_InputInst1", _wrap({
            "Pad": 2, "Trim": -3.0, "noiseGate": True,
            "threshold": -55.0, "decay": 0.2}))
        paths = bridge.hsp_to_paths(body, strict=False)
        assert paths[0]["input_params"] == {
            "Pad": 2, "Trim": -3.0, "noiseGate": True,
            "threshold": -55.0, "decay": 0.2}

    def test_stereo_input_params_lifted_with_channel_names(self):
        body = _hsp_body("P35_InputInst1_2", {
            "Pad": {"1": {"value": 1}, "2": {"value": 2}},
            "StereoLink": {"value": True},
        })
        paths = bridge.hsp_to_paths(body, strict=False)
        assert paths[0]["input_params"] == {
            "Pad.1": 1, "Pad.2": 2, "StereoLink": True}

    def test_output_params_lifted(self):
        body = _hsp_body("P35_InputInst1", {}, {"gain": -4.5, "pan": 0.25})
        paths = bridge.hsp_to_paths(body, strict=False)
        assert paths[0]["output_params"] == {"gain": -4.5, "pan": 0.25}


class TestTranscodeSynthesis:
    def test_input_params_survive(self):
        body = _hsp_body("P35_InputInst1", _wrap({
            "Pad": 2, "Trim": -3.0, "noiseGate": True,
            "threshold": -55.0, "decay": 0.2}))
        doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
        inp = _blocks_by_type(doc)[8][0]  # type 8 = input endpoint
        assert inp["mdls"][0]["id__"] == 770  # P35_InputInst1
        pids = _parm_by_pid(inp)
        assert pids[2] == 2          # Pad
        assert pids[3] == -3.0       # Trim
        assert pids[4] is True       # noiseGate
        assert pids[5] == -55.0      # threshold
        assert pids[6] == 0.2        # decay

    def test_output_params_survive(self):
        body = _hsp_body("P35_InputInst1", {}, {"gain": -4.5, "pan": 0.25})
        doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
        outs = _blocks_by_type(doc)[9]
        matrix = [o for o in outs if o["mdls"][0]["id__"] == 783]
        assert matrix
        pids = _parm_by_pid(matrix[0])
        assert pids[2] == -4.5       # gain
        assert pids[1] == 0.25       # pan

    def test_default_endpoints_unchanged_without_params(self):
        body = _hsp_body("P35_InputInst1", {})
        doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
        inp = _blocks_by_type(doc)[8][0]
        assert inp["mdls"][0]["id__"] == 770

    def test_impedance_ints_in_pm(self):
        body = _hsp_body("P35_InputInst1", {}, preset_params={
            "inst1Z": "1M", "inst2Z": "FirstBlock"})
        doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
        pm = {p["key_"]: p["val_"] for p in doc["pm__"]}
        assert pm["preset.inst1.z"] == 9   # 1M = enum index 9
        assert pm["preset.inst2.z"] == 0   # FirstBlock = 0

    def test_impedance_defaults_when_absent(self):
        body = _hsp_body("P35_InputInst1", {})
        doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
        pm = {p["key_"]: p["val_"] for p in doc["pm__"]}
        assert pm["preset.inst1.z"] == 1   # FirstEnabled (device default)

    def test_split_and_join_params_survive(self):
        extra = {
            "b02": {"type": "split", "position": 2, "path": 0,
                    "branch": "b15", "endpoint": "b04",
                    "slot": [{"model": "P35_AppDSPSplitXOver",
                              "params": _wrap({"Frequency": 800.0,
                                               "Reverse": True,
                                               "enable": False})}]},
            "b04": {"type": "join", "position": 4, "path": 0,
                    "branch": "b15", "endpoint": "b02",
                    "slot": [{"model": "P35_AppDSPJoin",
                              "params": _wrap({"A Level": -2.0,
                                               "B Pan": 0.1,
                                               "B Polarity": True})}]},
        }
        body = _hsp_body("P35_InputInst1", {}, extra_blocks=extra)
        doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
        by_type = _blocks_by_type(doc)
        split = by_type[3][0]
        assert split["mdls"][0]["id__"] == 476  # SplitXOver
        spids = _parm_by_pid(split)
        assert spids[1] == 800.0     # Frequency
        assert spids[2] is True      # Reverse
        join = by_type[4][0]
        assert join["mdls"][0]["id__"] == 478
        jpids = _parm_by_pid(join)
        assert jpids[1] == -2.0      # A Level
        assert jpids[4] == 0.1       # B Pan
        assert jpids[5] is True      # B Polarity
