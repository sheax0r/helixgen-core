"""Unit tests for the ``.hsp`` -> device chain + IR helpers (no device).

Uses real device model ids (resolved via the vendored defs) so
``device_category`` works, but never touches hardware. The old
template-authoring path (``author_chain``/``content_from_template``/…) was
retired once the transcoder shipped; its round-trip fidelity now lives in
``tests/test_transcode.py``.
"""
from __future__ import annotations

import pytest

pytest.importorskip("msgpack")

from helixgen.device import bridge  # noqa: E402

# real device model ids with known categories (from the vendored defs)
DIST = 310   # HD2_DistScream808Mono   -> distortion
AMP = 695    # HD2_AmpDasBenzinMega    -> amp
REVERB = 63  # HD2_ReverbPlateStereo   -> reverb
INPUT = 769  # P35_InputInst1_2        -> input
OUTPUT = 783  # P35_OutputMatrix       -> output


def test_device_category_resolves():
    assert bridge.device_category(DIST) == "distortion"
    assert bridge.device_category(AMP) == "amp"
    assert bridge.device_category(REVERB) == "reverb"
    assert bridge.device_category(999999999) is None


def test_map_params_name_then_positional():
    # model 310 (Screamer) device params are Gain, Tone, Level.
    # helixgen sends "Drive" (no exact match) + "Tone" (exact) -> Drive maps to
    # the first leftover device param (Gain) by position; Tone matches by name.
    out = bridge.map_params(DIST, {"Drive": 0.7, "Tone": 0.4})
    assert out.get("Gain") == pytest.approx(0.7)
    assert out.get("Tone") == pytest.approx(0.4)


def test_hsp_to_chain_resolves_and_skips_endpoints():
    body = {
        "preset": {"flow": [{
            "b00": {"slot": [{"model": "P35_InputInst1_2", "params": {}}]},
            "b01": {"slot": [{"model": "HD2_DistScream808Mono",
                              "params": {"Drive": {"value": 0.6}}}]},
            "b02": {"slot": [{"model": "HD2_ReverbPlateStereo",
                              "params": {"Mix": {"value": 0.2}}}]},
            "b13": {"slot": [{"model": "P35_OutputMatrix", "params": {}}]},
        }]}
    }
    # direct resolver (these device strings resolve via defs)
    chain = bridge.hsp_to_chain(body, resolve_model=lambda m: __import__(
        "helixgen.device.defs", fromlist=["model_id_for"]).model_id_for(m))
    ids = [mid for mid, _ in chain]
    assert DIST in ids and REVERB in ids       # user blocks kept
    assert INPUT not in ids and OUTPUT not in ids  # endpoints skipped


def test_hsp_to_chain_strict_raises_on_unresolved():
    body = {"preset": {"flow": [{"b01": {"slot": [{"model": "NOPE_notamodel",
                                                   "params": {}}]}}]}}
    with pytest.raises(bridge.UnresolvedModel):
        bridge.hsp_to_chain(body, resolve_model=lambda m: None, strict=True)


def test_hsp_ir_hashes_extracts_irhashes():
    body = {"preset": {"flow": [
        {"b01": {"slot": [{"model": "HX2_ImpulseResponseWithPan", "irhash": "aa11"}]},
         "b02": {"slot": [{"model": "HD2_AmpBrit2204"}]}},
        {"b05": {"slot": [{"model": "HX2_ImpulseResponse", "irhash": "bb22"},
                          {"model": "HX2_ImpulseResponse", "irhash": "cc33"}]}},
    ]}}
    assert bridge.hsp_ir_hashes(body) == {"aa11", "bb22", "cc33"}


def test_check_irs_partitions_present_and_missing():
    body = {"preset": {"flow": [
        {"b01": {"slot": [{"model": "HX2_ImpulseResponse", "irhash": "ondev"}]},
         "b02": {"slot": [{"model": "HX2_ImpulseResponse", "irhash": "missing"}]}},
    ]}}

    class FakeClient:
        def device_ir_hashes(self, *, verify=None):
            # check_irs cross-checks the apparently-missing hashes against the
            # point lookup (#38 Task 4); here the listing is not stale, so the
            # verify set changes nothing.
            assert set(verify or ()) == {"ondev", "missing"}
            return {"ondev", "other"}

    status = bridge.check_irs(FakeClient(), body)
    assert status["present"] == {"ondev"}
    assert status["missing"] == {"missing"}


def test_check_irs_does_not_read_the_listing_when_no_irs_are_referenced():
    """device_ir_hashes reads the -11 listing STRICTLY, so a dropped reply
    raises. A preset that references no IRs has nothing to compare — asking
    anyway would let a transient listing drop abort an install outright."""
    body = {"preset": {"flow": [
        {"b01": {"slot": [{"model": "HD2_AmpBrit2204"}]}},
    ]}}

    class Exploding:
        def device_ir_hashes(self, *, verify=None):
            raise AssertionError("must not read the IR listing for 0 IRs")

    status = bridge.check_irs(Exploding(), body)
    assert status == {"present": set(), "missing": set()}
