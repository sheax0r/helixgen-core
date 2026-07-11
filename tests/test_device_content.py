"""Unit tests for the _sbepgsm content codec + msgpack blob helpers (no hardware)."""
from __future__ import annotations

import struct

import pytest

msgpack = pytest.importorskip("msgpack")

from helixgen.device.content import (  # noqa: E402
    CONTENT_DATA_MAGIC,
    MAGIC,
    decode_any,
    decode_blob,
    decode_content,
    encode_content,
    fourcc_to_str,
    is_content_blob,
    is_content_data,
    str_to_fourcc,
    to_content_data,
)


def test_fourcc_str_inverse():
    for code in ("cid_", "name", "cctp", "posi", "ABCD"):
        n = str_to_fourcc(code)
        assert isinstance(n, int)
        assert fourcc_to_str(n) == code


def test_str_to_fourcc_passthrough_non_4char():
    assert str_to_fourcc("toolong") == "toolong"
    assert str_to_fourcc(1234) == 1234


def test_fourcc_to_str_passthrough_non_printable():
    # 0 packs to \x00\x00\x00\x00 -> not printable -> unchanged int
    assert fourcc_to_str(0) == 0
    assert fourcc_to_str("x") == "x"


def test_is_content_blob():
    assert is_content_blob(MAGIC + b"whatever")
    assert not is_content_blob(b"nope not magic")


def test_encode_decode_content_roundtrip():
    obj = {
        "cid_": 904,
        "name": "Dream On",
        "cctp": 1000,
        "blks": [
            {"modl": 12345, "para": {"gain": 0.5, "levl": 0.8}},
            {"modl": 6789, "para": {"mix_": 0.25}},
        ],
    }
    blob = encode_content(obj)
    assert blob[:8] == MAGIC
    back = decode_content(blob)
    assert back == obj


def test_encode_content_structural_roundtrip_encode_of_decode():
    obj = {"root": {"chld": [1, 2, {"leaf": "v"}]}, "cctp": 1000}
    blob = encode_content(obj)
    assert encode_content(decode_content(blob)) == blob


def test_decode_content_rejects_non_magic():
    with pytest.raises(ValueError):
        decode_content(b"badmagic" + msgpack.packb({1: 2}))


def test_decode_blob_plain_msgpack_map():
    raw = msgpack.packb({"a": 1, "b": [1, 2, 3]}, use_bin_type=True)
    assert decode_blob(raw) == {"a": 1, "b": [1, 2, 3]}


def test_decode_blob_4byte_prefixed_map():
    inner = msgpack.packb({"x": 9}, use_bin_type=True)
    framed = struct.pack(">I", len(inner)) + inner
    assert decode_blob(framed) == {"x": 9}


def test_decode_blob_sbepgsm_content():
    obj = {"cid_": 1, "name": "P"}
    blob = encode_content(obj)
    assert decode_blob(blob) == obj


def test_decode_blob_empty():
    assert decode_blob(b"") is None


def test_to_content_data_swaps_magic_and_drops_hist():
    obj = {"cg__": {"a": 1}, "hist": 7, "pm__": [], "sfg_": {"flow": []}}
    sbe = encode_content(obj)                 # _sbepgsm + all keys
    assert sbe[:8] == MAGIC
    cd = to_content_data(sbe)
    assert cd[:8] == CONTENT_DATA_MAGIC
    assert is_content_data(cd)
    back = decode_any(cd)
    assert "hist" not in back                 # volatile key dropped
    assert back["cg__"] == {"a": 1}
    assert back["sfg_"] == {"flow": []}


def test_to_content_data_idempotent_on_stored():
    obj = {"cg__": {}, "pm__": [], "sfg_": {}}
    cd = to_content_data(encode_content(obj))
    assert to_content_data(cd) == cd          # already stored -> unchanged


def test_decode_any_handles_both_magics():
    obj = {"cg__": 1, "pm__": 2, "sfg_": 3}
    assert decode_any(encode_content(obj)) == {**obj, }
    assert decode_any(to_content_data(encode_content(obj))) == obj
