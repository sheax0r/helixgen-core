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
        self.ptypes = []
        self.edited = 0
        self.property_fails = False
        self.fast_reads = []
        self.preset_reads = 0
        self.firmware = "1.4.1"
        self.pool_of = {}        # setlist reference cid -> pool preset cid
        self.on_read = None      # hooks: run in the middle of a device call
        self.on_write = None
        self.snapshots_activated = []
        self.loaded = []
        # what the preset picker lists: the user pool + one setlist of refs
        self.pool_presets = [{"cid_": 1863, "name": "Venom", "cctp": 1000, "posi": 41}]
        self.setlists = [{"cid_": 100, "name": "Rock", "cctp": 1001, "posi": 0}]
        self.setlist_contents = {100: [{"cid_": 1950, "name": "Venom Live",
                                        "cctp": 1003, "posi": 0}]}

    def _next(self):
        if self.fail is not None:
            raise self.fail
        self.last_request_id = self._rid
        self._rid += 1

    def read_edit_buffer(self, *, fast=False):
        if self.fail is not None:
            raise self.fail
        self.reads += 1
        self.fast_reads.append(fast)
        out = copy.deepcopy(self.eb)
        if self.on_read is not None:
            hook, self.on_read = self.on_read, None
            hook()
        return out

    def active_preset(self):
        if self.fail is not None:
            raise self.fail
        self.preset_reads += 1
        return dict(self.preset)

    def product_info(self):
        return {"firmware": self.firmware}

    def resolve_pool_cid(self, cid, ccid=None):
        if self.fail is not None:
            raise self.fail
        if ccid == -2:
            return cid
        return self.pool_of.get(cid)

    def set_param(self, path, block, pid, value, *, force=False, fast=False,
                  ptype=None):
        self._next()
        self.params.append((path, block, pid, value, force, fast))
        self.ptypes.append(ptype)
        if self.on_write is not None:
            hook, self.on_write = self.on_write, None
            hook()
        return self.ack

    def get_property(self, key):
        if self.property_fails or self.fail is not None:
            raise HelixError("property read failed")
        assert key == "volatile.preset.edited"
        return type("PV", (), {"key": key, "type": "i", "value": self.edited})()

    def set_block_enable(self, path, block, enable, *, fast=False):
        self._next()
        self.enables.append((path, block, enable, fast))
        return self.ack

    def save_edit_buffer(self, cid):
        self._next()
        self.saves.append(cid)
        return self.ack

    def activate_snapshot(self, index):
        self._next()
        self.snapshots_activated.append(int(index))
        return True

    def load_preset(self, cid):
        self._next()
        self.loaded.append(int(cid))
        return self.ack

    def list_presets(self, container=-2, *, strict=False):
        if self.fail is not None:
            raise self.fail
        return [dict(m) for m in self.pool_presets]

    def list_setlists(self, *, strict=False):
        if self.fail is not None:
            raise self.fail
        return [dict(m) for m in self.setlists]

    def list_container(self, cid, *, strict=False):
        if self.fail is not None:
            raise self.fail
        return [dict(m) for m in self.setlist_contents.get(cid, [])]


class FakeLease:
    def __init__(self):
        self.busy = None
        self.ensured, self.transients, self.ticks, self.dones = 0, [], 0, 0

    def ensure(self):
        if self.busy:
            raise LiveError(self.busy, status=409)
        self.ensured += 1

    def done(self):
        self.dones += 1

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


def test_decode_dirty_flag_property_from_the_wire_blob():
    # on the wire the property is a `lavppgsm` value blob, not a dict
    from helixgen.device import settings
    blob = settings.encode_value_blob("volatile.preset.edited", "i", 1)
    assert events.decode_event("/setPropertyValue", [1, 2, blob]) == ("dirty", True)
    blob = settings.encode_value_blob("volatile.preset.edited", "i", 0)
    assert events.decode_event("/setPropertyValue", [1, 2, blob]) == ("dirty", False)
    other = settings.encode_value_blob("preset.expsw.active", "i", 1)
    assert events.decode_event("/setPropertyValue", [1, 2, other]) is None
    assert events.decode_event("/setPropertyValue", [1, 2, b"garbage"]) is None


def test_decode_bool_param_event():
    # bool params arrive as real bools (OSC T/F tags)
    assert events.decode_event("/setParamValue", [1, 1004, 0, 0, 0, 4, False]) == \
        ("param", 1004, 0, 0, 4, 0.0)
    assert events.decode_event("/setParamValue", [1, 1004, 0, 0, 0, 4, None]) is None


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


def test_unsaved_flag_comes_from_the_device_property_not_the_buffer():
    c = FakeClient()
    c.eb["hist"] = 1                     # history present, but nothing unsaved
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    assert sess.public_state()["dirty"] is False
    c.edited = 1
    sess.reread()
    assert sess.public_state()["dirty"] is True


def test_unreadable_unsaved_flag_does_not_block_the_load():
    c = FakeClient()
    c.property_fails = True
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    assert sess.public_state()["dirty"] is False and sess.public_state()["paths"]


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


def test_each_param_is_written_in_its_own_type(s):
    # the device drops an int or bool sent as a float (HW 2026-11-13)
    s.set_param(0, 0, 3, -6.0)           # input Trim   : float
    s.set_param(0, 0, 2, 2)              # input Pad    : int / list
    s.set_param(0, 0, 4, 0)              # input gate   : bool
    assert s.client.ptypes == ["f", "i", "b"]
    assert [p[3] for p in s.client.params] == [-6.0, 2.0, 0.0]


def test_a_write_from_a_page_showing_an_older_structure_is_refused(s):
    # the page's control may now sit over a DIFFERENT param (preset or model
    # changed between its render and this write) — never write it blind.
    old = s.rev
    s.reread()
    with pytest.raises(LiveError) as e:
        s.set_param(0, 1, 1, 0.9, rev=old)
    assert e.value.status == 409 and e.value.value == pytest.approx(0.3)
    with pytest.raises(LiveError) as e:
        s.set_bypass(0, 1, False, rev=old)
    assert e.value.status == 409 and e.value.value is True
    assert s.client.params == [] and s.client.enables == []
    assert s.set_param(0, 1, 1, 0.9, rev=s.rev) == {"value": 0.9}
    assert s.set_bypass(0, 1, False, rev=s.rev) == {"enabled": False}


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


def test_snapshot_and_preset_are_refused_while_another_process_holds_the_device(s):
    s.lease.busy = "locked by agent-x"
    for call in (lambda: s.activate_snapshot(2), lambda: s.load_preset(1950)):
        with pytest.raises(LiveError) as e:
            call()
        assert e.value.status == 409 and "agent-x" in str(e.value)
    assert s.client.snapshots_activated == [] and s.client.loaded == []


def test_preset_load_device_failure_marks_disconnected(s):
    s.client.fail = HelixError("connection lost")
    with pytest.raises(LiveError) as e:
        s.load_preset(1950)
    assert e.value.status == 502
    assert s.public_state()["connected"] is False


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


def test_save_of_a_tone_loaded_from_a_setlist_goes_to_its_pool_preset(s):
    # HW 2026-11-13: the active id of a setlist-loaded tone is the setlist
    # REFERENCE; saving into the pool preset it points at is what persists
    # (and what the hardware's own Save does). Refusing it would make Save
    # useless for anything loaded from a setlist.
    s.client.preset = {"cid": 1950, "name": "Dream On", "posi": 0, "slot": "1A", "ccid": 1949}
    s.client.pool_of = {1950: 1750}
    assert s.save() == {"saved": "Dream On", "cid": 1750}
    assert s.client.saves == [1750]


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


def test_batched_values_trailing_a_snapshot_reread_do_not_cost_a_second_read():
    # HW 2026-11-13: /activateSnapshot and its /setBatchedParamVals arrive in
    # DIFFERENT poll batches when the first re-read (~1.5 s) is slow — the
    # values were already in that read, so the straggler must not trigger
    # another one (and another full re-render of the page).
    now = [100.0]
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease(), clock=lambda: now[0])
    sess.load()
    sess.handle_events([_ev("/activateSnapshot", 1, 2, 3)])
    assert c.reads == 2
    now[0] += 0.05                                   # next poll, straight after
    sess.handle_events([_ev("/setBatchedParamVals", 1, 3, b"x")])
    assert c.reads == 2
    now[0] += 5.0                                    # an unrelated batch later on
    sess.handle_events([_ev("/setBatchedParamVals", 1, 9, b"x")])
    assert c.reads == 3


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
    assert s.public_state()["dirty"] is False      # a freshly loaded preset


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


# --- findings from the adversarial review (each reproduced before the fix) ----

def test_load_reads_the_buffer_without_the_settle_drain(s):
    assert s.client.fast_reads == [True]


def test_a_snapshot_reread_is_the_buffer_only(s):
    # same preset: its identity and unsaved flag did not change, so the
    # switch costs ONE round-trip, not four
    presets = s.client.preset_reads
    s.handle_events([_ev("/activateSnapshot", 1, 2, 3)])
    assert s.client.reads == 2 and s.client.preset_reads == presets
    assert s.public_state()["preset"] == {"cid": 1863, "name": "Venom"}


def test_unsaved_flag_survives_a_snapshot_reread(s):
    s.set_param(0, 1, 1, 0.5)
    s.handle_events([_ev("/activateSnapshot", 1, 2, 3)])
    assert s.public_state()["dirty"] is True


def test_values_that_preceded_a_reread_in_the_same_batch_are_not_replayed(s):
    # [knob moved, block bypassed, THEN snapshot switched] in one poll batch:
    # the re-read returns the new snapshot; the two older events must not be
    # laid back on top of it.
    s.handle_events([_ev("/setParamValue", 1, 77, 0, 1, 0, 1, 0.95),
                     _ev("/setBlockEnable", 1, 78, 0, 1, 0),
                     _ev("/activateSnapshot", 1, 79, 2)])
    assert _value(s, 0, 1, 1) == pytest.approx(0.3)       # what the device holds
    assert s.block(0, 1)["enabled"] is True


def test_own_echo_after_a_foreign_change_is_what_the_device_holds(s):
    # stream order = device order: [hardware 0.70, then OUR 0.40]. Our ack was
    # processed first, so state went 0.40 -> 0.70; the echo must bring it back.
    s.set_param(0, 1, 1, 0.40)                             # rid 1000
    v, r = s.version, s.rev
    s.handle_events([_ev("/setParamValue", 9, 77, 0, 1, 0, 1, 0.70),
                     _ev("/setParamValue", 9, 1000, 0, 1, 0, 1, 0.40)])
    assert _value(s, 0, 1, 1) == pytest.approx(0.40)
    values = [m["value"] for m in _drain(s, v, r)[2] if m["t"] == "param"]
    assert values == [pytest.approx(0.70), pytest.approx(0.40)]


def test_a_remembered_request_id_is_consumed_by_its_echo(s):
    # request ids are small counters every helixgen process starts at 1000
    s.set_param(0, 1, 1, 0.40)                             # rid 1000
    s.handle_events([_ev("/setParamValue", 9, 1000, 0, 1, 0, 1, 0.40)])   # its echo
    s.set_param(0, 1, 1, 0.50)                             # rid 1001
    s.handle_events([_ev("/setParamValue", 9, 1001, 0, 1, 0, 1, 0.50)])
    # another process now writes 0.40 and happens to use seq 1000 too
    s.handle_events([_ev("/setParamValue", 9, 1000, 0, 1, 0, 1, 0.40)])
    assert _value(s, 0, 1, 1) == pytest.approx(0.40)


def test_remembered_echoes_do_not_survive_a_new_buffer(s):
    s.set_param(0, 1, 1, 0.40)                             # rid 1000
    s.set_param(0, 1, 1, 0.50)                             # rid 1001 (1000 is now "stale")
    s.reread()                                             # device (fake) still says 0.3
    s.handle_events([_ev("/setParamValue", 9, 1000, 0, 1, 0, 1, 0.40)])
    assert _value(s, 0, 1, 1) == pytest.approx(0.40)       # not swallowed as an old echo


def test_preset_switch_while_a_write_is_on_the_wire(s, monkeypatch):
    # slot 13 is the output (pid 2: level, -120..+20 dB). While our +12 dB
    # write is in flight the device loads a preset whose slot 13 pid 2 is a
    # 0..1 knob. 12.0 must NOT be written into the new tone's state.
    other = _eb({0: _blk(IN_INST1), 13: _blk(SCREAM, {1: 0.3, 2: 0.8})})
    monkeypatch.setattr("helixgen.liveedit.session.decode_blob", lambda blob: other)
    s.client.on_write = lambda: s.handle_events(
        [_ev("/setEditBuffer", 1, 2, b"blob"),
         _ev("/loadContentRef", 1, 3, {"cid_": 7, "name": "Other"})])
    with pytest.raises(LiveError) as e:
        s.set_param(0, 13, 2, 12.0)
    assert e.value.status == 409
    assert e.value.value == pytest.approx(0.8)             # the NEW tone's value
    assert _value(s, 0, 13, 2) == pytest.approx(0.8)
    # ...and the truth is fetched: the stray write may have landed on the new tone
    reads = s.client.reads
    s.tick()
    assert s.client.reads == reads + 1
    s.tick()
    assert s.client.reads == reads + 1                     # once


def test_bypass_commit_is_also_guarded_against_a_switch(s, monkeypatch):
    class Enabler:
        pass
    other = _eb({0: _blk(IN_INST1), 1: _blk(AMP), 13: _blk(OUT_MATRIX)})
    monkeypatch.setattr("helixgen.liveedit.session.decode_blob", lambda blob: other)
    real = s.client.set_block_enable

    def racing(path, block, enable, *, fast=False):
        out = real(path, block, enable, fast=fast)
        s.handle_events([_ev("/setEditBuffer", 1, 2, b"blob")])
        return out
    s.client.set_block_enable = racing
    with pytest.raises(LiveError) as e:
        s.set_bypass(0, 1, False)
    assert e.value.status == 409 and s.block(0, 1)["enabled"] is True


def test_a_read_overtaken_by_a_pushed_buffer_is_discarded(s, monkeypatch):
    # the page's Re-read is in flight (it read preset A) when the device
    # pushes preset B; installing the read afterwards would put A back.
    b_eb = _split_join()
    monkeypatch.setattr("helixgen.liveedit.session.decode_blob", lambda blob: b_eb)

    def pushed_meanwhile():
        s.client.eb = b_eb                                 # the device now holds B
        s.client.preset = {"cid": 2037, "name": "B", "posi": 1, "slot": "1A", "ccid": -2}
        s.handle_events([_ev("/setEditBuffer", 1, 2, b"blob"),
                         _ev("/loadContentRef", 1, 3, {"cid_": 2037, "name": "B"})])
    s.client.on_read = pushed_meanwhile
    s.reread()
    st = s.public_state()
    assert st["preset"]["name"] == "B"
    assert any(seg["kind"] == "parallel" for seg in st["paths"][0]["segments"])


def test_blob_then_its_ref_in_a_later_batch_only_patches_the_label(s, monkeypatch):
    monkeypatch.setattr("helixgen.liveedit.session.decode_blob", lambda blob: _split_join())
    s.handle_events([_ev("/setEditBuffer", 1, 2, b"blob")])
    assert s.public_state()["preset"] == {"cid": None, "name": None}
    reads = s.client.reads
    s.handle_events([_ev("/loadContentRef", 1, 3, {"cid_": 2037, "name": "White Limo"})])
    assert s.client.reads == reads                         # NOT "a different preset"
    assert s.public_state()["preset"] == {"cid": 2037, "name": "White Limo"}


def test_value_events_before_a_pushed_buffer_belong_to_the_old_tone(s, monkeypatch):
    monkeypatch.setattr("helixgen.liveedit.session.decode_blob", lambda blob: _rig())
    s.handle_events([_ev("/setParamValue", 1, 77, 0, 1, 0, 1, 0.95),
                     _ev("/setEditBuffer", 1, 2, b"blob")])
    assert _value(s, 0, 1, 1) == pytest.approx(0.3)


def test_reconnect_attempts_are_spaced_out():
    now = [100.0]
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease(), clock=lambda: now[0])
    sess.load()
    c.fail = HelixError("gone")
    with pytest.raises(LiveError):
        sess.set_param(0, 1, 1, 0.9)
    c.fail = None
    c.ack = True
    calls = []
    real = c.read_edit_buffer
    c.read_edit_buffer = lambda **k: (calls.append(1), (_ for _ in ()).throw(HelixError("still gone")))[1]
    sess.tick()
    now[0] += 0.5
    sess.tick()
    now[0] += 0.5
    sess.tick()
    assert len(calls) == 1                                 # not once per half-second pass
    now[0] += 3.0
    c.read_edit_buffer = real
    sess.tick()
    assert sess.public_state()["connected"] is True


def test_nothing_is_written_or_saved_while_disconnected(s):
    s.client.fail = HelixError("gone")
    with pytest.raises(LiveError):
        s.set_param(0, 1, 1, 0.9)
    s.client.fail = None
    for call in (lambda: s.set_bypass(0, 1, False), s.save):
        with pytest.raises(LiveError) as e:
            call()
        assert e.value.status == 503
    assert s.client.enables == [] and s.client.saves == []


def test_absurd_numbers_are_a_400_not_a_crash(s):
    with pytest.raises(LiveError) as e:
        s.set_param(0, 1, 1, 10 ** 400)
    assert e.value.status == 400 and s.client.params == []


def test_every_write_releases_its_hold_on_the_lease(s):
    s.set_param(0, 1, 1, 0.5)
    s.set_bypass(0, 1, False)
    s.client.ack = False
    with pytest.raises(LiveError):
        s.set_param(0, 1, 1, 0.6)
    s.client.fail = HelixError("gone")
    s.client.ack = True
    with pytest.raises(LiveError):
        s.set_param(0, 1, 1, 0.7)
    assert s.lease.ensured == 4 and s.lease.dones == 4


def test_session_survives_concurrent_writers_events_and_rereads():
    """No test here ran LiveSession on more than one thread; the server does.
    Hammer it from page threads, the subscriber thread and re-reads at once:
    nothing may raise, deadlock, or leave state the fake device cannot explain."""
    import threading
    c = FakeClient()
    lock = threading.Lock()
    real_set = c.set_param

    def serial_set(*a, **k):                   # the real client is not thread-safe:
        assert lock.acquire(blocking=False), "two device calls at once"   # devlock must serialize
        try:
            return real_set(*a, **k)
        finally:
            lock.release()
    c.set_param = serial_set
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    errors, stop = [], threading.Event()

    def writer(pid, n):
        try:
            for i in range(n):
                try:
                    sess.set_param(0, 1, pid, (i % 10) / 10.0, rev=None)
                except LiveError:
                    pass
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def events():
        try:
            i = 0
            while not stop.is_set():
                i += 1
                sess.handle_events([_ev("/setParamValue", 1, 5000 + i, 0, 2, 0, DRIVE, (i % 7) / 7.0)])
                if i % 25 == 0:
                    sess.handle_events([_ev("/activateSnapshot", 1, 2, i % 8)])
                sess.tick()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            v = r = -1
            while not stop.is_set():
                v, r, _ = sess.changes_since(v, r)
                sess.wait(v, r, 0.01)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(pid, 150)) for pid in (1, 2, 3)]
    aux = [threading.Thread(target=events), threading.Thread(target=reader),
           threading.Thread(target=lambda: [sess.reread() for _ in range(20)])]
    for th in threads + aux:
        th.start()
    for th in threads:
        th.join(timeout=20)
    stop.set()
    for th in aux:
        th.join(timeout=20)
    assert not errors, errors
    assert not any(th.is_alive() for th in threads + aux), "deadlock"
    st = sess.public_state()
    assert st["connected"] is True and st["paths"]
    assert sess.lease.ensured == sess.lease.dones


def test_an_echo_that_beats_its_own_ack_is_still_recognised(s):
    # the subscriber thread can see our write's echo BEFORE the writing thread
    # has the ack (and so before the request id is remembered). It must not be
    # mistaken for someone else's change and broadcast a second time.
    def echo_arrives_first():
        s.handle_events([_ev("/setParamValue", 9, 1000, 0, 1, 0, 1, 0.31)])
    s.client.on_write = echo_arrives_first
    v, r = s.version, s.rev
    s.set_param(0, 1, 1, 0.31, client="tab-a")
    msgs = [m for m in _drain(s, v, r)[2] if m["t"] == "param"]
    assert msgs == [{"t": "param", "path": 0, "block": 1, "pid": 1,
                     "value": 0.31, "client": "tab-a"}]


def test_a_foreign_event_during_our_write_is_not_lost(s):
    # ...but an event for that control from someone else, arriving in the same
    # window, still has to land (after ours, in stream order)
    def hardware_moves_it_too():
        s.handle_events([_ev("/setParamValue", 9, 1000, 0, 1, 0, 1, 0.31),   # our echo
                         _ev("/setParamValue", 9, 77, 0, 1, 0, 1, 0.66)])    # then the hardware
    s.client.on_write = hardware_moves_it_too
    s.set_param(0, 1, 1, 0.31)
    assert _value(s, 0, 1, 1) == pytest.approx(0.66)


def test_events_deferred_by_a_failed_write_still_apply(s):
    s.client.ack = False
    s.client.on_write = lambda: s.handle_events(
        [_ev("/setParamValue", 9, 77, 0, 1, 0, 1, 0.66)])
    with pytest.raises(LiveError):
        s.set_param(0, 1, 1, 0.9)
    assert _value(s, 0, 1, 1) == pytest.approx(0.66)
