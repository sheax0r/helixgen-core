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
        self.swaps = []          # (path, block, model_id) from set_block_model (sub=0)
        self.second_cabs = []    # (path, block, model_id) from set_block_model(sub=1)
        self.model_fail = None   # set to an exception to make set_block_model raise
        self.model_noop = False  # record a sub=1 set but do NOT mutate (device ignored it)
        self.splits_added = []   # (path, block) from add_split
        self.split_fail = None   # set to an exception to make add_split raise
        self.moves = []          # (path, src, dst) from move_block
        self.move_fail = None    # set to an exception to make move_block raise
        self.move_noop = False   # record the move but do NOT mutate (device ignored it)
        self.removes = []        # (path, block) from remove_block
        self.remove_fail = None  # set to an exception to make remove_block raise
        self.remove_noop = False # record the remove but do NOT drop (device ignored it)
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
        self.renames = []
        self.colors = []         # (index, colr) from set_snapshot_color
        self.loaded = []
        self._active = None      # last activated snapshot index
        self.snapshot_ebs = None  # {index: eb} -> read_edit_buffer follows _active
        # controller assignment (81v.3)
        self.attaches = []          # (path, block, pid, sub)
        self.midi_sources = []      # (controller_cid, cc)
        self.removed_ctrls = []     # controller_cid
        self.next_ctrl_cid = 50
        self.midi_source_fail = False   # make add_midi_cc_source return False
        # local (EXP/FS) controller source (81v.3)
        self.src_sets = []              # (cid, locl, ctxt, source_type)
        self.behaviors = []             # (cid, behv)
        self.removed_srcs = []          # cid
        self.src_set_fail = False       # make set_controller_source return False
        self.behv_fail = False          # make set_cid_behavior return False
        # Command Center authoring (81v.4 write)
        self.cmd_attaches = []          # (source_type, locl, ctxt, family, subtype, slot)
        self.cmd_param_sets = []        # (slot, pvl_index, value)
        self.cmd_removes = []           # slot
        self.next_cmd_slot = 1
        self.cmd_param_fail = False     # make set_command_param return False
        self.cmd_remove_fail = False    # make remove_command return False
        # per-param snapshot control (81v.2)
        self.snap_assigns = []          # (path, block, pid, value)
        self.snap_removes = []          # target_id
        self.snap_targets = {}          # (path, block, pid) -> current target_id
        self.next_snap_tid = 60
        self.snap_remove_ack = True
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
        src = self.eb
        if self.snapshot_ebs is not None and self._active in self.snapshot_ebs:
            src = self.snapshot_ebs[self._active]
        out = copy.deepcopy(src)
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

    def set_block_model(self, path, block, model_id, sub=0):
        self._next()
        if self.model_fail is not None:
            raise self.model_fail
        if int(sub) == 1:
            self.second_cabs.append((int(path), int(block), int(model_id)))
            if not self.model_noop:              # reflect the 2nd cab so a verify re-read sees it
                raw = self._raw_at(int(path), int(block))
                if raw is not None:
                    mdls = raw.setdefault("mdls", [{}])
                    if len(mdls) < 2:
                        mdls.append({"id__": int(model_id)})
                    else:
                        mdls[1] = {"id__": int(model_id)}
        else:
            self.swaps.append((int(path), int(block), int(model_id)))
        return self.ack

    def _raw_at(self, path, slot):
        flow = (self.eb.get("sfg_") or {}).get("flow") or []
        if not (0 <= path < len(flow)):
            return None
        blks = flow[path].get("blks")
        if isinstance(blks, list):
            for i in range(0, len(blks) - 1, 2):
                if blks[i] == slot and isinstance(blks[i + 1], dict):
                    return blks[i + 1]
        return None

    def add_split(self, path, block):
        self._next()
        if self.split_fail is not None:
            raise self.split_fail
        self.splits_added.append((int(path), int(block)))
        return self.ack

    def move_block(self, path, src, dst):
        self._next()
        if self.move_fail is not None:
            raise self.move_fail
        self.moves.append((int(path), int(src), int(dst)))
        if self.move_noop:
            return self.ack
        # reflect the movb EXCHANGE in the buffer so a verify re-read sees it
        flow = (self.eb.get("sfg_") or {}).get("flow") or []
        if 0 <= int(path) < len(flow):
            blks = flow[int(path)].get("blks")
            if isinstance(blks, list):
                pos = {blks[i]: i + 1 for i in range(0, len(blks) - 1, 2)
                       if isinstance(blks[i], int) and not isinstance(blks[i], bool)}
                si, di = pos.get(int(src)), pos.get(int(dst))
                if si is not None and di is not None:
                    blks[si], blks[di] = blks[di], blks[si]       # exchange
                elif si is not None:
                    blks[si - 1] = int(dst)                        # plain move to empty dst
        return self.ack

    def remove_block(self, path, block):
        self._next()
        if self.remove_fail is not None:
            raise self.remove_fail
        self.removes.append((int(path), int(block)))
        if self.remove_noop:
            return self.ack                      # recorded, but the slot stays
        # drop the slot from blks so the session's verify re-read sees it gone
        flow = (self.eb.get("sfg_") or {}).get("flow") or []
        if 0 <= int(path) < len(flow):
            blks = flow[int(path)].get("blks")
            if isinstance(blks, list):
                for i in range(0, len(blks) - 1, 2):
                    if blks[i] == int(block):
                        del blks[i:i + 2]
                        break
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

    # --- controller assignment (81v.3) ---
    def attach_param_controller(self, path, block, pid, sub=0):
        self._next()
        self.attaches.append((int(path), int(block), int(pid), int(sub)))
        cid = self.next_ctrl_cid
        self.next_ctrl_cid += 1
        return cid

    def add_midi_cc_source(self, controller_cid, cc):
        self._next()
        self.midi_sources.append((int(controller_cid), int(cc)))
        return not self.midi_source_fail

    def remove_controller(self, controller_cid):
        self._next()
        self.removed_ctrls.append(int(controller_cid))
        return True

    # --- local (EXP/FS) controller source (81v.3) ---
    def set_controller_source(self, controller_cid, locl, ctxt, source_type=1):
        self._next()
        self.src_sets.append((int(controller_cid), int(locl), int(ctxt),
                              int(source_type)))
        return not self.src_set_fail

    def set_cid_behavior(self, controller_cid, behavior):
        self._next()
        self.behaviors.append((int(controller_cid), int(behavior)))
        return not self.behv_fail

    def remove_controller_source(self, controller_cid):
        self._next()
        self.removed_srcs.append(int(controller_cid))
        return True

    # --- Command Center authoring (81v.4 write) ---
    def attach_command(self, source_type, locl, ctxt, family, subtype=0):
        self._next()
        slot = self.next_cmd_slot
        self.next_cmd_slot += 1
        self.cmd_attaches.append((int(source_type), int(locl), int(ctxt),
                                  int(family), int(subtype), slot))
        return slot

    def set_command_param(self, slot, pvl_index, value):
        self._next()
        self.cmd_param_sets.append((int(slot), int(pvl_index), int(value)))
        return not self.cmd_param_fail

    def remove_command(self, slot):
        self._next()
        self.cmd_removes.append(int(slot))
        return not self.cmd_remove_fail

    # --- per-param snapshot control (81v.2) ---
    def assign_snap_param(self, path, block, pid, value, sub=0):
        self._next()
        self.snap_assigns.append((int(path), int(block), int(pid), float(value)))
        tid = self.next_snap_tid
        self.next_snap_tid += 1
        self.snap_targets[(int(path), int(block), int(pid))] = tid
        return tid

    def remove_snapshot_target(self, target_id):
        self._next()
        self.snap_removes.append(int(target_id))
        for k, v in list(self.snap_targets.items()):
            if v == int(target_id):
                del self.snap_targets[k]
        return self.snap_remove_ack

    def snapshot_param_target(self, path, block, pid, sub=0, *, doc=None):
        return self.snap_targets.get((int(path), int(block), int(pid)))

    def save_edit_buffer(self, cid):
        self._next()
        self.saves.append(cid)
        return self.ack

    def activate_snapshot(self, index):
        self._next()
        self.snapshots_activated.append(int(index))
        self._active = int(index)
        return True

    def set_snapshot_name(self, index, name):
        self._next()
        self.renames.append((int(index), name))
        # reflect the new name on the eb so a re-read sees it
        snps = (self.eb.setdefault("cg__", {}).setdefault("entt", {})
                .setdefault("snps", []))
        for s in snps:
            if isinstance(s, dict) and s.get("si__") == int(index):
                s["name"] = name
                break
        else:
            snps.append({"si__": int(index), "name": name})
        return True

    def set_snapshot_color(self, index, color):
        self._next()
        self.colors.append((int(index), int(color)))
        snps = (self.eb.setdefault("cg__", {}).setdefault("entt", {})
                .setdefault("snps", []))
        for s in snps:
            if isinstance(s, dict) and s.get("si__") == int(index):
                s["colr"] = int(color)
                break
        else:
            snps.append({"si__": int(index), "colr": int(color), "name": f"Snap {int(index)+1}"})
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

    # --- Global EQ (81v.6) ---
    def get_globaleq_state(self):
        if self.fail is not None:
            raise self.fail
        bands = ("lowcut", "lowshelf", "low", "mid", "high", "highshelf",
                 "highcut")
        return {"outputs": {o: {"enable": False, "level": 0.0,
                                "bands": {b: {"enable": True, "freq": 100.0}
                                          for b in bands}}
                            for o in ("qtr", "xlr", "pho")}}

    def set_globaleq(self, output, band, param, value):
        self._next()
        self.__dict__.setdefault("globaleq_sets", []).append(
            (output, band, param, value))
        return self.ack

    def set_globaleq_enable(self, output, on):
        self._next()
        self.__dict__.setdefault("globaleq_enables", []).append(
            (output, bool(on)))
        return self.ack


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


def test_decode_batched_param_vals(s=None):
    import msgpack
    deltas = [[0, 1, 0, 1, 0.5], [0, 2, 0, 2, 0.25]]
    # the subscriber already msgpack-decodes a blob arg into this nested list
    assert events.decode_event("/setBatchedParamVals", [257, 945, deltas]) == \
        ("batch", [(0, 1, 0, 1, 0.5), (0, 2, 0, 2, 0.25)])
    # defensive: a still-raw msgpack blob is decoded here too
    blob = msgpack.packb(deltas, use_bin_type=True)
    assert events.decode_event("/setBatchedParamVals", [257, 945, blob]) == \
        ("batch", [(0, 1, 0, 1, 0.5), (0, 2, 0, 2, 0.25)])
    # anything that is not a 5-tuple array -> the safety-net re-read
    assert events.decode_event("/setBatchedParamVals", [1, 2, b"x"]) == \
        ("reread", "/setBatchedParamVals")
    assert events.decode_event("/setBatchedParamVals", [1, 2, 3]) == \
        ("reread", "/setBatchedParamVals")
    assert events.decode_event("/setBatchedParamVals", [1, 2, [[0, 1, 0]]]) == \
        ("reread", "/setBatchedParamVals")


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


def test_rename_snapshot_writes_and_rereads(s):
    s.client.eb["cg__"]["entt"] = {"snps": [{"si__": 1, "name": "Snapshot 2"}]}
    st = s.rename_snapshot(1, "Chorus")
    assert s.client.renames == [(1, "Chorus")]
    assert st["snapshots"][1] == "Chorus"


@pytest.mark.parametrize("bad", [-1, 8, 99, "x", None])
def test_rename_snapshot_rejects_a_bad_index(s, bad):
    with pytest.raises(LiveError):
        s.rename_snapshot(bad, "Chorus")
    assert s.client.renames == []


@pytest.mark.parametrize("bad", ["", "   ", None, 5])
def test_rename_snapshot_rejects_an_empty_name(s, bad):
    with pytest.raises(LiveError):
        s.rename_snapshot(0, bad)
    assert s.client.renames == []


def test_rename_snapshot_is_refused_while_another_process_holds_the_device(s):
    s.lease.busy = "locked by agent-x"
    with pytest.raises(LiveError) as e:
        s.rename_snapshot(1, "Chorus")
    assert e.value.status == 409 and "agent-x" in str(e.value)
    assert s.client.renames == []


def test_rename_snapshot_device_failure_marks_disconnected(s):
    s.client.fail = HelixError("connection lost")
    with pytest.raises(LiveError) as e:
        s.rename_snapshot(1, "Chorus")
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


def _batched(*deltas):
    # the undo-history array a snapshot recall broadcasts right after
    # /activateSnapshot, already msgpack-decoded (as the subscriber hands it)
    return [list(d) for d in deltas]


def test_snapshot_batch_applies_deltas_in_place_without_a_reread(s):
    v, r, reads = s.version, s.rev, s.client.reads
    s.handle_events([_ev("/activateSnapshot", 1, 2, 2),
                     _ev("/setBatchedParamVals", 1, 3,
                         _batched((0, 1, 0, 1, 0.9), (0, 2, 0, DRIVE, 0.1)))])
    assert s.client.reads == reads          # no device re-read
    assert s.rev == r                        # no page re-render
    st = s.public_state()
    assert st["snapshot"] == 2
    assert _value(s, 0, 1, 1) == pytest.approx(0.9)
    assert _value(s, 0, 2, DRIVE) == pytest.approx(0.1)
    msgs = _drain(s, v, r)[2]
    assert any(m["t"] == "meta" and m["snapshot"] == 2 for m in msgs)
    moved = {(m["path"], m["block"], m["pid"], round(m["value"], 3))
             for m in msgs if m["t"] == "param"}
    assert moved == {(0, 1, 1, 0.9), (0, 2, DRIVE, 0.1)}


def test_snapshot_batch_skips_sub_nonzero_params(s):
    # dual-cab 2nd model / harness params (sub>0, hgc-5gh) have no page control:
    # skipped, the modeled delta still applies in place, no re-read
    reads = s.client.reads
    s.handle_events([_ev("/activateSnapshot", 1, 2, 1),
                     _ev("/setBatchedParamVals", 1, 3,
                         _batched((0, 2, 1, 99, 0.9), (0, 1, 0, 1, 0.42)))])
    assert s.client.reads == reads
    assert _value(s, 0, 1, 1) == pytest.approx(0.42)
    assert s.public_state()["snapshot"] == 1


def test_snapshot_batch_falls_back_to_reread_for_an_unknown_control(s):
    # a sub==0 delta naming a control we do not model -> take the safety-net
    # full re-read + re-render rather than leave stale values on screen
    r, reads = s.rev, s.client.reads
    s.handle_events([_ev("/activateSnapshot", 1, 2, 1),
                     _ev("/setBatchedParamVals", 1, 3,
                         _batched((0, 9, 0, 1, 0.9)))])    # no block 9
    assert s.client.reads == reads + 1       # fell back to a re-read
    assert s.rev == r + 1                     # and a re-render


def test_snapshot_batch_with_a_bypass_change_rereads(s):
    # a snapshot that flips a block on/off sends the enable as a SEPARATE event
    # (/setBlockEnable) — it is NOT in the param batch. The in-place path would
    # drop it, so a poll carrying a bypass action MUST fall back to a re-read
    # (hgc-9qd review), not silently apply the batch and leave stale on/off.
    r, reads = s.rev, s.client.reads
    s.handle_events([_ev("/activateSnapshot", 1, 2, 2),
                     _ev("/setBatchedParamVals", 1, 3,
                         _batched((0, 1, 0, 1, 0.9))),
                     _ev("/setBlockEnable", 1, 4, 0, 1, 0)])
    assert s.client.reads == reads + 1       # fell back to a re-read
    assert s.rev == r + 1                     # and a re-render


def test_snapshot_without_a_batch_falls_back_to_reread(s):
    # /activateSnapshot alone (its batch undecodable or not yet here) still
    # re-reads, as before — never a silent stale switch
    r, reads = s.rev, s.client.reads
    s.handle_events([_ev("/activateSnapshot", 1, 2, 3),
                     _ev("/setBatchedParamVals", 1, 3, b"x")])
    assert s.client.reads == reads + 1
    assert s.rev == r + 1


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


# --- 2001 sequence-gap reconciliation (hgc-0a0) ------------------------------
# A silent SUB reconnect drops frames without any disconnect: the 2001 header's
# per-stream sequence counter jumps. A jump means we missed changes, so we
# reconcile with one full re-read. `s.load()` in the fixture leaves reads == 1.

def _seqf(seq, addr="/ping", *args, port=2001):
    return Event(port, addr, list(args), seq)


def test_consecutive_sequences_do_not_reread(s):
    s.handle_events([_seqf(5)])          # first frame: establishes the baseline
    s.handle_events([_seqf(6), _seqf(7)])
    assert s.client.reads == 1


def test_first_frame_never_rereads(s):
    s.handle_events([_seqf(100)])
    assert s.client.reads == 1


def test_a_sequence_jump_rereads_exactly_once(s):
    s.handle_events([_seqf(5)])          # baseline
    s.handle_events([_seqf(9)])          # jumped: frames 6,7,8 were dropped
    assert s.client.reads == 2
    s.handle_events([_seqf(10), _seqf(11)])   # resumed in order: no further read
    assert s.client.reads == 2


def test_uint32_wraparound_is_not_a_gap(s):
    s.handle_events([_seqf(0xFFFFFFFF)])      # baseline at the top of the range
    s.handle_events([_seqf(0)])               # wrapped: continuous
    assert s.client.reads == 1


def test_a_later_jump_rereads_again(s):
    s.handle_events([_seqf(5)])
    s.handle_events([_seqf(9)])               # gap 1
    s.handle_events([_seqf(10)])              # back in order
    s.handle_events([_seqf(20)])              # gap 2
    assert s.client.reads == 3


def test_sequence_tracked_per_stream(s):
    # the counter is per-stream: each port's run is measured on its own, so one
    # stream's jump is neither masked nor caused by another's frame
    assert s._seq_gap(2001, 5) is False      # first frame on each stream
    assert s._seq_gap(2003, 100) is False
    assert s._seq_gap(2001, 6) is False      # 2001 continues in order
    assert s._seq_gap(2003, 105) is True     # 2003 jumped, on its own count
    assert s._seq_gap(2001, 7) is False      # 2001 is still fine


def test_list_presets_lights_setlist_entry_loaded_under_pool_cid():
    # hgc-6y1: a setlist entry is a REFERENCE (rcid -> pool preset). The device
    # can report the loaded tone under that POOL cid rather than the reference
    # cid, and the picker must still light the setlist entry.
    c = FakeClient()
    # the "Venom Live" setlist reference (cid_ 1950) points at pool preset 1863
    c.setlist_contents[100][0]["rcid"] = 1863
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    # the device reports the active tone under the POOL cid 1863, not ref 1950
    sess._preset = {"cid": 1863, "name": "Venom"}
    out = sess.list_presets()
    rock = next(g for g in out["groups"] if g["name"] == "Rock")
    entry = rock["presets"][0]
    assert entry["cid"] == 1950 and entry.get("pool_cid") == 1863
    assert entry["active"] is True            # lit via the pool-cid match
    pool = next(g for g in out["groups"] if g["name"] == "Pool")
    assert any(e["cid"] == 1863 and e["active"] for e in pool["presets"])


def test_list_presets_still_lights_a_directly_loaded_entry(s):
    # Regression guard: the direct reference/pool-cid match still works when the
    # device reports the loaded tone under the entry's own cid.
    s._preset = {"cid": 1950, "name": "Venom Live"}
    out = s.list_presets()
    rock = next(g for g in out["groups"] if g["name"] == "Rock")
    assert rock["presets"][0]["active"] is True


# --- model swap (hgc-cc4) --------------------------------------------------

def test_swap_model_same_category_issues_the_swap_and_rereads():
    # the rig's slot-2 block is AMP (758); swapping to another amp (628) must
    # send /ModelSet and re-read the buffer, returning fresh state.
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    reads0 = c.reads
    st = sess.swap_model(0, 2, 628)
    assert c.swaps == [(0, 2, 628)]          # device swap issued
    assert c.reads > reads0                   # re-read happened
    assert "paths" in st                      # fresh state for the page


def test_swap_model_rejects_a_cross_category_model():
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.swap_model(0, 2, 310)            # amp block -> a distortion (310)
    assert e.value.status == 400
    assert c.swaps == []                       # no device write on a bad swap


def test_swap_model_rejects_unknown_model_and_missing_block():
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e1:
        sess.swap_model(0, 2, 999999)
    assert e1.value.status == 400
    with pytest.raises(LiveError) as e2:
        sess.swap_model(0, 99, 628)           # no such block
    assert e2.value.status == 404
    assert c.swaps == []


def test_add_block_appends_at_next_free_row0_slot_and_rereads():
    # _rig() has row-0 blocks at slots 1 and 2 (input 0, output 13) -> the
    # append lands at slot 3, the next free row-0 slot before the output.
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    reads0 = c.reads
    st = sess.add_block(0, 628)
    assert c.swaps == [(0, 3, 628)]          # ADD issued at the next free slot
    assert c.reads > reads0                   # re-read happened
    assert "paths" in st


def test_add_block_appends_at_slot_1_on_an_empty_path():
    c = FakeClient(eb=_eb({0: _blk(IN_INST1, {}), 13: _blk(OUT_MATRIX, {})}, asnp=1))
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    sess.add_block(0, 628)
    assert c.swaps == [(0, 1, 628)]          # first content block goes to slot 1


def test_add_block_rejects_unknown_model():
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.add_block(0, 99999999)
    assert e.value.status == 400
    assert c.swaps == []


def test_add_block_rejects_routing_and_endpoint_models():
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    for mid in (475, 478, 770):          # split, join, input nodes
        with pytest.raises(LiveError) as e:
            sess.add_block(0, mid)
        assert e.value.status == 400
    assert c.swaps == []


def test_add_block_rejects_a_nonexistent_path():
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.add_block(5, 628)
    assert e.value.status == 404
    assert c.swaps == []


def test_add_block_fills_the_lowest_gap_when_the_tail_is_full():
    # row-0 blocks at 1 and 12: appending after the last would hit the output
    # (13), so fall back to the lowest free slot (2).
    c = FakeClient(eb=_eb({0: _blk(IN_INST1, {}), 1: _blk(SCREAM, {}),
                           12: _blk(AMP, {}), 13: _blk(OUT_MATRIX, {})}, asnp=1))
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    sess.add_block(0, 628)
    assert c.swaps == [(0, 2, 628)]


def test_add_block_refuses_a_full_row():
    blks = {0: _blk(IN_INST1, {}), 13: _blk(OUT_MATRIX, {})}
    for s in range(1, 13):
        blks[s] = _blk(SCREAM, {})
    c = FakeClient(eb=_eb(blks, asnp=1))
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.add_block(0, 628)
    assert e.value.status == 400
    assert c.swaps == []


def test_split_block_delegates_to_add_split_and_rereads():
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    reads0 = c.reads
    st = sess.split_block(0, 2)
    assert c.splits_added == [(0, 2)]        # delegated to the engine add_split
    assert c.reads > reads0                   # re-read happened
    assert "paths" in st


def test_split_block_surfaces_add_split_guard_error_as_400():
    c = FakeClient()
    c.split_fail = HelixError("block 20 is on lane B; add_split operates on 1..11")
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.split_block(0, 20)
    assert e.value.status == 400
    assert "cannot split here" in str(e.value)


def test_split_block_maps_transport_failure_to_502_and_disconnects():
    # a dropped socket mid-agenda is not a guard violation: 502, mark the
    # device offline, and re-read quietly since the write may have landed.
    c = FakeClient()
    c.split_fail = OSError("socket dead")
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.split_block(0, 2)
    assert e.value.status == 502
    assert sess.public_state()["connected"] is False


def test_move_block_delegates_to_move_block_and_rereads():
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    reads0 = c.reads
    st = sess.move_block(0, 2, 1)
    assert c.moves == [(0, 2, 1)]                 # delegated to the engine move
    assert c.reads > reads0                        # re-read happened
    assert "paths" in st


def test_move_block_rejects_a_no_op_same_slot_move():
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.move_block(0, 2, 2)
    assert e.value.status == 400
    assert c.moves == []                           # never reached the device


def test_move_block_rejects_an_out_of_range_slot():
    # a stale page / direct caller sending an I/O or lane-B slot is refused
    # BEFORE any device write — no orphaned block.
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    for s, d in [(0, 2), (2, 13), (2, 16)]:        # input, output, lane B
        with pytest.raises(LiveError) as e:
            sess.move_block(0, s, d)
        assert e.value.status == 400
    assert c.moves == []


def test_move_block_rejects_a_missing_source_block():
    # src must be an occupied processing slot in the LIVE buffer
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.move_block(0, 5, 1)                    # slot 5 is empty in _rig()
    assert e.value.status == 400
    assert c.moves == []


def test_move_block_surfaces_engine_guard_error_as_400():
    c = FakeClient()
    c.move_fail = HelixError("move_block: slots [0, 13, 14, 27] are fixed")
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.move_block(0, 2, 1)                    # valid slots; engine refuses
    assert e.value.status == 400
    assert "cannot move here" in str(e.value)


def test_move_block_raises_502_when_the_device_ignores_the_agenda():
    # the 2002 reply is meaningless, so a silent no-op must surface as an error
    c = FakeClient()
    c.move_noop = True
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.move_block(0, 2, 1)
    assert e.value.status == 502
    assert c.moves == [(0, 2, 1)]                   # it DID fire; just didn't land


def test_move_block_maps_transport_failure_to_502_and_disconnects():
    c = FakeClient()
    c.move_fail = OSError("socket dead")
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.move_block(0, 2, 1)
    assert e.value.status == 502
    assert sess.public_state()["connected"] is False


def test_remove_block_delegates_and_rereads():
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    reads0 = c.reads
    st = sess.remove_block(0, 2)                  # slot 2 = AMP (processing)
    assert c.removes == [(0, 2)]                  # delegated to the engine remove
    assert c.reads > reads0                        # re-read happened
    assert "paths" in st


@pytest.mark.parametrize("slot", [0, 13, 14, 27])
def test_remove_block_rejects_io_slots(slot):
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.remove_block(0, slot)
    assert e.value.status == 400
    assert c.removes == []                         # never reached the device


def test_remove_block_maps_engine_guard_error_to_400():
    # the routing/endpoint + missing-block guards live in the client (so the CLI
    # verb is safe too); the session maps that HelixError to a 400.
    c = FakeClient()
    c.remove_fail = HelixError(
        "remove_block: a 'split' node is a routing or endpoint block")
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.remove_block(0, 2)
    assert e.value.status == 400
    assert "cannot remove here" in str(e.value)


def test_remove_block_raises_502_without_disconnect_when_ignored():
    # a silent no-op (device answered, slot unchanged) is a 502 — but NOT a
    # disconnect: the device is reachable, it just did not apply the write
    # (matches move_block, unlike a transport failure).
    c = FakeClient()
    c.remove_noop = True
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.remove_block(0, 2)
    assert e.value.status == 502
    assert c.removes == [(0, 2)]                    # it DID fire; just didn't land
    assert sess.public_state()["connected"] is True


def test_remove_block_maps_transport_failure_to_502_and_disconnects():
    c = FakeClient()
    c.remove_fail = OSError("socket dead")
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.remove_block(0, 2)
    assert e.value.status == 502
    assert sess.public_state()["connected"] is False


def _cab_rig():
    # input, a cab (dual-cab capable), an amp, output
    return _eb({0: _blk(IN_INST1, {}), 1: _blk(434, {}),
                2: _blk(AMP, {}), 13: _blk(OUT_MATRIX, {})}, asnp=1)


def _dual_cab_rig(second=453):
    # _cab_rig, but the slot-1 cab already carries a 2nd cab (mdls[1])
    eb = _cab_rig()
    blks = eb["sfg_"]["flow"][0]["blks"]
    for i in range(0, len(blks) - 1, 2):
        if blks[i] == 1:
            mdls = blks[i + 1].setdefault("mdls", [{}])
            (mdls.append({"id__": second}) if len(mdls) < 2
             else mdls.__setitem__(1, {"id__": second}))
            break
    return eb


def test_set_second_cab_places_the_2nd_cab_at_sub1_and_rereads():
    c = FakeClient(eb=_cab_rig())
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    reads0 = c.reads
    st = sess.set_second_cab(0, 1, 453)
    assert c.second_cabs == [(0, 1, 453)]        # ModelSet sub=1
    assert c.swaps == []                          # NOT a primary-model swap
    assert c.reads > reads0
    assert "paths" in st


def test_set_second_cab_rejects_a_non_cab_block():
    c = FakeClient(eb=_cab_rig())
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.set_second_cab(0, 2, 453)            # slot 2 is the amp
    assert e.value.status == 400
    assert "cab block" in str(e.value)
    assert c.second_cabs == []


def test_set_second_cab_rejects_a_non_cab_model():
    c = FakeClient(eb=_cab_rig())
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.set_second_cab(0, 1, AMP)            # an amp is not a cab
    assert e.value.status == 400
    assert "must be a cab" in str(e.value)
    assert c.second_cabs == []


def test_set_second_cab_rejects_an_unknown_model():
    c = FakeClient(eb=_cab_rig())
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.set_second_cab(0, 1, 999999)
    assert e.value.status == 400
    assert c.second_cabs == []


def test_set_second_cab_maps_device_failure_to_502_and_disconnects():
    c = FakeClient(eb=_cab_rig())
    c.model_fail = OSError("socket dead")
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.set_second_cab(0, 1, 453)
    assert e.value.status == 502
    assert sess.public_state()["connected"] is False


def test_set_second_cab_replaces_an_existing_2nd_cab():
    c = FakeClient(eb=_dual_cab_rig(second=453))
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    st = sess.set_second_cab(0, 1, 448)           # swap the 2nd cab 453 -> 448
    assert c.second_cabs == [(0, 1, 448)]
    assert "paths" in st


def test_set_second_cab_is_a_noop_when_already_that_2nd_cab():
    c = FakeClient(eb=_dual_cab_rig(second=453))
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    st = sess.set_second_cab(0, 1, 453)           # same 2nd cab: no device write
    assert c.second_cabs == []
    assert "paths" in st


def test_set_second_cab_rejects_an_empty_slot_as_404():
    c = FakeClient(eb=_cab_rig())
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.set_second_cab(0, 5, 453)            # slot 5 is empty
    assert e.value.status == 404
    assert c.second_cabs == []


def test_set_second_cab_raises_502_when_the_device_ignores_the_set():
    c = FakeClient(eb=_cab_rig())
    c.model_noop = True
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.set_second_cab(0, 1, 453)
    assert e.value.status == 502
    assert c.second_cabs == [(0, 1, 453)]         # fired, but didn't land


def test_swap_model_rejects_an_endpoint_or_routing_block():
    # input/output/split/join are not model-swappable; no /ModelSet on an
    # endpoint grid slot (hgc-cc4 review).
    c = FakeClient()
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    with pytest.raises(LiveError) as e:
        sess.swap_model(0, 0, 771)            # the slot-0 INPUT endpoint
    assert e.value.status == 400
    assert c.swaps == []


def test_models_in_category_lists_same_category_swaps():
    from helixgen.device import defs
    amps = {m["id"] for m in defs.models_in_category("amp")}
    assert 758 in amps and 628 in amps        # the block's model + a target
    assert 310 not in amps                     # a distortion is not an amp
    assert "amp" in defs.models_by_category()


# --- snapshot copy / swap (hgc-0g0.4): orchestrate granular writes, no new opcode ---

def _snap_eb(drive, *, scream_on=1, names=None, asnp=0):
    """A one-amp rig whose AMP Drive is snapshot-assigned, so two of these with
    different `drive` model two snapshots that differ in exactly that param (and
    optionally the SCREAM bypass). `names` -> cg__.entt.snps slot names."""
    eb = _eb({
        0: _blk(IN_INST1, {2: 1, 3: 0.0, 4: True, 5: -52.0}),
        1: _blk(SCREAM, {1: 0.3, 2: 0.8, 3: 0.84}, enbl=scream_on),
        2: _blk(AMP, {DRIVE: drive}, snap_pids={DRIVE}),
        13: _blk(OUT_MATRIX, {1: 0.5, 2: -3.0}),
    }, asnp=asnp)
    if names:
        eb["cg__"]["entt"] = {"snps": [{"si__": i, "name": n}
                                       for i, n in names.items()]}
    return eb


def _copy_sess(ebs):
    c = FakeClient(eb=ebs[min(ebs)])
    c.snapshot_ebs = ebs
    sess = LiveSession(c, lease=FakeLease())
    sess.load()
    c.params.clear(); c.enables.clear(); c.renames.clear()
    c.snapshots_activated.clear()
    return sess


def test_snapshot_writes_returns_only_the_differences():
    # the pure diff: a value identical across snapshots is NOT a write (so a
    # non-snapshot-controlled param self-skips); only real differences carry.
    sv = {(0, 2, DRIVE): 0.74, (0, 1, 1): 0.3}      # DRIVE differs, the other same
    dv = {(0, 2, DRIVE): 0.30, (0, 1, 1): 0.3}
    se = {(0, 1): True, (0, 2): True}               # SCREAM bypass differs
    de = {(0, 1): False, (0, 2): True}
    pw, bw = LiveSession._snapshot_writes(sv, dv, se, de)
    assert pw == [((0, 2, DRIVE), 0.74)]
    assert bw == [((0, 1), True)]


def test_copy_snapshot_replays_only_the_differing_values_into_the_target():
    src = _snap_eb(0.74, scream_on=1, asnp=1)
    dst = _snap_eb(0.30, scream_on=0, asnp=2)
    s = _copy_sess({1: src, 2: dst})
    s.copy_snapshot(1, 2)
    # read src then dst (dst stays active for the writes)
    assert s.client.snapshots_activated == [1, 2]
    # exactly one param write — DRIVE -> src's 0.74 — not the identical params
    assert len(s.client.params) == 1
    p, b, pid, val, *_ = s.client.params[0]
    assert (b, pid, val) == (2, DRIVE, 0.74)
    # exactly one bypass write — SCREAM back to src's enabled state
    assert len(s.client.enables) == 1
    ep, eb, en, *_ = s.client.enables[0]
    assert (eb, en) == (1, True)
    # copy never renames (state only)
    assert s.client.renames == []


def test_copy_snapshot_onto_itself_is_a_noop():
    s = _copy_sess({1: _snap_eb(0.74, asnp=1)})
    s.copy_snapshot(1, 1)
    assert s.client.snapshots_activated == [] and s.client.params == []


@pytest.mark.parametrize("src,dst", [(-1, 2), (2, 8), (0, 99)])
def test_copy_snapshot_rejects_a_bad_index(src, dst):
    s = _copy_sess({0: _snap_eb(0.5), 2: _snap_eb(0.5)})
    with pytest.raises(LiveError):
        s.copy_snapshot(src, dst)
    assert s.client.snapshots_activated == []


def test_copy_snapshot_is_refused_while_another_process_holds_the_device():
    s = _copy_sess({1: _snap_eb(0.74, asnp=1), 2: _snap_eb(0.30, asnp=2)})
    s.lease.busy = "another helixgen process holds the editbuffer"
    with pytest.raises(LiveError) as e:
        s.copy_snapshot(1, 2)
    assert e.value.status == 409
    assert s.client.params == []          # nothing written


def test_copy_snapshot_device_failure_marks_disconnected():
    s = _copy_sess({1: _snap_eb(0.74, asnp=1), 2: _snap_eb(0.30, asnp=2)})
    s.client.fail = HelixError("socket died")
    with pytest.raises(LiveError) as e:
        s.copy_snapshot(1, 2)
    assert e.value.status == 502 and s.connected is False


def test_swap_snapshot_swaps_state_both_ways_and_names():
    names = {0: "Lead", 1: "Rhythm"}
    a = _snap_eb(0.74, names=names, asnp=0)
    b = _snap_eb(0.30, names=names, asnp=1)
    s = _copy_sess({0: a, 1: b})
    s.swap_snapshot(0, 1)
    # recall a, recall b (write A in), recall a again (write B in)
    assert s.client.snapshots_activated == [0, 1, 0]
    # both directions written: 0.74 into the b-slot, 0.30 into the a-slot
    vals = sorted(round(p[3], 3) for p in s.client.params)
    assert vals == [0.30, 0.74]
    # names swapped
    assert s.client.renames == [(0, "Rhythm"), (1, "Lead")]


def test_swap_snapshot_same_index_is_a_noop():
    s = _copy_sess({0: _snap_eb(0.5, asnp=0)})
    s.swap_snapshot(0, 0)
    assert s.client.snapshots_activated == [] and s.client.renames == []


def test_swap_snapshot_does_not_rename_an_interior_placeholder():
    # a GAP between configured slots decodes as the "Snapshot N" placeholder
    # literal (chain._snapshot_names). Swapping a configured slot with it must
    # NOT stamp that literal onto the configured slot — that would corrupt its
    # identity (and hit the device, since the literal is a non-empty name).
    a = _snap_eb(0.74, names={0: "Lead", 2: "Crunch"}, asnp=0)   # slot 1 is a gap
    b = _snap_eb(0.30, names={0: "Lead", 2: "Crunch"}, asnp=1)
    s = _copy_sess({0: a, 1: b})
    s.swap_snapshot(0, 1)
    assert s.client.renames == []                        # neither slot renamed
    assert sorted(round(p[3], 3) for p in s.client.params) == [0.30, 0.74]  # state swapped


def test_swap_snapshot_does_not_rename_a_device_placeholder():
    # the DEVICE's own default label is "Snap N" (not "Snapshot N") — also a
    # placeholder, so an unrenamed slot's state swaps but its label stays.
    a = _snap_eb(0.74, names={0: "Lead", 1: "Snap 2"}, asnp=0)
    b = _snap_eb(0.30, names={0: "Lead", 1: "Snap 2"}, asnp=1)
    s = _copy_sess({0: a, 1: b})
    s.swap_snapshot(0, 1)
    assert s.client.renames == []
    assert sorted(round(p[3], 3) for p in s.client.params) == [0.30, 0.74]


def test_is_snapshot_named_rejects_both_placeholder_forms():
    assert LiveSession._is_snapshot_named("Lead", 0) is True
    assert LiveSession._is_snapshot_named("Snap 2", 1) is False     # device default
    assert LiveSession._is_snapshot_named("Snapshot 2", 1) is False  # synthesized gap
    assert LiveSession._is_snapshot_named("", 0) is False
    assert LiveSession._is_snapshot_named(None, 0) is False


# --- snapshot colour (hgc-0g0.1): /SnapshotColorSet, decoded 2026-11-23 ---

def test_set_snapshot_color_writes_and_rereads(s):
    st = s.set_snapshot_color(0, 4)
    assert s.client.colors == [(0, 4)]
    assert st["snapshot_colors"][0] == 4          # re-read reflects the new colr


def test_set_snapshot_color_coerces_a_palette_name(s):
    s.set_snapshot_color(1, "red")                # 'red' -> index 2 (maintenance palette)
    assert s.client.colors == [(1, 2)]


def test_set_snapshot_color_marks_the_buffer_dirty(s):
    # a colour lives in the edit buffer (cg__.entt.snps[].colr) — it is an
    # unsaved edit, so it must light the dirty flag like rename/copy/swap do,
    # or the page shows "saved" and the colour is silently lost on preset switch.
    assert s.public_state()["dirty"] is False
    st = s.set_snapshot_color(0, 4)
    assert st["dirty"] is True


@pytest.mark.parametrize("bad", [-1, 8, 99])
def test_set_snapshot_color_rejects_a_bad_index(s, bad):
    with pytest.raises(LiveError):
        s.set_snapshot_color(bad, 2)
    assert s.client.colors == []


def test_set_snapshot_color_rejects_an_unknown_colour(s):
    with pytest.raises(LiveError):
        s.set_snapshot_color(0, "chartreuse")
    assert s.client.colors == []


def test_set_snapshot_color_is_refused_while_another_process_holds_the_device(s):
    s.lease.busy = "another helixgen process holds the editbuffer"
    with pytest.raises(LiveError) as e:
        s.set_snapshot_color(0, 2)
    assert e.value.status == 409
    assert s.client.colors == []


def test_set_snapshot_color_device_failure_marks_disconnected(s):
    s.client.fail = HelixError("socket died")
    with pytest.raises(LiveError) as e:
        s.set_snapshot_color(0, 2)
    assert e.value.status == 502 and s.connected is False


# --- Global EQ pane (81v.6) --------------------------------------------------

def test_globaleq_state_returns_outputs(s):
    st = s.globaleq_state()
    assert set(st["outputs"]) == {"qtr", "xlr", "pho"}
    assert st["outputs"]["qtr"]["bands"]["low"]["freq"] == 100.0


def test_globaleq_state_is_lease_free(s):
    # browsing globals must never take the write lease (mirrors mixer_state)
    s.globaleq_state()
    assert s.lease.transients == [] and s.lease.ensured == 0


def test_globaleq_write_band_goes_through_set_globaleq(s):
    out = s.globaleq_write("qtr", "low", "gain", -3.5)
    assert s.client.globaleq_sets == [("qtr", "low", "gain", -3.5)]
    assert out["value"] == -3.5
    assert s.lease.transients == [("globals",)]       # globals scope, transient


def test_globaleq_write_master_enable_uses_the_property_path(s):
    out = s.globaleq_write("qtr", "", "enable", True)
    assert s.client.globaleq_enables == [("qtr", True)]
    assert getattr(s.client, "globaleq_sets", []) == []   # NOT a band write
    assert out["value"] is True


def test_globaleq_write_level(s):
    s.globaleq_write("pho", "", "level", -2.0)
    assert s.client.globaleq_sets == [("pho", "", "level", -2.0)]


def test_globaleq_write_rejects_a_bad_band(s):
    with pytest.raises(LiveError) as e:
        s.globaleq_write("qtr", "nope", "gain", 1.0)
    assert e.value.status == 400
    assert getattr(s.client, "globaleq_sets", []) == []


def test_globaleq_write_is_refused_while_another_process_holds_the_device(s):
    s.lease.busy = "another helixgen process holds globals"
    with pytest.raises(LiveError) as e:
        s.globaleq_write("qtr", "low", "gain", 1.0)
    assert e.value.status == 409


def test_globaleq_reset_writes_factory_defaults_under_one_lease(s):
    out = s.globaleq_reset("qtr")
    sets = s.client.globaleq_sets
    # every band's params written; cut bands have slope not gain/q
    assert ("qtr", "lowcut", "freq", 20.0) in sets
    assert ("qtr", "lowcut", "slope", 1) in sets
    assert ("qtr", "low", "gain", 0.0) in sets and ("qtr", "low", "q", 0.707) in sets
    assert all(k[0] == "qtr" for k in sets)
    # no gain/q written to a cut band
    assert not any(k[1] == "lowcut" and k[2] in ("gain", "q") for k in sets)
    assert out["bands"]["low"] == {"enable": True, "freq": 150.0, "gain": 0.0, "q": 0.707}
    assert s.lease.transients == [("globals",)]      # ONE lease for the whole batch


def test_globaleq_reset_rejects_a_bad_output(s):
    with pytest.raises(LiveError) as e:
        s.globaleq_reset("spdif")
    assert e.value.status == 400
    assert getattr(s.client, "globaleq_sets", []) == []


def test_globaleq_apply_writes_payload_and_level(s):
    out = s.globaleq_apply("xlr", {"low": {"gain": 3.0, "q": 1.2}}, level=-2.0)
    assert ("xlr", "low", "gain", 3.0) in s.client.globaleq_sets
    assert ("xlr", "low", "q", 1.2) in s.client.globaleq_sets
    assert ("xlr", "", "level", -2.0) in s.client.globaleq_sets
    assert out["bands"]["low"]["gain"] == 3.0 and out["level"] == -2.0
    assert s.lease.transients == [("globals",)]


def test_globaleq_apply_rejects_a_bad_param_before_writing_anything(s):
    with pytest.raises(LiveError) as e:
        s.globaleq_apply("qtr", {"lowcut": {"gain": 2.0}})   # cut band has no gain
    assert e.value.status == 400
    assert getattr(s.client, "globaleq_sets", []) == []       # atomic: nothing written
