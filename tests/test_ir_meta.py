"""Tests for helixgen.ir_meta + the copy-by-default register paths + the
default-IR-dir flip's legacy bridge (Task 12, PR 3).

All local: env overrides (HELIXGEN_HOME/LIBRARY/IRS/PREFS) point at tmp_path
so nothing touches a real ~/.helixgen. WAV fixtures are tiny synthetic bytes
(import_wav copies bytes + records a SUPPLIED irhash -- it never re-hashes --
so no libsndfile is needed here).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helixgen import home, ir_meta
from helixgen.ir import IrMapping


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Isolate the helixgen home + git identity so tmp homes are clean repos."""
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HELIXGEN_LIBRARY", raising=False)
    monkeypatch.delenv("HELIXGEN_IRS", raising=False)
    monkeypatch.delenv("HELIXGEN_SETLISTS", raising=False)
    monkeypatch.delenv("HELIXGEN_GIT_COMMIT_TONES", raising=False)
    fake = tmp_path / "_fakehome"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake / "gitconfig-none"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _wav(path: Path, content: bytes = b"RIFFxxxxWAVE-fake") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# scaffold / mix-guess / (de)serialization
# ---------------------------------------------------------------------------


def test_guess_mix_variants():
    assert ir_meta._guess_mix("YA BOGN Mix 01.wav") == "Mix 01"
    assert ir_meta._guess_mix("mix_3.wav") == "Mix 3"
    assert ir_meta._guess_mix("Cab Mix-12 blend.wav") == "Mix 12"
    assert ir_meta._guess_mix("no-mix-here.wav") is None
    assert ir_meta._guess_mix("mixture.wav") is None  # 'mix' not followed by digits


def test_scaffold_shape_measured_stays_none(tmp_path):
    lib_wav = home.library_irs_dir() / "ya-bogn" / "YA BOGN Mix 02.wav"
    m = ir_meta.scaffold(lib_wav, "a" * 32, imported_from="/src/x.wav")
    assert m.schema == 1
    assert m.irhash == "a" * 32
    assert m.wav == "irs/ya-bogn/YA BOGN Mix 02.wav"  # library-relative
    assert m.imported_from == "/src/x.wav"
    assert m.mix == "Mix 02"
    assert m.measured is None  # NO numpy in core
    assert m.tags == [] and m.mics == []
    assert m.pack is None and m.cab is None and m.notes_md is None


def test_meta_path_for_swaps_suffix():
    assert ir_meta.meta_path_for(Path("/a/b/cab.wav")) == Path("/a/b/cab.json")


def test_save_and_load_round_trip(tmp_path):
    m = ir_meta.IrMeta(irhash="b" * 32, wav="irs/p/c.wav", tags=["tight"],
                       mics=["57"], mix="Mix 01")
    path = home.library_irs_dir() / "p" / "c.json"
    ir_meta.save_ir_meta(m, path)
    back = ir_meta.load_ir_meta(path)
    assert back.irhash == "b" * 32
    assert back.tags == ["tight"] and back.mics == ["57"] and back.mix == "Mix 01"


def test_save_serializes_measured_null(tmp_path):
    m = ir_meta.IrMeta(irhash="c" * 32, wav="irs/p/c.wav")
    path = home.library_irs_dir() / "p" / "c.json"
    ir_meta.save_ir_meta(m, path)
    on_disk = json.loads(path.read_text())
    assert on_disk["measured"] is None
    assert on_disk["schema"] == 1


# ---------------------------------------------------------------------------
# import_wav: copy + scaffold + mix-guess + collision/idempotence
# ---------------------------------------------------------------------------


def test_import_wav_copies_scaffolds_and_returns_paths(tmp_path):
    src = _wav(tmp_path / "YA BOGN" / "YA BOGN Mix 01.wav")
    wav_path, meta_path = ir_meta.import_wav(src, "d" * 32)

    lib = home.library_irs_dir()
    assert wav_path == lib / "ya-bogn" / "YA BOGN Mix 01.wav"
    assert wav_path.exists() and wav_path.read_bytes() == src.read_bytes()
    assert src.exists()  # COPIED, never moved
    data = json.loads(meta_path.read_text())
    assert data["irhash"] == "d" * 32
    assert data["wav"] == "irs/ya-bogn/YA BOGN Mix 01.wav"
    assert data["imported_from"] == str(src.resolve())
    assert data["mix"] == "Mix 01"
    assert data["measured"] is None


def test_import_wav_skips_copy_when_identical_already_present(tmp_path):
    src = _wav(tmp_path / "pack" / "c.wav", b"RIFFsame")
    ir_meta.import_wav(src, "e" * 32)
    dest = home.library_irs_dir() / "pack" / "c.wav"
    mtime_before = dest.stat().st_mtime_ns
    # second import of identical bytes: no re-copy, sidecar preserved
    ir_meta.import_wav(src, "e" * 32)
    assert dest.stat().st_mtime_ns == mtime_before


def test_import_wav_disambiguates_cross_pack_basename_collision(tmp_path):
    # two source packs whose parent dir slugs to the SAME 'pack' with the same
    # basename but DIFFERENT content -> distinct library files, never aliased.
    # distinct dir names that slug to the same 'pack-one' (survives a
    # case-insensitive filesystem, unlike pack/PACK)
    src_a = _wav(tmp_path / "Pack One" / "cab.wav", b"RIFFaaaa")
    src_b = _wav(tmp_path / "Pack-One" / "cab.wav", b"RIFFbbbb")
    dest_a, _ = ir_meta.import_wav(src_a, "a" * 32)
    dest_b, _ = ir_meta.import_wav(src_b, "b" * 32)
    assert dest_a != dest_b
    assert dest_a.read_bytes() == b"RIFFaaaa"
    assert dest_b.read_bytes() == b"RIFFbbbb"


def test_import_wav_leaves_in_library_file_in_place(tmp_path):
    # a WAV already under library/irs is registered in place (no nested copy)
    in_lib = _wav(home.library_irs_dir() / "existing" / "c.wav")
    dest, meta_path = ir_meta.import_wav(in_lib, "f" * 32)
    assert dest == in_lib
    data = json.loads(meta_path.read_text())
    assert data["imported_from"] is None  # originated in the library


# ---------------------------------------------------------------------------
# FIX 2: pack-subdir derivation collapses the standard <Pack>/Mixes/ layout to
# the PACK name, not the generic "mixes" container.
# ---------------------------------------------------------------------------


def test_import_wav_uses_grandparent_pack_for_mixes_layout(tmp_path):
    # commercial layout <PackName>/Mixes/*.wav -> library/irs/<pack>/, not mixes/
    src = _wav(tmp_path / "York Audio BOGN" / "Mixes" / "Mix 01.wav")
    wav_path, _ = ir_meta.import_wav(src, "d" * 32)
    assert wav_path == home.library_irs_dir() / "york-audio-bogn" / "Mix 01.wav"


def test_import_wav_uses_parent_when_not_generic_container(tmp_path):
    # a WAV whose immediate parent is a real pack dir keeps the parent name
    src = _wav(tmp_path / "SomePack" / "cab.wav")
    wav_path, _ = ir_meta.import_wav(src, "e" * 32)
    assert wav_path == home.library_irs_dir() / "somepack" / "cab.wav"


def test_derive_pack_rules():
    from pathlib import Path as _P
    # generic container with a real grandparent -> grandparent
    assert ir_meta.derive_pack(_P("/x/York Audio BOGN/Mixes/Mix 01.wav")) == "york-audio-bogn"
    # non-generic parent -> parent
    assert ir_meta.derive_pack(_P("/x/SomePack/cab.wav")) == "somepack"
    # generic parent but NO grandparent -> fall back to the parent name
    assert ir_meta.derive_pack(_P("Mixes/x.wav")) == "mixes"
    # bare <dir>/x.wav still works
    assert ir_meta.derive_pack(_P("guitar-cabs/x.wav")) == "guitar-cabs"


# ---------------------------------------------------------------------------
# FIX 3: IR-tag validation is case-insensitive (matches guitar_settings).
# ---------------------------------------------------------------------------


def test_validate_ir_metas_tag_check_is_case_insensitive(tmp_path):
    lib = home.library_dir()
    wav_rel = "irs/p/x.wav"
    (lib / "irs" / "p").mkdir(parents=True)
    (lib / wav_rel).write_bytes(b"RIFFxxxx")
    mapping = IrMapping.load()
    mapping.register("a" * 32, lib / wav_rel)

    ok = ir_meta.IrMeta(irhash="a" * 32, wav=wav_rel, tags=["Bright", "TIGHT"])
    problems, warnings = ir_meta.validate_ir_metas([ok], mapping)
    assert problems == []
    assert not any("Bright" in w or "TIGHT" in w for w in warnings)

    bad = ir_meta.IrMeta(irhash="a" * 32, wav=wav_rel, tags=["sparkly"])
    _, warnings2 = ir_meta.validate_ir_metas([bad], mapping)
    assert any("sparkly" in w for w in warnings2)


# ---------------------------------------------------------------------------
# register-irs / ir-scan copy-by-default (via CLI)
# ---------------------------------------------------------------------------


def test_register_irs_preset_form_places_library_copy_in_mapping(tmp_path):
    from click.testing import CliRunner
    from helixgen.cli import cli

    HSP_MAGIC = b"rpshnosj"
    src = _wav(tmp_path / "MyPack" / "cab.wav")
    preset = tmp_path / "p.hsp"
    body = {"meta": {"name": "t"}, "preset": {"flow": [{"b01": {
        "path": 0, "position": 1,
        "slot": [{"model": "HX2_ImpulseResponseWithPan", "irhash": "9" * 32}]}}]}}
    preset.write_bytes(HSP_MAGIC + json.dumps(body).encode())

    res = CliRunner().invoke(cli, ["register-irs", str(preset), str(src)])
    assert res.exit_code == 0, res.output

    mapping = IrMapping.load()
    resolved = mapping.resolve_by_hash("9" * 32)
    lib = home.library_irs_dir()
    assert resolved == (lib / "mypack" / "cab.wav").resolve()
    assert resolved.exists()
    # sidecar scaffolded next to the copy, imported_from = source
    sidecar = json.loads((lib / "mypack" / "cab.json").read_text())
    assert sidecar["imported_from"] == str(src.resolve())


def test_register_irs_no_copy_registers_in_place(tmp_path):
    from click.testing import CliRunner
    from helixgen.cli import cli

    HSP_MAGIC = b"rpshnosj"
    src = _wav(tmp_path / "elsewhere" / "cab.wav")
    preset = tmp_path / "p.hsp"
    body = {"meta": {"name": "t"}, "preset": {"flow": [{"b01": {
        "path": 0, "position": 1,
        "slot": [{"model": "HX2_ImpulseResponseWithPan", "irhash": "8" * 32}]}}]}}
    preset.write_bytes(HSP_MAGIC + json.dumps(body).encode())

    res = CliRunner().invoke(cli, ["register-irs", "--no-copy", str(preset), str(src)])
    assert res.exit_code == 0, res.output
    mapping = IrMapping.load()
    assert mapping.resolve_by_hash("8" * 32) == src.resolve()
    # no library copy, no sidecar
    assert not (home.library_irs_dir() / "elsewhere").exists()


# ---------------------------------------------------------------------------
# ir-backfill idempotence
# ---------------------------------------------------------------------------


def test_backfill_copies_outside_wav_and_is_idempotent(tmp_path):
    src = _wav(tmp_path / "OldPack" / "cab.wav")
    # register an entry pointing OUTSIDE the library (no metadata)
    mapping = IrMapping.load()
    mapping.register("1" * 32, src)
    mapping.save()

    result = ir_meta.backfill(mapping)
    assert result["backfilled"] == ["1" * 32]
    assert result["skipped"] == [] and result["errors"] == []
    lib = home.library_irs_dir()
    dest = lib / "oldpack" / "cab.wav"
    assert dest.exists()
    assert (lib / "oldpack" / "cab.json").exists()
    # mapping rewritten to the library copy
    assert mapping.resolve_by_hash("1" * 32) == dest.resolve()

    # second run: all skips, no churn
    entries_before = dict(mapping.entries)
    result2 = ir_meta.backfill(mapping)
    assert result2["backfilled"] == []
    assert result2["skipped"] == ["1" * 32]
    assert mapping.entries == entries_before


def test_backfill_cli_reports_and_commits(tmp_path):
    from click.testing import CliRunner
    from helixgen.cli import cli

    src = _wav(tmp_path / "Pack" / "cab.wav")
    mapping = IrMapping.load()
    mapping.register("2" * 32, src)
    mapping.save()

    res = CliRunner().invoke(cli, ["library", "ir-backfill", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["backfilled"] == ["2" * 32]


# ---------------------------------------------------------------------------
# validate: off-vocabulary tags -> warnings; unregistered/missing -> problems
# ---------------------------------------------------------------------------


def test_validate_flags_off_vocabulary_tag_as_warning(tmp_path):
    lib = home.library_irs_dir()
    wav = _wav(lib / "p" / "c.wav")
    m = ir_meta.IrMeta(irhash="3" * 32, wav="irs/p/c.wav",
                       tags=["tight", "sparkly-nonsense"])
    ir_meta.save_ir_meta(m, ir_meta.meta_path_for(wav))
    mapping = IrMapping.load()
    mapping.register("3" * 32, wav)
    mapping.save()

    metas = ir_meta.load_all_ir_metas()
    problems, warnings = ir_meta.validate_ir_metas(metas, IrMapping.load())
    assert problems == []  # tight ok, wav exists, hash registered
    assert any("sparkly-nonsense" in w for w in warnings)


def test_validate_flags_unregistered_hash_and_missing_wav_as_problems(tmp_path):
    lib = home.library_irs_dir()
    m = ir_meta.IrMeta(irhash="4" * 32, wav="irs/p/gone.wav", tags=["tight"])
    ir_meta.save_ir_meta(m, lib / "p" / "gone.json")

    problems, warnings = ir_meta.validate_ir_metas(
        ir_meta.load_all_ir_metas(), IrMapping.load())
    assert any("not registered" in p for p in problems)
    assert any("wav file not found" in p for p in problems)
    assert warnings == []


# ---------------------------------------------------------------------------
# THE FLIP: default_irs_path + legacy-mapping.json bridge
# ---------------------------------------------------------------------------


def test_default_irs_path_is_library(tmp_path, monkeypatch):
    from helixgen.ir import default_irs_path
    assert default_irs_path() == home.library_irs_dir()
    assert default_irs_path() == home.helixgen_home() / "library" / "irs"


def test_fresh_home_uses_library_location(tmp_path):
    # no legacy, no HELIXGEN_IRS -> library location, no bridge
    m = IrMapping.load()
    assert m.irs_dir == home.library_irs_dir()
    m.register("5" * 32, _wav(tmp_path / "x" / "a.wav"))
    m.save()
    assert (home.library_irs_dir() / "mapping.json").exists()
    assert not (home.legacy_irs_dir() / "mapping.json").exists()


def test_legacy_mapping_is_bridged_up_to_library(tmp_path):
    # a pre-flip mapping.json sitting at legacy_irs_dir(), library absent
    legacy_dir = home.legacy_irs_dir()
    legacy_dir.mkdir(parents=True)
    ext_wav = _wav(tmp_path / "ext" / "cab.wav")
    legacy_rel_wav = _wav(legacy_dir / "local.wav")
    (legacy_dir / "mapping.json").write_text(json.dumps({
        "aaaa": str(ext_wav),      # absolute value
        "bbbb": "local.wav",        # relative-to-legacy value
    }))

    m = IrMapping.load()
    # adopted entries, but targeted at the library for the next save
    assert m.irs_dir == home.library_irs_dir()
    assert m.resolve_by_hash("aaaa") == ext_wav.resolve()
    assert m.resolve_by_hash("bbbb") == legacy_rel_wav.resolve()  # still resolves

    m.save()
    # new file written at library; legacy renamed to .migrated-legacy (not
    # lost) -- the "-legacy" suffix matches the home .gitignore's "*.migrated-*"
    assert (home.library_irs_dir() / "mapping.json").exists()
    assert not (legacy_dir / "mapping.json").exists()
    assert (legacy_dir / "mapping.json.migrated-legacy").exists()

    # idempotent: a fresh load now finds the library file, never re-bridges
    m2 = IrMapping.load()
    assert m2.irs_dir == home.library_irs_dir()
    assert m2.resolve_by_hash("aaaa") == ext_wav.resolve()
    assert m2.resolve_by_hash("bbbb") == legacy_rel_wav.resolve()
    m2.save()
    assert not (legacy_dir / "mapping.json").exists()


def test_helixgen_irs_env_takes_precedence_no_bridge(tmp_path, monkeypatch):
    # an explicit $HELIXGEN_IRS is a self-consistent location: used verbatim,
    # and a legacy mapping.json is NOT bridged into it.
    explicit = tmp_path / "explicit-irs"
    explicit.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(explicit))
    legacy_dir = home.legacy_irs_dir()
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "mapping.json").write_text(json.dumps({"zzzz": "/x/y.wav"}))

    m = IrMapping.load()
    assert m.irs_dir == explicit
    assert m.entries == {}  # legacy NOT adopted
    m.register("6" * 32, _wav(explicit / "a.wav"))
    m.save()
    assert (explicit / "mapping.json").exists()
    # legacy untouched (no bridge, no rename)
    assert (legacy_dir / "mapping.json").exists()


def test_explicit_irs_dir_arg_no_bridge(tmp_path):
    explicit = tmp_path / "arg-irs"
    explicit.mkdir()
    legacy_dir = home.legacy_irs_dir()
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "mapping.json").write_text(json.dumps({"zzzz": "/x/y.wav"}))
    m = IrMapping.load(explicit)
    assert m.irs_dir == explicit
    assert m.entries == {}
    assert (legacy_dir / "mapping.json").exists()  # untouched
