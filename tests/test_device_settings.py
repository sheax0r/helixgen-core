"""Codec tests for device global-settings (property) blobs.

Golden blobs captured from a live Stadium XL (v1.3.2.9805) on 2026-07-13 — see
``docs/superpowers/specs/2026-07-13-global-settings-re-findings.md``.
"""
import pytest

msgpack = pytest.importorskip("msgpack")

from helixgen.device import settings as S

# --- golden blobs (hex) captured off the device ---------------------------

# /PropertyValueSet payload the app sent when setting preset.tempo.bpm = 132.0
APP_VALUE_BLOB_132 = bytes.fromhex(
    "6c6176707067736d83ce6b65795fb07072657365742e74656d706f2e62706d"
    "ce74797065a166ce76616c5fcb4060800000000000")

# /getPropertyValue blob for global.tuner.type (enum int, current = 0)
VAL_TUNER_TYPE = bytes.fromhex(
    "6c6176707067736d83ce6b65795fb1676c6f62616c2e74756e65722e74797065"
    "ce74797065a169ce76616c5f00")

# /keyPropertyDefinition blob for global.tuner.type (enum Needle/Strobe)
DEF_TUNER_TYPE = bytes.fromhex(
    "666564707067736d8ace64697370a0ce6476616c83ce6b65795fb1676c6f6261"
    "6c2e74756e65722e74797065ce74797065a169ce76616c5f01ce69645f5fcce3"
    "ce6e616d65aa54756e65722054797065ce73687274a0ce7479706500ce756e74"
    "730fce766d617801ce766d696e00ce766e6d6592a64e6565646c65a65374726f"
    "6265")

# /keyPropertyDefinition blob for global.midi.channel (int 1..16, default 1)
DEF_MIDI_CH = bytes.fromhex(
    "666564707067736d8ace64697370a0ce6476616c83ce6b65795fb3676c6f6261"
    "6c2e6d6964692e6368616e6e656cce74797065a169ce76616c5f01ce69645f5f"
    "63ce6e616d65b3476c6f62616c204d4944490a4368616e6e656cce73687274a0"
    "ce7479706500ce756e747302ce766d617810ce766d696e01ce766e6d6590")


def test_encode_value_blob_matches_app_bytes():
    """Byte-for-byte identical to what HX Edit put on the wire."""
    got = S.encode_value_blob("preset.tempo.bpm", "f", 132.0)
    assert got == APP_VALUE_BLOB_132


def test_encode_value_blob_roundtrips():
    blob = S.encode_value_blob("global.midi.channel", "i", 7)
    pv = S.decode_value_blob(blob)
    assert pv.key == "global.midi.channel"
    assert pv.type == "i"
    assert pv.value == 7


def test_encode_rejects_bad_type():
    with pytest.raises(ValueError):
        S.encode_value_blob("x", "s", "nope")


def test_decode_value_blob_enum_current():
    pv = S.decode_value_blob(VAL_TUNER_TYPE)
    assert pv.key == "global.tuner.type"
    assert pv.type == "i"
    assert pv.value == 0


def test_decode_property_def_enum():
    d = S.decode_property_def(DEF_TUNER_TYPE)
    assert d.key == "global.tuner.type"
    assert d.name == "Tuner Type"
    assert d.type == "i"
    assert d.vmin == 0 and d.vmax == 1
    assert d.enum == ["Needle", "Strobe"]
    assert d.default == 1
    assert d.id == 227


def test_decode_property_def_int_range():
    d = S.decode_property_def(DEF_MIDI_CH)
    assert d.key == "global.midi.channel"
    assert d.name == "Global MIDI Channel"   # newline collapsed
    assert d.type == "i"
    assert d.vmin == 1 and d.vmax == 16
    assert d.enum == []
    assert d.default == 1


def test_decode_rejects_wrong_magic():
    with pytest.raises(ValueError):
        S.decode_value_blob(DEF_TUNER_TYPE)     # def magic, not value


def test_coerce_enum_by_label_and_index():
    d = S.decode_property_def(DEF_TUNER_TYPE)
    assert S.coerce_value(d, "Strobe") == 1
    assert S.coerce_value(d, "needle") == 0     # case-insensitive
    assert S.coerce_value(d, "1") == 1          # bare index
    with pytest.raises(ValueError):
        S.coerce_value(d, "Sitar")
    with pytest.raises(ValueError):
        S.coerce_value(d, "9")                  # out of range


def test_coerce_int_range_validation():
    d = S.decode_property_def(DEF_MIDI_CH)
    assert S.coerce_value(d, "16") == 16
    with pytest.raises(ValueError):
        S.coerce_value(d, "0")                  # below vmin=1
    with pytest.raises(ValueError):
        S.coerce_value(d, "17")                 # above vmax=16
    with pytest.raises(ValueError):
        S.coerce_value(d, "x")                  # not an int


def test_render_value_enum_label():
    d = S.decode_property_def(DEF_TUNER_TYPE)
    assert S.render_value(d, 1) == "Strobe (1)"
    assert S.render_value(d, 0) == "Needle (0)"


# --- catalog integrity (offline) ------------------------------------------

def test_pages_catalog_loads_and_is_disjoint():
    pages = S.pages()
    assert set(pages) >= {"ins-outs", "midi", "tuner", "tempo-click",
                          "switches-pedals", "displays", "date-time"}
    seen = {}
    for page, keys in pages.items():
        for k in keys:
            assert k.startswith("global."), k
            assert k not in seen, f"{k} in both {seen.get(k)} and {page}"
            seen[k] = page
    assert len(seen) == len(S.all_keys()) >= 150


def test_page_for_key_and_keys_for_page():
    assert S.page_for_key("global.tuner.type") == "tuner"
    assert "global.tuner.type" in S.keys_for_page("tuner")
    assert S.page_for_key("global.nonexistent.key") is None


def test_keys_for_unknown_page_raises():
    with pytest.raises(KeyError):
        S.keys_for_page("nope")


# --- hardening (adversarial-review fixes) ---------------------------------

def test_coerce_rejects_non_finite_float():
    d = S.decode_property_def(DEF_MIDI_CH)._replace(type="f", vmin=None, vmax=None,
                                                    enum=[])
    for bad in ("nan", "inf", "-inf", "1e999"):
        with pytest.raises(ValueError):
            S.coerce_value(d, bad)


def test_coerce_rejects_huge_int():
    d = S.decode_property_def(DEF_MIDI_CH)._replace(vmin=None, vmax=None)
    with pytest.raises(ValueError):
        S.coerce_value(d, "99999999999999999999999")


def test_encode_rejects_non_finite_and_non_integral():
    with pytest.raises(ValueError):
        S.encode_value_blob("k", "f", float("inf"))
    with pytest.raises(ValueError):
        S.encode_value_blob("k", "i", 1.5)
    # integral float is fine for an int prop
    assert S.decode_value_blob(S.encode_value_blob("k", "i", 3.0)).value == 3


def test_guard_key_refuses_self_severing_keys():
    for k in ("global.wifi.enable", "global.remote.access"):
        with pytest.raises(ValueError):
            S.guard_key(k)
    S.guard_key("global.tuner.type")   # safe key: no raise


def test_decode_rejects_non_map_body():
    import msgpack
    bad = S.VALUE_MAGIC + msgpack.packb([1, 2, 3])
    with pytest.raises(ValueError):
        S.decode_value_blob(bad)


def test_decode_def_survives_non_str_name_and_missing_dval():
    import msgpack, struct
    u32 = lambda s: struct.unpack(">I", s.encode())[0]
    blob = S.DEF_MAGIC + msgpack.packb({u32("name"): 5, u32("key_"): "x.y"})
    d = S.decode_property_def(blob)          # must not raise
    assert d.name == "" and d.type == "f" and d.default is None and d.enum == []


def test_pages_returns_defensive_copy():
    p = S.pages()
    p["tuner"].append("global.bogus")
    assert "global.bogus" not in S.pages()["tuner"]


def test_catalog_excludes_connectivity_footguns():
    keys = set(S.all_keys())
    for k in S.DANGEROUS_KEYS:
        assert k not in keys
    assert "wireless" not in S.pages()
