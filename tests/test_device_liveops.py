"""Live edit-buffer control (snapshot / bypass / model / blocks) — wire-shape
tests. Arg layouts decoded from the 2026-07-14 parity capture; each command
leads with the auto-prepended request id, then the decoded fields.
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
    h = HelixClient()
    _wire(h)
    assert h.activate_snapshot(2) is True
    addr, args = _last_sent(h)
    assert addr == "/activateSnapshot"
    # [reqid, index]
    assert args[1] == 2 and len(args) == 2


@pytest.mark.parametrize("bad", [-1, 8, 99])
def test_activate_snapshot_range(bad):
    h = HelixClient()
    _wire(h)
    with pytest.raises(ValueError):
        h.activate_snapshot(bad)


def test_set_block_enable_wire():
    # Public coords are the `device blocks` / sfg_.flow blks position keys
    # (odd ints); the wire wants (key-1)/2. HW-verified 2026-07-14: bypassing
    # the amp at blks key 7 only works when the wire carries block 3.
    h = HelixClient()
    _wire(h)
    h.set_block_enable(0, 7, False)
    addr, args = _last_sent(h)
    assert addr == "/BlockEnableSet"
    # [reqid, dsp, wire_block, enable]
    assert args[1:] == [0, 3, 0]
    _wire(h)
    h.set_block_enable(1, 11, True)
    _, args2 = _last_sent(h)
    assert args2[1:] == [1, 5, 1]


def test_set_block_model_wire():
    h = HelixClient()
    _wire(h)
    h.set_block_model(0, 9, 70)
    addr, args = _last_sent(h)
    assert addr == "/ModelSet"
    # [reqid, dsp, wire_block, sub=0, modelId]
    assert args[1:] == [0, 4, 0, 70]


def test_set_param_wire():
    # /ParamValueSet [reqid, path, wire_block, 0, paramId, value, -1] with the
    # value in RAW units (a dB float is accepted verbatim; HW 2026-07-14).
    h = HelixClient()
    _wire(h)
    h.set_param(0, 11, 2, -6.0)
    addr, args = _last_sent(h)
    assert addr == "/ParamValueSet"
    assert args[1:] == [0, 5, 0, 2, -6.0, -1]


@pytest.mark.parametrize("bad_key", [0, 2, 8, -3])
def test_liveops_reject_non_blks_keys(bad_key):
    from helixgen.device.client import HelixError
    h = HelixClient()
    _wire(h)
    for call in (lambda: h.set_block_enable(0, bad_key, True),
                 lambda: h.set_block_model(0, bad_key, 70),
                 lambda: h.set_param(0, bad_key, 1, 0.5)):
        with pytest.raises(HelixError, match="device blocks"):
            call()


def test_edit_buffer_blocks_parses(monkeypatch):
    fake_eb = {
        "sfg_": {"flow": [
            {"blks": {
                1: {"mdls": [{"id__": 596}], "enbl": 1},
                3: {"mdls": [{"id__": 286}], "enbl": 0},
                2: {"foo": "not a modeled block"},   # no mdls -> skipped
            }},
            {"blks": {1: {"mdls": [{"id__": 68}], "enbl": 1}}},
        ]}
    }
    h = HelixClient()
    monkeypatch.setattr(h, "read_edit_buffer", lambda: fake_eb)
    blocks = h.edit_buffer_blocks()
    coords = {(b["path"], b["block"]) for b in blocks}
    assert coords == {(0, 1), (0, 3), (1, 1)}
    b01 = [b for b in blocks if (b["path"], b["block"]) == (0, 1)][0]
    assert b01["model_id"] == 596 and b01["enabled"] is True
    b03 = [b for b in blocks if (b["path"], b["block"]) == (0, 3)][0]
    assert b03["enabled"] is False


def test_edit_buffer_blocks_empty_on_bad_shape(monkeypatch):
    h = HelixClient()
    monkeypatch.setattr(h, "read_edit_buffer", lambda: {"sfg_": {}})
    assert h.edit_buffer_blocks() == []
