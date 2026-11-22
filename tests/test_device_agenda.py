"""Topology-edit agenda (`/doAgenda`) — op builders + wire shape.

The op builders in :mod:`helixgen.device.agenda` are pinned against the REAL
agenda decoded by a frida MITM of the Helix Stadium app's 2002 socket on
2026-11-22 (a drag-down split; see the engine design doc §0). `HelixClient.
do_agenda` is tested with the same FakeSock/FakePoller harness as
``test_device_liveops.py``.
"""
import msgpack
import pytest

from helixgen.device import agenda
from helixgen.device.client import HelixClient, HelixError
from helixgen.device.osc import parse_osc_message


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


# -- op builders pinned to the captured drag-down-split agenda --------------

# Decoded verbatim from the device (frida, 2026-11-22). cmnd ints are the 4CCs
# movb/setm/spld/jono packed big-endian.
CAPTURED_SPLIT_OPS = [
    {"bl_d": 20, "bl_s": 6, "cmnd": 1836021346, "fl_d": 0, "fl_s": 0},
    {"bl_d": 9, "bl_s": 8, "cmnd": 1836021346, "fl_d": 0, "fl_s": 0},
    {"bl_d": 8, "bl_s": 7, "cmnd": 1836021346, "fl_d": 0, "fl_s": 0},
    {"bloc": 6, "cmnd": 1936028781, "flow": 0, "modl": 475, "slot": 0, "type": 3},
    {"bblk": 20, "bloc": 6, "cmnd": 1936747620, "flow": 0},
    {"bloc": 7, "cmnd": 1936028781, "flow": 0, "modl": 478, "slot": 0, "type": 4},
    {"bblk": 21, "bloc": 7, "cmnd": 1785687663, "flow": 0},
]


def test_builders_reproduce_captured_split():
    built = [
        agenda.move(bl_s=6, bl_d=20),
        agenda.move(bl_s=8, bl_d=9),
        agenda.move(bl_s=7, bl_d=8),
        agenda.set_model(bloc=6, modl=475, btype=3),
        agenda.split(bloc=6, bblk=20),
        agenda.set_model(bloc=7, modl=478, btype=4),
        agenda.merge(bloc=7, bblk=21),
    ]
    assert built == CAPTURED_SPLIT_OPS


def test_clear_and_swap_shapes():
    # 'clrb' / 'swap' captured as single-op agendas
    assert agenda.clear(bloc=1) == {"cmnd": 0x636C7262, "flow": 0, "bloc": 1}
    assert agenda.swap(blk0=7, blk1=6) == {
        "cmnd": 0x73776170, "flow": 0, "blk0": 7, "blk1": 6}


def test_fourcc_round_trips_back_to_name():
    import struct
    for op, name in [(agenda.move(0, 1), "movb"), (agenda.swap(0, 1), "swap"),
                     (agenda.clear(0), "clrb"), (agenda.split(0, 1), "spld"),
                     (agenda.merge(0, 1), "jono"),
                     (agenda.set_model(0, 1, 1), "setm")]:
        assert struct.pack(">I", op["cmnd"]) == name.encode("ascii")


# -- do_agenda wire ---------------------------------------------------------

def _sent_doagenda(h):
    raw = h.sock.sent[-1]
    addr, args, _ = parse_osc_message(raw, raw.find(b"/"))
    vals = [v for _t, v in args]
    return addr, vals


def test_do_agenda_wire_shape():
    h = HelixClient("10.0.0.99")
    _wire(h)  # no reply frames -> happy path
    ops = [agenda.swap(blk0=4, blk1=5)]
    assert h.do_agenda(ops) is True
    addr, vals = _sent_doagenda(h)
    assert addr == "/doAgenda"
    # [reqid, msgpack(op-list)]
    assert isinstance(vals[0], int)
    assert msgpack.unpackb(vals[1], raw=False) == ops


def test_do_agenda_empty_raises():
    h = HelixClient("10.0.0.99")
    _wire(h)
    with pytest.raises(HelixError):
        h.do_agenda([])


def test_do_agenda_tolerates_error_address_ack():
    # HW fact (2026-11-22): the device acks an APPLIED agenda on the /error
    # address with a code that does NOT indicate failure (both 0 and 1 seen on
    # applied agendas), so do_agenda must not mistake that ack for a rejection.
    from helixgen.device.osc import osc_encode
    h = HelixClient("10.0.0.99")
    h._rid = iter([88])
    ack = osc_encode("/error", [("i", 88), ("i", 1), ("i", 1)])
    _wire(h, frames=[ack])
    assert h.do_agenda([agenda.clear(bloc=1)]) is True
