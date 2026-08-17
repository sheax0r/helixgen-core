"""`helixgen library fork` (hgc-2ja) -- one test per acceptance criterion,
plus the two identity guards that ship with it.

Every test runs against the autouse `$HELIXGEN_HOME` tmp redirect in
conftest.py, so the real `~/.helixgen` library is never touched.
"""
import json

import pytest
from click.testing import CliRunner

from helixgen import guitars, home, tone_meta
from helixgen.cli import cli
from helixgen.device.manifest import SetlistManifest


def _profile(name, short, *, active, pickups):
    return guitars.save_profile(guitars.GuitarProfile(
        name=name, short_name=short, type="guitar", active=active,
        pickups=pickups, construction=None, character_md=None))


@pytest.fixture
def seeded(tmp_path, hsp_library):
    """One authored tone (`aerosmith-dream-on`, Ibanez-Prestige variant) plus
    three guitar profiles: a passive HSH, an active-humbucker, and a second
    passive HSH that classifies IDENTICALLY to the first."""
    _profile("Ibanez Prestige", "Ibanez Prestige", active=False, pickups="HSH")
    _profile("ESP LTD EC-1000", "EC-1000", active=True, pickups="active EMG HH")
    _profile("Suhr Modern", "Suhr", active=False, pickups="HSH")

    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps({
        "name": "Probe",
        "paths": [{"blocks": [
            {"block": "Tube Drive", "params": {"Gain": 0.4, "Tone": 0.6}},
            {"block": "Brit Amp", "params": {"Drive": 0.7, "Master": 0.55}},
        ]}],
    }))
    r = CliRunner().invoke(cli, [
        "generate", str(recipe), "--library", str(hsp_library.root),
        "--artist", "Aerosmith", "--song", "Dream On",
        "--guitar", "Ibanez Prestige"])
    assert r.exit_code == 0, r.output
    return hsp_library


def _fork(*args):
    return CliRunner().invoke(cli, ["library", "fork", *args])


SRC_NAME = "Aerosmith - Dream On - Ibanez Prestige"


# --- criterion 1: --guitar alone yields a second variant of the SAME tone ---


def test_fork_adds_second_variant_of_same_logical_tone(seeded):
    r = _fork("aerosmith-dream-on", "--guitar", "EC-1000")
    assert r.exit_code == 0, r.output

    # ONE metadata file, TWO variants under it.
    assert sorted(p.name for p in home.tones_dir().glob("*.json")) == [
        "aerosmith-dream-on.json"]
    meta = tone_meta.load_tone_meta("aerosmith-dream-on")
    assert sorted(meta.variants) == ["esp-ltd-ec-1000", "ibanez-prestige"]

    # ...and `describe` lists both.
    d = CliRunner().invoke(cli, ["describe", "aerosmith-dream-on"])
    assert d.exit_code == 0, d.output
    assert "Aerosmith - Dream On - Ibanez Prestige" in d.output
    assert "Aerosmith - Dream On - EC-1000" in d.output

    # The fork is a real, registered tone (manifest + .hsp on disk).
    dest = home.tones_dir() / "aerosmith-dream-on-esp-ltd-ec-1000.hsp"
    assert dest.is_file()
    assert "Aerosmith - Dream On - EC-1000" in SetlistManifest.load().tones


def test_fork_by_exact_preset_name_and_by_path(seeded):
    assert _fork(SRC_NAME, "--guitar", "EC-1000").exit_code == 0
    src = home.tones_dir() / "aerosmith-dream-on-ibanez-prestige.hsp"
    r = _fork(str(src), "--guitar", "Suhr")
    assert r.exit_code == 0, r.output
    meta = tone_meta.load_tone_meta("aerosmith-dream-on")
    assert sorted(meta.variants) == [
        "esp-ltd-ec-1000", "ibanez-prestige", "suhr-modern"]


def test_fork_of_multi_variant_slug_is_ambiguous(seeded):
    assert _fork("aerosmith-dream-on", "--guitar", "EC-1000").exit_code == 0
    r = _fork("aerosmith-dream-on", "--guitar", "Suhr")
    assert r.exit_code != 0
    assert "name the exact variant" in r.output
    assert SRC_NAME in r.output


# --- criterion 2: the forked .hsp is param-identical to the source ---------


def test_forked_hsp_is_param_identical_to_source(seeded):
    assert _fork("aerosmith-dream-on", "--guitar", "EC-1000").exit_code == 0

    def _view(path):
        r = CliRunner().invoke(
            cli, ["view", str(path), "--library", str(seeded.root)])
        assert r.exit_code == 0, r.output
        return json.loads(r.output)

    src = _view(home.tones_dir() / "aerosmith-dream-on-ibanez-prestige.hsp")
    dst = _view(home.tones_dir() / "aerosmith-dream-on-esp-ltd-ec-1000.hsp")
    assert src.pop("name") == "Aerosmith - Dream On - Ibanez Prestige"
    assert dst.pop("name") == "Aerosmith - Dream On - EC-1000"
    assert src == dst  # identity is the ONLY difference


# --- criterion 3: an existing target variant errors and writes nothing -----


def test_fork_onto_existing_variant_errors_and_writes_nothing(seeded):
    assert _fork("aerosmith-dream-on", "--guitar", "EC-1000").exit_code == 0
    before = {p.name: p.read_bytes() for p in home.tones_dir().iterdir()}

    r = _fork(SRC_NAME, "--guitar", "EC-1000")
    assert r.exit_code != 0
    assert "already exists" in r.output
    assert "aerosmith-dream-on-esp-ltd-ec-1000.hsp" in r.output
    assert {p.name: p.read_bytes() for p in home.tones_dir().iterdir()} == before


# --- criterion 4: provenance is readable back -----------------------------


def test_provenance_is_recorded_and_readable(seeded):
    assert _fork("aerosmith-dream-on", "--guitar", "EC-1000").exit_code == 0

    variant = tone_meta.load_tone_meta("aerosmith-dream-on").variants[
        "esp-ltd-ec-1000"]
    assert variant.forked_from["tone"] == "aerosmith-dream-on"
    assert variant.forked_from["variant"] == "ibanez-prestige"
    assert variant.forked_from["hsp"] == (
        "tones/aerosmith-dream-on-ibanez-prestige.hsp")
    assert variant.forked_from["helixgen_version"]
    assert variant.forked_from["at"]

    d = CliRunner().invoke(cli, ["describe", "aerosmith-dream-on"])
    assert "forked from ibanez-prestige (aerosmith-dream-on)" in d.output

    s = CliRunner().invoke(cli, ["library", "show", "aerosmith-dream-on", "--json"])
    assert s.exit_code == 0, s.output
    on_disk = json.loads(s.output)["variants"]["esp-ltd-ec-1000"]["forked_from"]
    assert on_disk["variant"] == "ibanez-prestige"

    # ...and a plain (non-JSON) `library show` surfaces it too.
    h = CliRunner().invoke(cli, ["library", "show", "aerosmith-dream-on"])
    assert "forked from ibanez-prestige" in h.output


def test_forked_from_round_trips_through_save(seeded):
    assert _fork("aerosmith-dream-on", "--guitar", "EC-1000").exit_code == 0
    # `library doc` is a load -> mutate -> save round; provenance must survive.
    r = CliRunner().invoke(cli, ["library", "doc", "aerosmith-dream-on", "-"],
                           input="rewritten write-up")
    assert r.exit_code == 0, r.output
    meta = tone_meta.load_tone_meta("aerosmith-dream-on")
    assert meta.variants["esp-ltd-ec-1000"].forked_from["variant"] == "ibanez-prestige"


# --- criterion 5: the inherited write-up is marked as inherited ------------


def test_inherited_write_up_is_marked(seeded):
    CliRunner().invoke(cli, ["library", "doc", "aerosmith-dream-on", "-"],
                       input="# Dream On\n\nBuilt for the Prestige.")
    CliRunner().invoke(cli, ["library", "doc", "aerosmith-dream-on", "-",
                             "--variant", "ibanez-prestige"],
                       input="Neck pickup, tone rolled to 7.")
    assert _fork("aerosmith-dream-on", "--guitar", "EC-1000").exit_code == 0

    meta = tone_meta.load_tone_meta("aerosmith-dream-on")
    notes = meta.variants["esp-ltd-ec-1000"].notes_md
    assert "Inherited from the `ibanez-prestige` variant" in notes
    assert "written for the SOURCE" in notes
    assert notes.rstrip().endswith("Neck pickup, tone rolled to 7.")
    # A same-tone fork shares the logical description_md -- it is NOT rewritten.
    assert meta.description_md == "# Dream On\n\nBuilt for the Prestige."


def test_new_logical_tone_inherits_a_marked_description(seeded):
    CliRunner().invoke(cli, ["library", "doc", "aerosmith-dream-on", "-"],
                       input="# Dream On\n\nBuilt for the Prestige.")
    r = _fork("aerosmith-dream-on", "--artist", "Aerosmith", "--song", "Sweet Emotion")
    assert r.exit_code == 0, r.output
    assert "NEW logical tone" in r.output

    new = tone_meta.load_tone_meta("aerosmith-sweet-emotion")
    assert "Inherited from the `ibanez-prestige` variant" in new.description_md
    assert new.description_md.rstrip().endswith("Built for the Prestige.")
    # --guitar defaulted to the source variant's guitar.
    assert list(new.variants) == ["ibanez-prestige"]
    assert new.variants["ibanez-prestige"].preset_name == (
        "Aerosmith - Sweet Emotion - Ibanez Prestige")
    # ...and the source tone is untouched.
    assert list(tone_meta.load_tone_meta("aerosmith-dream-on").variants) == [
        "ibanez-prestige"]


# --- criterion 6: the adaptation checklist ---------------------------------


def test_checklist_prints_when_pickup_classes_differ(seeded):
    r = CliRunner().invoke(
        cli, ["library", "fork", "aerosmith-dream-on", "--guitar", "EC-1000"],
        catch_exceptions=False)
    assert r.exit_code == 0, r.output
    err = r.stderr
    assert "Pickups differ" in err
    assert "passive humbucker/single-coil (HSH)" in err
    assert "active humbucker (active EMG HH)" in err
    # ...naming the params THIS chain actually has, grouped and addressed by
    # grid coordinate. (These synthetic fixture models aren't in the vendored
    # display-name table, so the label falls back to the model id.)
    assert ("- drive into the amp: HD2_DistTube [0:b01] `Gain`, "
            "HD2_AmpBrit [0:b02] `Drive`\n") in err
    assert ("- input stage: P35_InputInst1_2 [0:b00] `Pad`, "
            "P35_InputInst1_2 [0:b00] `Trim`\n") in err
    assert "- level: HD2_AmpBrit [0:b02] `Master`" in err
    # ...and no group this chain has no params for.
    assert "brightness" not in err
    assert "Presence" not in err
    # a param outside the table is never listed
    assert "`Tone`" not in err
    # the EMPTY second path contributes nothing (its endpoints are noise)
    assert "[1:b" not in err


def test_checklist_is_silent_when_pickup_classes_match(seeded):
    # Suhr Modern is a passive HSH, same class as the Ibanez Prestige.
    r = CliRunner().invoke(
        cli, ["library", "fork", "aerosmith-dream-on", "--guitar", "Suhr"],
        catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert "Pickups differ" not in r.stderr
    assert "Reconsider" not in r.stderr


def test_pickup_class_reads_flag_and_prose():
    def cls(active, pickups):
        return guitars.pickup_class(guitars.GuitarProfile(
            name="x", short_name="x", type="guitar", active=active,
            pickups=pickups, construction=None, character_md=None))

    assert cls(True, "active EMG HH") == ("active", frozenset({"humbucker"}))
    assert cls(False, "HSH") == ("passive", frozenset({"humbucker", "single-coil"}))
    assert cls(False, "one bridge P-90 (single-coil soapbar)") == (
        "passive", frozenset({"single-coil"}))
    # the `active` flag is absent -> inferred from the prose
    assert cls(None, "Fishman Fluence Modern")[0] == "active"
    assert cls(None, "vintage PAF humbuckers") == (
        None, frozenset({"humbucker"}))
    # nothing to go on classifies as unknown, which matches nothing populated
    assert cls(None, None) == (None, frozenset())
    assert guitars.pickup_class(None) == (None, frozenset())


# --- criterion 7: --dry-run writes nothing --------------------------------


def test_dry_run_writes_nothing(seeded):
    before_files = {p.name: p.read_bytes() for p in home.tones_dir().iterdir()}
    before_manifest = json.dumps(SetlistManifest.load().tones, sort_keys=True)

    r = _fork("aerosmith-dream-on", "--guitar", "EC-1000", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "Would fork" in r.output
    assert "Dry run -- nothing was written." in r.output
    assert "aerosmith-dream-on-esp-ltd-ec-1000.hsp" in r.output

    assert {p.name: p.read_bytes() for p in home.tones_dir().iterdir()} == before_files
    assert json.dumps(SetlistManifest.load().tones, sort_keys=True) == before_manifest


def test_json_output_shape(seeded):
    r = _fork("aerosmith-dream-on", "--guitar", "EC-1000", "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    rec = json.loads(r.stdout)
    assert rec["dry_run"] is True
    assert rec["source"]["variant"] == "ibanez-prestige"
    assert rec["target"]["variant"] == "esp-ltd-ec-1000"
    assert rec["target"]["new_logical_tone"] is False
    assert rec["forked_from"]["tone"] == "aerosmith-dream-on"
    assert rec["adaptation"]["pickups_differ"] is True
    assert rec["adaptation"]["checklist"]


# --- source resolution edge cases -----------------------------------------


def test_unregistered_hsp_needs_an_identity(tmp_path, seeded):
    from helixgen.hsp import write_hsp

    loose = tmp_path / "loose.hsp"
    write_hsp(loose, {"meta": {"name": "Loose"}, "preset": {"flow": []}})
    r = _fork(str(loose), "--guitar", "EC-1000")
    assert r.exit_code != 0
    assert "no identity to inherit" in r.output

    ok = _fork(str(loose), "--guitar", "EC-1000", "--descriptor", "Loose Tone")
    assert ok.exit_code == 0, ok.output
    meta = tone_meta.load_tone_meta("loose-tone")
    assert meta.variants["esp-ltd-ec-1000"].forked_from["tone"] is None
    assert meta.variants["esp-ltd-ec-1000"].forked_from["hsp"] == str(loose)


def test_fork_of_unknown_source_exits_1(seeded):
    r = _fork("no-such-tone", "--guitar", "EC-1000")
    assert r.exit_code != 0
    assert "no tone found" in r.output


# --- guard 1: an identity smuggled into --descriptor ----------------------


@pytest.mark.parametrize("bad", [
    "Dream On - EC1000",
    "Dream On -- EC1000",
    "Dream On — EC1000",
    "Dream On EC-1000",      # trailing guitar short_name, no separator
    "Dream On Ibanez Prestige",
])
def test_smuggled_descriptor_is_refused_everywhere(tmp_path, seeded, bad):
    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps({"name": "X", "paths": [{"blocks": []}]}))

    g = CliRunner().invoke(cli, ["generate", str(recipe), "--library",
                                 str(seeded.root), "--descriptor", bad])
    assert g.exit_code != 0, g.output
    assert "--descriptor" in g.output

    f = _fork("aerosmith-dream-on", "--descriptor", bad)
    assert f.exit_code != 0, f.output

    src = home.tones_dir() / "aerosmith-dream-on-ibanez-prestige.hsp"
    i = CliRunner().invoke(cli, ["library", "import", str(src), "--keep-source",
                                 "--descriptor", bad])
    assert i.exit_code != 0, i.output

    # nothing was written by any of the three
    assert sorted(p.name for p in home.tones_dir().glob("*.json")) == [
        "aerosmith-dream-on.json"]


def test_clean_descriptor_still_works(tmp_path, seeded):
    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps({"name": "X", "paths": [{"blocks": []}]}))
    r = CliRunner().invoke(cli, ["generate", str(recipe), "--library",
                                 str(seeded.root), "--descriptor",
                                 "Warm Jazz Clean", "--guitar", "EC-1000"])
    assert r.exit_code == 0, r.output
    assert tone_meta.meta_path("warm-jazz-clean").exists()


def test_recipe_name_fallback_is_not_guarded(tmp_path, seeded):
    """A recipe's own `name` is an existing artifact's name, not a naming
    choice made at the CLI -- generating from it must keep working."""
    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps({"name": "Clean - Dirty Split",
                                  "paths": [{"blocks": []}]}))
    r = CliRunner().invoke(cli, ["generate", str(recipe),
                                 "--library", str(seeded.root)])
    assert r.exit_code == 0, r.output


# --- guard 2: `generate -o` silently discarding the naming flags ----------


def test_generate_o_warns_when_naming_flags_are_given(tmp_path, seeded):
    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps({"name": "X", "paths": [{"blocks": []}]}))
    out = tmp_path / "out.hsp"
    r = CliRunner().invoke(
        cli, ["generate", str(recipe), "-o", str(out), "--library",
              str(seeded.root), "--artist", "A", "--song", "S",
              "--guitar", "EC-1000"],
        catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert "IGNORES --artist, --song, --guitar" in r.stderr
    assert "NO metadata JSON" in r.stderr
    assert out.is_file()
    # ...and the warning is honest: no metadata JSON was written.
    assert not tone_meta.meta_path("a-s").exists()


def test_generate_o_is_silent_without_naming_flags(tmp_path, seeded):
    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps({"name": "X", "paths": [{"blocks": []}]}))
    out = tmp_path / "out.hsp"
    r = CliRunner().invoke(
        cli, ["generate", str(recipe), "-o", str(out), "--library",
              str(seeded.root)], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert "IGNORES" not in r.stderr
