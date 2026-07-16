"""Tests for helixgen.migrate: plan_migration / run_migration / migrate_instruments.

The migration is the highest-risk surface in PR 2 (data movement + idempotence),
so these tests hammer:

- name-inference in ``plan_migration`` (known-instrument trailing segment ->
  guitar; unknown trailing token -> descriptor fallback; instruments recorded).
- ``run_migration`` moving ``.hsp`` files into ``tones_dir()`` under the new
  slug, rewriting ``meta.name``, folding a sibling ``.md`` into
  ``description_md``, writing the ToneMeta JSON, re-keying the manifest, and
  copying + rewriting IR mappings.
- IDEMPOTENCE (re-run == all skips, no dup files, mapping.json byte-identical).
- SLUG COLLISION (two tones -> one slug -> recorded, no overwrite).
- DATA SAFETY (a per-tone error is recorded and does not abort the run).
- ``migrate_instruments`` seeds guitar profiles from prefs.instruments and
  strips the retired ``instruments`` / ``preset_output_dir`` prefs keys.

Git identity is isolated like ``tests/test_gitops.py`` so a dev machine's git
config can't leak into the tmp home repos, and the module skips when git is
absent (the auto-commit paths shell out to git).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helixgen import guitars, home, migrate, naming, tone_meta
from helixgen.hsp import read_hsp, write_hsp
from helixgen.ir import IrMapping
from helixgen.device.manifest import SetlistManifest

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available on PATH"
)


@pytest.fixture(autouse=True)
def _isolated_git_env(tmp_path, monkeypatch):
    """Keep a real user's git config / global gitignore out of the tmp home."""
    monkeypatch.delenv("HELIXGEN_GIT_COMMIT_TONES", raising=False)
    fake_home = tmp_path / "_fake_home_for_git"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "gitconfig-does-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _write_hsp(path: Path, name: str) -> None:
    write_hsp(path, {"meta": {"name": name}, "preset": {"flow": []}})


def _write_prefs(home_dir: Path, monkeypatch, instruments: list[dict]) -> None:
    prefs = home_dir / "preferences.json"
    prefs.write_text(json.dumps({"schema_version": 1, "instruments": instruments}))
    monkeypatch.setenv("HELIXGEN_PREFS", str(prefs))


def _register(hsp_path: Path, *, source: str = "authored", slot=None) -> str:
    m = SetlistManifest.load()
    name = m.register_tone(hsp_path, source=source)
    if slot is not None:
        m.tones[name]["slot"] = slot
    m.save()
    return name


# ---------------------------------------------------------------------------
# plan_migration inference
# ---------------------------------------------------------------------------


def test_plan_infers_guitar_from_trailing_instrument(tmp_home, monkeypatch):
    _write_prefs(tmp_home, monkeypatch, [{"name": "Les Paul Jr", "type": "guitar"}])
    exports = tmp_home / "exports"
    exports.mkdir()
    hsp = exports / "old1.hsp"
    _write_hsp(hsp, "White Limo Lead - Les Paul Jr")
    _register(hsp)

    plan = migrate.plan_migration()
    entry = next(t for t in plan["tones"] if t["name"] == "White Limo Lead - Les Paul Jr")
    assert entry["guitar"] == "Les Paul Jr"
    assert entry["descriptor"] == "White Limo Lead"
    assert entry["artist"] is None and entry["song"] is None
    assert entry["new_slug"] == "white-limo-lead-les-paul-jr"


def test_plan_infers_artist_song_with_two_leading_segments(tmp_home, monkeypatch):
    _write_prefs(tmp_home, monkeypatch, [{"name": "Les Paul Jr", "type": "guitar"}])
    exports = tmp_home / "exports"
    exports.mkdir()
    hsp = exports / "old.hsp"
    _write_hsp(hsp, "Foo Fighters - White Limo - Les Paul Jr")
    _register(hsp)

    plan = migrate.plan_migration()
    entry = plan["tones"][0]
    assert entry["artist"] == "Foo Fighters"
    assert entry["song"] == "White Limo"
    assert entry["guitar"] == "Les Paul Jr"
    assert entry["descriptor"] is None


def test_plan_unknown_trailing_token_falls_back_to_descriptor(tmp_home, monkeypatch):
    _write_prefs(tmp_home, monkeypatch, [{"name": "Les Paul Jr", "type": "guitar"}])
    exports = tmp_home / "exports"
    exports.mkdir()
    hsp = exports / "old.hsp"
    _write_hsp(hsp, "Song Title - Satriani")
    _register(hsp)

    plan = migrate.plan_migration()
    entry = plan["tones"][0]
    # "Satriani" is not a known instrument -> whole name is the descriptor.
    assert entry["guitar"] is None
    assert entry["descriptor"] == "Song Title - Satriani"
    assert entry["artist"] is None and entry["song"] is None


def test_plan_records_instruments_and_irs(tmp_home, monkeypatch):
    instruments = [{"name": "Les Paul Jr", "type": "guitar"},
                   {"name": "Jazzmaster", "type": "guitar"}]
    _write_prefs(tmp_home, monkeypatch, instruments)
    # register an IR
    pack = tmp_home / "packs" / "YA BOGN"
    pack.mkdir(parents=True)
    wav = pack / "cab.wav"
    wav.write_bytes(b"RIFFxxxxWAVE-fake-audio")
    mapping = IrMapping.load()
    mapping.register("deadbeef" * 4, wav)
    mapping.save()

    plan = migrate.plan_migration()
    assert [i["name"] for i in plan["instruments"]] == ["Les Paul Jr", "Jazzmaster"]
    assert len(plan["irs"]) == 1
    assert plan["irs"][0]["hash"] == "deadbeef" * 4


# ---------------------------------------------------------------------------
# run_migration: the happy path
# ---------------------------------------------------------------------------


def _build_two_tone_home(tmp_home, monkeypatch):
    _write_prefs(tmp_home, monkeypatch, [{"name": "Les Paul Jr", "type": "guitar"}])
    exports = tmp_home / "exports"
    exports.mkdir()
    h1 = exports / "a.hsp"
    _write_hsp(h1, "White Limo Lead - Les Paul Jr")
    (exports / "a.md").write_text("# White Limo\nThe full write-up.")
    _register(h1, slot="1A")
    h2 = exports / "b.hsp"
    _write_hsp(h2, "Warm Jazz Clean")
    _register(h2)
    return h1, h2


def test_run_migration_moves_folds_and_rekeys(tmp_home, monkeypatch):
    h1, h2 = _build_two_tone_home(tmp_home, monkeypatch)

    plan = migrate.plan_migration()
    summary = migrate.run_migration(plan)

    # .hsp moved under tones_dir, source gone (moved, not copied)
    dest1 = home.tones_dir() / "white-limo-lead-les-paul-jr.hsp"
    dest2 = home.tones_dir() / "warm-jazz-clean.hsp"
    assert dest1.exists() and dest2.exists()
    assert not h1.exists() and not h2.exists()

    # meta.name rewritten to the new display name
    assert read_hsp(dest1)["meta"]["name"] == "White Limo Lead - Les Paul Jr"

    # tone metadata JSON written, md folded into description_md
    meta1 = tone_meta.load_tone_meta("white-limo-lead")
    assert "les-paul-jr" in meta1.variants
    assert meta1.description_md is not None and "full write-up" in meta1.description_md
    assert meta1.descriptor == "White Limo Lead"

    # manifest re-keyed to new_name at new path, slot preserved, old key gone
    m = SetlistManifest.load()
    assert "White Limo Lead - Les Paul Jr" in m.tones
    rec = m.tones["White Limo Lead - Les Paul Jr"]
    assert rec["slot"] == "1A"
    assert Path(rec["path"]).resolve() == dest1.resolve()
    assert rec["content_hash"] is not None

    assert summary["tones"]["moved"]
    assert not summary["tones"]["errors"]


def test_run_migration_copies_and_rewrites_irs(tmp_home, monkeypatch):
    _write_prefs(tmp_home, monkeypatch, [])
    pack = tmp_home / "packs" / "YA BOGN"
    pack.mkdir(parents=True)
    wav = pack / "cab.wav"
    wav.write_bytes(b"RIFFxxxxWAVE-fake-audio")
    h = "abc123" * 5 + "de"  # 32 hex chars
    mapping = IrMapping.load()
    mapping.register(h, wav)
    mapping.save()

    plan = migrate.plan_migration()
    migrate.run_migration(plan)

    dest = home.library_irs_dir() / "ya-bogn" / "cab.wav"
    stub = home.library_irs_dir() / "ya-bogn" / "cab.json"
    assert dest.exists()
    assert wav.exists()  # COPIED, never moved
    stub_data = json.loads(stub.read_text())
    assert stub_data["schema"] == 1
    assert stub_data["irhash"] == h
    assert stub_data["imported_from"] == str(wav.resolve())

    # mapping rewritten to the library copy
    mapping2 = IrMapping.load()
    assert Path(mapping2.entries[h]).resolve() == dest.resolve()


# ---------------------------------------------------------------------------
# idempotence
# ---------------------------------------------------------------------------


def test_run_migration_is_idempotent(tmp_home, monkeypatch):
    _build_two_tone_home(tmp_home, monkeypatch)
    pack = tmp_home / "packs" / "YA BOGN"
    pack.mkdir(parents=True)
    wav = pack / "cab.wav"
    wav.write_bytes(b"RIFFxxxxWAVE-fake-audio")
    mapping = IrMapping.load()
    mapping.register("f00d" * 8, wav)
    mapping.save()

    migrate.run_migration(migrate.plan_migration())

    tones_after_first = sorted(p.name for p in home.tones_dir().glob("*.hsp"))
    irs_after_first = sorted(p.name for p in (home.library_irs_dir() / "ya-bogn").iterdir())
    mapping_bytes = (home.library_irs_dir() / "mapping.json").read_bytes()
    manifest_bytes = home.manifest_path().read_bytes()

    # second run: all skips, no churn
    summary2 = migrate.run_migration(migrate.plan_migration())
    assert summary2["tones"]["moved"] == []
    assert summary2["tones"]["skipped"]

    assert sorted(p.name for p in home.tones_dir().glob("*.hsp")) == tones_after_first
    assert sorted(p.name for p in (home.library_irs_dir() / "ya-bogn").iterdir()) == irs_after_first
    assert (home.library_irs_dir() / "mapping.json").read_bytes() == mapping_bytes
    assert home.manifest_path().read_bytes() == manifest_bytes


# ---------------------------------------------------------------------------
# slug collision
# ---------------------------------------------------------------------------


def test_run_migration_records_slug_collision_without_overwrite(tmp_home, monkeypatch):
    _write_prefs(tmp_home, monkeypatch, [])
    exports = tmp_home / "exports"
    exports.mkdir()
    h1 = exports / "a.hsp"
    _write_hsp(h1, "Warm Clean")
    _register(h1)
    h2 = exports / "b.hsp"
    _write_hsp(h2, "Warm  Clean")  # double space -> same slug "warm-clean"
    _register(h2)

    plan = migrate.plan_migration()
    # sanity: both map to the same slug
    slugs = [t["new_slug"] for t in plan["tones"]]
    assert slugs.count("warm-clean") == 2

    summary = migrate.run_migration(plan)
    assert summary["tones"]["collisions"]
    # both sources should be untouched (collision -> refuse), so no move happened
    dest = home.tones_dir() / "warm-clean.hsp"
    assert h1.exists() and h2.exists()
    assert not dest.exists()


# ---------------------------------------------------------------------------
# data safety: a per-tone error does not abort the run
# ---------------------------------------------------------------------------


def test_run_migration_continues_past_a_missing_source(tmp_home, monkeypatch):
    _write_prefs(tmp_home, monkeypatch, [])
    exports = tmp_home / "exports"
    exports.mkdir()
    good = exports / "good.hsp"
    _write_hsp(good, "Good Tone")
    _register(good)
    missing = exports / "missing.hsp"
    _write_hsp(missing, "Missing Tone")
    _register(missing)
    missing.unlink()  # source .hsp vanished after registration

    summary = migrate.run_migration(migrate.plan_migration())

    assert (home.tones_dir() / "good-tone.hsp").exists()
    assert any(e["name"] == "Missing Tone" for e in summary["tones"]["errors"])


# ---------------------------------------------------------------------------
# migrate_instruments: seed guitar profiles + strip retired prefs keys (Task 11)
# ---------------------------------------------------------------------------


def _write_prefs_full(home_dir, monkeypatch, data: dict) -> Path:
    prefs = home_dir / "preferences.json"
    prefs.write_text(json.dumps(data))
    monkeypatch.setenv("HELIXGEN_PREFS", str(prefs))
    return prefs


def test_migrate_instruments_seeds_four_profiles_and_strips_prefs(tmp_home, monkeypatch):
    prefs = _write_prefs_full(tmp_home, monkeypatch, {
        "schema_version": 1,
        "preset_output_dir": "~/presets",
        "instruments": [
            {"name": "Gibson Les Paul Junior", "short_name": "Les Paul Jr",
             "type": "guitar", "pickups": "one bridge P-90", "selector": "none",
             "active": False, "genres": ["punk"], "notes": "P-90 grind"},
            {"name": "Fender Stratocaster", "type": "guitar",
             "selector": "5-way", "genres": ["blues"]},
            {"name": "Fender Jazzmaster", "type": "guitar"},
            {"name": "Fender Precision Bass", "type": "bass"},
        ],
    })
    plan = migrate.plan_migration()
    # `preferences.Instrument` has no short_name field, so plan_migration can't
    # carry it -- the agent adds it when editing the plan. Simulate that for the
    # Les Paul entry; the others fall back to short_name = name.
    for entry in plan["instruments"]:
        if entry["name"] == "Gibson Les Paul Junior":
            entry["short_name"] = "Les Paul Jr"
    summary = migrate.migrate_instruments(plan)

    assert summary["status"] == "migrated"
    assert set(summary["profiles_created"]) == {
        "gibson-les-paul-junior", "fender-stratocaster",
        "fender-jazzmaster", "fender-precision-bass"}
    # four profile files on disk
    assert len(list(home.guitars_dir().glob("*.json"))) == 4

    lp = guitars.load_profile("gibson-les-paul-junior")
    assert lp.short_name == "Les Paul Jr"
    assert lp.character_md == "P-90 grind"          # notes -> character_md
    assert lp.controls[0].name == "pickup selector"  # selector -> control
    assert lp.controls[0].notes == "none"

    # prefs file had both retired keys removed
    assert set(summary["prefs_keys_removed"]) == {"instruments", "preset_output_dir"}
    on_disk = json.loads(prefs.read_text())
    assert "instruments" not in on_disk
    assert "preset_output_dir" not in on_disk
    assert on_disk["schema_version"] == 1  # other keys preserved


def test_migrate_instruments_is_idempotent(tmp_home, monkeypatch):
    _write_prefs_full(tmp_home, monkeypatch, {
        "schema_version": 1,
        "instruments": [{"name": "Les Paul Jr", "type": "guitar"}],
    })
    first = migrate.migrate_instruments(migrate.plan_migration())
    assert first["profiles_created"] == ["les-paul-jr"]

    # re-run: instruments already stripped from prefs, profile already exists
    second = migrate.migrate_instruments(migrate.plan_migration())
    assert second["profiles_created"] == []
    assert second["prefs_keys_removed"] == []
    assert len(list(home.guitars_dir().glob("*.json"))) == 1


def test_migrate_instruments_dry_run_writes_nothing(tmp_home, monkeypatch):
    prefs = _write_prefs_full(tmp_home, monkeypatch, {
        "schema_version": 1,
        "instruments": [{"name": "Les Paul Jr", "type": "guitar"}],
    })
    summary = migrate.migrate_instruments(migrate.plan_migration(), dry_run=True)
    # reports what WOULD happen...
    assert summary["profiles_created"] == ["les-paul-jr"]
    assert summary["prefs_keys_removed"] == ["instruments"]
    # ...but nothing was written
    assert not home.guitars_dir().exists() or not list(home.guitars_dir().glob("*.json"))
    assert "instruments" in json.loads(prefs.read_text())


# ---------------------------------------------------------------------------
# migrate_instruments: default_guitar reconciliation (Task 11 review FIX B)
# ---------------------------------------------------------------------------


def test_migrate_instruments_flags_unresolved_default_guitar(tmp_home, monkeypatch, capsys):
    """A pre-existing default_guitar set to a SHORT form no longer resolves once
    ``profile_from_instrument`` seeds ``short_name = name`` -- migrate must warn
    (STDERR) and record it, never crash, never silently rewrite."""
    _write_prefs_full(tmp_home, monkeypatch, {
        "schema_version": 1,
        "default_guitar": "Les Paul Jr",
        "instruments": [{"name": "Gibson Les Paul Junior", "type": "guitar"}],
    })
    summary = migrate.migrate_instruments(migrate.plan_migration())
    assert summary["default_guitar_unresolved"] == "Les Paul Jr"
    err = capsys.readouterr().err
    assert "default_guitar" in err and "Les Paul Jr" in err
    # default_guitar is NOT stripped/rewritten -- only warned about
    assert json.loads((tmp_home / "preferences.json").read_text())["default_guitar"] == "Les Paul Jr"


def test_migrate_instruments_resolvable_default_guitar_no_warning(tmp_home, monkeypatch, capsys):
    """A default_guitar that DOES resolve post-migration (equals the instrument
    name) produces no unresolved flag and no default_guitar warning."""
    _write_prefs_full(tmp_home, monkeypatch, {
        "schema_version": 1,
        "default_guitar": "Gibson Les Paul Junior",
        "instruments": [{"name": "Gibson Les Paul Junior", "type": "guitar"}],
    })
    summary = migrate.migrate_instruments(migrate.plan_migration())
    assert summary["default_guitar_unresolved"] is None
    assert "default_guitar" not in capsys.readouterr().err


def test_migrate_instruments_null_default_guitar_no_warning(tmp_home, monkeypatch, capsys):
    _write_prefs_full(tmp_home, monkeypatch, {
        "schema_version": 1,
        "instruments": [{"name": "Gibson Les Paul Junior", "type": "guitar"}],
    })
    summary = migrate.migrate_instruments(migrate.plan_migration())
    assert summary["default_guitar_unresolved"] is None
    assert "default_guitar" not in capsys.readouterr().err


def test_migrate_instruments_dry_run_flags_unresolved_default_writes_nothing(
        tmp_home, monkeypatch, capsys):
    """Dry-run reconciliation uses the WOULD-be-seeded profiles (in memory) so it
    reports the would-warn correctly while writing nothing."""
    _write_prefs_full(tmp_home, monkeypatch, {
        "schema_version": 1,
        "default_guitar": "Les Paul Jr",
        "instruments": [{"name": "Gibson Les Paul Junior", "type": "guitar"}],
    })
    summary = migrate.migrate_instruments(migrate.plan_migration(), dry_run=True)
    assert summary["default_guitar_unresolved"] == "Les Paul Jr"
    assert "default_guitar" in capsys.readouterr().err
    # nothing written
    assert not home.guitars_dir().exists() or not list(home.guitars_dir().glob("*.json"))


# ---------------------------------------------------------------------------
# dry-run mutates nothing
# ---------------------------------------------------------------------------


def test_run_migration_dry_run_mutates_nothing(tmp_home, monkeypatch):
    h1, h2 = _build_two_tone_home(tmp_home, monkeypatch)
    summary = migrate.run_migration(migrate.plan_migration(), dry_run=True)
    assert summary["dry_run"] is True
    assert h1.exists() and h2.exists()
    assert not (home.tones_dir() / "white-limo-lead-les-paul-jr.hsp").exists()


# ---------------------------------------------------------------------------
# migrated IR sidecar shape (ir_meta.scaffold, PR 3)


def test_migrated_ir_sidecar_has_full_irmeta_shape(tmp_home, monkeypatch):
    """After migration, each copied IR's sidecar is the full IrMeta shape
    (schema/irhash/imported_from + mix guessed, measured null), not the old
    minimal stub -- migrate now routes the scaffold through ir_meta."""
    from helixgen import ir_meta
    _write_prefs(tmp_home, monkeypatch, [])
    pack = tmp_home / "packs" / "YA BOGN"
    pack.mkdir(parents=True)
    wav = pack / "YA BOGN Mix 03.wav"
    wav.write_bytes(b"RIFFxxxxWAVE-fake-audio")
    h = "abc123" * 5 + "de"
    mapping = IrMapping.load()
    mapping.register(h, wav)
    mapping.save()

    migrate.run_migration(migrate.plan_migration())

    stub = home.library_irs_dir() / "ya-bogn" / "YA BOGN Mix 03.json"
    data = json.loads(stub.read_text())
    assert data["schema"] == 1
    assert data["irhash"] == h
    assert data["imported_from"] == str(wav.resolve())
    assert data["mix"] == "Mix 03"      # guessed from the filename
    assert data["measured"] is None     # NO numpy in core
    assert data["wav"] == "irs/ya-bogn/YA BOGN Mix 03.wav"



# ---------------------------------------------------------------------------
# FIX 1: the destructive prefs-key strip honors $HELIXGEN_HOME (never the real
# ~/.helixgen) even when $HELIXGEN_PREFS is unset.
# ---------------------------------------------------------------------------


def test_strip_deprecated_prefs_keys_honors_helixgen_home(tmp_home, monkeypatch):
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    # A sentinel standing in for the REAL home: it must stay untouched.
    real_home = tmp_home / "_real_home_guard"
    real_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: real_home))

    prefs = home.helixgen_home() / "preferences.json"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(json.dumps({
        "schema_version": 1,
        "instruments": [{"name": "X", "type": "guitar"}],
        "preset_output_dir": "~/presets",
    }))

    removed = migrate._strip_deprecated_prefs_keys(dry_run=False)

    assert set(removed) == {"instruments", "preset_output_dir"}
    on_disk = json.loads(prefs.read_text())
    assert "instruments" not in on_disk and "preset_output_dir" not in on_disk
    # the real-home sentinel was never written to
    assert not (real_home / ".helixgen" / "preferences.json").exists()


# ---------------------------------------------------------------------------
# M1: a malformed --plan (missing a required per-tone key) exits 1 cleanly
# ---------------------------------------------------------------------------


def test_migrate_plan_missing_key_is_clean_clickexception(tmp_home, monkeypatch):
    """An edited plan whose tone is missing ``new_slug`` must exit 1 with a
    ClickException naming the offender -- NOT crash with an uncaught KeyError."""
    from click.testing import CliRunner
    from helixgen.cli import cli

    _write_prefs(tmp_home, monkeypatch, [])
    plan = {
        "tones": [{"name": "Broken One", "path": "/tmp/x.hsp",
                   "logical": "broken-one", "new_name": "Broken One"}],  # new_slug missing
        "instruments": [],
        "irs": [],
    }
    plan_file = tmp_home / "plan.json"
    plan_file.write_text(json.dumps(plan))

    res = CliRunner().invoke(
        cli, ["library", "migrate", "--plan", str(plan_file)],
        catch_exceptions=False)
    assert res.exit_code == 1, res.output
    assert "new_slug" in res.output
    assert "Broken One" in res.output
    assert "Traceback" not in res.output


# ---------------------------------------------------------------------------
# M2: cross-pack IR basename collision -> distinct library copies, no aliasing
# ---------------------------------------------------------------------------


def test_migrate_irs_disambiguates_cross_pack_basename_collision(tmp_home, monkeypatch):
    """Two IRs sharing a WAV basename AND a slugified pack dir but with
    DIFFERENT content must each get their own library copy (no silent aliasing
    of the second onto the first's bytes)."""
    _write_prefs(tmp_home, monkeypatch, [])
    # Two distinct on-disk dirs that slugify to the SAME pack slug "ya-bogn".
    pack_a = tmp_home / "packs" / "YA BOGN"
    pack_b = tmp_home / "packs" / "YA_BOGN"
    pack_a.mkdir(parents=True)
    pack_b.mkdir(parents=True)
    wav_a = pack_a / "cab.wav"
    wav_b = pack_b / "cab.wav"
    wav_a.write_bytes(b"RIFFaaaaWAVE-content-A")
    wav_b.write_bytes(b"RIFFbbbbWAVE-content-B-totally-different")
    h_a = "a" * 32
    h_b = "b" * 32  # h_a sorts before h_b -> A copied first (natural), B disambiguated
    mapping = IrMapping.load()
    mapping.register(h_a, wav_a)
    mapping.register(h_b, wav_b)
    mapping.save()

    migrate.run_migration(migrate.plan_migration())

    mapping2 = IrMapping.load()
    dest_a = Path(mapping2.entries[h_a]).resolve()
    dest_b = Path(mapping2.entries[h_b]).resolve()

    assert dest_a != dest_b  # no aliasing: distinct library copies
    assert dest_a.read_bytes() == wav_a.read_bytes()
    assert dest_b.read_bytes() == wav_b.read_bytes()
    # both live under the shared pack slug dir
    lib_pack = home.library_irs_dir() / "ya-bogn"
    assert dest_a.parent == lib_pack.resolve()
    assert dest_b.parent == lib_pack.resolve()


# ---------------------------------------------------------------------------
# test net: old_name != new_name re-key (em-dash legacy name -> hyphen)
# ---------------------------------------------------------------------------


def test_run_migration_rekeys_when_name_changes(tmp_home, monkeypatch):
    """A legacy em-dash name migrates to a hyphen display name: the OLD manifest
    key must be gone, the NEW key present at the new path, and any setlist
    membership rewritten to the new key."""
    _write_prefs(tmp_home, monkeypatch, [{"name": "Les Paul Jr", "type": "guitar"}])
    exports = tmp_home / "exports"
    exports.mkdir()
    hsp = exports / "old.hsp"
    old_name = "White Limo — Les Paul Jr"  # em-dash separator
    _write_hsp(hsp, old_name)

    m = SetlistManifest.load()
    name = m.register_tone(hsp, source="authored")
    assert name == old_name
    m.tones[name]["slot"] = "2B"
    m.create_setlist("Live")
    m.add_to_setlist("Live", name)
    m.save()

    migrate.run_migration(migrate.plan_migration())

    new_name = "White Limo - Les Paul Jr"  # hyphen separator (re-key)
    assert new_name != old_name

    m2 = SetlistManifest.load()
    assert old_name not in m2.tones  # old key gone
    assert new_name in m2.tones  # new key present
    dest = home.tones_dir() / "white-limo-les-paul-jr.hsp"
    assert Path(m2.tones[new_name]["path"]).resolve() == dest.resolve()
    assert m2.tones[new_name]["slot"] == "2B"  # slot preserved
    # setlist membership rewritten to the new key
    assert m2.setlists_map["Live"]["tones"] == [new_name]


# ---------------------------------------------------------------------------
# test net: verify-failure data safety (source preserved, no partial dest)
# ---------------------------------------------------------------------------


def test_data_safe_place_preserves_source_on_verify_failure(tmp_home, monkeypatch):
    """If the copy's byte-verify fails, the SOURCE must be preserved (never
    deleted) and no partial destination left behind."""
    _write_prefs(tmp_home, monkeypatch, [])
    exports = tmp_home / "exports"
    exports.mkdir()
    hsp = exports / "good.hsp"
    _write_hsp(hsp, "Verify Fail Tone")
    _register(hsp)

    def _bad_copy(src, dst, *a, **k):
        # write mismatching bytes so the byte-verify in _data_safe_place fails
        Path(dst).write_bytes(b"CORRUPTED-does-not-match-source")
        return dst

    monkeypatch.setattr(migrate.shutil, "copy2", _bad_copy)

    summary = migrate.run_migration(migrate.plan_migration())

    assert hsp.exists()  # source preserved (move never completed)
    dest = home.tones_dir() / "verify-fail-tone.hsp"
    assert not dest.exists()  # partial dest cleaned up
    assert any(e["name"] == "Verify Fail Tone" for e in summary["tones"]["errors"])
