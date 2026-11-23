"""Decoder tests for the Matrix Mixer (`server.mixer.state`) value blob.

A synthetic blob exercises the exact normalization; the real hardware golden
(`tests/fixtures/mixer/mixer_state.blob`, Stadium XL fw 1.4.1) pins the decode
against actual device bytes.
"""
import struct
from pathlib import Path

import pytest

from helixgen.device import mixer

msgpack = pytest.importorskip("msgpack")

FIXTURE = Path(__file__).parent / "fixtures" / "mixer" / "mixer_state.blob"


def _u32(tag: str) -> int:
    return struct.unpack(">I", tag.encode("ascii"))[0]


def _k(d: dict) -> dict:
    """Pack a plain {tag: value} map into the device's uint32-4CC-keyed form,
    recursively — mirrors how the device serializes the mixer blob."""
    out = {}
    for tag, v in d.items():
        if isinstance(v, dict):
            v = _k(v)
        elif isinstance(v, list):
            v = [_k(x) if isinstance(x, dict) else x for x in v]
        out[_u32(tag)] = v
    return out


def _strip(name, vol=0.0, pan=0.0, mute=False, solo=False, enbl=-1, src=0):
    return {"name": name, "vol_": vol, "pan_": pan,
            "mute": mute, "solo": solo, "enbl": enbl, "src_": src}


def _layer(output, dst, master_db):
    return {
        "out_": {"name": output, "dst_": dst, "vol_": master_db, "mute": 0},
        "mute": 0, "solo": 0,
        "home": {"chan": [_strip("Path 1A"), _strip("Path 1B"),
                          _strip("Path 2A"), _strip("Path 2B")],
                 "mix_": _strip("Paths")},
        "song": {"chan": [_strip(f"Song {i}") for i in range(1, 9)],
                 "mix_": _strip("Song")},
        "nxus": [_strip(f"Nexus {i}") for i in range(1, 5)],
        "usbn": _strip("USB In"),
        "auxn": _strip("AUX In", enbl=0),      # greyed: no Aux routed
        "bt__": _strip("Bluetooth"),
        "clik": _strip("Click"),
        "cntn": _strip("Count"),
    }


def _synthetic_blob() -> bytes:
    val = {
        "atto": 3, "lom_": 0,
        "lmix": _layer("Link Mix", 0, 0.0),
        "lyrs": [_layer('1/4"', 1, -18.5),
                 _layer("XLR", 2, -22.2),
                 _layer("Phones", 3, -5.4)],
    }
    body = {"key_": mixer.MIXER_KEY, "type": "v", "val_": val}
    return b"lavppgsm" + msgpack.packb(_k(body), use_single_float=False)


def test_decode_synthetic_shape():
    d = mixer.decode_mixer_state(_synthetic_blob())
    assert [L["output"] for L in d["layers"]] == ['1/4"', "XLR", "Phones"]
    assert [L["dst"] for L in d["layers"]] == [1, 2, 3]
    assert d["link_mix"]["output"] == "Link Mix"
    assert d["link_mix"]["dst"] == 0

    qtr = d["layers"][0]
    assert qtr["master_db"] == pytest.approx(-18.5)
    # strip order: home 1A/1B/2A/2B + Paths, song 1..8 + Song, nexus 1..4,
    # usb, aux, bt, click, count = 5 + 9 + 4 + 5 = 23 rows.
    names = [c["name"] for c in qtr["channels"]]
    assert names[:5] == ["Path 1A", "Path 1B", "Path 2A", "Path 2B", "Paths"]
    assert names[5:14] == ["Song 1", "Song 2", "Song 3", "Song 4", "Song 5",
                           "Song 6", "Song 7", "Song 8", "Song"]
    assert names[14:] == ["Nexus 1", "Nexus 2", "Nexus 3", "Nexus 4",
                          "USB In", "AUX In", "Bluetooth", "Click", "Count"]
    # enbl: -1 -> enabled True; 0 -> disabled (the Aux strip here)
    assert qtr["channels"][0]["enabled"] is True
    aux = next(c for c in qtr["channels"] if c["name"] == "AUX In")
    assert aux["enabled"] is False


def test_decode_flags_and_values():
    val = {"atto": 3, "lom_": 0, "lmix": _layer("Link Mix", 0, 0.0),
           "lyrs": [_layer('1/4"', 1, 0.0)]}
    # mute one strip, solo another, set a pan
    val["lyrs"][0]["home"]["chan"][0] = _strip("Path 1A", vol=-3.5, pan=-0.7,
                                               mute=True)
    val["lyrs"][0]["home"]["chan"][1] = _strip("Path 1B", solo=True)
    body = {"key_": mixer.MIXER_KEY, "type": "v", "val_": val}
    blob = b"lavppgsm" + msgpack.packb(_k(body), use_single_float=False)
    d = mixer.decode_mixer_state(blob)
    c0, c1 = d["layers"][0]["channels"][0], d["layers"][0]["channels"][1]
    assert c0["mute"] is True and c0["solo"] is False
    assert c0["vol_db"] == pytest.approx(-3.5) and c0["pan"] == pytest.approx(-0.7)
    assert c1["solo"] is True and c1["mute"] is False


def test_bad_magic_rejected():
    with pytest.raises(ValueError, match="magic"):
        mixer.decode_mixer_state(b"garbage!" + b"\x80")


def test_missing_val_rejected():
    blob = b"lavppgsm" + msgpack.packb(_k({"key_": "x", "type": "v"}))
    with pytest.raises(ValueError, match="val_"):
        mixer.decode_mixer_state(blob)


@pytest.mark.skipif(not FIXTURE.exists(), reason="no device golden fixture")
def test_decode_real_fixture():
    d = mixer.decode_mixer_state(FIXTURE.read_bytes())
    # three per-output layers in order, plus the Link Mix.
    assert [L["output"] for L in d["layers"]] == ['1/4"', "XLR", "Phones"]
    assert [L["dst"] for L in d["layers"]] == [1, 2, 3]
    assert d["link_mix"] is not None and d["link_mix"]["output"] == "Link Mix"
    for L in d["layers"]:
        assert isinstance(L["master_db"], float)
        names = [c["name"] for c in L["channels"]]
        # the four Home paths lead every layer
        assert names[:4] == ["Path 1A", "Path 1B", "Path 2A", "Path 2B"]
        # every strip carries the full field set
        for c in L["channels"]:
            assert set(c) >= {"name", "vol_db", "pan", "mute", "solo", "enabled"}
            assert isinstance(c["mute"], bool) and isinstance(c["enabled"], bool)
