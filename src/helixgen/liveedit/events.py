"""Decode the device's 2001 change stream into live-editor actions.

The Stadium publishes every edit-buffer change on its 2001 PUB socket (the
stream the HX Edit app reacts to), including the echo of our own writes.

Arg layouts (captured on a Stadium XL, fw 1.4; ``seq`` of an echo equals the
request id of the write that caused it):

  /setParamValue    [origin, seq, path, grid_slot, snap, pid, value]
                    (value typed like the param: float, int, or OSC T/F)
  /setBlockEnable   [origin, seq, path, grid_slot, enable]
  /activateSnapshot [origin, seq, index]
  /setEditBuffer    [origin, seq, <edit-buffer blob>]   (preset load, LIVE buffer)
  /loadContentRef   [origin, seq, {cid_, name, posi, …}]
  /setPropertyValue [origin, seq, <value blob: key_, type, val_>]
                    (``volatile.preset.edited`` = the unsaved-edits flag)

Actions returned by :func:`decode_event`:

  ("param", seq, path, block, pid, value)
  ("bypass", seq, path, block, enabled)
  ("snapshot", index)
  ("batch", [(dsp, block, sub, pid, value), …])  — a snapshot recall's deltas
  ("editbuffer", blob)
  ("presetref", cid, name)
  ("dirty", bool)
  ("reread", addr)      — the buffer changed in a way we do not model
"""
from __future__ import annotations

from typing import Any, List, Optional

from helixgen.device import content as _content
from helixgen.device import settings as _settings

# Edit-buffer changes with no per-control decoding here: a model swap made on
# the hardware, per-snapshot bypass state. The right response is one fresh read
# of the buffer. (A snapshot recall's /setBatchedParamVals is decoded to a
# "batch" action below and applied in place; it falls back to a re-read only
# when its deltas cannot be decoded.)
REREAD_ADDRS = frozenset({
    "/setModelWithMID",
    "/setSnapshotBypassState",
})

DIRTY_KEY = "volatile.preset.edited"


def _property(arg: Any) -> tuple:
    """``(key, value)`` of a ``/setPropertyValue`` payload: on the wire a
    ``lavppgsm`` value blob (the same one ``/PropertyValueGet`` answers with);
    a decoded ``{key_, val_}`` map is accepted too."""
    if isinstance(arg, dict):
        return arg.get("key_"), arg.get("val_")
    if isinstance(arg, (bytes, bytearray)):
        try:
            pv = _settings.decode_value_blob(bytes(arg))
        except Exception:  # noqa: BLE001 — not a value blob: not ours
            return None, None
        return pv.key, pv.value
    return None, None


def _int(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError("not an int")
    return v


def _batched_param_deltas(args: list) -> Optional[List[tuple]]:
    """The deltas of a ``/setBatchedParamVals`` as
    ``[(dsp, block, sub, paramId, value), …]`` (a snapshot recall's undo-history
    msgpack array of 5-tuples), or ``None`` if no arg holds that array. The
    subscriber already msgpack-decodes a blob arg into the nested list; a
    still-raw blob is decoded here too."""
    for arg in args:
        if isinstance(arg, (bytes, bytearray)):
            try:
                arg = _content.decode_blob(bytes(arg))
            except Exception:  # noqa: BLE001 — not msgpack: not the batch
                continue
        if not isinstance(arg, (list, tuple)) or not arg:
            continue
        out: List[tuple] = []
        for t in arg:
            if not (isinstance(t, (list, tuple)) and len(t) == 5):
                out = []
                break
            val = t[4]
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                out = []
                break
            try:
                out.append((_int(t[0]), _int(t[1]), _int(t[2]), _int(t[3]),
                            float(val)))
            except TypeError:
                out = []
                break
        if out:
            return out
    return None


def decode_event(addr: str, args: list) -> Optional[tuple]:
    """One 2001 event -> an action tuple, or ``None`` if it is not ours."""
    try:
        if addr == "/setParamValue" and len(args) >= 7:
            value = args[6]
            if isinstance(value, bool):
                value = 1.0 if value else 0.0
            elif not isinstance(value, (int, float)):
                return None
            return ("param", _int(args[1]), _int(args[2]), _int(args[3]),
                    _int(args[5]), float(value))
        if addr == "/setBlockEnable" and len(args) >= 5:
            return ("bypass", _int(args[1]), _int(args[2]), _int(args[3]),
                    bool(args[4]))
        if addr == "/activateSnapshot" and len(args) >= 3:
            return ("snapshot", _int(args[2]))
    except TypeError:
        return None
    if addr == "/setBatchedParamVals":
        deltas = _batched_param_deltas(args)
        return ("batch", deltas) if deltas else ("reread", addr)
    if addr == "/setEditBuffer" and len(args) >= 3 and isinstance(args[2], (bytes, bytearray)):
        return ("editbuffer", bytes(args[2]))
    if addr == "/loadContentRef" and len(args) >= 3 and isinstance(args[2], dict):
        return ("presetref", args[2].get("cid_"), args[2].get("name"))
    if addr == "/setPropertyValue" and len(args) >= 3:
        key, val = _property(args[2])
        return ("dirty", bool(val)) if key == DIRTY_KEY else None
    if addr in REREAD_ADDRS:
        return ("reread", addr)
    return None
