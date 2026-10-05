"""USB output taps (`usb_taps` preference, `helixgen usb-taps`).

Each tapped path gets a Y split in lane 0 after its last block feeding a
lane-1 USB output at b27 — the structure verified by hand on a Stadium XL.
"""
import copy
import json

import pytest
from click.testing import CliRunner

from helixgen import usbtaps
from helixgen.cli import cli
from helixgen.hsp import dumps_hsp, read_hsp, write_hsp
from helixgen.preferences import PreferencesError, parse_usb_taps

TAPS = {"guitar": "3/4", "mic": "5/6"}


def _io(model: str, pos: int) -> dict:
    return {"@enabled": {"value": True}, "position": pos, "path": 0,
            "slot": [{"model": model, "@enabled": {"value": True}, "params": {}}],
            "type": "input" if pos == 0 else "output"}


def _fx(pos: int) -> dict:
    return {"@enabled": {"value": True, "snapshots": [True] * 8}, "type": "fx",
            "position": pos % 14, "path": pos // 14,
            "slot": [{"model": "HD2_DistMinotaurMono", "params": {}}]}


def _path(inp="P35_InputInst1", out="P35_OutputMatrix", cells=()) -> dict:
    p = {"b00": _io(inp, 0), "b13": _io(out, 13)}
    for c in cells:
        p[f"b{c:02d}"] = _fx(c)
    return p


def _body(*paths, name="T") -> dict:
    return {"meta": {"name": name}, "preset": {"flow": list(paths)}}


def _models(path: dict) -> dict:
    return {k: usbtaps._model(v) for k, v in path.items() if k not in ("b00", "b13")}


# --- placement + roles -----------------------------------------------------

def test_single_path_gets_the_guitar_tap_at_b11():
    body = _body(_path(cells=(1, 2, 3)))
    assert usbtaps.apply(body, TAPS)["status"] == "added"
    f = body["preset"]["flow"][0]
    assert f["b11"]["type"] == "split" and f["b11"]["position"] == 11
    assert usbtaps._model(f["b11"]) == "P35_AppDSPSplitY"
    assert (f["b27"]["type"], f["b27"]["position"], f["b27"]["path"]) == ("output", 13, 1)
    assert usbtaps._model(f["b27"]) == "P35_OutputUSB3_4"


def test_mic_path_gets_the_mic_pair():
    body = _body(_path(cells=(1, 2)), _path(inp="P35_InputMic"))
    assert usbtaps.apply(body, TAPS)["status"] == "added"
    g, m = body["preset"]["flow"]
    assert usbtaps._model(g["b27"]) == "P35_OutputUSB3_4"
    assert usbtaps._model(m["b27"]) == "P35_OutputUSB5_6"
    assert "b11" in m


def test_vestigial_empty_second_path_gets_the_mic_pair():
    body = _body(_path(cells=(1,)), _path(inp="P35_InputInst2"))
    usbtaps.apply(body, TAPS)
    assert usbtaps._model(body["preset"]["flow"][1]["b27"]) == "P35_OutputUSB5_6"


def test_dual_amp_taps_both_rigs_to_the_guitar_pair():
    body = _body(_path(cells=(1, 2)), _path(inp="P35_InputInst1_2", cells=(1, 2, 3)))
    usbtaps.apply(body, TAPS)
    assert [usbtaps._model(f["b27"]) for f in body["preset"]["flow"]] == [
        "P35_OutputUSB3_4", "P35_OutputUSB3_4"]


def test_foh_plus_amp_taps_only_the_foh_path():
    body = _body(_path(out="P35_OutputXLR", cells=(1, 2)),
                 _path(out="P35_OutputQtrInch", cells=(1, 2)))
    assert usbtaps.apply(body, TAPS)["status"] == "added"
    foh, amp = body["preset"]["flow"]
    assert usbtaps._model(foh["b27"]) == "P35_OutputUSB3_4"
    assert _models(amp) == {"b01": "HD2_DistMinotaurMono", "b02": "HD2_DistMinotaurMono"}


def test_dsp1_feeding_dsp2_taps_the_end_of_the_chain():
    body = _body(_path(out="P35_OutputPath2A", cells=(1,)),
                 _path(inp="P35_InputPath2A", cells=(1,)))
    usbtaps.apply(body, TAPS)
    first, second = body["preset"]["flow"]
    assert "b27" not in first
    assert usbtaps._model(second["b27"]) == "P35_OutputUSB3_4"


def test_split_goes_to_b12_when_b11_is_taken():
    body = _body(_path(cells=(1, 11)))
    usbtaps.apply(body, {"guitar": "3/4"})
    assert body["preset"]["flow"][0]["b12"]["type"] == "split"


def test_split_sits_after_lane_one_blocks():
    body = _body(_path(cells=(1, 25)))   # lane-1 block at position 11
    usbtaps.apply(body, {"guitar": "3/4"})
    assert body["preset"]["flow"][0]["b12"]["type"] == "split"


def test_role_missing_from_the_preference_is_not_tapped():
    body = _body(_path(cells=(1,)), _path(inp="P35_InputMic"))
    usbtaps.apply(body, {"guitar": "3/4"})
    assert "b27" not in body["preset"]["flow"][1]


def test_new_blocks_carry_no_snapshot_arrays():
    """An untracked block keeps its base state on every snapshot, so
    snapshot recall can never bypass the tap."""
    body = _body(_path(cells=(1,)))
    usbtaps.apply(body, TAPS)
    f = body["preset"]["flow"][0]
    for k in ("b11", "b27"):
        assert "snapshots" not in f[k]["@enabled"]
        assert f[k]["@enabled"]["value"] is True


# --- refusals: never half-patch, never stack --------------------------------

def test_full_lane_zero_skips_the_whole_preset():
    body = _body(_path(cells=range(1, 13)), _path(inp="P35_InputMic"))
    before = copy.deepcopy(body)
    r = usbtaps.apply(body, TAPS)
    assert r["status"] == "skipped" and "lane 0 full" in r["detail"]
    assert body == before    # the mic path was NOT tapped on its own


def test_occupied_b27_skips():
    body = _body(_path(cells=(1, 27)))
    before = copy.deepcopy(body)
    assert usbtaps.apply(body, TAPS)["status"] == "skipped"
    assert body == before


def test_idempotent():
    body = _body(_path(cells=(1,)), _path(inp="P35_InputMic"))
    usbtaps.apply(body, TAPS)
    once = copy.deepcopy(body)
    assert usbtaps.apply(body, TAPS)["status"] == "present"
    assert body == once


# --- the preference ----------------------------------------------------------

def test_preference_parses():
    assert parse_usb_taps(None) == {}
    assert parse_usb_taps(TAPS) == TAPS


@pytest.mark.parametrize("raw", [{"guitar": "7/8"}, {"bass": "3/4"}, "3/4"])
def test_bad_preference_is_refused(raw):
    with pytest.raises(PreferencesError, match="usb_taps"):
        parse_usb_taps(raw)


@pytest.fixture
def prefs(tmp_path, monkeypatch):
    p = tmp_path / "prefs.json"
    p.write_text(json.dumps({"usb_taps": TAPS}))
    monkeypatch.setenv("HELIXGEN_PREFS", str(p))
    return p


def test_every_hsp_write_applies_the_preference(prefs, tmp_path):
    body = _body(_path(cells=(1,)))
    out = tmp_path / "t.hsp"
    write_hsp(out, body)
    assert "b27" not in body["preset"]["flow"][0]          # caller's body untouched
    assert usbtaps.has_tap(read_hsp(out))


def test_write_without_the_preference_is_unchanged(tmp_path):
    body = _body(_path(cells=(1,)))
    assert not usbtaps.has_tap(json.loads(dumps_hsp(body)[8:]))


def test_write_of_an_untappable_tone_warns_by_name(prefs, capsys):
    body = _body(_path(cells=range(1, 13)), name="Stoner Blues Board")
    assert json.loads(dumps_hsp(body)[8:]) == body
    assert "Stoner Blues Board" in capsys.readouterr().err


def test_malformed_preference_warns_and_writes(tmp_path, monkeypatch, capsys):
    p = tmp_path / "prefs.json"
    p.write_text(json.dumps({"usb_taps": {"guitar": "9/10"}}))
    monkeypatch.setenv("HELIXGEN_PREFS", str(p))
    body = _body(_path(cells=(1,)))
    assert json.loads(dumps_hsp(body)[8:]) == body
    assert "usb_taps" in capsys.readouterr().err


# --- the verb ----------------------------------------------------------------

def test_verb_is_a_dry_run_by_default(prefs, tmp_path):
    f = tmp_path / "t.hsp"
    f.write_bytes(b"rpshnosj" + json.dumps(_body(_path(cells=(1,)))).encode())
    before = f.read_bytes()
    r = CliRunner().invoke(cli, ["usb-taps", str(f), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)[0]["status"] == "added"
    assert f.read_bytes() == before


def test_verb_apply_writes_and_reports(prefs, tmp_path):
    ok, full = tmp_path / "ok.hsp", tmp_path / "full.hsp"
    write_hsp(full, _body(_path(cells=range(1, 13)), name="Full"))
    ok.write_bytes(b"rpshnosj" + json.dumps(_body(_path(cells=(1,)))).encode())
    r = CliRunner().invoke(cli, ["usb-taps", "--apply", str(ok), str(full)])
    assert r.exit_code == 0, r.output
    assert "added" in r.output and "skipped  Full: path 1: lane 0 full" in r.output
    assert usbtaps.has_tap(read_hsp(ok))
    r = CliRunner().invoke(cli, ["usb-taps", str(ok)])
    assert r.output.startswith("present")


def test_verb_refuses_without_the_preference(tmp_path):
    f = tmp_path / "t.hsp"
    write_hsp(f, _body(_path(cells=(1,))))
    r = CliRunner().invoke(cli, ["usb-taps", str(f)])
    assert r.exit_code != 0 and "usb_taps preference is not set" in r.output


# --- device format: the tap split's bblk -------------------------------------

def test_transcoded_tap_split_enters_row_one_beneath_itself():
    """Engine 0.52.1 regression guard: the tap split's bblk must be
    14 + split position (hardcoded 15 routed USB 3/4 mono, ~24 dB down), and
    the tapped preset must transcode byte-exactly through a round trip."""
    pytest.importorskip("msgpack")
    from helixgen.device import content, transcode, untranscode
    # A parallel pair (split@3 -> b15 -> join@5) ahead of the tap, so a
    # regression to "first lane-1 slot" would show as 15 instead of 25.
    recipe = {"name": "tap", "paths": [{
        "blocks": [{"block": "HD2_AmpBritPlexiNrm", "params": {}, "lane": 0, "pos": 1},
                   {"block": "HD2_DistMinotaurMono", "params": {}, "lane": 1, "pos": 1},
                   {"block": "HX2_ImpulseResponseWithPan", "params": {}, "lane": 0, "pos": 8}],
        "structural": [
            {**copy.deepcopy(transcode._SPLIT_SCAFFOLD), "_pos": 3, "_lane": 0},
            {**copy.deepcopy(transcode._JOIN_SCAFFOLD), "_pos": 5, "_lane": 0}],
    }]}
    body = untranscode.sbe_bytes_to_hsp(
        content.encode_content_data(transcode.recipe_to_sbepgsm(recipe)), name="tap")
    assert usbtaps.apply(body, {"guitar": "3/4"})["status"] == "added"

    sbe = transcode.hsp_to_sbepgsm(body)
    blocks = dict(untranscode._iter_blocks(content.decode_any(sbe)["sfg_"]["flow"][0]))
    assert {gp: b["bblk"] for gp, b in blocks.items() if b.get("type") == 3} == {3: 15, 11: 25}

    again = untranscode.sbe_bytes_to_hsp(sbe, name="tap")
    assert usbtaps.has_tap(again)
    assert transcode.hsp_to_sbepgsm(again) == sbe


def test_malformed_preference_does_not_break_other_preferences(tmp_path):
    """hgc-6en lesson: a usb_taps typo must not take load_preferences down."""
    from helixgen.preferences import load_preferences
    p = tmp_path / "prefs.json"
    p.write_text(json.dumps({"usb_taps": {"guitar": "9/10"}, "favor_irs": True}))
    assert load_preferences(p).favor_irs is True


def test_verb_reports_a_malformed_preference(tmp_path, monkeypatch):
    p = tmp_path / "prefs.json"
    p.write_text(json.dumps({"usb_taps": {"guitar": "9/10"}}))
    monkeypatch.setenv("HELIXGEN_PREFS", str(p))
    f = tmp_path / "t.hsp"
    f.write_bytes(b"rpshnosj{}")
    r = CliRunner().invoke(cli, ["usb-taps", str(f)])
    assert r.exit_code != 0 and "usb_taps.guitar" in r.output


# --- review follow-ups -------------------------------------------------------

def test_foh_plus_amp_taps_the_foh_path_on_either_dsp():
    body = _body(_path(out="P35_OutputQtrInch", cells=(1, 2)),
                 _path(out="P35_OutputXLR", cells=(1, 2)))
    usbtaps.apply(body, TAPS)
    amp, foh = body["preset"]["flow"]
    assert "b27" not in amp
    assert usbtaps._model(foh["b27"]) == "P35_OutputUSB3_4"


def test_main_output_already_on_the_pair_is_not_tapped_again():
    body = _body(_path(out="P35_OutputUSB3_4", cells=(1,)), _path(inp="P35_InputMic"))
    usbtaps.apply(body, TAPS)
    g, m = body["preset"]["flow"]
    assert "b27" not in g and usbtaps._model(m["b27"]) == "P35_OutputUSB5_6"


def test_lone_block_at_b12_is_not_called_lane_zero_full():
    r = usbtaps.apply(_body(_path(cells=(12,))), TAPS)
    assert r["status"] == "skipped" and "block at b12" in r["detail"]


def test_stale_tap_is_moved_after_the_new_last_block():
    """A block landed after the tap split (added on the hardware, or
    re-authored from `view`): the next write moves the tap back to the end."""
    body = _body(_path(cells=(1,)))
    usbtaps.apply(body, TAPS)
    body["preset"]["flow"][0]["b12"] = _fx(12)
    r = usbtaps.apply(body, TAPS)
    assert r["status"] == "skipped" and "stale" in r["detail"]   # no room after b12

    body = _body(_path(cells=(1, 8)))
    body["preset"]["flow"][0]["b05"] = usbtaps._split_block(5)
    body["preset"]["flow"][0]["b27"] = usbtaps._tap_block("P35_OutputUSB3_4")
    r = usbtaps.apply(body, TAPS)
    assert r["status"] == "added" and "moved stale tap" in r["detail"]
    f = body["preset"]["flow"][0]
    assert f["b11"]["type"] == "split" and "b05" not in f and "b27" in f
    assert usbtaps.apply(body, TAPS)["status"] == "present"


def test_edit_verbs_work_on_a_tapped_tone(tmp_path, hsp_library, prefs):
    """Review HIGH: add/remove_block refuse a path holding a split — the tap
    split included. The edit verbs lift the taps around the edit."""
    from helixgen.generate import generate_preset
    spec = tmp_path / "in.json"
    spec.write_text(json.dumps({"name": "C", "paths": [{"blocks": [
        {"block": "Tube Drive", "params": {}}]}]}))
    out = tmp_path / "out.hsp"
    generate_preset(spec, out, hsp_library)
    assert usbtaps.has_tap(read_hsp(out))           # generate applied the pref
    lib = str(hsp_library.root)
    r = CliRunner().invoke(cli, ["add-block", str(out), "Brit Amp", "--library", lib])
    assert r.exit_code == 0, r.output
    f = read_hsp(out)["preset"]["flow"][0]
    assert usbtaps._model(f["b02"]).startswith("HD2_AmpBrit")
    assert f["b11"]["type"] == "split" and usbtaps._model(f["b27"]) == "P35_OutputUSB3_4"
    r = CliRunner().invoke(cli, ["remove-block", str(out), "Tube Drive", "--library", lib])
    assert r.exit_code == 0, r.output
    f = read_hsp(out)["preset"]["flow"][0]
    assert "b01" not in f and f["b11"]["type"] == "split" and "b27" in f


def test_lifted_puts_the_tap_after_an_appended_block():
    body = _body(_path(cells=range(1, 11)))
    usbtaps.apply(body, TAPS)
    with usbtaps.lifted(body):
        f = body["preset"]["flow"][0]
        assert "b11" not in f and "b27" not in f
        f["b11"] = _fx(11)
    assert f["b12"]["type"] == "split" and "b27" in f


def test_lifted_drops_a_tap_that_no_longer_fits_and_says_so(capsys):
    body = _body(_path(cells=range(1, 11)), _path(inp="P35_InputMic"), name="Crowded")
    usbtaps.apply(body, TAPS)
    with usbtaps.lifted(body):
        body["preset"]["flow"][0]["b11"] = _fx(11)
        body["preset"]["flow"][0]["b12"] = _fx(12)
    assert not usbtaps.has_tap(body)          # mic tap NOT kept on its own
    assert "Crowded" in capsys.readouterr().err
