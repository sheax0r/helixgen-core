"""Unit tests for the OSC encode/parse codec (no hardware)."""
from __future__ import annotations

import struct

import pytest

from helixgen.device.osc import osc_encode, parse_osc_message, osc_address


def test_roundtrip_int():
    buf = osc_encode("/foo", [("i", 42)])
    addr, args, nxt = parse_osc_message(buf)
    assert addr == "/foo"
    assert args == [("i", 42)]
    assert nxt == len(buf)


def test_roundtrip_float():
    buf = osc_encode("/f", [("f", 0.5)])
    addr, args, nxt = parse_osc_message(buf)
    assert addr == "/f"
    assert args[0][0] == "f"
    assert args[0][1] == pytest.approx(0.5)
    assert nxt == len(buf)


def test_roundtrip_int64():
    buf = osc_encode("/h", [("h", 2**40 + 7)])
    _addr, args, _ = parse_osc_message(buf)
    assert args == [("h", 2**40 + 7)]


def test_roundtrip_string():
    buf = osc_encode("/s", [("s", "Dream On")])
    addr, args, nxt = parse_osc_message(buf)
    assert addr == "/s"
    assert args == [("s", "Dream On")]
    assert nxt == len(buf)


def test_roundtrip_blob():
    payload = b"\x00\x01\x02\x03\x04"
    buf = osc_encode("/b", [("b", payload)])
    _addr, args, nxt = parse_osc_message(buf)
    assert args == [("b", payload)]
    assert nxt == len(buf)


def test_roundtrip_mixed():
    buf = osc_encode("/mix", [("i", 1000), ("f", 0.25), ("s", "hi"), ("h", 9), ("b", b"xyz")])
    addr, args, nxt = parse_osc_message(buf)
    assert addr == "/mix"
    assert args[0] == ("i", 1000)
    assert args[1][0] == "f" and args[1][1] == pytest.approx(0.25)
    assert args[2] == ("s", "hi")
    assert args[3] == ("h", 9)
    assert args[4] == ("b", b"xyz")
    assert nxt == len(buf)


def test_ibi_blob_offset_regression():
    """A 3-tag ",ibi" message: the comma counts toward the tag-block padding.

    tags="ibi" -> on-wire block is ","+"ibi"+"\\0" = 5 bytes padded to 8.
    A naive parser that pads len(tags)+1 (=4) mis-locates the blob. Verify the
    blob is read at the correct offset and both ints survive.
    """
    blob = bytes(range(13))  # 13 bytes -> exercises 4-byte length + padding
    buf = osc_encode("/GetX", [("i", 1000), ("b", blob), ("i", -1)])
    addr, args, nxt = parse_osc_message(buf)
    assert addr == "/GetX"
    assert args[0] == ("i", 1000)
    assert args[1] == ("b", blob)
    assert args[2] == ("i", -1)
    assert nxt == len(buf)


def test_blob_length_prefix_and_padding():
    """Blob is a 4-byte BE length then the bytes, padded to a 4-byte boundary."""
    payload = b"ABCDE"  # 5 bytes -> padded to 8 on the wire
    buf = osc_encode("/b", [("b", payload)])
    # address "/b\0\0" = 4 bytes, tag block ",b\0\0" = 4 bytes -> body at 8
    body_off = 8
    (blen,) = struct.unpack_from(">i", buf, body_off)
    assert blen == 5
    assert buf[body_off + 4: body_off + 4 + 5] == payload
    # total = 4 (addr "/b\0\0") + 4 (tags ",b\0\0") + 4 (len) + 8 (padded payload) = 20
    assert len(buf) == 20
    assert len(buf) % 4 == 0


def test_parse_returns_next_off_for_concatenated_messages():
    a = osc_encode("/one", [("i", 1)])
    b = osc_encode("/two", [("i", 2)])
    both = a + b
    addr1, args1, off1 = parse_osc_message(both, 0)
    assert (addr1, args1) == ("/one", [("i", 1)])
    assert off1 == len(a)
    addr2, args2, off2 = parse_osc_message(both, off1)
    assert (addr2, args2) == ("/two", [("i", 2)])
    assert off2 == len(both)


def test_message_with_no_args():
    buf = osc_encode("/ping", [])
    addr, args, _ = parse_osc_message(buf)
    assert addr == "/ping"
    assert args == []


def test_unknown_tag_rejected_on_encode():
    with pytest.raises(ValueError):
        osc_encode("/bad", [("z", 1)])


def test_osc_address_skips_binary_header():
    frame = b"\x00\x00" + osc_encode("/Head", [("i", 1)])
    assert osc_address(frame) == "/Head"
