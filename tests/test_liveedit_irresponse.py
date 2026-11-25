"""IR frequency-response endpoint (81v.23): session.ir_response + /ir-response.

The response is fully LOCAL — the block already carries the IR's content hash,
and the samples come from the user's local IR library (IrMapping), so these
tests fake the mapping and never touch a device.
"""
from __future__ import annotations

import struct
import wave

import pytest

import helixgen.ir as ir_mod
from helixgen.ir import IrMappingError
from helixgen.liveedit import server
from helixgen.liveedit.session import LiveError, LiveSession
from tests.test_liveedit_chain import IN_INST1, IN_NONE, IR, OUT_MATRIX, OUT_NONE, _blk, _eb
from tests.test_liveedit_session import FakeClient, FakeLease

HASH = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def _ir_eb(irmd):
    m0 = {"id__": IR, "enbl": 1, "parm": []}
    if irmd is not None:
        m0["irmd"] = irmd
    return _eb({
        0: _blk(IN_INST1), 1: {"enbl": 1, "mdls": [m0]},
        13: _blk(OUT_MATRIX), 14: _blk(IN_NONE), 27: _blk(OUT_NONE),
    })


def _sess(irmd=bytes.fromhex(HASH)):
    sess = LiveSession(FakeClient(eb=_ir_eb(irmd)), lease=FakeLease())
    sess.load()
    return sess


def _write_impulse_wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        samples = [1.0] + [0.0] * 1023
        w.writeframes(struct.pack("<1024h", *[int(round(v * 32767)) for v in samples]))


class _FakeMapping:
    def __init__(self, path=None, raise_=False):
        self._path, self._raise = path, raise_

    def resolve_by_hash(self, h):
        if self._raise:
            raise IrMappingError(f"unknown IR hash {h}")
        return self._path


def _patch_mapping(monkeypatch, mapping):
    monkeypatch.setattr(ir_mod.IrMapping, "load", classmethod(lambda cls, *a, **k: mapping))


def test_ir_response_success_returns_curve(monkeypatch, tmp_path):
    wav = tmp_path / "cab.wav"
    _write_impulse_wav(wav)
    _patch_mapping(monkeypatch, _FakeMapping(path=wav))
    out = _sess().ir_response(0, 1)
    assert out.get("error") is None
    assert out["freq"] and len(out["freq"]) == len(out["db"])
    assert out["name"] == "cab.wav"
    assert all(abs(d) < 1.0 for d in out["db"])          # impulse -> ~flat


def test_ir_response_unmapped_is_a_friendly_note(monkeypatch):
    _patch_mapping(monkeypatch, _FakeMapping(raise_=True))
    out = _sess().ir_response(0, 1)
    assert out["error"] == "unmapped"
    assert out["hash"] == HASH
    assert "local helixgen IR library" in out["reason"]
    assert "freq" not in out


def test_ir_response_empty_slot(monkeypatch):
    # no irmd on the block -> no hash -> "empty", and the mapping is never consulted
    called = {"n": 0}

    class _M(_FakeMapping):
        def resolve_by_hash(self, h):
            called["n"] += 1
            return None
    _patch_mapping(monkeypatch, _M())
    out = _sess(irmd=None).ir_response(0, 1)
    assert out["error"] == "empty"
    assert called["n"] == 0


def test_ir_response_unreadable_wav(monkeypatch, tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFF\x00\x00\x00\x00WAVEnotachunk")
    _patch_mapping(monkeypatch, _FakeMapping(path=bad))
    out = _sess().ir_response(0, 1)
    assert out["error"] == "unreadable"


def test_ir_response_non_ir_block_raises():
    sess = LiveSession(FakeClient(), lease=FakeLease())   # default rig: amp at slot 2
    sess.load()
    with pytest.raises(LiveError):
        sess.ir_response(0, 2)


def test_ir_response_unknown_block_raises():
    with pytest.raises(LiveError):
        _sess().ir_response(0, 9)


def test_route_ir_response_dispatches(monkeypatch, tmp_path):
    wav = tmp_path / "ir.wav"
    _write_impulse_wav(wav)
    _patch_mapping(monkeypatch, _FakeMapping(path=wav))
    out = server._route(_sess(), "/ir-response", {"path": 0, "block": 1})
    assert out["name"] == "ir.wav" and out["freq"]
