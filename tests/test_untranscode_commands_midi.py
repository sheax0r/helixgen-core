"""Reverse transcode of Command Center commands + MIDI CC bindings (bead
hgc-mid): device content -> ``preset.commands`` / ``preset._helixgen_midi``.

The forward transcoder is the spec; these tests invert it two ways. The
family-coverage tests drive the recipe -> ``.sbe`` -> ``.hsp`` -> ``.sbe``
oracle (``transcode.recipe_to_sbepgsm``), proving every Command Center family
and every MIDI binding survives BYTE-EXACT with no library and no hardware. The
end-to-end tests author a NATIVE ``.hsp`` with the real edit verbs, push it to
device content and pull it back, proving the recovered native keys match the
authored ones and that ``view`` re-projects them.
"""
from __future__ import annotations

import copy

import pytest

pytest.importorskip("msgpack")

from helixgen.device import content, transcode, untranscode  # noqa: E402
from tests.golden import harness  # noqa: E402

AMP = "HD2_AmpBritPlexiNrm"
DRIVE = "HD2_DistMinotaurMono"

FS1 = 0x01010100
FS3 = 0x01010102
INSTANT1 = 0x04040100
INSTANT2 = 0x04040101


def _chassis():
    from helixgen.chassis import extract_chassis_from_hsp
    return extract_chassis_from_hsp(copy.deepcopy(harness._CHASSIS_PAYLOAD))


def _roundtrip(recipe: dict) -> tuple[bytes, dict, bytes]:
    sbe1 = content.encode_content_data(transcode.recipe_to_sbepgsm(recipe))
    body = untranscode.sbe_bytes_to_hsp(sbe1, name="RT")
    sbe2 = transcode.hsp_to_sbepgsm(body)
    return sbe1, body, sbe2


def _assert_byte_exact(recipe: dict) -> dict:
    sbe1, body, sbe2 = _roundtrip(recipe)
    if sbe1 != sbe2:
        a, b = content.decode_any(sbe2), content.decode_any(sbe1)
        assert a == b, "round trip diverged"
        pytest.fail("content-identical but not byte-identical")
    return body


def _cmd(source, type_, func, **params):
    return {"source": source, "type": type_, "func": func, "params": params}


def _one_amp(commands):
    return {"name": "cc", "commands": commands,
            "paths": [{"blocks": [{"block": AMP, "params": {}}]}]}


# --- Command Center families (byte-exact, no library) ------------------------

@pytest.mark.parametrize("command", [
    _cmd(FS1, "PresetSnapshot", 0, Snapshot=3),
    _cmd(FS1, "MIDI", 0, **{"MIDI Ch": 2, "PC": 44, "MSB": 1, "LSB": 2}),    # fs PC
    _cmd(FS1, "MIDI", 1, **{"MIDI Ch": 3, "CC#": 85, "Value": 127}),        # fs CC
    _cmd(FS1, "MIDI", 3, **{"MIDI Ch": 1, "Note": 60, "Velocity": 100}),    # fs Note
    _cmd(FS1, "MIDI", 2, **{"MIDI Ch": 1, "Message": 5}),                   # fs MMC
    _cmd(INSTANT1, "MIDI", 0, **{"MIDI Ch": 4, "PC": 7, "MSB": 0, "LSB": 1}),  # inst PC
    _cmd(INSTANT1, "MIDI", 1, **{"MIDI Ch": 2, "CC#": 20, "Value": 64}),    # inst CC
    _cmd(INSTANT1, "MIDI", 3, **{"MIDI Ch": 1, "Note": 48, "Velocity": 90,
                                 "NoteOff": 1}),                            # inst Note
    _cmd(INSTANT1, "MIDI", 2, **{"MIDI Ch": 1, "Message": 2}),             # inst MMC
])
def test_every_command_family_round_trips_byte_exact(command):
    body = _assert_byte_exact(_one_amp([command]))
    recs = body["preset"]["commands"][str(command["source"])]
    assert len(recs) == 1
    p = recs[0]["params"]
    assert p["Command"]["value"] == command["func"]
    for name, val in command["params"].items():
        assert p[name]["value"] == val


def test_merged_switch_preserves_order_and_ordinals():
    body = _assert_byte_exact(_one_amp([
        _cmd(INSTANT1, "MIDI", 1, **{"MIDI Ch": 1, "CC#": 85, "Value": 127}),
        _cmd(INSTANT1, "MIDI", 1, **{"MIDI Ch": 1, "CC#": 85, "Value": 0}),
    ]))
    recs = body["preset"]["commands"][str(INSTANT1)]
    assert [r["ordinal"] for r in recs] == [0, 1]
    assert [r["params"]["Value"]["value"] for r in recs] == [127, 0]


def test_command_behavior_and_toggle_survive():
    recipe = _one_amp([{"source": FS1, "type": "PresetSnapshot", "func": 0,
                        "behavior": "momentary", "toggle": True,
                        "params": {"Snapshot": 1}}])
    body = _assert_byte_exact(recipe)
    rec = body["preset"]["commands"][str(FS1)][0]
    assert rec["behavior"] == "momentary"
    assert rec["toggle"] is True


def test_commands_on_several_sources_keep_distinct_keys():
    body = _assert_byte_exact(_one_amp([
        _cmd(FS1, "PresetSnapshot", 0, Snapshot=2),
        _cmd(INSTANT2, "MIDI", 0, **{"MIDI Ch": 1, "PC": 5, "MSB": 0, "LSB": 0}),
    ]))
    cmds = body["preset"]["commands"]
    assert set(cmds) == {str(FS1), str(INSTANT2)}


# --- MIDI CC bindings (byte-exact, no library) -------------------------------

def test_midi_bypass_and_params_round_trip_byte_exact():
    recipe = {"name": "m", "paths": [{"blocks": [
        {"block": AMP, "params": {}},
        {"block": DRIVE, "params": {}, "midi_bypass": {"cc": 20},
         "midi_params": {"Gain": {"cc": 61, "min": 0.1, "max": 0.9},
                         "Tone": {"cc": 62, "min": 0.0, "max": 1.0}}},
    ]}]}
    body = _assert_byte_exact(recipe)
    recs = body["preset"]["_helixgen_midi"]
    # bypass first (no param), then the two sweeps in authored order.
    assert recs[0] == {"cc": 20, "path": 0, "lane": 0, "pos": 2,
                       "block": DRIVE, "param": None}
    sweeps = {r["cc"]: r for r in recs[1:]}
    assert sweeps[61]["param"] == "Gain"
    assert sweeps[61]["min"] == pytest.approx(0.1)
    assert sweeps[62]["param"] == "Tone"


def test_midi_binding_on_second_dsp_path_keeps_its_path_index():
    recipe = {"name": "m", "paths": [
        {"blocks": [{"block": AMP, "params": {}}]},
        {"blocks": [{"block": DRIVE, "params": {}, "midi_bypass": {"cc": 30}}]},
    ]}
    body = _assert_byte_exact(recipe)
    rec = body["preset"]["_helixgen_midi"][0]
    assert rec["path"] == 1 and rec["param"] is None


# --- end-to-end with the real edit verbs + view ------------------------------

def _real_library(tmp_path):
    import copy as _copy
    from helixgen.chassis import extract_chassis_from_hsp
    from helixgen.library import Block, Library
    lib = Library(root=tmp_path / "reallib")
    lib.save_chassis(extract_chassis_from_hsp(
        _copy.deepcopy(harness._CHASSIS_PAYLOAD)))
    src = {"preset": "midcmd-test", "firmware": "test", "date": "2026-08-13"}
    for mid, cat, name, params in [
        ("HD2_DistMinotaurMono", "drive", "Minotaur Mono",
         {"Gain": 0.5, "Level": 0.5, "Tone": 0.5}),
        ("HD2_AmpBritPlexiNrm", "amp", "Brit Plexi Nrm",
         {"Bass": 0.5, "Drive": 0.5, "Mid": 0.5, "Treble": 0.5}),
    ]:
        lib.save_block(Block(
            model_id=mid, category=cat, display_name=name,
            params={k: {"type": "float"} for k in params},
            exemplar={"@model": mid, "@type": "fx", "@enabled": True, **params},
            first_seen=src))
    lib.rebuild_index()
    return lib


def _authored(tmp_path):
    """A native .hsp carrying both a command and a MIDI binding, via the recipe
    front door (the same path ``generate`` takes)."""
    from helixgen.recipe import apply_recipe
    library = _real_library(tmp_path)
    recipe = {
        "name": "both",
        "paths": [{"blocks": [
            {"block": "Minotaur Mono", "params": {"Gain": 0.3}},
            {"block": "Brit Plexi Nrm", "params": {"Drive": 0.6}},
        ]}],
        "commands": [
            {"switch": "FS1", "command": "snapshot", "snapshot": 2},
            {"switch": "Instant1", "command": "midi_cc", "cc": 85, "value": 127,
             "channel": 2},
        ],
        "midi": [
            {"cc": 61, "targets": [
                {"block": "Minotaur Mono", "param": "Gain", "min": 0.1,
                 "max": 0.9}]},
            {"cc": 20, "targets": [{"block": "Brit Plexi Nrm", "bypass": True}]},
        ],
    }
    return apply_recipe(recipe, library, chassis=_chassis()), library


def test_native_round_trip_recovers_commands_and_midi(tmp_path):
    body_in, library = _authored(tmp_path)
    sbe = transcode.hsp_to_sbepgsm(body_in)
    body_out = untranscode.sbe_bytes_to_hsp(sbe, name="RT", library=library)

    # Commands come back identical to what the edit verb authored, apart from the
    # device slot handle (cmnd cid_) the recovered side carries for the live
    # editor — it is assigned from the device bytes, so the authored side has
    # none, and the forward path ignores it (the fixed-point assertion below
    # proves it never reaches the .sbe).
    def _no_cid(commands):
        return {src: [{k: v for k, v in rec.items() if k != "cid_"} for rec in recs]
                for src, recs in commands.items()}
    assert _no_cid(body_out["preset"]["commands"]) == \
        _no_cid(body_in["preset"]["commands"])
    # the recovered side DOES carry the device slot handle
    assert all(isinstance(rec.get("cid_"), int)
               for recs in body_out["preset"]["commands"].values() for rec in recs)

    # MIDI records match on everything a re-install reads; ``block`` comes back
    # as the stable model id rather than the display name the author typed (the
    # coordinate is authoritative either way).
    def key(r):
        return {k: r[k] for k in r if k != "block"}
    assert [key(r) for r in body_out["preset"]["_helixgen_midi"]] == \
        [key(r) for r in body_in["preset"]["_helixgen_midi"]]

    # The whole thing is a fixed point: re-install reproduces the device bytes.
    assert transcode.hsp_to_sbepgsm(body_out) == sbe


def test_recovered_body_reprojects_through_view(tmp_path):
    from helixgen.spec import parse_spec
    from helixgen.view import view
    body_in, library = _authored(tmp_path)
    sbe = transcode.hsp_to_sbepgsm(body_in)
    body_out = untranscode.sbe_bytes_to_hsp(sbe, name="RT", library=library)

    projected = view(body_out, library)
    by_switch = {c["switch"]: c for c in projected["commands"]}
    assert by_switch["FS1"]["command"] == "snapshot"
    assert by_switch["Instant1"]["command"] == "midi_cc"
    assert by_switch["Instant1"]["cc"] == 85

    midi = {m["cc"]: m for m in projected["midi"]}
    assert midi[61]["targets"][0]["param"] == "Gain"
    assert midi[20]["targets"][0].get("bypass") is True

    # The projection re-parses cleanly.
    parse_spec({"name": "x", "paths": [{"blocks": [
        {"block": "Minotaur Mono"}, {"block": "Brit Plexi Nrm"}]}],
        "commands": projected["commands"], "midi": projected["midi"]})


def test_out_of_spec_midi_command_surfaces_in_unknowns(tmp_path):
    # hgc-qf3: a device-origin Command Center command whose MIDI channel is
    # outside 1..16 (e.g. a backup that 0-bases the channel) must land in
    # `unknown_controllers` — visible — rather than vanishing silently from the
    # view projection. It still round-trips through preset.commands.
    from helixgen.view import view
    body, library = _authored(tmp_path)
    # Corrupt the Instant1 midi_cc command's channel to an out-of-range 0.
    for recs in body["preset"]["commands"].values():
        for rec in recs:
            if rec.get("type") == "MIDI":
                rec["params"]["MIDI Ch"]["value"] = 0
    proj = view(body, library)
    # No longer emitted as a first-class `commands` entry...
    assert not any(str(c.get("command", "")).startswith("midi")
                   for c in proj.get("commands", []))
    # ...but surfaced (not silently dropped) under unknown_controllers.
    assert any(u.get("kind") == "command"
               for u in proj.get("unknown_controllers", []))
