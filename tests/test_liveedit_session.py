"""liveedit.session / liveedit.events — live state, device writes and 2001
event handling for `device live` (hgc-58a). No device: a fake client records
what would go on the wire.
"""
import copy

import pytest

from helixgen.device.client import HelixError
from helixgen.device.subscribe import Event
from helixgen.liveedit import events
from helixgen.liveedit.session import LiveError, LiveSession
from tests.test_liveedit_chain import (AMP, IN_INST1, IN_MIC, OUT_MATRIX,
                                       SCREAM, _blk, _eb, _split_join)

DRIVE = 2          # Agoura_AmpBrit2203MV "Drive" pid (see defs)


def _rig():
    return _eb({
        0: _blk(IN_INST1, {2: 1, 3: 0.0, 4: True, 5: -52.0}),
        1: _blk(SCREAM, {1: 0.3, 2: 0.8, 3: 0.84}),
        2: _blk(AMP, {DRIVE: 0.74}),
        13: _blk(OUT_MATRIX, {1: 0.5, 2: -3.0}),
    }, asnp=1)


class FakeClient:
    ip = "10.0.0.99"

    def __init__(self, eb=None):
        self.eb = eb if eb is not None else _rig()
        self.preset = {"cid": 1863, "name": "Venom", "posi": 41, "slot": "11B", "ccid": -2}
        self.reads = 0
        self.params, self.enables, self.saves = [], [], []
        self.ack = True
        self.fail: Exception | None = None
        self.last_request_id = None
        self._rid = 1000

    def _next(self):
        if self.fail is not None:
            raise self.fail
        self.last_request_id = self._rid
        self._rid += 1

    def read_edit_buffer(self):
        if self.fail is not None:
            raise self.fail
        self.reads += 1
        return copy.deepcopy(self.eb)

    def active_preset(self):
        if self.fail is not None:
            raise self.fail
        return dict(self.preset)

    def set_param(self, path, block, pid, value, *, force=False, fast=False):
        self._next()
        self.params.append((path, block, pid, value, force, fast))
        return self.ack

    def set_block_enable(self, path, block, enable, *, fast=False):
        self._next()
        self.enables.append((path, block, enable, fast))
        return self.ack

    def save_edit_buffer(self, cid):
        self._next()
        self.saves.append(cid)
        return self.ack


class FakeLease:
    def __init__(self):
        self.busy = None
        self.ensured, self.transients, self.ticks = 0, [], 0

    def ensure(self):
        if self.busy:
            raise LiveError(self.busy, status=409)
        self.ensured += 1

    def transient(self, scopes):
        if self.busy:
            raise LiveError(self.busy, status=409)
        self.transients.append(tuple(scopes))
        import contextlib
        return contextlib.nullcontext()

    def tick(self):
        self.ticks += 1


@pytest.fixture
def s():
    sess = LiveSession(FakeClient(), lease=FakeLease())
    sess.load()
    return sess


def _ev(addr, *args):
    return Event(2001, addr, list(args))


def _value(sess, path, block, pid):
    return sess.param(path, block, pid)["value"]


def _drain(sess, version, rev):
    return sess.changes_since(version, rev)


# --- events.decode_event -----------------------------------------------------

def test_decode_param_and_bypass_carry_seq():
    # layouts captured on a Stadium XL (fw 1.4): [origin, seq, path, block, snap, pid, value]
    assert events.decode_event("/setParamValue", [78129, 1005, 0, 2, 0, 2, 0.55]) == \
        ("param", 1005, 0, 2, 2, 0.55)
    # [origin, seq, path, block, enable]
    assert events.decode_event("/setBlockEnable", [12593, 1004, 0, 2, 1]) == \
        ("bypass", 1004, 0, 2, True)


def test_decode_structural_events():
    assert events.decode_event("/activateSnapshot", [257, 945, 1]) == ("snapshot", 1)
    assert events.decode_event("/setEditBuffer", [1, 2, b"blob"]) == ("editbuffer", b"blob")
    assert events.decode_event("/loadContentRef", [1, 2, {"cid_": 7, "name": "X"}]) == \
        ("presetref", 7, "X")
    # things that change the buffer in ways we do not model -> re-read
    for addr in ("/setModelWithMID", "/setSnapshotBypassState", "/setBatchedParamVals"):
        assert events.decode_event(addr, [1, 2, 3]) == ("reread", addr)


def test_decode_dirty_flag_property():
    on = {"key_": "volatile.preset.edited", "type": "i", "val_": 1}
    assert events.decode_event("/setPropertyValue", [1, 2, on]) == ("dirty", True)
    off = dict(on, val_=0)
    assert events.decode_event("/setPropertyValue", [1, 2, off]) == ("dirty", False)
    other = {"key_": "preset.expsw.active", "type": "i", "val_": 1}
    assert events.decode_event("/setPropertyValue", [1, 2, other]) is None


def test_decode_ignores_noise_and_malformed():
    assert events.decode_event("/heartbeat", []) is None
    assert events.decode_event("/dspEvent", [b"x"]) is None
    assert events.decode_event("/setParamValue", [1, 2, 3]) is None
    assert events.decode_event("/setParamValue", [1, 2, "a", 0, 0, 1, 0.5]) is None
    assert events.decode_event("/setBlockEnable", [1, 2, 0]) is None


# --- load / state ------------------------------------------------------------

def test_load_is_exactly_one_edit_buffer_read(s):
    assert s.client.reads == 1
    st = s.public_state()
    assert st["preset"] == {"cid": 1863, "name": "Venom"}
    assert st["snapshot"] == 1 and st["connected"] is True and st["dirty"] is False
    assert [seg["block"]["block"] for seg in st["paths"][0]["segments"]] == [0, 1, 2, 13]


def test_load_failure_is_a_live_error_and_marks_disconnected():
    c = FakeClient()
    c.fail = HelixError("no route")
    sess = LiveSession(c, lease=FakeLease())
    with pytest.raises(LiveError) as e:
        sess.load()
    assert e.value.status == 502
    assert sess.public_state()["connected"] is False


# --- param writes ------------------------------------------------------------

def test_set_param_writes_fast_forced_and_clamped(s):
    out = s.set_param(0, 2, DRIVE, 5.0)                 # way out of range
    assert out == {"value": 1.0}
    assert s.client.params == [(0, 2, DRIVE, 1.0, True, True)]
    assert _value(s, 0, 2, DRIVE) == 1.0
    assert s.lease.ensured == 1


def test_set_param_emits_a_delta_not_a_restructure(s):
    v, r = s.version, s.rev
    s.set_param(0, 1, 1, 0.5)
    _, rev, msgs = _drain(s, v, r)
    assert rev == r
    assert {"t": "param", "path": 0, "block": 1, "pid": 1, "value": 0.5} in msgs
    assert {"t": "meta", "dirty": True} in [
        {k: m[k] for k in ("t", "dirty")} for m in msgs if m["t"] == "meta"]


def test_set_param_unknown_control_is_404_and_writes_nothing(s):
    with pytest.raises(LiveError) as e:
        s.set_param(0, 9, 1, 0.5)
    assert e.value.status == 404 and s.client.params == []


def test_set_param_rejects_non_numbers(s):
    for bad in ("x", None, float("nan"), float("inf")):
        with pytest.raises(LiveError) as e:
            s.set_param(0, 1, 1, bad)
        assert e.value.status == 400
    assert s.client.params == []


def test_unacknowledged_write_fails_loudly_and_keeps_the_old_value(s):
    s.client.ack = False
    with pytest.raises(LiveError) as e:
        s.set_param(0, 1, 1, 0.9)
    assert e.value.status == 502
    assert e.value.value == pytest.approx(0.3)           # what the page reverts to
    assert _value(s, 0, 1, 1) == pytest.approx(0.3)


def test_device_error_on_write_marks_disconnected(s):
    s.client.fail = HelixError("connection lost")
    with pytest.raises(LiveError) as e:
        s.set_param(0, 1, 1, 0.9)
    assert e.value.status == 502
    assert s.public_state()["connected"] is False


def test_write_is_refused_while_another_process_holds_the_device(s):
    s.lease.busy = "device 10.0.0.99 scope 'editbuffer' is locked by agent-x"
    with pytest.raises(LiveError) as e:
        s.set_param(0, 1, 1, 0.9)
    assert e.value.status == 409 and "agent-x" in str(e.value)
    assert s.client.params == []


def test_busy_refusal_tells_the_page_what_to_show(s):
    s.lease.busy = "locked by agent-x"
    with pytest.raises(LiveError) as e:
        s.set_param(0, 1, 1, 0.9)
    assert e.value.value == pytest.approx(0.3)
    with pytest.raises(LiveError) as e:
        s.set_bypass(0, 1, False)
    assert e.value.status == 409 and e.value.value is True


def test_page_writes_carry_the_writers_id_so_it_can_skip_its_own(s):
    v, r = s.version, s.rev
    s.set_param(0, 1, 1, 0.5, client="tab-a")
    s.set_bypass(0, 1, False, client="tab-a")
    msgs = _drain(s, v, r)[2]
    assert {"t": "param", "path": 0, "block": 1, "pid": 1, "value": 0.5,
            "client": "tab-a"} in msgs
    assert {"t": "bypass", "path": 0, "block": 1, "enabled": False,
            "client": "tab-a"} in msgs


# --- bypass ------------------------------------------------------------------

def test_set_bypass_writes_fast_and_updates_state(s):
    v, r = s.version, s.rev
    assert s.set_bypass(0, 1, False) == {"enabled": False}
    assert s.client.enables == [(0, 1, False, True)]
    assert s.block(0, 1)["enabled"] is False
    assert {"t": "bypass", "path": 0, "block": 1, "enabled": False} in _drain(s, v, r)[2]


def test_set_bypass_unknown_block_and_no_ack(s):
    with pytest.raises(LiveError) as e:
        s.set_bypass(0, 9, False)
    assert e.value.status == 404
    s.client.ack = False
    with pytest.raises(LiveError) as e:
        s.set_bypass(0, 1, False)
    assert e.value.status == 502 and e.value.value is True
    assert s.block(0, 1)["enabled"] is True


# --- save --------------------------------------------------------------------

def test_save_persists_the_active_user_preset_in_place(s):
    s.set_param(0, 1, 1, 0.5)
    v, r = s.version, s.rev
    assert s.save() == {"saved": "Venom", "cid": 1863}
    assert s.client.saves == [1863]
    assert s.lease.transients == [("editbuffer", "library")]
    assert s.public_state()["dirty"] is False
    assert any(m["t"] == "meta" and m["dirty"] is False for m in _drain(s, v, r)[2])


def test_save_refuses_anything_but_a_user_pool_preset(s):
    s.client.preset["ccid"] = -1                          # e.g. a factory preset
    with pytest.raises(LiveError) as e:
        s.save()
    assert e.value.status == 409 and s.client.saves == []
    s.client.preset = {"cid": 5, "name": None, "posi": None, "slot": "", "ccid": None}
    with pytest.raises(LiveError):
        s.save()
    assert s.client.saves == []


def test_save_reports_a_device_refusal(s):
    s.client.ack = False
    with pytest.raises(LiveError) as e:
        s.save()
    assert e.value.status == 502


# --- 2001 events -------------------------------------------------------------

def test_param_event_from_the_device_updates_one_control(s):
    v, r = s.version, s.rev
    s.handle_events([_ev("/setParamValue", 3341, 77, 0, 1, 1, 1, 0.61)])
    assert _value(s, 0, 1, 1) == pytest.approx(0.61)
    _, rev, msgs = _drain(s, v, r)
    assert rev == r                                        # no restructure
    assert msgs == [{"t": "param", "path": 0, "block": 1, "pid": 1,
                     "value": pytest.approx(0.61)}]
    assert s.client.params == []                           # never re-written


def test_bool_param_event_is_normalised(s):
    s.handle_events([_ev("/setParamValue", 1, 77, 0, 0, 1, 4, False)])
    assert _value(s, 0, 0, 4) == 0.0


def test_own_echo_is_dropped_even_when_it_arrives_stale(s):
    # two quick writes; the echo of the FIRST arrives after the second landed.
    s.set_param(0, 1, 1, 0.40)                             # rid 1000
    s.set_param(0, 1, 1, 0.50)                             # rid 1001
    v, r = s.version, s.rev
    s.handle_events([_ev("/setParamValue", 9, 1000, 0, 1, 1, 1, 0.40)])
    assert _value(s, 0, 1, 1) == 0.50                      # not dragged back
    assert _drain(s, v, r)[2] == []


def test_foreign_event_reusing_one_of_our_request_ids_still_applies(s):
    s.set_param(0, 1, 1, 0.40)                             # rid 1000
    # same seq as our write, but a DIFFERENT control (pid 2): not our echo
    s.handle_events([_ev("/setParamValue", 9, 1000, 0, 1, 0, 2, 0.9)])
    assert _value(s, 0, 1, 2) == pytest.approx(0.9)


def test_event_for_an_unknown_control_is_ignored(s):
    v, r = s.version, s.rev
    s.handle_events([_ev("/setParamValue", 1, 77, 0, 9, 1, 1, 0.5)])
    assert _drain(s, v, r)[2] == []


def test_bypass_event_updates_the_block(s):
    v, r = s.version, s.rev
    s.handle_events([_ev("/setBlockEnable", 1, 77, 0, 1, 0)])
    assert s.block(0, 1)["enabled"] is False
    assert _drain(s, v, r)[2] == [{"t": "bypass", "path": 0, "block": 1, "enabled": False}]


def test_dirty_property_event(s):
    on = {"key_": "volatile.preset.edited", "type": "i", "val_": 1}
    s.handle_events([_ev("/setPropertyValue", 1, 2, on)])
    assert s.public_state()["dirty"] is True


def test_snapshot_switch_rereads_once_per_batch(s):
    s.client.eb = _rig()
    s.client.eb["cg__"]["asnp"] = 3
    r = s.rev
    s.handle_events([_ev("/activateSnapshot", 1, 2, 3),
                     _ev("/setBatchedParamVals", 1, 3, b"x"),
                     _ev("/setSnapshotBypassState", 1, 4, 2, 0)])
    assert s.client.reads == 2                             # load + ONE re-read
    assert s.public_state()["snapshot"] == 3
    assert s.rev == r + 1                                  # page re-renders


def test_pushed_edit_buffer_rebuilds_without_a_device_read(s, monkeypatch):
    new = _split_join()
    monkeypatch.setattr("helixgen.liveedit.session.decode_blob", lambda blob: new)
    r = s.rev
    s.handle_events([_ev("/setEditBuffer", 1, 2, b"\x00blob"),
                     _ev("/loadContentRef", 1, 3, {"cid_": 2037, "name": "White Limo"})])
    assert s.client.reads == 1                             # no extra read
    st = s.public_state()
    assert st["preset"] == {"cid": 2037, "name": "White Limo"}
    assert any(seg["kind"] == "parallel" for seg in st["paths"][0]["segments"])
    assert s.rev > r
    assert s.public_state()["dirty"] is False


def test_lone_preset_ref_for_a_different_preset_rereads(s):
    s.handle_events([_ev("/loadContentRef", 1, 3, {"cid_": 2037, "name": "White Limo"})])
    assert s.client.reads == 2
    # ...but a ref for the SAME preset (a rename) only patches the label
    s.client.preset["cid"] = 2037
    s.load()
    reads = s.client.reads
    s.handle_events([_ev("/loadContentRef", 1, 4, {"cid_": 2037, "name": "Limo v2"})])
    assert s.client.reads == reads
    assert s.public_state()["preset"]["name"] == "Limo v2"


def test_undecodable_blob_falls_back_to_a_reread(s, monkeypatch):
    def boom(blob):
        raise ValueError("bad blob")
    monkeypatch.setattr("helixgen.liveedit.session.decode_blob", boom)
    s.handle_events([_ev("/setEditBuffer", 1, 2, b"junk")])
    assert s.client.reads == 2


def test_a_failing_reread_marks_disconnected_instead_of_raising(s):
    s.client.fail = HelixError("gone")
    s.handle_events([_ev("/activateSnapshot", 1, 2, 3)])   # must not raise
    assert s.public_state()["connected"] is False


# --- change log --------------------------------------------------------------

def test_changes_since_sends_full_state_on_restructure(s):
    v, r = s.version, s.rev
    s.load()
    _, rev, msgs = _drain(s, v, r)
    assert rev == s.rev and len(msgs) == 1 and msgs[0]["t"] == "state"
    assert msgs[0]["paths"]


def test_changes_since_falls_back_to_full_state_when_the_log_overflowed(s):
    v, r = s.version, s.rev
    for i in range(LiveSession.LOG_MAX + 10):
        s.handle_events([_ev("/setParamValue", 1, 77, 0, 1, 1, 1, i / 1000.0)])
    _, _, msgs = _drain(s, v, r)
    assert len(msgs) == 1 and msgs[0]["t"] == "state"


def test_changes_since_nothing_new(s):
    assert _drain(s, s.version, s.rev)[2] == []


# --- liveness / reconnect ----------------------------------------------------

def test_silence_marks_disconnected_and_traffic_reconnects():
    now = [100.0]
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease(), liveness=True, clock=lambda: now[0])
    sess.load()
    sess.handle_events([Event(2003, "/heartbeat", [])])
    now[0] += 3
    sess.tick()
    assert sess.public_state()["connected"] is True
    now[0] += 10                                            # the stream went quiet
    sess.tick()
    assert sess.public_state()["connected"] is False
    reads = c.reads
    sess.handle_events([Event(2003, "/heartbeat", [])])     # it is back
    sess.tick()
    assert sess.public_state()["connected"] is True
    assert c.reads == reads + 1                             # fresh read on reconnect


def test_without_liveness_silence_is_not_a_disconnect(s):
    s.tick()
    assert s.public_state()["connected"] is True
    assert s.lease.ticks == 1


def test_disconnected_writes_are_refused_fast():
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    c.fail = HelixError("gone")
    with pytest.raises(LiveError):
        sess.set_param(0, 1, 1, 0.9)
    c.fail = None
    calls = len(c.params)
    with pytest.raises(LiveError) as e:
        sess.set_param(0, 1, 1, 0.9)                        # still marked down
    assert e.value.status == 503 and len(c.params) == calls
    sess.reread()                                           # explicit recovery
    assert sess.set_param(0, 1, 1, 0.9) == {"value": 0.9}


def test_second_path_block_is_addressable(s):
    # a second DSP path (the mic lane): its blocks are addressed by path 1
    s.client.eb = _eb({0: _blk(IN_MIC), 13: _blk(OUT_MATRIX)},
                      {0: _blk(IN_INST1, {3: 0.0}), 13: _blk(OUT_MATRIX)})
    s.reread()
    assert s.set_param(1, 0, 3, -6.0) == {"value": -6.0}      # input Trim, dB
    assert s.client.params[-1][:4] == (1, 0, 3, -6.0)
