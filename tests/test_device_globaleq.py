"""Global EQ codec/catalog tests.

The golden blobs in ``tests/fixtures/globaleq/`` are the exact bytes the Helix
Stadium desktop app sent for two edits (captured 2026-07-14): the low-cut corner
frequency set to 26 Hz, and the low-cut band disabled. The encoder must
reproduce them byte-for-byte.
"""
import os

import pytest

from helixgen.device import globaleq

pytest.importorskip("msgpack")

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "globaleq")


def _golden(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def test_encode_matches_app_bytes_freq():
    # app: dsp.globaleq.qtr.lowcut.freq -> {parm:2, valu:26.0}
    blob = globaleq.encode_value_blob("qtr", "lowcut", "freq", 26.0)
    assert blob == _golden("freq_26.blob")


def test_encode_matches_app_bytes_enable_false():
    # app: dsp.globaleq.qtr.lowcut.enable -> {parm:1, valu:false}
    blob = globaleq.encode_value_blob("qtr", "lowcut", "enable", False)
    assert blob == _golden("enable_false.blob")


def test_encode_starts_with_property_magic():
    blob = globaleq.encode_value_blob("xlr", "mid", "gain", 3.5)
    assert blob[:8] == globaleq.VALUE_MAGIC


def test_key_for_band_and_level():
    assert globaleq.key_for("qtr", "lowcut", "freq") == "dsp.globaleq.qtr.lowcut.freq"
    # level has no band segment
    assert globaleq.key_for("pho", "", "level") == "dsp.globaleq.pho.level"


def test_case_insensitive_inputs():
    a = globaleq.encode_value_blob("QTR", "LowCut", "FREQ", 26.0)
    assert a == _golden("freq_26.blob")


def test_enable_accepts_bool_strings():
    assert globaleq.encode_value_blob("qtr", "lowcut", "enable", "off") == \
        _golden("enable_false.blob")


@pytest.mark.parametrize("output", ["qtr", "xlr", "pho"])
def test_all_outputs_encode(output):
    assert globaleq.encode_value_blob(output, "mid", "freq", 1000.0)[:8] == \
        globaleq.VALUE_MAGIC


def test_slope_is_int_encoded():
    import msgpack
    blob = globaleq.encode_value_blob("qtr", "lowcut", "slope", 2)
    body = msgpack.unpackb(blob[8:], raw=False, strict_map_key=False)
    inner = body[globaleq.K_VAL]
    assert inner[globaleq.K_PARM] == 5
    assert inner[globaleq.K_VALU] == 2 and isinstance(inner[globaleq.K_VALU], int)


def test_unknown_output_band_param_raise():
    with pytest.raises(ValueError):
        globaleq.encode_value_blob("bogus", "mid", "freq", 100)
    with pytest.raises(ValueError):
        globaleq.encode_value_blob("qtr", "bogus", "freq", 100)
    with pytest.raises(ValueError):
        globaleq.encode_value_blob("qtr", "mid", "bogus", 100)


def test_param_not_valid_for_band_raises():
    # low-cut is a filter: it has slope, not gain or Q
    with pytest.raises(ValueError):
        globaleq.encode_value_blob("qtr", "lowcut", "gain", 3.0)
    with pytest.raises(ValueError):
        globaleq.encode_value_blob("qtr", "highcut", "q", 0.7)
    # shelves have gain but not Q
    with pytest.raises(ValueError):
        globaleq.encode_value_blob("qtr", "lowshelf", "q", 0.7)


def test_non_numeric_freq_raises():
    with pytest.raises(ValueError):
        globaleq.encode_value_blob("qtr", "mid", "freq", "loud")


def test_level_with_a_band_is_rejected():
    # 'level' is the per-output level; pairing it with a real band is a mistake
    with pytest.raises(ValueError):
        globaleq.key_for("qtr", "low", "level")
    # but '-'/'' band is fine
    assert globaleq.key_for("qtr", "-", "level") == "dsp.globaleq.qtr.level"


def test_cli_set_accepts_negative_value(monkeypatch):
    # Regression: a leading-'-' value (an EQ cut) must not be parsed as an option.
    from click.testing import CliRunner
    from helixgen import cli as cli_mod

    sent = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def set_globaleq(self, output, band, param, value):
            sent.update(output=output, band=band, param=param, value=value)
            return True

    import helixgen.device as devmod
    monkeypatch.setattr(devmod, "HelixClient", _FakeClient)
    r = CliRunner().invoke(
        cli_mod.cli,
        ["device", "globaleq", "set", "qtr", "low", "gain", "-3.5"])
    assert r.exit_code == 0, r.output
    assert sent["value"] == "-3.5" and sent["param"] == "gain"
    # and the output-level negative form from the docstring example
    r2 = CliRunner().invoke(
        cli_mod.cli,
        ["device", "globaleq", "set", "pho", "-", "level", "-2.0"])
    assert r2.exit_code == 0, r2.output
    assert sent["value"] == "-2.0" and sent["param"] == "level" and sent["band"] == ""


def test_catalog_shape():
    cat = globaleq.catalog()
    # 3 outputs * (7 bands + 1 level row) = 24
    assert len(cat) == 24
    lowcut = [r for r in cat if r["band"] == "lowcut" and r["output"] == "qtr"][0]
    assert lowcut["band_index"] == 0
    assert "slope" in lowcut["params"] and "gain" not in lowcut["params"]
    mid = [r for r in cat if r["band"] == "mid" and r["output"] == "qtr"][0]
    assert set(mid["params"]) == {"enable", "freq", "gain", "q"}
