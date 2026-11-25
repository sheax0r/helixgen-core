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


def test_move_block_sends_movb():
    h = HelixClient("10.0.0.99")
    _wire(h)
    assert h.move_block(1, 6, 8) is True
    _, vals = _sent_doagenda(h)
    assert msgpack.unpackb(vals[1], raw=False) == [
        agenda.move(bl_s=6, bl_d=8, fl_s=1, fl_d=1)]


def test_move_block_into_split_sends_bundled_transaction():
    h = HelixClient("10.0.0.99")
    _wire(h)
    # move lane-A slot 4 into the split@2/join@3 lane B — one bundled /doAgenda
    assert h.move_block_into_split(0, 4, 2, 3) is True
    _, vals = _sent_doagenda(h)
    assert msgpack.unpackb(vals[1], raw=False) == agenda.move_into_split(4, 2, 3)


@pytest.mark.parametrize("args", [
    (0, 0, 2, 3),    # src is the input slot
    (0, 14, 2, 3),   # src is a lane-B I/O slot
    (0, 4, 3, 2),    # join not above split
    (0, 4, 2, 2),    # split == join
])
def test_move_block_into_split_rejects_bad_args(args):
    h = HelixClient("10.0.0.99")
    with pytest.raises(HelixError):
        h.move_block_into_split(*args)  # guarded before any wire send


def test_set_block_model_sub_wire():
    h = HelixClient("10.0.0.99")
    _wire(h)
    assert h.set_block_model(0, 9, 464, sub=1) is True
    raw = h.sock.sent[-1]
    addr, args, _ = parse_osc_message(raw, raw.find(b"/"))
    vals = [v for _t, v in args]
    assert addr == "/ModelSet"
    assert vals[1:] == [0, 9, 1, 464]  # [reqid, path, slot, sub, modelId]


def test_split_out_builder():
    # split out lane-A col 3 with a later block at col 4 (shift case). Pinned to
    # the HW-validated structure: block->laneB, split@col, join@col+1, shift.
    ops = agenda.split_out(3, [4], flow=0)
    assert ops == [
        agenda.move(3, 17),          # block col3 -> lane B slot 17
        agenda.move(4, 5),           # shift the later block right
        agenda.set_model(3, 475, 3), # split node
        agenda.split(3, 17),         # spld: split@3 -> lane B 17
        agenda.set_model(4, 478, 4), # join node
        agenda.merge(4, 18),         # jono: join@4 <- lane B 18
    ]


def test_move_into_split_builder():
    # Move lane-A slot 4 into an existing split (split@2, join@3) — append to its
    # lane-B series. Pinned to the HW-validated bundled transaction (2026-11-24):
    # move + re-asserted split + re-pointed join, all in one /doAgenda.
    ops = agenda.move_into_split(4, 2, 3, flow=0)
    assert ops == [
        agenda.move(4, 17),   # block -> the join's lane-B merge slot (3+14)
        agenda.split(2, 16),  # re-assert split@2 -> lane B 16 (2+14)
        agenda.merge(3, 17),  # join@3 now merges from the new block at 17
    ]


def _flow(*slots):
    """A stubbed edit buffer whose flow 0 occupies the given grid slots."""
    blks = []
    for s in slots:
        blks += [s, {}]
    return {"sfg_": {"flow": [{"blks": blks}]}}


# col 12 would land the join on the output (slot 13); 0/13 are I/O; 14+ is lane B.
@pytest.mark.parametrize("block", [0, 12, 13, 14, 20, 27])
def test_add_split_rejects_non_lane_a(block):
    h = HelixClient("10.0.0.99")
    with pytest.raises(HelixError):
        h.add_split(0, block)  # guarded before any buffer read


def test_split_out_builder_rejects_bad_col():
    import pytest as _p
    with _p.raises(ValueError):
        agenda.split_out(12, [])        # join would hit the output
    with _p.raises(ValueError):
        agenda.split_out(3, [2])        # later col <= split col


def test_add_split_wire_shift_case():
    h = HelixClient("10.0.0.99")
    # 1st read = before (series 1,2,3); 2nd read = after (lane B 16 now holds it)
    reads = iter([_flow(0, 1, 2, 3, 13), _flow(0, 2, 3, 4, 13, 16)])
    h.read_edit_buffer = lambda **k: next(reads)
    _wire(h)
    assert h.add_split(0, 2) is True  # splits col 2, shifts col 3
    _, vals = _sent_doagenda(h)
    assert msgpack.unpackb(vals[1], raw=False) == agenda.split_out(2, [3], flow=0)


def test_add_split_wire_no_shift_case():
    h = HelixClient("10.0.0.99")
    reads = iter([_flow(0, 1, 2, 13), _flow(0, 1, 2, 3, 13, 16)])
    h.read_edit_buffer = lambda **k: next(reads)
    _wire(h)
    assert h.add_split(0, 2) is True  # last block, no shift
    _, vals = _sent_doagenda(h)
    assert msgpack.unpackb(vals[1], raw=False) == agenda.split_out(2, [], flow=0)


def test_add_split_rejects_occupied_lane_b():
    h = HelixClient("10.0.0.99")
    h.read_edit_buffer = lambda **k: _flow(2, 16)  # lane B 16 already used
    _wire(h)
    with pytest.raises(HelixError):
        h.add_split(0, 2)


def _flow_m(models):
    """A stubbed flow 0 whose {slot: model_id} are occupied with that model
    (so the split/join node detection in add_split sees real ids)."""
    blks = []
    for s, mid in sorted(models.items()):
        blks += [s, {"mdls": [{"id__": mid}]}]
    return {"sfg_": {"flow": [{"blks": blks}]}}


# split=475, join=478; 770 input, 758 amp, 310 scream, 783 output, 425/426 cabs
def test_add_split_allows_serial_second_split_past_the_last_join():
    h = HelixClient("10.0.0.99")
    # split@2 / join@3 / lane-B block@16 already present; split block@5 (main path)
    before = _flow_m({0: 770, 1: 758, 2: 475, 3: 478, 5: 310, 13: 783, 16: 425})
    after = _flow_m({0: 770, 1: 758, 2: 475, 3: 478, 5: 475, 13: 783, 16: 425, 19: 310})
    reads = iter([before, after])
    h.read_edit_buffer = lambda **k: next(reads)
    _wire(h)
    assert h.add_split(0, 5) is True        # 5 > last join (3); lane-B 19/20 free
    _, vals = _sent_doagenda(h)
    assert msgpack.unpackb(vals[1], raw=False) == agenda.split_out(5, [], flow=0)


def test_add_split_rejects_a_second_split_before_the_section():
    h = HelixClient("10.0.0.99")
    h.read_edit_buffer = lambda **k: _flow_m(
        {0: 770, 1: 758, 2: 475, 3: 478, 13: 783, 16: 425})
    _wire(h)
    with pytest.raises(HelixError):
        h.add_split(0, 1)                   # block@1 is before the join@3


def test_add_split_rejects_a_second_split_when_its_lane_b_slot_is_taken():
    h = HelixClient("10.0.0.99")
    # block@5 is past join@3, but its lane-B slot 19 is already occupied
    h.read_edit_buffer = lambda **k: _flow_m(
        {0: 770, 1: 758, 2: 475, 3: 478, 5: 310, 13: 783, 16: 425, 19: 426})
    _wire(h)
    with pytest.raises(HelixError):
        h.add_split(0, 5)


def test_add_split_rejects_a_second_split_before_a_later_split_node():
    h = HelixClient("10.0.0.99")
    # split@2/join@3 section, series@5, then an OUTPUT split@8 (splits to two
    # outputs, NO join). A split at 5 is past join@3 but BEFORE split@8 — its
    # later-shift would move split@8 and break its wiring. Must refuse (the
    # guard counts split columns, not just joins).
    h.read_edit_buffer = lambda **k: _flow_m(
        {0: 770, 1: 758, 2: 475, 3: 478, 5: 310, 8: 475, 13: 783, 16: 425, 22: 783, 27: 785})
    _wire(h)
    with pytest.raises(HelixError):
        h.add_split(0, 5)


def test_add_split_rejects_no_block_there():
    h = HelixClient("10.0.0.99")
    h.read_edit_buffer = lambda **k: _flow(0, 1, 13)  # nothing at slot 2
    _wire(h)
    with pytest.raises(HelixError):
        h.add_split(0, 2)


def test_add_split_rejects_capacity():
    h = HelixClient("10.0.0.99")
    h.read_edit_buffer = lambda **k: _flow(1, 2, 12, 13)  # a block at 12 -> shift to 13
    _wire(h)
    with pytest.raises(HelixError):
        h.add_split(0, 1)


@pytest.mark.parametrize("path", [-1, 3])
def test_add_split_rejects_bad_path(path):
    h = HelixClient("10.0.0.99")
    h.read_edit_buffer = lambda **k: _flow(0, 1, 2, 13)  # only flow 0
    _wire(h)
    with pytest.raises(HelixError):
        h.add_split(path, 1)


def test_add_split_raises_if_not_applied():
    h = HelixClient("10.0.0.99")
    # before OK, but after the send lane B 16 is still empty -> device didn't apply
    reads = iter([_flow(0, 1, 2, 13), _flow(0, 1, 2, 13)])
    h.read_edit_buffer = lambda **k: next(reads)
    _wire(h)
    with pytest.raises(HelixError):
        h.add_split(0, 2)


def _flow_models(models):
    """A stubbed edit buffer whose flow 0 carries real models at the given
    slots: ``{grid_slot: model_id}``."""
    blks = []
    for s, mid in sorted(models.items()):
        blks += [s, {"mdls": [{"id__": mid}]}]
    return {"sfg_": {"flow": [{"blks": blks}]}}


# real model ids (vendored defs): a distortion, a Split-Y routing node
_DIST, _SPLIT = 310, 475


def test_remove_block_sends_null_modelset():
    from helixgen.device.client import NULL_MODEL
    h = HelixClient("10.0.0.99")
    h.read_edit_buffer = lambda **k: _flow_models({0: 769, 2: _DIST, 13: 783})
    _wire(h)
    assert h.remove_block(0, 2) is True
    raw = h.sock.sent[-1]
    addr, args, _ = parse_osc_message(raw, raw.find(b"/"))
    vals = [v for _t, v in args]
    assert addr == "/ModelSet"
    # [reqid, path, slot, sub, modelId] — removal is a null-model ModelSet
    assert vals[1:] == [0, 2, 0, NULL_MODEL]


@pytest.mark.parametrize("slot", [0, 13, 14, 27])
def test_remove_block_rejects_io_slots(slot):
    h = HelixClient("10.0.0.99")
    _wire(h)
    with pytest.raises(HelixError):
        h.remove_block(0, slot)  # guarded before any read/write


def test_remove_block_rejects_a_routing_node():
    # a split/join node is routing — nulling it would orphan the parallel lane,
    # and the CLI verb reaches this guard with no session in front of it.
    h = HelixClient("10.0.0.99")
    h.read_edit_buffer = lambda **k: _flow_models({0: 769, 2: _SPLIT, 13: 783})
    _wire(h)
    with pytest.raises(HelixError):
        h.remove_block(0, 2)
    assert h.sock.sent == []                      # refused before any write


def test_remove_block_rejects_a_missing_block():
    h = HelixClient("10.0.0.99")
    h.read_edit_buffer = lambda **k: _flow_models({0: 769, 13: 783})  # nothing at 2
    _wire(h)
    with pytest.raises(HelixError):
        h.remove_block(0, 2)
    assert h.sock.sent == []


@pytest.mark.parametrize("path", [-1, 3])
def test_remove_block_rejects_absent_path(path):
    h = HelixClient("10.0.0.99")
    h.read_edit_buffer = lambda **k: _flow_models({0: 769, 2: _DIST, 13: 783})
    _wire(h)
    with pytest.raises(HelixError):
        h.remove_block(path, 2)
    assert h.sock.sent == []


def test_cli_topology_verbs_help_contract():
    # The new live-ops verbs' behavioral contract lives in their --help.
    from click.testing import CliRunner
    from helixgen.cli import cli
    r = CliRunner()
    move = r.invoke(cli, ["device", "move-block", "--help"]).output
    assert "movb" in move and "lane B" in move
    model = r.invoke(cli, ["device", "model", "--help"]).output
    assert "--sub" in model and "dual-cab" in model
    split = r.invoke(cli, ["device", "add-split", "--help"]).output
    assert "parallel" in split and "lane B" in split
    rm = r.invoke(cli, ["device", "remove-block", "--help"]).output
    assert "Null model" in rm and "HOLE" in rm and "clrb" in rm


def test_do_agenda_status_nonzero_is_tolerated():
    # HW fact (2026-11-23): a synthesized split APPLIED while the device replied
    # /status 1, so the reply code is NOT a failure gate — do_agenda must not
    # mistake it for a rejection (the 2001 echo is the real confirmation).
    from helixgen.device.osc import osc_encode
    h = HelixClient("10.0.0.99")
    h._rid = iter([91])
    status1 = osc_encode("/status", [("i", 91), ("i", 1)])
    _wire(h, frames=[status1])
    assert h.do_agenda([agenda.clear(bloc=1)]) is True


def test_do_agenda_error_code_zero_ack_ok():
    # The other observed ack variant: /error [rid, 0, ""] on an applied agenda.
    from helixgen.device.osc import osc_encode
    h = HelixClient("10.0.0.99")
    h._rid = iter([92])
    ok = osc_encode("/error", [("i", 92), ("i", 0), ("s", "")])
    _wire(h, frames=[ok])
    assert h.do_agenda([agenda.clear(bloc=1)]) is True


@pytest.mark.parametrize("src,dst", [(0, 5), (5, 13), (14, 5), (5, 27)])
def test_move_block_rejects_io_slots(src, dst):
    h = HelixClient("10.0.0.99")
    _wire(h)
    with pytest.raises(HelixError):
        h.move_block(0, src, dst)


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
