"""hgc-3ll: block display names are the editor's own, and they are unique.

The library used to store whatever the DEVICE called a block (its `@name`),
falling back to a string-munged model id. Both were broken in a way that hurt
authoring, because recipes reference blocks BY name:

- TRUNCATION — `_CATEGORY_PREFIXES` is a category-inference table, not a word
  list, so `HX2_Comp` cut "Compressor" mid-word ("ressor LAStudio Comp Mono")
  and entries that ARE whole model ids (`TapeEater`) cut to "".
- COLLISION — 333 blocks shared 320 names. "Stereo" named six models, one of
  them `VIC_DynPlateStereo`, the most-used reverb in the factory setlist.

These tests pin the fix: the vendored editor table wins, it is unique by
construction, and a model id is always accepted in place of a name.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from helixgen.device import paraminfo
from helixgen.ingest import humanize_model_id, resolve_display_name
from helixgen.library import Block, Library


# --- the truncation bug, at its root ----------------------------------------

@pytest.mark.parametrize(
    "model_id, expected",
    [
        # `HX2_Comp` used to eat the "Comp" out of "Compressor".
        ("HX2_CompressorLAStudioCompMono", "Compressor LA Studio Comp Mono"),
        # These model ids are themselves prefix-table entries; stripping left "".
        ("TapeEater", "Tape Eater"),
        ("HD2_DM4BassOctaver", "Bass Octaver"),
        ("P35_LooperHelix", "Looper Helix"),
        # Stripping the category word must not reduce a model to its channel.
        ("VIC_DynPlateStereo", "Dyn Plate Stereo"),
        ("HD2_ReturnMono1", "Return Mono 1"),
        # Runs of capitals split before a capitalised word.
        ("HD2_CaliQMono", "Cali Q Mono"),
        # Unchanged behaviour for the ordinary case.
        ("HD2_AmpBrit2204Custom", "Brit 2204 Custom"),
        ("HD2_Cab4x12Greenback25", "4x12 Greenback 25"),
    ],
)
def test_humanize_never_eats_characters(model_id, expected):
    assert humanize_model_id(model_id) == expected


def test_humanize_never_returns_empty():
    # Every prefix-table entry is a model id in its own right for at least one
    # model, and several ARE one exactly (`TapeEater`). None may humanize to "".
    from helixgen.ingest import _CATEGORY_PREFIXES

    for model_id, _category in _CATEGORY_PREFIXES:
        assert humanize_model_id(model_id), model_id


# --- the vendored table is the source of truth ------------------------------

@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("HX2_CompressorLAStudioCompMono", "LA Studio Comp Mono"),
        ("HX2_CompressorLAStudioCompStereo", "LA Studio Comp Stereo"),
        # Absent from the table under its own id; resolved via its mono twin,
        # then re-qualified with the channel the model id carries.
        ("VIC_DynPlateStereo", "Dynamic Plate Stereo"),
        ("VIC_DynPlateMono", "Dynamic Plate Mono"),
        # Two models, one vendor name: the plain amp keeps it.
        ("HD2_AmpWoodyBlue", "Woody Blue"),
        ("HD2_PreampWoodyBlue", "Woody Blue (Preamp)"),
        # Used to be "" because the model id is its own prefix-table entry.
        ("TapeEater", "Tape Eater"),
        ("HD2_DM4BassOctaver", "Bass Octaver"),
        # A helixgen id the device spells differently must not inherit the
        # device variant's channel word.
        ("HD2_DrvScream808", "Scream 808"),
        ("HD2_DistCompulsiveDrive", "Compulsive Drive"),
    ],
)
def test_resolve_display_name_uses_the_vendored_table(model_id, expected):
    assert resolve_display_name(model_id) == expected


def test_vendored_name_beats_the_devices_own_short_name():
    # The device wrote "Stereo" into the .hsp; it must not win any more.
    assert resolve_display_name("VIC_DynPlateStereo", "Stereo") == "Dynamic Plate Stereo"


def test_a_curated_name_survives_for_a_model_the_editor_never_heard_of():
    assert paraminfo.model_display_name("HD2_DistTube") is None
    assert resolve_display_name("HD2_DistTube", "Tube Drive") == "Tube Drive"


@pytest.mark.parametrize("degenerate", ["", "  ", "Mono", "Stereo", "Mono 1"])
def test_a_degenerate_stored_name_is_dropped(degenerate):
    # A name that says nothing but the channel is the device's, not yours.
    assert resolve_display_name("HD2_DistTube", degenerate) == "Tube"


def test_vendored_names_are_unique():
    names = paraminfo.load_param_ui()["model_names"]
    seen: dict[str, str] = {}
    clashes = []
    for model_id, name in sorted(names.items()):
        if name in seen:
            clashes.append((name, seen[name], model_id))
        seen[name] = model_id
    assert not clashes


def test_resolved_names_are_unique_across_every_model_id_we_know_of():
    """The uniqueness guarantee, over every model id in the vendored assets.

    Runs on a clean clone — unlike the real-library check below — and covers
    the RUNTIME resolver (which strips a channel word the build-time table
    still carries), not just the table the build script asserted.
    """
    from helixgen.device import defs, modelmap

    universe = set(paraminfo.load_param_ui()["model_names"])
    universe |= set(defs.load_defs().get("models", {}))
    universe |= set(modelmap.load_modelmap().get("map", {}))
    assert len(universe) > 700

    by_name: dict[str, list[str]] = {}
    for model_id in sorted(universe):
        by_name.setdefault(resolve_display_name(model_id), []).append(model_id)
    assert {n: ids for n, ids in by_name.items() if len(ids) > 1} == {}


# --- the read-time overlay --------------------------------------------------

def _library_with(root: Path, entries) -> Library:
    lib = Library(root=root)
    for model_id, category, stored_name in entries:
        lib.save_block(Block(
            model_id=model_id, category=category, display_name=stored_name,
            params={"Mix": {"type": "float", "default": 0.5}},
            exemplar={"@model": model_id, "@type": "fx", "@enabled": True},
            first_seen={"preset": "_", "firmware": "_", "date": "2026-08-14"}))
    lib.rebuild_index()
    return lib


def test_an_existing_library_is_corrected_without_a_migration(tmp_path):
    """A library written BEFORE the fix reads back correct — file untouched."""
    lib = _library_with(tmp_path / "lib", [
        ("VIC_DynPlateStereo", "reverb", "Stereo"),
        ("HX2_CompressorLAStudioCompMono", "dynamics", "ressor LAStudio Comp Mono"),
    ])
    on_disk = json.loads(
        (lib.blocks_dir / "reverb" / "VIC_DynPlateStereo.json").read_text())
    assert on_disk["display_name"] == "Stereo"  # nothing was rewritten

    assert lib.find_block("Dynamic Plate Stereo").model_id == "VIC_DynPlateStereo"
    assert lib.find_block("LA Studio Comp Mono").model_id == (
        "HX2_CompressorLAStudioCompMono")


def test_the_worst_collision_becomes_referenceable(tmp_path):
    """`Stereo` named six models; each now has a name of its own."""
    colliding = [
        ("HD2_RetroReelStereo", "delay"), ("HD2_CaliQStereo", "eq"),
        ("L6SPB_AcousGtrSimStereo", "filter"), ("HD2_ChorusStereo", "modulation"),
        ("VIC_DynPlateStereo", "reverb"), ("HD2_DL4AutoVolStereo", "volume"),
    ]
    lib = _library_with(
        tmp_path / "lib", [(m, c, "Stereo") for m, c in colliding])
    names = {b.display_name for b in lib.list_blocks()}
    assert len(names) == len(colliding)
    for model_id, _cat in colliding:
        assert lib.find_block(lib.load_block(model_id).display_name).model_id == model_id


def test_the_old_name_keeps_working_as_a_legacy_alias(tmp_path):
    """A recipe written against a pre-fix library still resolves."""
    lib = _library_with(tmp_path / "lib", [
        ("HD2_ReverbPlateStereo", "reverb", "Plate Stereo"),
        ("HX2_ImpulseResponseWithPan", "cab", "With Pan"),
    ])
    assert lib.find_block("Plate").model_id == "HD2_ReverbPlateStereo"
    assert lib.find_block("Plate Stereo").model_id == "HD2_ReverbPlateStereo"
    assert lib.find_block("With Pan").model_id == "HX2_ImpulseResponseWithPan"
    # A degenerate old name is NOT bridged — it never named one block anyway.
    assert "Stereo" not in lib.load_block("HD2_ReverbPlateStereo").aliases


def test_a_legacy_alias_never_shadows_another_blocks_real_name(tmp_path):
    """`Woody Blue` is the amp, even though it is the preamp's old name."""
    lib = _library_with(tmp_path / "lib", [
        ("HD2_AmpWoodyBlue", "amp", "Woody Blue"),
        ("HD2_PreampWoodyBlue", "amp", "Woody Blue"),
    ])
    assert "Woody Blue" in lib.load_block("HD2_PreampWoodyBlue").aliases
    assert lib.find_block("Woody Blue").model_id == "HD2_AmpWoodyBlue"
    assert lib.find_block("Woody Blue (Preamp)").model_id == "HD2_PreampWoodyBlue"


def test_a_missing_vendored_asset_never_breaks_a_library_read(tmp_path, monkeypatch):
    """Names degrade; reads do not fail. Every read path comes through here."""
    def boom(_model_id):
        raise ImportError("simulated broken install")

    monkeypatch.setattr(paraminfo, "model_display_name", boom)
    lib = _library_with(tmp_path / "lib", [
        ("VIC_DynPlateStereo", "reverb", "Stereo"),
        ("HD2_ReverbPlateStereo", "reverb", "Plate Stereo"),
    ])
    names = {b.model_id: b.display_name for b in lib.list_blocks()}
    assert names["VIC_DynPlateStereo"] == "Dyn Plate Stereo"   # humanized
    assert names["HD2_ReverbPlateStereo"] == "Plate Stereo"    # stored kept


def test_an_ambiguous_name_names_the_candidate_model_ids(tmp_path):
    # Two models the editor does not know, both curated to the same name.
    lib = _library_with(tmp_path / "lib", [
        ("HD2_DistFakeOne", "drive", "Twin"),
        ("HD2_DistFakeTwo", "drive", "Twin"),
    ])
    with pytest.raises(LookupError) as excinfo:
        lib.find_block("Twin")
    message = str(excinfo.value)
    assert "HD2_DistFakeOne" in message and "HD2_DistFakeTwo" in message
    # ...and the model id is the way out.
    assert lib.find_block("HD2_DistFakeOne").model_id == "HD2_DistFakeOne"


# --- a model id is always an accepted handle --------------------------------

def test_a_recipe_can_place_and_reference_a_block_by_model_id(hsp_library):
    """Placement AND every later reference (snapshot delta, footswitch).

    `_resolve_spec_block` raises `GenerateError` on a reference it cannot
    resolve, so composing at all is the assertion for the snapshot and
    footswitch targets below.
    """
    from helixgen.generate import compose_preset
    from helixgen.spec import parse_spec

    preset = compose_preset(parse_spec({
        "name": "By Model Id",
        "paths": [{"blocks": [
            {"block": "HD2_DistTube", "params": {"Gain": 0.6}},
            {"block": "HD2_AmpBrit"},
        ]}],
        "snapshots": [{"name": "Lead", "params": {
            "HD2_DistTube": {"Gain": 0.9}}}],
        "footswitches": [{"switch": "FS1", "block": "HD2_DistTube"}],
    }), hsp_library, source="t")

    models = [
        slot["slot"][0]["model"]
        for flow in preset["preset"]["flow"] for key, slot in flow.items()
        if key.startswith("b") and key[1:].isdigit()
    ]
    assert "HD2_DistTube" in models and "HD2_AmpBrit" in models


# --- the shipped library, end to end ----------------------------------------

def _real_library() -> Library | None:
    root = Path(os.environ.get("HELIXGEN_LIBRARY") or Path.home() / ".helixgen/library")
    return Library(root=root) if (root / "blocks").is_dir() else None


def test_every_block_in_the_real_library_has_a_unique_name():
    """The guarantee that matters, over the library actually in use.

    Skipped on a machine with no ingested library (a clean clone), same as the
    other real-data fixtures in this suite.
    """
    lib = _real_library()
    if lib is None:
        pytest.skip("no ingested block library on this machine")
    blocks = lib.list_blocks()
    if not blocks:
        pytest.skip("block library is empty")
    by_name: dict[str, list[str]] = {}
    for block in blocks:
        assert block.display_name, block.model_id
        by_name.setdefault(block.display_name, []).append(block.model_id)
    assert {n: ids for n, ids in by_name.items() if len(ids) > 1} == {}
    assert len(by_name) == len(blocks)
