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
- ``migrate_instruments`` is a no-op returning a "deferred to PR 3" marker.

Git identity is isolated like ``tests/test_gitops.py`` so a dev machine's git
config can't leak into the tmp home repos, and the module skips when git is
absent (the auto-commit paths shell out to git).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helixgen import home, migrate, naming, tone_meta
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
    mapping_bytes = (home.legacy_irs_dir() / "mapping.json").read_bytes()
    manifest_bytes = home.manifest_path().read_bytes()

    # second run: all skips, no churn
    summary2 = migrate.run_migration(migrate.plan_migration())
    assert summary2["tones"]["moved"] == []
    assert summary2["tones"]["skipped"]

    assert sorted(p.name for p in home.tones_dir().glob("*.hsp")) == tones_after_first
    assert sorted(p.name for p in (home.library_irs_dir() / "ya-bogn").iterdir()) == irs_after_first
    assert (home.legacy_irs_dir() / "mapping.json").read_bytes() == mapping_bytes
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
# migrate_instruments is a no-op deferred to PR 3
# ---------------------------------------------------------------------------


def test_migrate_instruments_is_a_deferred_noop(tmp_home, monkeypatch):
    _write_prefs(tmp_home, monkeypatch, [{"name": "Les Paul Jr", "type": "guitar"}])
    plan = migrate.plan_migration()
    marker = migrate.migrate_instruments(plan)
    text = json.dumps(marker).lower()
    assert "pr 3" in text or "deferred" in text
    # prefs untouched, no guitar profiles created
    assert not home.guitars_dir().exists() or not any(home.guitars_dir().glob("*.json"))


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
# _scaffold_ir_stub shape
# ---------------------------------------------------------------------------


def test_scaffold_ir_stub_shape(tmp_path):
    stub = tmp_path / "s.json"
    migrate._scaffold_ir_stub(stub, irhash="a" * 32, wav="cab.wav",
                              imported_from="/x/cab.wav")
    data = json.loads(stub.read_text())
    assert data == {"schema": 1, "irhash": "a" * 32, "wav": "cab.wav",
                    "imported_from": "/x/cab.wav"}
