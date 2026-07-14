"""Controller depth (parity #21): FS→param toggles, merge switches, scribble
labels/colors, curve/threshold — spec parsing, `.hsp` synthesis, and `view`
round-trip. Field shapes are evidence-derived; see
docs/superpowers/specs/2026-07-14-controller-depth-device-info-design.md.
"""
import pytest

from helixgen import controllers
from helixgen.generate import compose_preset
from helixgen.spec import SpecError, parse_spec
from helixgen.view import view


# --- vocabulary tables -------------------------------------------------------

def test_curve_vocabulary_and_index():
    assert controllers.CURVES[5] == "linear"
    assert controllers.curve_index("linear") == 5
    assert controllers.curve_index("slow5") == 0
    assert controllers.curve_index("fast5") == 10
    with pytest.raises(controllers.ControllerError):
        controllers.curve_index("banana")


def test_color_palette_anchors():
    # Hardware-anchored pairings (live pulls vs the same presets' exports).
    assert controllers.color_int("auto") == 1
    assert controllers.color_int("red") == 2
    assert controllers.color_int("dkorange") == 3
    assert controllers.color_int("ltorange") == 4
    assert controllers.color_int("purple") == 9
    assert controllers.color_int("white") == 11
    with pytest.raises(controllers.ControllerError):
        controllers.color_int("mauve")


# --- spec parsing ------------------------------------------------------------

def _spec(fs=None, exp=None, blocks=("Tube Drive",)):
    d = {"name": "n", "paths": [{"blocks": [{"block": b} for b in blocks]}]}
    if fs is not None:
        d["footswitches"] = fs
    if exp is not None:
        d["expression"] = exp
    return d


def test_parse_fs_param_entry():
    spec = parse_spec(_spec(fs=[{"switch": "FS4", "block": "Tube Drive",
                                 "param": "Gain", "min": 0.4, "max": 0.65,
                                 "curve": "fast1", "threshold": 0.1,
                                 "label": "BOOST", "color": "red"}]))
    fs = spec.footswitches[0]
    assert fs.param == "Gain" and fs.min == 0.4 and fs.max == 0.65
    assert fs.curve == "fast1" and fs.threshold == 0.1
    assert fs.label == "BOOST" and fs.color == "red"


def test_parse_fs_param_requires_min_max():
    with pytest.raises(SpecError, match="min"):
        parse_spec(_spec(fs=[{"switch": "FS4", "block": "Tube Drive",
                              "param": "Gain"}]))


def test_parse_fs_bypass_rejects_min_max():
    with pytest.raises(SpecError, match="min"):
        parse_spec(_spec(fs=[{"switch": "FS4", "block": "Tube Drive",
                              "min": 0.0, "max": 1.0}]))


def test_parse_fs_bad_curve_and_color():
    with pytest.raises(SpecError, match="curve"):
        parse_spec(_spec(fs=[{"switch": "FS4", "block": "Tube Drive",
                              "curve": "banana"}]))
    with pytest.raises(SpecError, match="color"):
        parse_spec(_spec(fs=[{"switch": "FS4", "block": "Tube Drive",
                              "color": "mauve"}]))


def test_parse_merge_switch_allowed():
    spec = parse_spec(_spec(fs=[
        {"switch": "FS3", "block": "Tube Drive"},
        {"switch": "FS3", "block": "Brit Amp"},
        {"switch": "FS3", "block": "Tube Drive", "param": "Gain",
         "min": 0.2, "max": 0.8},
    ]))
    assert len(spec.footswitches) == 3


def test_parse_duplicate_target_rejected():
    with pytest.raises(SpecError, match="duplicate footswitch target"):
        parse_spec(_spec(fs=[
            {"switch": "FS3", "block": "Tube Drive"},
            {"switch": "FS4", "block": "Tube Drive"},
        ]))
    with pytest.raises(SpecError, match="duplicate footswitch target"):
        parse_spec(_spec(fs=[
            {"switch": "FS3", "block": "Tube Drive", "param": "Gain",
             "min": 0.0, "max": 1.0},
            {"switch": "FS4", "block": "Tube Drive", "param": "Gain",
             "min": 0.1, "max": 0.9},
        ]))


def test_parse_conflicting_strip_rejected_identical_ok():
    with pytest.raises(SpecError, match="conflicting label/color"):
        parse_spec(_spec(fs=[
            {"switch": "FS3", "block": "Tube Drive", "label": "A"},
            {"switch": "FS3", "block": "Brit Amp", "label": "B"},
        ]))
    spec = parse_spec(_spec(fs=[
        {"switch": "FS3", "block": "Tube Drive", "label": "A", "color": "red"},
        {"switch": "FS3", "block": "Brit Amp", "label": "A", "color": "red"},
    ]))
    assert len(spec.footswitches) == 2


def test_parse_expression_curve():
    spec = parse_spec(_spec(exp=[{"pedal": "EXP1", "targets": [
        {"block": "Tube Drive", "param": "Gain", "curve": "slow2"}]}]))
    assert spec.expression[0].targets[0].curve == "slow2"
    with pytest.raises(SpecError, match="curve"):
        parse_spec(_spec(exp=[{"pedal": "EXP1", "targets": [
            {"block": "Tube Drive", "param": "Gain", "curve": "nope"}]}]))


def test_parse_param_on_fs_and_exp_rejected():
    with pytest.raises(SpecError, match="one controller per param"):
        parse_spec(_spec(
            fs=[{"switch": "FS3", "block": "Tube Drive", "param": "Gain",
                 "min": 0.0, "max": 1.0}],
            exp=[{"pedal": "EXP1", "targets": [
                {"block": "Tube Drive", "param": "Gain"}]}],
        ))


# --- .hsp synthesis ----------------------------------------------------------

def _compose(hsp_library, fs=None, exp=None, blocks=("Tube Drive",)):
    return compose_preset(parse_spec(_spec(fs=fs, exp=exp, blocks=blocks)),
                          hsp_library, source="t")


def test_fs_param_controller_shape(hsp_library):
    preset = _compose(hsp_library, fs=[
        {"switch": "FS4", "block": "Tube Drive", "param": "Gain",
         "min": 0.4, "max": 0.65, "behavior": "momentary"}])
    b01 = preset["preset"]["flow"][0]["b01"]
    assert "controller" not in b01["@enabled"]
    ctrl = b01["slot"][0]["params"]["Gain"]["controller"]
    assert ctrl["type"] == "param"
    assert ctrl["behavior"] == "momentary"
    assert ctrl["min"] == 0.4 and ctrl["max"] == 0.65
    assert ctrl["curve"] == "linear" and ctrl["threshold"] == 0.0
    assert ctrl["source"] == 0x01010103
    assert str(0x01010103) in preset["preset"]["sources"]


def test_fs_param_raw_units_pass_through(hsp_library):
    # Raw param units (e.g. dB) — corpus-real: Level min=-7.0 max=-5.2.
    preset = _compose(hsp_library, fs=[
        {"switch": "FS4", "block": "Tube Drive", "param": "Gain",
         "min": -7.0, "max": -5.2}])
    ctrl = preset["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Gain"]["controller"]
    assert ctrl["min"] == -7.0 and ctrl["max"] == -5.2


def test_fs_param_unknown_param_raises(hsp_library):
    from helixgen.mutate import MutateError
    with pytest.raises(MutateError, match="unknown param"):
        _compose(hsp_library, fs=[
            {"switch": "FS4", "block": "Tube Drive", "param": "Nope",
             "min": 0.0, "max": 1.0}])


def test_fs_label_color_written_to_sources(hsp_library):
    preset = _compose(hsp_library, fs=[
        {"switch": "FS3", "block": "Tube Drive", "label": "DRIVE", "color": "red"}])
    entry = preset["preset"]["sources"][str(0x01010102)]
    assert entry["fs_label"] == "DRIVE"
    assert entry["fs_color"] == "red"
    assert entry["fs_topidx"] == 0
    assert entry["bypass"] is False


def test_fs_long_label_warns(hsp_library, capsys):
    _compose(hsp_library, fs=[
        {"switch": "FS3", "block": "Tube Drive", "label": "THIRTEEN CHR."}])
    assert "at most 12" in capsys.readouterr().err


def test_fs_bypass_curve_and_threshold(hsp_library):
    preset = _compose(hsp_library, fs=[
        {"switch": "FS3", "block": "Tube Drive", "curve": "fast2",
         "threshold": 0.65}])
    ctrl = preset["preset"]["flow"][0]["b01"]["@enabled"]["controller"]
    assert ctrl["curve"] == "fast2"
    # An explicit threshold forces the explicit-bounds encoding (the
    # corpus-majority shape: min=False, max=True, numeric threshold).
    assert ctrl["threshold"] == 0.65
    assert ctrl["min"] is False and ctrl["max"] is True


def test_fs_digital_bypass_keeps_null_bounds(hsp_library):
    preset = _compose(hsp_library, fs=[{"switch": "FS3", "block": "Tube Drive"}])
    ctrl = preset["preset"]["flow"][0]["b01"]["@enabled"]["controller"]
    assert ctrl["min"] is None and ctrl["max"] is None and ctrl["threshold"] is None


def test_merge_switch_two_blocks_one_source(hsp_library):
    preset = _compose(hsp_library, blocks=("Tube Drive", "Brit Amp"), fs=[
        {"switch": "FS3", "block": "Tube Drive"},
        {"switch": "FS3", "block": "Brit Amp"},
    ])
    flow0 = preset["preset"]["flow"][0]
    c1 = flow0["b01"]["@enabled"]["controller"]
    c2 = flow0["b02"]["@enabled"]["controller"]
    assert c1["source"] == c2["source"] == 0x01010102
    # one physical source, one sources entry
    assert list(preset["preset"]["sources"].keys()).count(str(0x01010102)) == 1


def test_exp_curve_propagates(hsp_library):
    preset = _compose(hsp_library, exp=[{"pedal": "EXP2", "targets": [
        {"block": "Tube Drive", "param": "Gain", "curve": "slow1"}]}])
    ctrl = preset["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Gain"]["controller"]
    assert ctrl["curve"] == "slow1"
    assert ctrl["behavior"] == "continuous"


# --- view round-trip ---------------------------------------------------------

def test_view_recovers_fs_param_entry(hsp_library):
    preset = _compose(hsp_library, fs=[
        {"switch": "FS4", "block": "Tube Drive", "param": "Gain",
         "min": 0.4, "max": 0.65, "behavior": "momentary"}])
    spec = view(preset, hsp_library)
    fs = spec["footswitches"]
    assert len(fs) == 1
    entry = fs[0]
    assert entry["switch"] == "FS4" and entry["param"] == "Gain"
    assert entry["min"] == 0.4 and entry["max"] == 0.65
    assert entry["behavior"] == "momentary"
    # and the projection re-parses + re-composes
    parse_spec(spec)


def test_view_recovers_label_color_curve_threshold(hsp_library):
    preset = _compose(hsp_library, fs=[
        {"switch": "FS3", "block": "Tube Drive", "curve": "fast2",
         "threshold": 0.65, "label": "DRIVE", "color": "purple"}])
    spec = view(preset, hsp_library)
    entry = spec["footswitches"][0]
    assert entry["curve"] == "fast2"
    assert entry["threshold"] == 0.65
    assert entry["label"] == "DRIVE"
    assert entry["color"] == "purple"
    parse_spec(spec)


def test_view_omits_defaults(hsp_library):
    preset = _compose(hsp_library, fs=[{"switch": "FS3", "block": "Tube Drive"}])
    entry = view(preset, hsp_library)["footswitches"][0]
    for absent in ("curve", "threshold", "label", "color", "param"):
        assert absent not in entry


def test_view_merge_switch_round_trip(hsp_library, strip_provenance):
    fs = [
        {"switch": "FS3", "block": "Tube Drive", "label": "RIG", "color": "red"},
        {"switch": "FS3", "block": "Brit Amp"},
        {"switch": "FS4", "block": "Brit Amp", "param": "Drive",
         "min": 0.3, "max": 0.85},
    ]
    preset = _compose(hsp_library, blocks=("Tube Drive", "Brit Amp"), fs=fs)
    spec = view(preset, hsp_library)
    regen = compose_preset(parse_spec(spec), hsp_library, source="t")
    assert strip_provenance(regen) == strip_provenance(preset)


def test_view_exp_curve_round_trip(hsp_library, strip_provenance):
    preset = _compose(hsp_library, exp=[{"pedal": "EXP1", "targets": [
        {"block": "Tube Drive", "param": "Gain", "min": 0.1, "max": 0.9,
         "curve": "fast3"}]}])
    spec = view(preset, hsp_library)
    assert spec["expression"][0]["targets"][0]["curve"] == "fast3"
    regen = compose_preset(parse_spec(spec), hsp_library, source="t")
    assert strip_provenance(regen) == strip_provenance(preset)

def test_authoring_resets_chassis_scribble_carryover(tmp_path, sample_serial_preset_hsp):
    """A chassis whose originating export carried scribble labels must not
    leak them onto an authored tone (they'd read back from `view` as if the
    recipe had set them)."""
    import json
    from helixgen.hsp import HSP_MAGIC
    from helixgen.ingest import ingest_path
    from helixgen.library import Block, Library

    body = json.loads(json.dumps(sample_serial_preset_hsp))
    body["preset"]["sources"] = {
        str(0x01010100): {"bypass": False, "fs_color": "red",
                          "fs_label": "STALE", "fs_topidx": 0},
        str(0x01020100): {"bypass": False},
    }
    chassis = tmp_path / "c.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(body).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(chassis, lib)
    lib.save_block(Block(
        model_id="HD2_DistTube", category="drive", display_name="Tube Drive",
        params={"Gain": {"type": "float"}},
        exemplar={"@model": "HD2_DistTube", "@type": "fx", "@enabled": True,
                  "Gain": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    preset = compose_preset(
        parse_spec(_spec(fs=[{"switch": "FS3", "block": "Tube Drive",
                              "label": "MINE"}])),
        lib, source="t")

    sources = preset["preset"]["sources"]
    stale = sources[str(0x01010100)]
    assert stale["fs_label"] == "" and stale["fs_color"] == "auto"
    assert sources[str(0x01010102)]["fs_label"] == "MINE"
    # and view attributes only the authored label
    spec = view(preset, lib)
    (entry,) = spec["footswitches"]
    assert entry["label"] == "MINE" and "color" not in entry


# --- adversarial-review fixes (2026-07-14) ------------------------------------

def test_duplicate_target_coordinate_aliasing_rejected():
    """A bare reference and an explicitly-coordinated reference to the same
    unique block alias each other — both dedup checks must catch it."""
    with pytest.raises(SpecError, match="duplicate footswitch target"):
        parse_spec(_spec(fs=[
            {"switch": "FS3", "block": "Tube Drive"},
            {"switch": "FS4", "block": "Tube Drive", "path": 0},
        ]))
    with pytest.raises(SpecError, match="one controller per param"):
        parse_spec(_spec(
            fs=[{"switch": "FS3", "block": "Tube Drive", "param": "Gain",
                 "min": 0.0, "max": 1.0}],
            exp=[{"pedal": "EXP1", "targets": [
                {"block": "Tube Drive", "param": "Gain", "path": 0}]}],
        ))


def test_distinct_coordinates_do_not_alias():
    spec = parse_spec(_spec(fs=[
        {"switch": "FS3", "block": "Tube Drive", "pos": 1},
        {"switch": "FS4", "block": "Tube Drive", "pos": 2},
    ]))
    assert len(spec.footswitches) == 2


def test_fs_param_int_min_max_preserved(hsp_library):
    """Corpus-real: FS toggles on INT params carry int min/max (Interval 2->4,
    Transport 0->1). Parse and generate must not float-coerce them."""
    spec = parse_spec(_spec(fs=[{"switch": "FS4", "block": "Tube Drive",
                                 "param": "Gain", "min": 2, "max": 4}]))
    fs = spec.footswitches[0]
    assert isinstance(fs.min, int) and isinstance(fs.max, int)
    preset = _compose(hsp_library, fs=[
        {"switch": "FS4", "block": "Tube Drive", "param": "Gain",
         "min": 2, "max": 4}])
    ctrl = preset["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Gain"]["controller"]
    assert ctrl["min"] == 2 and not isinstance(ctrl["min"], float)
    assert ctrl["max"] == 4 and not isinstance(ctrl["max"], float)
    # and it round-trips exactly
    entry = view(preset, hsp_library)["footswitches"][0]
    assert entry["min"] == 2 and not isinstance(entry["min"], float)


def test_exp_threshold_round_trip(hsp_library, strip_provenance):
    """view emits threshold on EXP targets; parse/generate must consume it."""
    preset = _compose(hsp_library, exp=[{"pedal": "EXP1", "targets": [
        {"block": "Tube Drive", "param": "Gain", "threshold": 0.4}]}])
    ctrl = preset["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Gain"]["controller"]
    assert ctrl["threshold"] == 0.4
    spec = view(preset, hsp_library)
    assert spec["expression"][0]["targets"][0]["threshold"] == 0.4
    regen = compose_preset(parse_spec(spec), hsp_library, source="t")
    assert strip_provenance(regen) == strip_provenance(preset)


def test_wire_footswitch_param_requires_min_max(hsp_library):
    from helixgen import mutate
    from helixgen.mutate import MutateError
    preset = _compose(hsp_library)
    with pytest.raises(MutateError, match="numeric min and max"):
        mutate.wire_footswitch(preset, "FS4", "Tube Drive", "latching",
                               hsp_library, param="Gain")


# --- PR #38 re-review fixes (2026-07-14) ---------------------------------------

def test_exp_exp_duplicate_target_coordinate_aliasing_rejected():
    """F1: the EXP↔EXP duplicate-target guard must treat None coordinates as
    wildcards, like the FS↔FS and FS↔EXP checks already do."""
    with pytest.raises(SpecError, match="duplicate"):
        parse_spec(_spec(exp=[
            {"pedal": "EXP1", "targets": [
                {"block": "Tube Drive", "param": "Gain"}]},
            {"pedal": "EXP2", "targets": [
                {"block": "Tube Drive", "param": "Gain", "pos": 0}]},
        ]))
    with pytest.raises(SpecError, match="duplicate"):
        parse_spec(_spec(exp=[
            {"pedal": "EXP1", "targets": [
                {"block": "Tube Drive", "param": "Gain", "path": 0}]},
            {"pedal": "EXP2", "targets": [
                {"block": "Tube Drive", "param": "Gain"}]},
        ]))


def test_exp_exp_distinct_coordinates_do_not_alias():
    spec = parse_spec(_spec(exp=[
        {"pedal": "EXP1", "targets": [
            {"block": "Tube Drive", "param": "Gain", "pos": 1}]},
        {"pedal": "EXP2", "targets": [
            {"block": "Tube Drive", "param": "Gain", "pos": 2}]},
    ]))
    assert len(spec.expression) == 2


def test_toe_label_color_warns_no_scribble(hsp_library, capsys):
    """F3: label/color on a switch without a scribble strip (EXP1Toe) warns
    and is not written into preset.sources (corpus shape: toe/EXP sources
    carry no fs_* keys)."""
    preset = _compose(hsp_library, fs=[
        {"switch": "EXP1Toe", "block": "Tube Drive", "label": "WAH",
         "color": "red"}])
    assert "no scribble strip" in capsys.readouterr().err
    entry = preset["preset"]["sources"][str(0x01010500)]
    assert "fs_label" not in entry and "fs_color" not in entry


def test_view_routes_unknown_bypass_behavior_to_unknowns(hsp_library):
    """F4: a bypass controller with a behavior outside latching/momentary
    (future firmware / toedown) must go to unknown_controllers, not break
    parse_spec(view(x))."""
    preset = _compose(hsp_library, fs=[{"switch": "FS3", "block": "Tube Drive"}])
    ctrl = preset["preset"]["flow"][0]["b01"]["@enabled"]["controller"]
    ctrl["behavior"] = "toedown"
    spec = view(preset, hsp_library)
    assert "footswitches" not in spec
    (unk,) = spec["unknown_controllers"]
    assert unk["kind"] == "footswitch" and "toedown" in unk["label"]
    parse_spec(spec)  # projection still re-parses


def test_wire_footswitch_bounds_without_param_rejected(hsp_library):
    """F6: min/max on a bypass assignment are rejected via the direct API,
    mirroring spec validation."""
    from helixgen import mutate
    from helixgen.mutate import MutateError
    preset = _compose(hsp_library)
    with pytest.raises(MutateError, match="min.*max.*param|param.*min"):
        mutate.wire_footswitch(preset, "FS4", "Tube Drive", "latching",
                               hsp_library, min=0.0, max=1.0)
