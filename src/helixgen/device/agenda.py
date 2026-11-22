"""Topology-edit agenda ops for the live edit buffer (``/doAgenda``).

Decoded 2026-11-22 by a frida MITM of the Helix Stadium app's 2002 socket (see
``docs/superpowers/specs/2026-11-21-topology-synthesis-engine-design.md`` §0 in
the plugin repo). A structural edit — move / swap / split / merge / clear — is
sent as ONE ``/doAgenda [reqid, msgpack(op-list)]`` transaction; the device
applies it in place and echoes ``/setEditBuffer`` on network 2001 (no
``/loadContentRef``, no reload). Single-model placement/swap is NOT an agenda
op — that is the granular ``/ModelSet`` (:meth:`HelixClient.set_block_model`),
with ``sub=1`` addressing a dual-cab 2nd model.

Each op is a dict with 4CC string keys; the ``cmnd`` value is the op name
packed as a big-endian 4CC int, exactly as the device encodes it. Grid slots:
lane A = 0..13, lane B = 14..27, per DSP flow.
"""
from __future__ import annotations

import struct
from typing import Dict


def _cc(name: str) -> int:
    """Pack a 4-char op name into the big-endian int the device uses."""
    b = name.encode("ascii")
    if len(b) != 4:
        raise ValueError(f"agenda cmnd must be 4 ASCII chars, got {name!r}")
    return struct.unpack(">I", b)[0]


def move(bl_s: int, bl_d: int, fl_s: int = 0, fl_d: int = 0) -> Dict[str, int]:
    """``movb`` — move the block at grid slot ``bl_s`` (flow ``fl_s``) to slot
    ``bl_d`` (flow ``fl_d``). Used to open a parallel lane (B = slots 14..27)."""
    return {"cmnd": _cc("movb"), "fl_s": int(fl_s), "fl_d": int(fl_d),
            "bl_s": int(bl_s), "bl_d": int(bl_d)}


def swap(blk0: int, blk1: int, flow: int = 0) -> Dict[str, int]:
    """``swap`` — exchange two blocks' positions (adjacent reorder)."""
    return {"cmnd": _cc("swap"), "flow": int(flow),
            "blk0": int(blk0), "blk1": int(blk1)}


def set_model(bloc: int, modl: int, btype: int, slot: int = 0,
              flow: int = 0) -> Dict[str, int]:
    """``setm`` — place/set a block's model at grid slot ``bloc``. ``btype`` =
    block category (split=3, merge=4); ``modl`` = numeric model id; ``slot`` =
    subpos (0 for the primary model). Emits the wire key ``type``."""
    return {"cmnd": _cc("setm"), "flow": int(flow), "bloc": int(bloc),
            "modl": int(modl), "slot": int(slot), "type": int(btype)}


def split(bloc: int, bblk: int, flow: int = 0) -> Dict[str, int]:
    """``spld`` — declare a split at block ``bloc`` branching to block ``bblk``."""
    return {"cmnd": _cc("spld"), "flow": int(flow),
            "bloc": int(bloc), "bblk": int(bblk)}


def merge(bloc: int, bblk: int, flow: int = 0) -> Dict[str, int]:
    """``jono`` — declare a join/merge at block ``bloc`` from branch ``bblk``."""
    return {"cmnd": _cc("jono"), "flow": int(flow),
            "bloc": int(bloc), "bblk": int(bblk)}


# Routing-node models + the lane geometry, decoded from captured splits + the
# device's own `sfg_` wiring (2026-11-22/23). A split at lane-A column C puts
# its parallel block on lane B at slot C + LANE_B_OFFSET.
SPLIT_MODEL, SPLIT_TYPE = 475, 3   # "App DSP Split Y"
JOIN_MODEL, JOIN_TYPE = 478, 4     # "App DSP Join"
LANE_B_OFFSET = 14                 # lane A col C -> lane B slot C+14 (slots 14..27)
LANE_A_LAST = 13                   # output slot on lane A


def split_out(col: int, later_cols, flow: int = 0):
    """Agenda that splits the block at lane-A ``col`` onto a NEW parallel lane B
    (the app's drag-down-to-parallel, synthesized). Moves that block to lane B
    (slot ``col+14``), inserts a split node at ``col`` and a join at ``col+1``,
    and shifts the lane-A blocks in ``later_cols`` (those at columns > ``col``)
    right by one to make room for the join. Returns the op list for
    :meth:`HelixClient.do_agenda`; the caller owns slot-freedom/capacity checks
    (see :meth:`HelixClient.add_split`)."""
    ops = [move(col, col + LANE_B_OFFSET, flow, flow)]
    for c in sorted(later_cols, reverse=True):   # highest first, avoid collisions
        ops.append(move(c, c + 1, flow, flow))
    ops += [
        set_model(col, SPLIT_MODEL, SPLIT_TYPE, flow=flow),
        split(col, col + LANE_B_OFFSET, flow),
        set_model(col + 1, JOIN_MODEL, JOIN_TYPE, flow=flow),
        merge(col + 1, col + 1 + LANE_B_OFFSET, flow),
    ]
    return ops


def clear(bloc: int, flow: int = 0) -> Dict[str, int]:
    """``clrb`` — clear/remove the block at grid slot ``bloc``.

    Wire shape pinned to the app's capture, but a STANDALONE ``clrb`` agenda had
    no effect on a Stadium XL (2026-11-22) — the app's Edit>Clear selects the
    block first, so ``clrb`` likely needs a ``/setSelectedBlock`` prerequisite
    or only applies inside a compound agenda (it appears in the remove-split
    sequence alongside ``setm``). Not yet exposed as a client method; resolve
    the prerequisite before wiring one."""
    return {"cmnd": _cc("clrb"), "flow": int(flow), "bloc": int(bloc)}


if __name__ == "__main__":  # pragma: no cover - self-check
    # Pinned against the captured drag-down-split agenda (frida, 2026-11-22).
    got = [move(6, 20), move(8, 9), move(7, 8),
           set_model(6, 475, 3), split(6, 20),
           set_model(7, 478, 4), merge(7, 21)]
    want = [
        {"bl_d": 20, "bl_s": 6, "cmnd": 1836021346, "fl_d": 0, "fl_s": 0},
        {"bl_d": 9, "bl_s": 8, "cmnd": 1836021346, "fl_d": 0, "fl_s": 0},
        {"bl_d": 8, "bl_s": 7, "cmnd": 1836021346, "fl_d": 0, "fl_s": 0},
        {"bloc": 6, "cmnd": 1936028781, "flow": 0, "modl": 475, "slot": 0, "type": 3},
        {"bblk": 20, "bloc": 6, "cmnd": 1936747620, "flow": 0},
        {"bloc": 7, "cmnd": 1936028781, "flow": 0, "modl": 478, "slot": 0, "type": 4},
        {"bblk": 21, "bloc": 7, "cmnd": 1785687663, "flow": 0},
    ]
    assert got == want, got
    assert clear(1)["cmnd"] == 0x636C7262 and swap(7, 6)["cmnd"] == 0x73776170
    print("agenda self-check ok")
