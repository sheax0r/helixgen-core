"""Preset-content (``_sbepgsm``) blob codec + msgpack blob helpers.

The device sends a preset's full state (the "edit buffer") as a blob:
``b"_sbepgsm"`` (8-byte ASCII magic) followed by a msgpack document whose map
keys are 4-character codes packed big-endian into uint32s.  This module decodes
that into a nested Python structure with readable 4CC string keys, and encodes
it back verbatim.

Note: this is the device's *native* schema, which is disjoint from helixgen's
``.hsp`` JSON schema.  Round-tripping the blob (backup/restore/clone) is exact;
mapping it to ``.hsp`` is a separate, larger effort (see the authoring bridge).
"""
from __future__ import annotations

import struct
from typing import Any

MAGIC = b"_sbepgsm"


def _require_msgpack():
    try:
        import msgpack  # noqa: F401
        return msgpack
    except ImportError as exc:  # pragma: no cover - exercised via install error
        raise RuntimeError(
            "the device content codec needs msgpack; install with "
            "`pip install 'helixgen[device]'`"
        ) from exc


def fourcc_to_str(n: Any) -> Any:
    """uint32 -> 4-char code string when printable, else the value unchanged."""
    if isinstance(n, int) and 0 <= n < 2**32:
        b = struct.pack(">I", n)
        if all(0x20 <= c < 0x7F for c in b):
            return b.decode("ascii")
    return n


def str_to_fourcc(s: Any) -> Any:
    """4-char code string -> uint32; pass ints/other through unchanged."""
    if isinstance(s, str) and len(s) == 4:
        return struct.unpack(">I", s.encode("ascii"))[0]
    return s


def _keys_to_str(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {fourcc_to_str(k): _keys_to_str(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_keys_to_str(v) for v in obj]
    return obj


def _keys_to_fourcc(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str_to_fourcc(k): _keys_to_fourcc(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_keys_to_fourcc(v) for v in obj]
    return obj


def is_content_blob(blob: bytes) -> bool:
    return blob[:8] == MAGIC


def decode_content(blob: bytes) -> Any:
    """Decode a ``_sbepgsm`` content blob into a nested dict with 4CC str keys."""
    msgpack = _require_msgpack()
    if blob[:8] != MAGIC:
        raise ValueError("not a _sbepgsm content blob")
    obj = msgpack.unpackb(blob[8:], raw=False, strict_map_key=False)
    return _keys_to_str(obj)


def encode_content(obj: Any) -> bytes:
    """Encode a nested dict (4CC str keys) back to a ``_sbepgsm`` blob."""
    msgpack = _require_msgpack()
    packed = msgpack.packb(_keys_to_fourcc(obj), use_bin_type=True)
    return MAGIC + packed


def decode_blob(blob: bytes) -> Any:
    """Decode a metadata msgpack blob (raw, or with a 4-byte length prefix)."""
    if not blob:
        return None
    if blob[:8] == MAGIC:
        return decode_content(blob)
    msgpack = _require_msgpack()
    for start in (0, 4):
        try:
            return msgpack.unpackb(blob[start:], raw=False, strict_map_key=False)
        except Exception:  # noqa: BLE001 - probe both framings
            continue
    return blob
