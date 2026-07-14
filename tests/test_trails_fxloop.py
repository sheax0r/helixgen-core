"""FX-loop trails (parity #18): the author-facing `trails` field now covers
FX-Loop blocks (`HD2_FXLoop*`) alongside delay/reverb — the device manual
documents Trails on FX Loop blocks; Send-/Return-only blocks still reject it.
"""
import pytest

from helixgen import mutate
from helixgen.generate import GenerateError, compose_preset
from helixgen.mutate import MutateError
from helixgen.spec import parse_spec
from helixgen.view import view


@pytest.fixture
def loop_library(hsp_library):
    """hsp_library + a synthetic FX-Loop block and a Send-only block (the
    corpus carries no FX-Loop exemplar; shapes follow the device defs:
    Send/Return/Mix on fxloop, Send/DryThru on send)."""
    from helixgen.library import Block
    hsp_library.save_block(Block(
        model_id="HD2_FXLoopMono1", category="send", display_name="FX Loop 1",
        params={"Send": {"type": "float"}, "Return": {"type": "float"},
                "Mix": {"type": "float"}},
        exemplar={"@model": "HD2_FXLoopMono1", "@type": "fx", "@enabled": True,
                  "Send": 0.0, "Return": 0.0, "Mix": 1.0},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-07-14"}))
    hsp_library.save_block(Block(
        model_id="HD2_SendMono1", category="send", display_name="Send 1",
        params={"Send": {"type": "float"}, "DryThru": {"type": "float"}},
        exemplar={"@model": "HD2_SendMono1", "@type": "fx", "@enabled": True,
                  "Send": 0.0, "DryThru": 0.0},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-07-14"}))
    hsp_library.rebuild_index()
    return hsp_library


def _compose(lib, blocks):
    spec = parse_spec({"name": "t", "paths": [{"blocks": blocks}]})
    return compose_preset(spec, lib, source="t")


def _b01_harness_trails(body):
    return body["preset"]["flow"][0]["b01"]["harness"]["params"]["Trails"]


class TestGenerate:
    def test_trails_on_fxloop_accepted(self, loop_library):
        body = _compose(loop_library, [{"block": "FX Loop 1", "trails": True}])
        assert _b01_harness_trails(body) == {"value": True}

    def test_trails_on_send_only_rejected(self, loop_library):
        with pytest.raises(GenerateError, match="[Tt]rails"):
            _compose(loop_library, [{"block": "Send 1", "trails": True}])


class TestMutate:
    def test_set_trails_on_fxloop(self, loop_library):
        body = _compose(loop_library, [{"block": "FX Loop 1"}])
        mutate.set_trails(body, "FX Loop 1", True, loop_library)
        assert _b01_harness_trails(body) == {"value": True}

    def test_set_trails_on_send_only_rejected(self, loop_library):
        body = _compose(loop_library, [{"block": "Send 1"}])
        with pytest.raises(MutateError, match="[Tt]rails"):
            mutate.set_trails(body, "Send 1", True, loop_library)


class TestViewLift:
    def test_view_lifts_fxloop_trails(self, loop_library):
        body = _compose(loop_library, [{"block": "FX Loop 1", "trails": True}])
        out = view(body, loop_library)
        entry = out["paths"][0]["blocks"][0]
        assert entry["trails"] is True
        assert "Trails" not in (entry.get("raw", {}).get("harness", {})
                                .get("params", {}))

    def test_view_keeps_send_only_trails_verbatim(self, loop_library):
        body = _compose(loop_library, [{"block": "Send 1"}])
        # simulate a device-authored harness Trails on a Send-only block
        bnn = body["preset"]["flow"][0]["b01"]
        bnn["harness"] = {"@enabled": {"value": True},
                          "params": {"Trails": {"value": True}}}
        out = view(body, loop_library)
        entry = out["paths"][0]["blocks"][0]
        assert "trails" not in entry
        assert entry["raw"]["harness"]["params"]["Trails"] == {"value": True}
