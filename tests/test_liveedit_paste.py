"""Block copy/paste (session.paste_block + /paste-block).

Paste APPENDS a duplicate (the engine has no insert-and-shift) and reproduces
the source block's params, bypass and dual-cab 2nd model. The FakeClient here
simulates the device: add_block places a model at the next free slot, and the
subsequent param/bypass/second-cab writes are recorded.
"""
from __future__ import annotations

import pytest

from helixgen.liveedit import chain as _chain
from helixgen.liveedit import server
from helixgen.liveedit.session import LiveError, LiveSession
from tests.test_liveedit_chain import AMP, IN_INST1, OUT_MATRIX, SCREAM, _blk, _eb
from tests.test_liveedit_session import DRIVE, FakeClient, FakeLease


def _sess():
    sess = LiveSession(FakeClient(), lease=FakeLease())   # rig: IN@0, SCREAM@1, AMP@2, OUT@13
    sess.load()
    return sess


def _row0(sess, path=0):
    with sess.cond:
        return sorted(b for (p, b) in sess._blocks if p == path and b < _chain.ROW1_INPUT)


def test_paste_appends_a_block_at_the_next_free_slot():
    sess = _sess()
    before = _row0(sess)                       # [0,1,2,13]
    spec = {"model_id": SCREAM, "enabled": True,
            "params": [{"pid": 1, "value": 0.42}]}
    sess.paste_block(0, spec)
    after = _row0(sess)
    new = set(after) - set(before)
    assert len(new) == 1 and new.pop() == 3    # appended right after AMP@2


def test_paste_reproduces_params_on_the_new_slot():
    sess = _sess()
    c = sess.client
    spec = {"model_id": SCREAM, "enabled": True,
            "params": [{"pid": 1, "value": 0.42}, {"pid": 2, "value": 0.8}]}
    sess.paste_block(0, spec)
    # the two source params were written to the appended slot (3)
    wrote = [(p, b, pid, v) for (p, b, pid, v, _f, _fa) in c.params if b == 3]
    assert any(pid == 1 and abs(v - 0.42) < 1e-6 for (_, _, pid, v) in wrote)
    assert any(pid == 2 and abs(v - 0.8) < 1e-6 for (_, _, pid, v) in wrote)


def test_paste_bypassed_source_sets_bypass():
    sess = _sess()
    c = sess.client
    sess.paste_block(0, {"model_id": SCREAM, "enabled": False, "params": []})
    assert any(b == 3 and en is False for (p, b, en, fast) in c.enables)


def test_paste_enabled_source_sets_no_bypass():
    sess = _sess()
    c = sess.client
    sess.paste_block(0, {"model_id": SCREAM, "enabled": True, "params": []})
    assert not any(b == 3 for (p, b, en, fast) in c.enables)


def test_paste_reproduces_dual_cab_second_model():
    sess = _sess()
    c = sess.client
    CAB, CAB2 = 425, 426          # HD2_CabMicIr_* — category cab_ir_interp (dual-cab capable)
    spec = {"model_id": CAB, "enabled": True, "params": [],
            "second_model": {"model_id": CAB2, "params": []}}
    sess.paste_block(0, spec)
    assert any(b == 3 and mid == CAB2 for (p, b, mid) in c.second_cabs)


def test_paste_skips_a_pid_the_model_lacks():
    sess = _sess()
    # pid 9999 doesn't exist on SCREAM -> skipped, paste still succeeds
    sess.paste_block(0, {"model_id": SCREAM, "enabled": True,
                         "params": [{"pid": 9999, "value": 0.5}, {"pid": 1, "value": 0.2}]})
    assert 3 in _row0(sess)                    # block still landed


def test_paste_surfaces_a_device_failure_during_replay(monkeypatch):
    # a mid-paste device error (502) must NOT be swallowed like a pid-skip —
    # otherwise a disconnect silently drops params yet reports success.
    sess = _sess()

    def boom(path, block, pid, value, **k):
        raise LiveError("device write failed", status=502)
    monkeypatch.setattr(sess, "set_param", boom)
    with pytest.raises(LiveError) as e:
        sess.paste_block(0, {"model_id": SCREAM, "enabled": True,
                             "params": [{"pid": 1, "value": 0.2}]})
    assert e.value.status == 502


def test_paste_skips_unknown_pid_but_not_device_error(monkeypatch):
    # a 404 (pid the model lacks) is skipped; the paste still completes
    sess = _sess()

    def notfound(path, block, pid, value, **k):
        raise LiveError("no such control", status=404)
    monkeypatch.setattr(sess, "set_param", notfound)
    sess.paste_block(0, {"model_id": SCREAM, "enabled": True,
                         "params": [{"pid": 1, "value": 0.2}]})   # must not raise
    assert 3 in _row0(sess)


def test_paste_rejects_non_addable_model():
    sess = _sess()
    with pytest.raises(LiveError):
        sess.paste_block(0, {"model_id": OUT_MATRIX, "params": []})   # an output node


def test_paste_rejects_bad_spec():
    sess = _sess()
    with pytest.raises(LiveError):
        sess.paste_block(0, {"params": []})    # no model_id
    with pytest.raises(LiveError):
        sess.paste_block(0, "not a dict")


def test_route_paste_block_dispatches():
    sess = _sess()
    out = server._route(sess, "/paste-block",
                        {"path": 0, "spec": {"model_id": SCREAM, "params": []}})
    assert "paths" in out                      # public_state
    assert 3 in _row0(sess)


def test_route_paste_block_requires_object_spec():
    sess = _sess()
    with pytest.raises(LiveError):
        server._route(sess, "/paste-block", {"path": 0, "spec": "x"})
