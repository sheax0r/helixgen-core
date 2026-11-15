"""CLI venue paths that need a real close IR + ROOM IR (corpus library)."""
import json

import pytest
from click.testing import CliRunner

from helixgen import tone_meta
from helixgen.cli import cli
from helixgen.hsp import read_hsp
from tests.test_venue import CLOSE, ROOM, _chain, _long, irs, lib, needs_sf  # noqa: F401


def _run(*args):
    return CliRunner().invoke(cli, list(args))


def _write(tmp_path, blocks, name="r.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"name": "Probe", "paths": [{"blocks": blocks}]}))
    return p


def _gen(lib, irs, recipe, *extra, descriptor="Probe"):
    return _run("generate", str(recipe), "--library", str(lib.root),
                "--irs-dir", str(irs.irs_dir), "--descriptor", descriptor, *extra)


@pytest.fixture(autouse=True)
def _no_default_venue(monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)


def _meta(slug="probe"):
    return tone_meta.load_tone_meta(slug)


@needs_sf
def test_default_venue_does_not_reauthor_but_explicit_venue_makes_a_variant(
        lib, irs, tmp_path, monkeypatch):
    irs.save()
    rec = _write(tmp_path, _chain())
    assert _gen(lib, irs, rec).exit_code == 0          # plain, bare variant
    before = _meta().variants
    monkeypatch.setenv("HELIXGEN_DEFAULT_VENUE", "headphones")
    r = _gen(lib, irs, rec)
    assert r.exit_code != 0 and "refusing to overwrite" in r.output
    assert _meta().variants == before and list(before) == ["generic"]
    r = _gen(lib, irs, rec, "--venue", "amp-in")
    assert r.exit_code == 0, r.output
    assert sorted(_meta().variants) == ["generic", "generic@amp-in"]


@needs_sf
def test_default_headphones_on_a_full_chain_still_writes_the_tone(
        lib, irs, tmp_path, monkeypatch):
    irs.save()
    monkeypatch.setenv("HELIXGEN_DEFAULT_VENUE", "headphones")
    r = _gen(lib, irs, _write(tmp_path, _long(8)))      # 12 blocks: not even Tile fits
    assert r.exit_code == 0, r.output
    assert "chain left unchanged" in r.output
    v = _meta().variants["generic"]
    assert v.venue == "headphones" and v.venue_detail["treatment"] == "none"


@needs_sf
def test_headphones_end_to_end_then_direct_and_fork(lib, irs, tmp_path):
    from helixgen import home
    irs.save()
    rec = _write(tmp_path, _chain())
    assert _gen(lib, irs, rec, "--venue", "direct", descriptor="Plain").exit_code == 0
    plain = read_hsp(home.tones_dir() / "plain.hsp")["preset"]["flow"]

    r = _gen(lib, irs, rec, "--venue", "headphones")
    assert r.exit_code == 0, r.output
    v = _meta().variants["generic"]
    assert v.venue == "headphones"
    assert v.venue_detail["treatment"] == "room-ir" and v.venue_detail["room_ir"] == ROOM
    hp = read_hsp(home.tones_dir() / "probe.hsp")
    flow0 = hp["preset"]["flow"][0]
    assert any(k[1:].isdigit() and int(k[1:]) >= 14 and b["slot"][0].get("irhash") == ROOM
               for k, b in flow0.items() if isinstance(b, dict) and b.get("slot"))
    assert "ROOM MIC" in json.dumps(hp["preset"]["sources"])

    # fork from the headphones variant: no cab, no room stage
    r = _run("library", "fork", "probe", "--venue", "amp-in", "--library", str(lib.root),
             "--irs-dir", str(irs.irs_dir))
    assert r.exit_code == 0, r.output
    fork = _meta().variants["generic@amp-in"]
    blob = json.dumps(read_hsp(home.helixgen_home() / "library" / fork.hsp))
    assert ROOM not in blob and CLOSE not in blob and "HD2_EQParametric" in blob

    r = _run("library", "venue", str(home.tones_dir() / "probe.hsp"), "direct", "--library", str(lib.root),
             "--irs-dir", str(irs.irs_dir))
    assert r.exit_code == 0, r.output
    assert read_hsp(home.tones_dir() / "probe.hsp")["preset"]["flow"] == plain


@needs_sf
def test_in_place_venue_clears_normalized_and_fork_does_not_inherit(lib, irs, tmp_path):
    from helixgen import home
    irs.save()
    assert _gen(lib, irs, _write(tmp_path, _chain())).exit_code == 0
    m = _meta()
    m.variants["generic"].normalized = {"mike": {"gain_db": 1.0}}
    tone_meta.save_tone_meta(m)
    r = _run("library", "fork", "probe", "--venue", "amp-in", "--library", str(lib.root))
    assert r.exit_code == 0, r.output
    assert _meta().variants["generic@amp-in"].normalized is None
    assert _meta().variants["generic"].normalized == {"mike": {"gain_db": 1.0}}
    r = _run("library", "venue", str(home.tones_dir() / "probe.hsp"), "amp-in",
             "--library", str(lib.root))
    assert r.exit_code == 0, r.output
    assert _meta().variants["generic"].normalized is None


@needs_sf
def test_room_flags_warn_when_no_venue_applies(lib, irs, tmp_path):
    irs.save()
    rec = _write(tmp_path, _chain())
    r = _gen(lib, irs, rec, "--room", "tile")
    assert r.exit_code == 0 and "only apply to --venue headphones" in r.output
    out = tmp_path / "o.hsp"
    r = _run("generate", str(rec), "--library", str(lib.root), "--irs-dir",
             str(irs.irs_dir), "-o", str(out), "--room-ir", ROOM)
    assert r.exit_code == 0 and "only apply to --venue headphones" in r.output


# --- headphones High Cut ceiling (flag + preference + retrofit) --------------

def _ir_hc(hp, irhash):
    flow0 = hp["preset"]["flow"][0]
    for b in flow0.values():
        if isinstance(b, dict) and b.get("slot") and b["slot"][0].get("irhash") == irhash:
            w = (b["slot"][0].get("params") or {}).get("HighCut")
            return w["value"] if isinstance(w, dict) else w
    return None


@needs_sf
def test_room_highcut_off_leaves_both_mics_wide_open(lib, irs, tmp_path):
    from helixgen import home
    irs.save()
    rec = _write(tmp_path, _chain())
    assert _gen(lib, irs, rec, "--venue", "headphones", "--room-highcut", "off").exit_code == 0
    hp = read_hsp(home.tones_dir() / "probe.hsp")
    assert _ir_hc(hp, CLOSE) == 20100.0 and _ir_hc(hp, ROOM) == 20100.0


@needs_sf
def test_room_highcut_defaults_to_pref_and_flag_overrides(lib, irs, tmp_path):
    from helixgen import home
    irs.save()
    rec = _write(tmp_path, _chain())
    assert _gen(lib, irs, rec, "--venue", "headphones", descriptor="Dfl").exit_code == 0
    hp = read_hsp(home.tones_dir() / "dfl.hsp")
    assert _ir_hc(hp, CLOSE) == 8000.0 and _ir_hc(hp, ROOM) == 8000.0   # preference default
    assert _gen(lib, irs, rec, "--venue", "headphones", "--room-highcut", "6000",
                descriptor="Six").exit_code == 0
    hp = read_hsp(home.tones_dir() / "six.hsp")
    assert _ir_hc(hp, CLOSE) == 6000.0 and _ir_hc(hp, ROOM) == 6000.0


@needs_sf
def test_venue_retrofit_dry_run_then_apply(lib, irs, tmp_path):
    from helixgen import home
    irs.save()
    rec = _write(tmp_path, _chain())
    # an old-style variant: room mic wide open
    assert _gen(lib, irs, rec, "--venue", "headphones", "--room-highcut", "off").exit_code == 0
    probe = home.tones_dir() / "probe.hsp"
    assert _ir_hc(read_hsp(probe), ROOM) == 20100.0

    dry = _run("library", "venue-retrofit", "--library", str(lib.root),
               "--irs-dir", str(irs.irs_dir))
    assert dry.exit_code == 0, dry.output
    assert "Would update 1" in dry.output
    assert _ir_hc(read_hsp(probe), ROOM) == 20100.0            # nothing written

    ap = _run("library", "venue-retrofit", "--apply", "--library", str(lib.root),
              "--irs-dir", str(irs.irs_dir))
    assert ap.exit_code == 0, ap.output
    assert _ir_hc(read_hsp(probe), ROOM) == 8000.0 and _ir_hc(read_hsp(probe), CLOSE) == 8000.0
    assert _meta().variants["generic"].venue_detail["room_highcut"] == 8000.0
