"""CLI surface for venue variants: generate --venue, library fork --venue,
library venue, and venue shown by describe / library show."""
import json

import pytest
from click.testing import CliRunner

from helixgen import tone_meta
from helixgen.cli import cli
from helixgen.hsp import read_hsp
from helixgen.library import Block


@pytest.fixture
def lib(hsp_library):
    hsp_library.save_block(Block(
        model_id="HD2_EQParametric", category="eq", display_name="Parametric Mono",
        params={"LowCut": {"type": "float"}, "LowCutSlope": {"type": "int"},
                "HighCut": {"type": "float"}, "HighCutSlope": {"type": "int"}},
        exemplar={"@model": "HD2_EQParametric", "@type": "fx", "@enabled": True,
                  "LowCut": 19.9, "LowCutSlope": 0, "HighCut": 20100.0,
                  "HighCutSlope": 0},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-11-12"}))
    hsp_library.rebuild_index()
    return hsp_library


@pytest.fixture
def recipe(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"name": "Probe", "paths": [{"blocks": [
        {"block": "Tube Drive", "params": {"Gain": 0.4}},
        {"block": "Brit Amp", "params": {"Drive": 0.7}}]}]}))
    return p


def _run(*args):
    return CliRunner().invoke(cli, list(args))


def _gen(lib, recipe, *extra):
    r = _run("generate", str(recipe), "--library", str(lib.root),
             "--descriptor", "Probe", *extra)
    assert r.exit_code == 0, r.output
    return r


def _snapshot(path):
    return {p: p.read_bytes() for p in path.rglob("*") if p.is_file()
            and ".git" not in p.parts}


def _meta():
    return tone_meta.load_tone_meta("probe")


def _has_eq(path):
    return "HD2_EQParametric" in json.dumps(read_hsp(path))


def test_generate_without_pref_or_flag_is_unchanged(lib, recipe, monkeypatch,
                                                    strip_provenance):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    _gen(lib, recipe)
    v = _meta().variants["generic"]
    assert v.venue is None and v.venue_detail is None
    from helixgen import home
    out = home.helixgen_home() / "legacy.hsp"
    r = _run("generate", str(recipe), "--library", str(lib.root), "-o", str(out))
    assert r.exit_code == 0, r.output
    # -o has no venue treatment, so equal bytes == the library flow is untouched
    assert (strip_provenance(read_hsp(home.tones_dir() / "probe.hsp"))
            == strip_provenance(read_hsp(out)))


def test_generate_uses_default_venue_and_records_it(lib, recipe, monkeypatch):
    monkeypatch.setenv("HELIXGEN_DEFAULT_VENUE", "amp-in")
    _gen(lib, recipe)
    from helixgen import home
    assert _meta().variants["generic"].venue == "amp-in"
    assert _has_eq(home.tones_dir() / "probe.hsp")


def test_generate_venue_direct_opts_out(lib, recipe, monkeypatch):
    monkeypatch.setenv("HELIXGEN_DEFAULT_VENUE", "amp-in")
    _gen(lib, recipe, "--venue", "direct")
    from helixgen import home
    assert _meta().variants["generic"].venue == "direct"
    assert not _has_eq(home.tones_dir() / "probe.hsp")


def test_generate_dash_o_with_venue_is_usage_error(lib, recipe, tmp_path):
    r = _run("generate", str(recipe), "--library", str(lib.root),
             "--venue", "amp-in", "-o", str(tmp_path / "o.hsp"))
    assert r.exit_code == 2
    assert not (tmp_path / "o.hsp").exists()


def test_generate_dash_o_ignores_default_venue(lib, recipe, tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_DEFAULT_VENUE", "amp-in")
    out = tmp_path / "o.hsp"
    r = _run("generate", str(recipe), "--library", str(lib.root), "-o", str(out))
    assert r.exit_code == 0, r.output
    assert not _has_eq(out)


def test_fork_venue_same_guitar_makes_suffixed_variant(lib, recipe, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    _gen(lib, recipe)
    r = _run("library", "fork", "probe", "--venue", "amp-in",
             "--library", str(lib.root))
    assert r.exit_code == 0, r.output
    m = _meta()
    assert sorted(m.variants) == ["generic", "generic@amp-in"]
    v = m.variants["generic@amp-in"]
    assert v.preset_name.endswith(" (Amp In)") and v.venue == "amp-in"
    assert v.hsp.endswith("probe-amp-in.hsp")
    assert v.forked_from["venue_from"] is None
    assert m.variants["generic"].venue is None


def test_fork_venue_onto_existing_target_errors_and_writes_nothing(
        lib, recipe, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    _gen(lib, recipe, "--venue", "amp-in")
    from helixgen import home
    before = _snapshot(home.helixgen_home())
    r = _run("library", "fork", "probe", "--venue", "amp-in",
             "--library", str(lib.root))
    assert r.exit_code == 1
    assert _snapshot(home.helixgen_home()) == before


def test_library_venue_in_place_keeps_key_file_and_name(lib, recipe, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    _gen(lib, recipe)
    before = _meta().variants["generic"]
    hsp, name = before.hsp, before.preset_name
    r = _run("library", "venue", "probe", "amp-in", "--library", str(lib.root))
    assert r.exit_code == 0, r.output
    m = _meta()
    assert list(m.variants) == ["generic"]
    v = m.variants["generic"]
    assert (v.hsp, v.preset_name, v.venue) == (hsp, name, "amp-in")
    from helixgen import home
    assert _has_eq(home.tones_dir() / "probe.hsp")
    assert "venue: amp-in" in r.output


def test_library_venue_same_venue_errors(lib, recipe, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    _gen(lib, recipe, "--venue", "amp-in")
    r = _run("library", "venue", "probe", "amp-in", "--library", str(lib.root))
    assert r.exit_code == 1 and "already amp-in" in r.output


def test_dry_run_writes_nothing(lib, recipe, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    _gen(lib, recipe)
    from helixgen import home
    before = _snapshot(home.helixgen_home())
    for args in (["library", "venue", "probe", "amp-in", "--dry-run"],
                 ["library", "fork", "probe", "--venue", "amp-in", "--dry-run"]):
        r = _run(*args, "--library", str(lib.root))
        assert r.exit_code == 0, r.output
        assert "venue: amp-in" in r.output
        assert _snapshot(home.helixgen_home()) == before


def test_describe_and_show_print_venue(lib, recipe, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    _gen(lib, recipe, "--venue", "amp-in")
    d = _run("describe", "probe")
    assert d.exit_code == 0 and "venue: amp-in" in d.output
    s = _run("library", "show", "probe")
    assert s.exit_code == 0 and "venue amp-in" in s.output


def test_library_venue_restores_hsp_when_metadata_save_fails(lib, recipe, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    _gen(lib, recipe)
    from helixgen import home
    hsp = home.tones_dir() / "probe.hsp"
    before = hsp.read_bytes()
    hsp.chmod(0o644)
    mode = hsp.stat().st_mode

    def boom(_meta):
        raise OSError("disk full")
    monkeypatch.setattr(tone_meta, "save_tone_meta", boom)
    r = _run("library", "venue", "probe", "amp-in", "--library", str(lib.root))
    assert r.exit_code != 0
    assert hsp.read_bytes() == before
    assert hsp.stat().st_mode == mode
    assert not list(home.tones_dir().glob("*.tmp"))


def test_fork_from_suffixed_generic_variant(lib, recipe, monkeypatch):
    from helixgen import guitars, naming
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    guitars.save_profile(guitars.GuitarProfile(
        name="Ibanez Prestige", short_name="Ibanez Prestige", type="guitar",
        active=False, pickups="HSH", construction=None, character_md=None))
    _gen(lib, recipe)
    r = _run("library", "fork", "probe", "--venue", "direct", "--library", str(lib.root))
    assert r.exit_code == 0, r.output
    name = _meta().variants["generic@direct"].preset_name
    r = _run("library", "fork", name, "--venue", "amp-in", "--library", str(lib.root))
    assert r.exit_code == 0, r.output
    v = _meta().variants["generic@amp-in"]
    assert v.preset_name == naming.display_name(
        descriptor="Probe", guitar_short=None,
        venue_suffix=naming.VENUE_SUFFIX["amp-in"])
    assert v.hsp.endswith(f"{naming.variant_slug('probe', None, 'amp-in')}.hsp")


def test_fork_records_non_null_venue_from(lib, recipe, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    _gen(lib, recipe, "--venue", "direct")
    r = _run("library", "fork", "probe", "--venue", "amp-in", "--library", str(lib.root))
    assert r.exit_code == 0, r.output
    assert _meta().variants["generic@amp-in"].forked_from["venue_from"] == "direct"


def test_room_flags_warn_when_venue_is_not_headphones(lib, recipe, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    r = _run("generate", str(recipe), "--library", str(lib.root),
             "--descriptor", "Probe", "--venue", "amp-in", "--room", "tile")
    assert r.exit_code == 0, r.output
    assert "only apply to --venue headphones" in r.output


def test_library_venue_rewrite_keeps_file_mode(lib, recipe, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)
    _gen(lib, recipe)
    from helixgen import home
    hsp = home.tones_dir() / "probe.hsp"
    hsp.chmod(0o644)
    r = _run("library", "venue", "probe", "amp-in", "--library", str(lib.root))
    assert r.exit_code == 0, r.output
    assert hsp.stat().st_mode & 0o777 == 0o644
