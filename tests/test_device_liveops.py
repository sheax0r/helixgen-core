"""Live edit-buffer control (snapshot / bypass / model / params / blocks) —
wire-shape tests.

Block addressing (2026-07-15 erratum, HW-proven): the live-ops wire commands
take the block's DSP **grid slot** — the int PAIRED with the block dict in the
``sfg_.flow[dsp].blks`` flat list (0..27; outputs at 13/27) — passed through
UNCHANGED. The old ``(blks_key-1)/2`` translation of the block's flat-list
position only coincided with the true slot for chains occupying contiguous
slots from 0 (which is why the output block's params were unaddressable).
"""
import msgpack
import pytest

from helixgen.device.client import HelixClient
from helixgen.device.osc import osc_encode, parse_osc_message


class _FakePoller:
    def __init__(self, frames):
        self._remaining = len(frames)

    def register(self, *_a, **_k):
        pass

    def poll(self, _ms):
        if self._remaining > 0:
            self._remaining -= 1
            return [("sock", 1)]
        return []


class _FakeSock:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def recv(self):
        return self._frames.pop(0)

    def close(self):
        pass


def _wire(client, frames=()):
    client.sock = _FakeSock(frames)
    client.poller = _FakePoller(frames)


def _last_sent(h):
    raw = h.sock.sent[-1]
    addr, args, _ = parse_osc_message(raw, raw.find(b"/"))
    return addr, [v for _t, v in args]


def test_activate_snapshot_wire():
    h = HelixClient("10.0.0.99")
    _wire(h)
    assert h.activate_snapshot(2) is True
    addr, args = _last_sent(h)
    assert addr == "/activateSnapshot"
    # [reqid, index]
    assert args[1] == 2 and len(args) == 2


@pytest.mark.parametrize("bad", [-1, 8, 99])
def test_activate_snapshot_range(bad):
    h = HelixClient("10.0.0.99")
    _wire(h)
    with pytest.raises(ValueError):
        h.activate_snapshot(bad)


def test_set_block_enable_wire():
    # The grid slot goes on the wire unchanged — including the output
    # block's non-contiguous slot 13 (HW-proven 2026-07-15).
    h = HelixClient("10.0.0.99")
    _wire(h)
    h.set_block_enable(0, 3, False)
    addr, args = _last_sent(h)
    assert addr == "/BlockEnableSet"
    # [reqid, dsp, grid_slot, enable]
    assert args[1:] == [0, 3, 0]
    _wire(h)
    h.set_block_enable(1, 13, True)
    _, args2 = _last_sent(h)
    assert args2[1:] == [1, 13, 1]


def test_set_block_model_wire():
    h = HelixClient("10.0.0.99")
    _wire(h)
    h.set_block_model(0, 4, 70)
    addr, args = _last_sent(h)
    assert addr == "/ModelSet"
    # [reqid, dsp, grid_slot, sub=0, modelId]
    assert args[1:] == [0, 4, 0, 70]


def test_set_param_wire(monkeypatch):
    # /ParamValueSet [reqid, path, grid_slot, 0, paramId, value, -1] with the
    # value in RAW units (dB floats verbatim; HW 2026-07-15: slot 13 gain
    # 6.0→3.0→6.0 acked + read back).
    h = HelixClient("10.0.0.99")
    _wire(h)
    monkeypatch.setattr(h, "read_edit_buffer", lambda: _FLAT_EB)
    h.set_param(0, 13, 2, -6.0)
    addr, args = _last_sent(h)
    assert addr == "/ParamValueSet"
    assert args[1:] == [0, 13, 0, 2, -6.0, -1]


def test_set_param_rejects_out_of_range(monkeypatch):
    # hc-bgx: the device happily applies +25 dB on a gain the defs (and
    # `device params`) advertise as [-120..20]. The write path validates
    # against the advertised range instead of letting a typo land.
    from helixgen.device.client import HelixError

    h = HelixClient("10.0.0.99")
    _wire(h)
    monkeypatch.setattr(h, "read_edit_buffer", lambda: _FLAT_EB)
    with pytest.raises(HelixError, match=r"-120\.\.20"):
        h.set_param(0, 13, 2, 25.0)
    assert h.sock.sent == []       # nothing reached the wire


def test_set_param_force_writes_out_of_range(monkeypatch):
    # The range is the vendored defs', not the hardware's — --force writes it.
    h = HelixClient("10.0.0.99")
    _wire(h)
    monkeypatch.setattr(h, "read_edit_buffer", lambda: _FLAT_EB)
    h.set_param(0, 13, 2, 25.0, force=True)
    addr, args = _last_sent(h)
    assert addr == "/ParamValueSet"
    assert args[1:] == [0, 13, 0, 2, 25.0, -1]


def test_set_param_bounds_are_inclusive(monkeypatch):
    h = HelixClient("10.0.0.99")
    _wire(h)
    monkeypatch.setattr(h, "read_edit_buffer", lambda: _FLAT_EB)
    for value in (20.0, -120.0):
        h.set_param(0, 13, 2, value)
    assert len(h.sock.sent) == 2


def test_set_param_unknown_pid_has_no_range(monkeypatch):
    # pid 999 is stored but absent from the defs: no advertised range, so
    # nothing to validate against — the write goes through as before.
    h = HelixClient("10.0.0.99")
    _wire(h)
    monkeypatch.setattr(h, "read_edit_buffer", lambda: _FLAT_EB)
    h.set_param(0, 1, 999, 12345.0)
    addr, _args = _last_sent(h)
    assert addr == "/ParamValueSet"


def test_get_param_wire_and_reply():
    # /ParamValueGet [reqid, path, grid_slot, 0, paramId] ->
    # /getParamValue [reqid, path, grid_slot, 0, paramId, value]
    reply = osc_encode("/getParamValue",
                       [("i", 1000), ("i", 0), ("i", 13), ("i", 0),
                        ("i", 2), ("f", 6.0)])
    h = HelixClient("10.0.0.99")
    _wire(h, [reply])
    assert h.get_param(0, 13, 2) == 6.0
    addr, args = _last_sent(h)
    assert addr == "/ParamValueGet"
    assert args[1:] == [0, 13, 0, 2]


def test_get_param_no_value_raises():
    from helixgen.device.client import HelixError

    # A short reply (no value field) means nothing answered at that
    # coordinate — must raise, not return junk.
    reply = osc_encode("/getParamValue",
                       [("i", 1000), ("i", 0), ("i", 5), ("i", 0)])
    h = HelixClient("10.0.0.99")
    _wire(h, [reply])
    with pytest.raises(HelixError, match="no value"):
        h.get_param(0, 5, 2)


@pytest.mark.parametrize("bad_slot", [-3, -1, 28, 99])
def test_liveops_reject_out_of_grid_slots(bad_slot):
    from helixgen.device.client import HelixError
    h = HelixClient("10.0.0.99")
    _wire(h)
    for call in (lambda: h.set_block_enable(0, bad_slot, True),
                 lambda: h.set_block_model(0, bad_slot, 70),
                 lambda: h.set_param(0, bad_slot, 1, 0.5)):
        with pytest.raises(HelixError, match="device blocks"):
            call()


# Real wire shape: blks is a FLAT alternating [grid_slot:int, block:dict, …]
# list; the slots need not be contiguous (outputs at 13/27).
_FLAT_EB = {
    "sfg_": {"flow": [
        {"blks": [
            0, {"mdls": [{"id__": 769}], "enbl": 1},
            1, {"mdls": [{"id__": 612,
                          "parm": [
                              {"pid_": 1, "valu": 0.67},
                              {"pid_": 999, "valu": 42.0},  # unknown pid
                          ]}], "enbl": 1},
            13, {"mdls": [{"id__": 783,
                           "parm": [{"pid_": 1, "valu": 0.5},
                                    {"pid_": 2, "valu": 6.0}]}], "enbl": 0},
        ]},
        {"blks": [0, {"mdls": [{"id__": 774}], "enbl": 1}]},
    ]}
}


def test_edit_buffer_blocks_reports_grid_slots(monkeypatch):
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "read_edit_buffer", lambda: _FLAT_EB)
    blocks = h.edit_buffer_blocks()
    coords = {(b["path"], b["block"]) for b in blocks}
    assert coords == {(0, 0), (0, 1), (0, 13), (1, 0)}
    b13 = next(b for b in blocks if (b["path"], b["block"]) == (0, 13))
    assert b13["model_id"] == 783 and b13["enabled"] is False


def test_edit_buffer_blocks_dict_shape_passthrough(monkeypatch):
    # A dict-shaped blks (synthetic fixtures) yields its keys unchanged.
    fake_eb = {
        "sfg_": {"flow": [
            {"blks": {
                1: {"mdls": [{"id__": 596}], "enbl": 1},
                3: {"mdls": [{"id__": 286}], "enbl": 0},
                2: {"foo": "not a modeled block"},   # no mdls -> skipped
            }},
        ]}
    }
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "read_edit_buffer", lambda: fake_eb)
    coords = {(b["path"], b["block"]) for b in h.edit_buffer_blocks()}
    assert coords == {(0, 1), (0, 3)}


def test_edit_buffer_blocks_empty_on_bad_shape(monkeypatch):
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "read_edit_buffer", lambda: {"sfg_": {}})
    assert h.edit_buffer_blocks() == []


def test_edit_buffer_params_joins_defs(monkeypatch):
    # Model 783 = P35_OutputMatrix: defs say pan = pid 1, gain = pid 2.
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "read_edit_buffer", lambda: _FLAT_EB)
    info = h.edit_buffer_params(0, 13)
    assert info["model"] == "P35_OutputMatrix"
    assert info["block"] == 13 and info["enabled"] is False
    by_pid = {p["pid"]: p for p in info["params"]}
    assert by_pid[1]["name"] == "pan" and by_pid[1]["value"] == 0.5
    assert by_pid[2]["name"] == "gain" and by_pid[2]["value"] == 6.0


def test_edit_buffer_params_unknown_pid_kept(monkeypatch):
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "read_edit_buffer", lambda: _FLAT_EB)
    info = h.edit_buffer_params(0, 1)  # HD2_AmpA30FawnBrt
    by_pid = {p["pid"]: p for p in info["params"]}
    assert by_pid[1]["name"] == "Drive" and by_pid[1]["value"] == 0.67
    # a stored pid the defs don't know is kept with name=None
    assert by_pid[999]["name"] is None and by_pid[999]["value"] == 42.0
    # a defs param with no stored entry reports value=None
    assert any(p["value"] is None for p in info["params"])


def test_edit_buffer_params_no_block_raises(monkeypatch):
    from helixgen.device.client import HelixError
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "read_edit_buffer", lambda: _FLAT_EB)
    with pytest.raises(HelixError, match="device blocks"):
        h.edit_buffer_params(0, 5)


def test_active_preset(monkeypatch):
    from helixgen.device import settings as S

    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(
        h, "get_property",
        lambda key: S.PropertyValue(key=key, type="i", value=1202))
    monkeypatch.setattr(
        h, "get_ref",
        lambda cid: {"cid_": cid, "name": "Prehistoric Dog", "posi": 17,
                     "ccid": -2})
    info = h.active_preset()
    assert info == {"cid": 1202, "name": "Prehistoric Dog", "posi": 17,
                    "slot": "5B", "ccid": -2}


def test_active_preset_unresolvable_ref(monkeypatch):
    from helixgen.device import settings as S

    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(
        h, "get_property",
        lambda key: S.PropertyValue(key=key, type="i", value=0))
    monkeypatch.setattr(h, "get_ref", lambda cid: None)
    info = h.active_preset()
    assert info["cid"] == 0 and info["name"] is None and info["slot"] == ""


# --- fast-ack live writes (hgc-58a) ----------------------------------------
# A live knob UI issues many writes a second. The default RPC keeps polling a
# 0.4 s settle window AFTER the ack has arrived (614 ms / write measured on a
# Stadium XL, 2026-11-12); `fast=True` waits only for the ack (10 ms measured).

class _RecordingPoller:
    """Hands out one frame per poll until exhausted; records each timeout."""

    def __init__(self, frames):
        self._remaining = len(frames)
        self.timeouts = []

    def register(self, *_a, **_k):
        pass

    def poll(self, ms):
        self.timeouts.append(ms)
        if self._remaining > 0:
            self._remaining -= 1
            return [("sock", 1)]
        return []


def _status_ack(rid, code=0):
    return osc_encode("/status", [("i", rid), ("i", code)])


def _wire_recording(client, frames):
    client.sock = _FakeSock(frames)
    client.poller = _RecordingPoller(frames)


def test_set_param_default_keeps_the_settle_drain():
    h = HelixClient("10.0.0.99")
    _wire_recording(h, [_status_ack(1000)])
    assert h.set_param(0, 13, 2, -6.0, force=True) is True
    # first poll waits the full rpc timeout, the post-ack poll the 0.4 s settle
    assert h.poller.timeouts == [2000, 400]


def test_set_param_fast_waits_only_for_the_ack():
    h = HelixClient("10.0.0.99")
    _wire_recording(h, [_status_ack(1000)])
    assert h.set_param(0, 13, 2, -6.0, force=True, fast=True) is True
    addr, args = _last_sent(h)
    assert addr == "/ParamValueSet"
    assert args[1:] == [0, 13, 0, 2, -6.0, -1]       # same wire shape
    first, after_ack = h.poller.timeouts
    assert 0 < first <= 500                           # short ack wait
    assert after_ack == 0                             # NO settle drain


def test_set_param_fast_reports_a_missing_ack():
    # a write at a slot with no block is silently dropped (no ack) — the fast
    # path must still surface that as False, not as success.
    h = HelixClient("10.0.0.99")
    _wire_recording(h, [])
    assert h.set_param(0, 13, 2, -6.0, force=True, fast=True) is False


def test_set_block_enable_fast_waits_only_for_the_ack():
    h = HelixClient("10.0.0.99")
    _wire_recording(h, [_status_ack(1000)])
    assert h.set_block_enable(0, 3, False, fast=True) is True
    addr, args = _last_sent(h)
    assert addr == "/BlockEnableSet" and args[1:] == [0, 3, 0]
    assert h.poller.timeouts[-1] == 0


def test_set_block_enable_fast_reports_a_missing_ack():
    h = HelixClient("10.0.0.99")
    _wire_recording(h, [])
    assert h.set_block_enable(0, 3, False, fast=True) is False


def test_set_block_enable_default_still_returns_true_without_an_ack():
    # legacy contract (the verb treats the call as fire-and-confirm-by-echo)
    h = HelixClient("10.0.0.99")
    _wire_recording(h, [])
    assert h.set_block_enable(0, 3, False) is True


def test_last_request_id_names_the_most_recent_rpc():
    # The device echoes an edit-buffer write on the 2001 stream with `seq` ==
    # our request id (HW 2026-11-12: /ParamValueSet reqid 1005 -> echo
    # /setParamValue [origin, 1005, …]); a live UI uses it to tell its own
    # echoes from someone else's change.
    h = HelixClient("10.0.0.99")
    assert h.last_request_id is None
    _wire_recording(h, [_status_ack(1000)])
    h.set_param(0, 13, 2, -6.0, force=True, fast=True)
    assert h.last_request_id == 1000
    _wire_recording(h, [_status_ack(1001)])
    h.set_block_enable(0, 3, True, fast=True)
    assert h.last_request_id == 1001


def test_save_edit_buffer_persists_into_an_existing_cid():
    h = HelixClient("10.0.0.99")
    _wire_recording(h, [_status_ack(1000)])
    assert h.save_edit_buffer(1863) is True
    addr, args = _last_sent(h)
    assert addr == "/SavePresetWithCID"
    assert args[1:] == [1863, 0, 0]


def test_save_edit_buffer_reports_a_refusal():
    h = HelixClient("10.0.0.99")
    _wire_recording(h, [_status_ack(1000, code=1)])
    assert h.save_edit_buffer(1863) is False
