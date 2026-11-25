"""Trails (spillover) set-control: chain `trails_capable`, session.set_trails,
/trails route. The write goes to the fx-harness Trails param (pid 1) via
`/HarnessParamValueSet` — decoded from a Stadium-app frida capture and
HW-confirmed. The device doesn't expose the live state, so it's a SET action.
"""
from __future__ import annotations

import pytest

from helixgen.liveedit import chain, server
from helixgen.liveedit.session import LiveError, LiveSession
from tests.test_liveedit_chain import IN_INST1, OUT_MATRIX, SCREAM, _blk, _eb
from tests.test_liveedit_session import FakeClient, FakeLease

REVERB = 488   # HD2_Reverb63SpringStereo (category reverb)
DELAY = 54     # a delay model on King Gizzard's path (category delay)


def _rig(mid):
    return _eb({0: _blk(IN_INST1), 2: _blk(mid), 13: _blk(OUT_MATRIX)})


def _block(st, cat):
    return next(b for b in (s["block"] for s in st["paths"][0]["segments"]
                            if s["kind"] == "block") if b["category"] == cat)


# --- chain: which blocks expose the control ---------------------------------
def test_reverb_is_trails_capable():
    b = _block(chain.build_chain(_rig(REVERB)), "reverb")
    assert b.get("trails_capable") is True


def test_delay_is_trails_capable():
    b = _block(chain.build_chain(_rig(DELAY)), "delay")
    assert b.get("trails_capable") is True


def test_distortion_is_not_trails_capable():
    b = _block(chain.build_chain(_rig(SCREAM)), "distortion")
    assert "trails_capable" not in b


# --- session.set_trails ------------------------------------------------------
def _sess(mid):
    s = LiveSession(FakeClient(eb=_rig(mid)), lease=FakeLease())
    s.load()
    return s


def test_set_trails_writes_harness_pid_1():
    s = _sess(REVERB)
    s.set_trails(0, 2, True)
    assert (0, 2, 1, True, "b") in s.client.harness_params


def test_set_trails_off_writes_false():
    s = _sess(DELAY)
    s.set_trails(0, 2, False)
    assert (0, 2, 1, False, "b") in s.client.harness_params


def test_set_trails_rejects_a_non_delay_reverb_block():
    s = _sess(SCREAM)                       # distortion
    with pytest.raises(LiveError):
        s.set_trails(0, 2, True)


def test_set_trails_rejects_unknown_block():
    s = _sess(REVERB)
    with pytest.raises(LiveError):
        s.set_trails(0, 9, True)


# --- /trails route -----------------------------------------------------------
def test_route_trails_dispatches():
    s = _sess(REVERB)
    out = server._route(s, "/trails", {"path": 0, "block": 2, "on": True})
    assert "paths" in out
    assert (0, 2, 1, True, "b") in s.client.harness_params


def test_route_trails_requires_bool_on():
    s = _sess(REVERB)
    with pytest.raises(LiveError):
        server._route(s, "/trails", {"path": 0, "block": 2, "on": "yes"})
