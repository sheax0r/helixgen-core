"""OSC (Open Sound Control) encode/decode for the Helix Stadium network protocol.

Messages on the wire are OSC packets carried in ZeroMQ frames: an address
string (NUL-terminated, padded to 4 bytes), a type-tag string (``","`` + tags,
NUL-terminated, padded to 4 — the comma counts toward the padding), then the
typed argument payload.  Blob (``b``) arguments carry a 4-byte big-endian length
then the bytes; those bytes are msgpack (decoded elsewhere).

Pure stdlib — no third-party imports here, so this module is always importable.
"""
from __future__ import annotations

import struct
from typing import Any, List, Tuple


def _pad4(n: int) -> int:
    return (n + 3) & ~3


def _padz(s: str) -> bytes:
    """Encode ``s`` as a NUL-terminated string padded to a 4-byte boundary."""
    b = s.encode("latin1") + b"\x00"
    while len(b) % 4:
        b += b"\x00"
    return b


def osc_encode(addr: str, args: List[Tuple[str, Any]]) -> bytes:
    """Encode one OSC message.

    ``args`` is a list of ``(tag, value)`` pairs where tag is one of
    ``i`` int32, ``f`` float32, ``h`` int64, ``s`` string, ``b`` blob (bytes).
    """
    out = _padz(addr)
    tags = ","
    body = b""
    for tag, value in args:
        tags += tag
        if tag == "i":
            body += struct.pack(">i", value)
        elif tag == "f":
            body += struct.pack(">f", value)
        elif tag == "h":
            body += struct.pack(">q", value)
        elif tag in ("s", "S"):
            body += _padz(value)
        elif tag == "b":
            body += struct.pack(">i", len(value)) + bytes(value)
            while len(body) % 4:
                body += b"\x00"
        else:
            raise ValueError(f"unknown OSC type tag {tag!r}")
    out += _padz(tags) + body
    return out


def parse_osc_message(buf: bytes, off: int = 0) -> Tuple[str, List[Tuple[str, Any]], int]:
    """Parse one OSC message at ``off``; return ``(address, args, next_off)``.

    ``args`` is a list of ``(tag, value)`` pairs (blobs come back as raw bytes
    under tag ``b``).
    """
    end = buf.index(b"\x00", off)
    addr = buf[off:end].decode("latin1")
    p = off + _pad4(len(addr) + 1)
    if p >= len(buf) or buf[p : p + 1] != b",":
        return addr, [], p
    tend = buf.index(b"\x00", p)
    tags = buf[p + 1 : tend].decode("latin1")
    # on-wire tag block = "," + tags + "\0", padded to 4 bytes
    q = p + _pad4(len(tags) + 2)
    args: List[Tuple[str, Any]] = []
    for t in tags:
        if t == "i":
            args.append(("i", struct.unpack_from(">i", buf, q)[0])); q += 4
        elif t == "f":
            args.append(("f", struct.unpack_from(">f", buf, q)[0])); q += 4
        elif t == "h":
            args.append(("h", struct.unpack_from(">q", buf, q)[0])); q += 8
        elif t == "d":
            args.append(("d", struct.unpack_from(">d", buf, q)[0])); q += 8
        elif t in ("s", "S"):
            se = buf.index(b"\x00", q)
            s = buf[q:se].decode("latin1")
            args.append(("s", s)); q += _pad4(len(s) + 1)
        elif t == "b":
            blen = struct.unpack_from(">i", buf, q)[0]; q += 4
            args.append(("b", buf[q : q + blen])); q += _pad4(blen)
        elif t in "TFN":
            args.append((t, None))
        else:
            args.append(("?" + t, None))
    return addr, args, q


def osc_address(raw: bytes) -> str:
    """Best-effort: return the OSC address in a frame (skip any binary header)."""
    i = raw.find(b"/")
    if i < 0:
        return "?"
    try:
        end = raw.index(b"\x00", i)
        return raw[i:end].decode("latin1")
    except ValueError:
        return "?"
