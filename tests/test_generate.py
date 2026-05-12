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


# ---------------------------------------------------------------------------
# .hsp (Stadium) chassis shape — shape-aware dispatch in compose_preset.
# ---------------------------------------------------------------------------

from helixgen.hsp import HSP_MAGIC, extract_blocks_from_hsp, read_hsp


def _hsp_fixture_payload():
    """A Stadium .hsp payload with 1 path holding a drive, an amp, and a cab.
    Slot params are wrapped in {"value": ...} as the wire format requires.
    """
    return {
        "meta": {"name": "Seed", "device_id": 0, "device_version": 38},
        "preset": {
            "flow": [
                {
                    "@enabled": True,
                    "b00": {
                        "type": "input",
                        "position": 0,
                        "path": 0,
                        "slot": [{"model": "P35_InputInst1", "params": {}, "version": 0}],
                    },
                    "b01": {
                        "type": "fx",
                        "position": 1,
                        "path": 0,
                        "slot": [{
                            "model": "HD2_DistScream808Mono",  # gets translated to HD2_DrvScream808
                            "@enabled": {"value": True},
                            "params": {
                                "Gain": {"value": 0.4},
                                "Tone": {"value": 0.5},
                                "Level": {"value": 0.6},
                            },
                            "version": 0,
                        }],
                    },
                    "b02": {
                        "type": "amp",
                        "position": 3,
                        "path": 0,
                        "slot": [{
                            "model": "HD2_AmpBrit2204",
                            "@enabled": {"value": True},
                            "params": {
                                "Drive": {"value": 0.62},
                                "Master": {"value": 0.36},
                            },
                            "version": 0,
                        }],
                    },
                    "b03": {
                        "type": "cab",
                        "position": 4,
                        "path": 0,
                        "slot": [{
                            "model": "HD2_Cab4x12Greenback25",
                            "@enabled": {"value": True},
                            "params": {
                                "LowCut": {"value": 80.0},
                                "HighCut": {"value": 8000.0},
                            },
                            "version": 0,
                        }],
                    },
                    "b13": {
                        "type": "output",
                        "position": 13,
                        "path": 0,
                        "slot": [{"model": "P35_OutputMatrix", "params": {}, "version": 0}],
                    },
                },
                {  # empty second path
                    "b00": {
                        "type": "input",
                        "position": 0,
                        "path": 1,
                        "slot": [{"model": "P35_InputNone", "params": {}}],
                    },
                    "b13": {
                        "type": "output",
                        "position": 13,
                        "path": 1,
                        "slot": [{"model": "P35_OutputMatrix", "params": {}}],
                    },
                },
            ],
        },
    }


def _populate_hsp_library(lib, tmp_path):
    """Ingest the fixture .hsp into `lib` so it has a Stadium chassis + blocks."""
    from helixgen.ingest import ingest_file
    f = tmp_path / "seed.hsp"
    f.write_bytes(HSP_MAGIC + json.dumps(_hsp_fixture_payload()).encode("utf-8"))
    ingest_file(f, lib)
    lib.rebuild_index()


def test_compose_preset_hsp_returns_hsp_shape(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)

    spec = parse_spec({
        "name": "HSP Test",
        "paths": [{"blocks": [
            {"block": "Scream 808"},
            {"block": "Brit 2204"},
            {"block": "4x12 Greenback 25"},
        ]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    # .hsp shape: preset.flow exists, data.tone.dsp0 does not
    assert "preset" in preset and "flow" in preset["preset"]
    assert "data" not in preset or "tone" not in preset.get("data", {})


def test_compose_preset_hsp_places_blocks_in_sequential_bNN_slots(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)

    spec = parse_spec({
        "name": "S",
        "paths": [{"blocks": [
            {"block": "Scream 808"},
            {"block": "Brit 2204"},
            {"block": "4x12 Greenback 25"},
        ]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    path0 = preset["preset"]["flow"][0]
    assert "b01" in path0 and "b02" in path0 and "b03" in path0
    # Endpoints preserved from chassis
    assert "b00" in path0 and "b13" in path0


def test_compose_preset_hsp_enables_placed_blocks_at_bnn_level(tmp_library, tmp_path):
    """Real Stadium exports carry @enabled at the bNN level — that's the bypass
    switch the device reads. If we emit only slot-level @enabled the block
    loads as bypassed. Every block we place must be enabled by default.
    """
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)

    spec = parse_spec({
        "name": "Enabled",
        "paths": [{"blocks": [
            {"block": "Scream 808"},
            {"block": "Brit 2204"},
            {"block": "4x12 Greenback 25"},
        ]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    path0 = preset["preset"]["flow"][0]
    for key in ("b01", "b02", "b03"):
        assert path0[key].get("@enabled") == {"value": True}, (
            f"bNN-level @enabled missing/false on {key}"
        )


def test_compose_preset_hsp_wraps_params_in_value_envelope(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)

    spec = parse_spec({
        "name": "S",
        "paths": [{"blocks": [{"block": "Brit 2204", "params": {"Drive": 0.9}}]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    slot = preset["preset"]["flow"][0]["b01"]["slot"][0]
    # User override applied AND rewrapped
    assert slot["params"]["Drive"] == {"value": 0.9}
    # Exemplar params survive and are also wrapped
    assert slot["params"]["Master"] == {"value": 0.36}
    # @enabled also wrapped
    assert slot["@enabled"] == {"value": True}


def test_compose_preset_hsp_translates_library_id_back_to_stadium(tmp_library, tmp_path):
    """Library stores HD2_DrvScream808 (the .hlx-normalized id); .hsp output
    must use the Stadium-side id HD2_DistScream808Mono so the device loads it.
    """
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)

    spec = parse_spec({
        "name": "S",
        "paths": [{"blocks": [{"block": "Scream 808"}]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    slot = preset["preset"]["flow"][0]["b01"]["slot"][0]
    assert slot["model"] == "HD2_DistScream808Mono"


def test_compose_preset_hsp_strips_chassis_marker(tmp_library, tmp_path):
    """The output .hsp must not carry the helixgen chassis-shape marker
    (it's a private library annotation, not part of the wire format).
    """
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)

    spec = parse_spec({
        "name": "S",
        "paths": [{"blocks": [{"block": "Brit 2204"}]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    assert "_helixgen_chassis_shape" not in preset


def test_compose_preset_hsp_sets_meta_name(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)

    spec = parse_spec({
        "name": "My Stadium Preset",
        "paths": [{"blocks": [{"block": "Brit 2204"}]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    assert preset["meta"]["name"] == "My Stadium Preset"


def test_compose_preset_hsp_too_many_paths_errors(tmp_library, tmp_path):
    """Spec has more paths than the .hsp chassis flow can hold → clean error."""
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)

    # The fixture chassis has 2 flow paths. Force a spec with 3? Spec parser
    # caps at 2 — so use a chassis with 1 flow path and a 2-path spec instead.
    # Simulate that by chopping the chassis's second flow entry.
    chassis = lib.load_chassis()
    chassis["preset"]["flow"] = chassis["preset"]["flow"][:1]
    lib.save_chassis(chassis)

    spec = parse_spec({
        "name": "X",
        "paths": [
            {"blocks": [{"block": "Brit 2204"}]},
            {"blocks": [{"block": "Brit 2204"}]},
        ],
    }, source="t.json")
    with pytest.raises(GenerateError, match="paths"):
        compose_preset(spec, lib, source="t.json")


def test_compose_preset_unknown_chassis_shape_errors(tmp_library, tmp_path):
    """Future-proofing: an unrecognized chassis shape must fail cleanly."""
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    chassis = lib.load_chassis()
    chassis["_helixgen_chassis_shape"] = "future-shape"
    lib.save_chassis(chassis)

    spec = parse_spec({
        "name": "X",
        "paths": [{"blocks": [{"block": "Brit 2204"}]}],
    }, source="t.json")
    with pytest.raises(GenerateError, match="chassis shape"):
        compose_preset(spec, lib, source="t.json")


def test_generate_preset_writes_hsp_with_magic_header(tmp_library, tmp_path):
    """When the chassis is .hsp shape, generate_preset must emit an .hsp file
    (8-byte magic header + JSON body), readable by read_hsp.
    """
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "Output Test",
        "paths": [{"blocks": [{"block": "Brit 2204"}]}],
    }))
    out_path = tmp_path / "out.hsp"
    generate_preset(spec_path, out_path, lib)

    # File starts with the .hsp magic, and read_hsp can parse it.
    raw = out_path.read_bytes()
    assert raw.startswith(HSP_MAGIC)
    parsed = read_hsp(out_path)
    assert parsed["meta"]["name"] == "Output Test"
    assert "b01" in parsed["preset"]["flow"][0]


# ---------------------------------------------------------------------------
# .hsp snapshots — Stadium scenes. Each spec snapshot is a delta from the
# base path values (disable + param overrides). The generator emits 8
# snapshot metadata slots in preset.snapshots, sets activesnapshot=0, and
# inlines per-snapshot variation as `{"value": base, "snapshots": [...]}`
# wrappers only where it actually varies.
# ---------------------------------------------------------------------------


def _spec_with_snapshots(snapshots):
    return parse_spec({
        "name": "S",
        "paths": [{"blocks": [
            {"block": "Scream 808"},
            {"block": "Brit 2204", "params": {"Drive": 0.5}},
            {"block": "4x12 Greenback 25"},
        ]}],
        "snapshots": snapshots,
    }, source="t.json")


def test_compose_preset_hsp_no_snapshots_keeps_plain_value_wrappers(tmp_library, tmp_path):
    """Spec with no snapshots: per-block values stay as plain {"value": x}."""
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)

    spec = parse_spec({
        "name": "S",
        "paths": [{"blocks": [{"block": "Brit 2204"}]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    b01 = preset["preset"]["flow"][0]["b01"]
    # bNN @enabled has no snapshots array
    assert "snapshots" not in b01["@enabled"]
    # No param has a snapshots array either
    for v in b01["slot"][0]["params"].values():
        assert "snapshots" not in v


def test_compose_preset_hsp_emits_snapshot_metadata(tmp_library, tmp_path):
    """preset.snapshots is an 8-entry list; user-named slots use spec names,
    unused slots get auto names. All 8 are valid (so the device sees them).
    """
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = _spec_with_snapshots([
        {"name": "Rhythm"},
        {"name": "Lead"},
        {"name": "Clean"},
    ])

    preset = compose_preset(spec, lib, source="t.json")
    snaps = preset["preset"]["snapshots"]
    assert len(snaps) == 8
    assert snaps[0]["name"] == "Rhythm"
    assert snaps[1]["name"] == "Lead"
    assert snaps[2]["name"] == "Clean"
    # Unused slots get placeholder names but are still valid
    assert snaps[3]["name"] == "Snap 4"
    assert all(s.get("valid") is True for s in snaps)


def test_compose_preset_hsp_sets_active_snapshot_to_zero(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = _spec_with_snapshots([{"name": "Rhythm"}, {"name": "Lead"}])

    preset = compose_preset(spec, lib, source="t.json")
    assert preset["preset"]["params"]["activesnapshot"] == 0


def test_compose_preset_hsp_disable_emits_bnn_snapshots_array(tmp_library, tmp_path):
    """A block disabled in snapshot N must emit @enabled with snapshots[N]=False."""
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = _spec_with_snapshots([
        {"name": "Rhythm"},
        {"name": "Clean", "disable": ["Scream 808"]},
        {"name": "Lead"},
    ])

    preset = compose_preset(spec, lib, source="t.json")
    drive_bnn = preset["preset"]["flow"][0]["b01"]
    en = drive_bnn["@enabled"]
    assert en["value"] is True
    assert en["snapshots"] == [None, False, None, None, None, None, None, None]


def test_compose_preset_hsp_undisabled_block_has_no_snapshots_array(tmp_library, tmp_path):
    """Even if other blocks have snapshot variation, untouched blocks stay plain."""
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = _spec_with_snapshots([
        {"name": "Rhythm"},
        {"name": "Clean", "disable": ["Scream 808"]},
    ])

    preset = compose_preset(spec, lib, source="t.json")
    amp_bnn = preset["preset"]["flow"][0]["b02"]
    assert "snapshots" not in amp_bnn["@enabled"]


def test_compose_preset_hsp_param_override_emits_snapshots_array(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = _spec_with_snapshots([
        {"name": "Rhythm"},
        {"name": "Lead", "params": {"Brit 2204": {"Drive": 0.9}}},
        {"name": "Clean", "params": {"Brit 2204": {"Drive": 0.3}}},
    ])

    preset = compose_preset(spec, lib, source="t.json")
    drive_param = preset["preset"]["flow"][0]["b02"]["slot"][0]["params"]["Drive"]
    assert drive_param["value"] == 0.5  # base from spec
    assert drive_param["snapshots"] == [None, 0.9, 0.3, None, None, None, None, None]


def test_compose_preset_hsp_param_without_override_stays_plain(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = _spec_with_snapshots([
        {"name": "Lead", "params": {"Brit 2204": {"Drive": 0.9}}},
    ])

    preset = compose_preset(spec, lib, source="t.json")
    master_param = preset["preset"]["flow"][0]["b02"]["slot"][0]["params"]["Master"]
    assert "snapshots" not in master_param


def test_compose_preset_hsp_snapshot_disable_unknown_block_errors(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = _spec_with_snapshots([{"name": "X", "disable": ["No Such Block"]}])
    with pytest.raises(GenerateError, match="No Such Block"):
        compose_preset(spec, lib, source="t.json")


def test_compose_preset_hsp_snapshot_params_unknown_block_errors(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = _spec_with_snapshots([
        {"name": "X", "params": {"Nonexistent Block": {"Drive": 0.5}}},
    ])
    with pytest.raises(GenerateError, match="Nonexistent Block"):
        compose_preset(spec, lib, source="t.json")


def test_compose_preset_hsp_snapshot_params_unknown_param_errors(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = _spec_with_snapshots([
        {"name": "X", "params": {"Brit 2204": {"NotARealKnob": 0.5}}},
    ])
    with pytest.raises(ParamValidationError, match="NotARealKnob"):
        compose_preset(spec, lib, source="t.json")
