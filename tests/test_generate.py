import json
from pathlib import Path

import pytest

from helixgen.chassis import extract_chassis
from helixgen.generate import (
    GenerateError,
    ParamValidationError,
    compose_preset,
    generate_preset,
    resolve_blocks,
    validate_params,
)
from helixgen.ingest import block_from_raw
from helixgen.library import Library
from helixgen.spec import parse_spec


def populate_library(lib, sample_amp_block, sample_cab_block):
    src = {"preset": "fixture", "firmware": "test", "date": "2026-05-01"}
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.save_block_with_dedup(block_from_raw(sample_cab_block, src))
    lib.rebuild_index()


def populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block):
    populate_library(lib, sample_amp_block, sample_cab_block)
    lib.save_chassis(extract_chassis(sample_serial_preset))


# ---- resolve_blocks ----

def test_resolve_blocks_by_display_name(tmp_library, sample_amp_block, sample_cab_block):
    lib = Library(tmp_library)
    populate_library(lib, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "Test",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom", "params": {"Drive": 0.8}}]}],
    }, source="t.json")

    resolved = resolve_blocks(spec, lib)
    assert len(resolved) == 1
    assert len(resolved[0]) == 1
    block, user_params = resolved[0][0]
    assert block.model_id == "HD2_AmpBrit2204Custom"
    assert user_params == {"Drive": 0.8}


def test_resolve_blocks_missing_block_raises(tmp_library, sample_amp_block, sample_cab_block):
    lib = Library(tmp_library)
    populate_library(lib, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "Test",
        "paths": [{"blocks": [{"block": "Nonexistent"}]}],
    }, source="t.json")

    with pytest.raises(KeyError, match="not found in library"):
        resolve_blocks(spec, lib)


def test_resolve_blocks_ambiguous_raises(tmp_library, sample_amp_block):
    lib = Library(tmp_library)
    src = {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    other = dict(sample_amp_block)
    other["@model"] = "HD2_AmpBrit2204Variant"
    other["@name"] = "Brit 2204 Custom"
    lib.save_block_with_dedup(block_from_raw(other, src))
    lib.rebuild_index()

    spec = parse_spec({
        "name": "T",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }, source="t.json")

    with pytest.raises(LookupError, match="multiple library entries"):
        resolve_blocks(spec, lib)


# ---- validate_params ----

def test_validate_params_known_keys_pass(tmp_library, sample_amp_block, sample_cab_block):
    lib = Library(tmp_library)
    populate_library(lib, sample_amp_block, sample_cab_block)
    block = lib.find_block("Brit 2204 Custom")
    validate_params(block, {"Drive": 0.7, "Bass": 0.5})


def test_validate_params_unknown_key_raises(tmp_library, sample_amp_block, sample_cab_block):
    lib = Library(tmp_library)
    populate_library(lib, sample_amp_block, sample_cab_block)
    block = lib.find_block("Brit 2204 Custom")
    with pytest.raises(ParamValidationError) as excinfo:
        validate_params(block, {"Drive2": 0.7})
    msg = str(excinfo.value)
    assert "Drive2" in msg
    assert "Brit 2204 Custom" in msg
    assert "Drive" in msg


def test_validate_params_lists_all_unknown_keys(tmp_library, sample_amp_block, sample_cab_block):
    lib = Library(tmp_library)
    populate_library(lib, sample_amp_block, sample_cab_block)
    block = lib.find_block("Brit 2204 Custom")
    with pytest.raises(ParamValidationError) as excinfo:
        validate_params(block, {"Drive2": 0, "BassX": 0})
    msg = str(excinfo.value)
    assert "Drive2" in msg
    assert "BassX" in msg


# ---- compose_preset ----

def test_compose_preset_places_blocks_at_dsp_top_level(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "Composed",
        "paths": [{"blocks": [
            {"block": "Brit 2204 Custom", "params": {"Drive": 0.99}},
            {"block": "4x12 Greenback 25"},
        ]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    dsp0 = preset["data"]["tone"]["dsp0"]
    assert dsp0["block0"]["@model"] == "HD2_AmpBrit2204Custom"
    assert dsp0["block0"]["Drive"] == 0.99
    assert dsp0["block0"]["Bass"] == 0.5
    assert dsp0["cab0"]["@model"] == "HD2_Cab4x12Greenback25"
    # No stray block1 (only one non-cab block in the chain)
    assert "block1" not in dsp0


def test_compose_preset_links_amp_to_paired_cab(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    """When an amp is followed by a cab, set the amp's @cab to the cab slot key."""
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "Linked",
        "paths": [{"blocks": [
            {"block": "Brit 2204 Custom"},
            {"block": "4x12 Greenback 25"},
        ]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    dsp0 = preset["data"]["tone"]["dsp0"]
    assert dsp0["block0"]["@cab"] == "cab0"


def test_compose_preset_keeps_dsp_infrastructure(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    """Composed presets must keep inputA/outputA/split/join from the chassis."""
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "Keep",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    dsp0 = preset["data"]["tone"]["dsp0"]
    for key in ("inputA", "inputB", "outputA", "outputB", "split", "join"):
        assert key in dsp0, f"missing infrastructure key {key!r}"


def test_compose_preset_sets_meta_name(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "My Cool Preset",
        "author": "mike",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    assert preset["data"]["meta"]["name"] == "My Cool Preset"
    assert preset["data"]["meta"]["author"] == "mike"


def test_compose_preset_writes_provenance(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "X",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }, source="my-spec.json")

    preset = compose_preset(spec, lib, source="my-spec.json")
    prov = preset["data"]["meta"]["helixgen"]
    assert prov["spec_source"] == "my-spec.json"
    assert "version" in prov
    assert "generated_at" in prov


# ---- generate_preset ----

def test_generate_preset_writes_file(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block, tmp_path
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "Disk Test",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }))

    out_path = tmp_path / "out.hlx"
    generate_preset(spec_path, out_path, lib)

    assert out_path.exists()
    content = json.loads(out_path.read_text())
    assert content["data"]["meta"]["name"] == "Disk Test"


def test_generate_preset_pretty_prints(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block, tmp_path
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "X",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }))
    out_path = tmp_path / "out.hlx"
    generate_preset(spec_path, out_path, lib)

    text = out_path.read_text()
    assert "\n" in text
    assert text.startswith("{")


from helixgen.ingest import ingest_path


def test_goldfinger_generates_successfully(
    tmp_library, sample_serial_preset, tmp_path
):
    """Acceptance test: generate the canonical Goldfinger preset from spec + library."""
    preset_path = tmp_path / "seed.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)
    ingest_path(preset_path, lib)

    spec_path = Path("tests/fixtures/specs/goldfinger.json")
    out_path = tmp_path / "goldfinger.hlx"
    generate_preset(spec_path, out_path, lib)

    out = json.loads(out_path.read_text())
    assert out["data"]["meta"]["name"] == "Goldfinger Superman Rhythm"
    assert out["data"]["meta"]["author"] == "mike"
    dsp0 = out["data"]["tone"]["dsp0"]
    block_keys = sorted(k for k in dsp0 if k.startswith("block"))
    cab_keys = sorted(k for k in dsp0 if k.startswith("cab"))
    block_models = [dsp0[k]["@model"] for k in block_keys]
    cab_models = [dsp0[k]["@model"] for k in cab_keys]
    assert block_models == [
        "HD2_DrvScream808",
        "HD2_AmpBrit2204Custom",
        "HD2_DlyDigital",
        "HD2_RvbPlate",
    ]
    assert cab_models == ["HD2_Cab4x12Greenback25"]
    # Amp param overlay survives
    amp_slot = next(k for k in block_keys if dsp0[k]["@model"] == "HD2_AmpBrit2204Custom")
    assert dsp0[amp_slot]["Mid"] == 0.75
    # Reverb param overlay survives
    rvb_slot = next(k for k in block_keys if dsp0[k]["@model"] == "HD2_RvbPlate")
    assert dsp0[rvb_slot]["Mix"] == 0.10
    # Amp got auto-linked to the cab placed after it
    assert dsp0[amp_slot]["@cab"] == "cab0"
