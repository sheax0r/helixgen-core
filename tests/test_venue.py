import pytest

from helixgen import venue


def test_validate_venue_accepts_vocabulary_and_none():
    assert venue.validate_venue("headphones") == "headphones"
    assert venue.validate_venue(None) is None
    with pytest.raises(ValueError, match="amp-in"):
        venue.validate_venue("stadium")


from pathlib import Path

from helixgen.ir import IrMapping
from tests.test_ir_cli import _libsndfile_available, _write_synth_wav

needs_sf = pytest.mark.skipif(not _libsndfile_available(), reason="libsndfile missing")


def _wav(path: Path, amp: float, n: int = 100) -> Path:
    v = int(amp * (2**23 - 1))
    return _write_synth_wav(path, n, samples_per_channel=[[v] * n])


@pytest.fixture
def irs_fixture(tmp_path):
    root = tmp_path / "irs"
    irs = IrMapping(irs_dir=root)
    layout = {
        "a": ("YA MRSH 412 T75/natural-phase/YA MRSH 412 T75 ROOM 2.wav", 0.5),
        "b": ("YA MRSH 412 T75/natural-phase/YA MRSH 412 T75 ROOM 1.wav", 0.2),
        "c": ("YA MRSH 412 T75/YA MRSH 412 T75 ROOM 3.wav", 0.9),  # louder, not natural-phase
        "d": ("YA MRSH 412 T75/other/YA MRSH 412 T75 ROOM 2.wav", 0.1),  # same basename as a
        "e": ("YA MRSH 412 T75/YA MRSH 412 T75 Mix 12.wav", 0.5),
        "f": ("YA DXVB 112/YA DXVB 112 Mix 01.wav", 0.5),
    }
    for h, (rel, amp) in layout.items():
        irs.register(h, _wav(root / rel, amp))
    return irs


def _close(tmp_path, name):
    return Path(tmp_path / "close" / name)


@needs_sf
def test_find_room_ir_matches_prefix_prefers_natural_phase_then_energy(irs_fixture, tmp_path):
    h, p = venue.find_room_ir(_close(tmp_path, "YA MRSH 412 T75 Mix 12.wav"), irs_fixture)
    assert h == "a" and p.name == "YA MRSH 412 T75 ROOM 2.wav" and "natural-phase" in str(p)


@needs_sf
def test_find_room_ir_none_when_pack_has_no_room(irs_fixture, tmp_path):
    assert venue.find_room_ir(_close(tmp_path, "YA DXVB 112 Mix 01.wav"), irs_fixture) is None


@needs_sf
def test_override_by_hash_and_ambiguous_basename(irs_fixture, tmp_path):
    close = _close(tmp_path, "YA MRSH 412 T75 Mix 12.wav")
    assert venue.find_room_ir(close, irs_fixture, override="b")[0] == "b"
    with pytest.raises(venue.VenueError, match="hash"):
        venue.find_room_ir(close, irs_fixture, override="YA MRSH 412 T75 ROOM 2.wav")
    with pytest.raises(venue.VenueError):
        venue.find_room_ir(close, irs_fixture, override="nope.wav")


def test_room_level_reproduces_reference(monkeypatch):
    energies = {"close.wav": 7.3, "room.wav": 15.2}
    monkeypatch.setattr(venue, "ir_energy_db", lambda p: energies[Path(p).name])
    assert venue.room_level_db(-18.0, Path("close.wav"), Path("room.wav")) == pytest.approx(-29.9, abs=0.1)


@needs_sf
def test_ir_energy_db_and_room_level_from_real_wavs(tmp_path):
    close, room = _wav(tmp_path / "c.wav", 0.5), _wav(tmp_path / "r.wav", 0.5, n=400)
    assert venue.ir_energy_db(close) == pytest.approx(10 * __import__("math").log10(100 * 0.25), abs=0.01)
    # room has 6.02 dB more energy: -18 - 6.0 - 4
    assert venue.room_level_db(-18.0, close, room) == pytest.approx(-28.0, abs=0.1)


def test_missing_room_wav_is_a_clear_error():
    with pytest.raises(venue.VenueError, match="ROOM 2.wav"):
        venue.ir_energy_db(Path("/nope/YA MRSH 412 T75 ROOM 2.wav"))


# --- Task 4: headphones transform ------------------------------------------

import copy

from helixgen import mutate, usbtaps
from helixgen.generate import compose_preset
from helixgen.library import Block
from helixgen.spec import parse_spec
from helixgen.view import view
from tests.golden import harness

CLOSE = "c" * 32       # YA MRSH 412 T75 Mix 12 (has a ROOM sibling)
ROOM = "a" * 32        # YA MRSH 412 T75 ROOM 2, natural-phase
LONER = "d" * 32       # YA DXVB 112 Mix 01 (no ROOM sibling)
EXPECTED_LEVEL = -28.0  # -18 - (room is 4x longer: +6.02 dB) - 4


@pytest.fixture
def lib(tmp_path):
    lib = harness.build_corpus_library(tmp_path / "lib")
    src = {"preset": "venue", "firmware": "test", "date": "2026-11-12"}
    lib.save_block(Block(
        model_id="HD2_RvbTile", category="reverb", display_name="Tile",
        params={"Decay": {"type": "float"}, "Mix": {"type": "float"}},
        exemplar={"@model": "HD2_RvbTile", "@type": "fx", "@enabled": True,
                  "Decay": 0.5, "Mix": 0.3},
        first_seen=src))
    lib.save_block(Block(
        model_id="HD2_EQParametric", category="eq", display_name="Parametric Mono",
        params={"LowCut": {"type": "float"}, "LowCutSlope": {"type": "int"},
                "HighCut": {"type": "float"}, "HighCutSlope": {"type": "int"}},
        exemplar={"@model": "HD2_EQParametric", "@type": "fx", "@enabled": True,
                  "LowCut": 19.9, "LowCutSlope": 0, "HighCut": 20100.0, "HighCutSlope": 0},
        first_seen=src))
    lib.save_block(Block(
        model_id="HD2_WahUKWah846", category="filter", display_name="UK Wah 846",
        params={"Pedal": {"type": "float"}, "Mix": {"type": "float"}},
        exemplar={"@model": "HD2_WahUKWah846", "@type": "fx", "@enabled": True,
                  "Pedal": 0.5, "Mix": 1.0},
        first_seen=src))
    lib.rebuild_index()
    return lib


@pytest.fixture
def irs(tmp_path):
    root = tmp_path / "irs"
    m = IrMapping(irs_dir=root)
    m.register(CLOSE, _wav(root / "YA MRSH 412 T75/YA MRSH 412 T75 Mix 12.wav", 0.5))
    m.register(ROOM, _wav(root / "YA MRSH 412 T75/natural-phase/YA MRSH 412 T75 ROOM 2.wav",
                          0.5, n=400))
    m.register(LONER, _wav(root / "YA DXVB 112/YA DXVB 112 Mix 01.wav", 0.5))
    return m


def _chain(*extra, ir=CLOSE):
    return [{"block": "Scream 808"},
            {"block": "Brit 2204 Custom", "params": {"Drive": 0.55}},
            *([{"block": "IR", "ir": ir}] if ir else []),
            *extra,
            {"block": "Plate"}]


def _body(lib, irs, blocks=None, **top):
    recipe = {"name": "T", "author": "me", "paths": [{"blocks": blocks or _chain()}], **top}
    return compose_preset(parse_spec(recipe), lib, source="t", irs=irs)


@pytest.fixture
def single_ir_body(lib, irs):
    return _body(lib, irs,
                 snapshots=[{"name": "Rhythm"},
                            {"name": "Lead", "params": {"Brit 2204 Custom": {"Drive": 0.85}}},
                            {"name": "Clean", "disable": ["Scream 808"]}],
                 footswitches=[{"switch": "FS1", "block": "Scream 808"}],
                 expression=[{"pedal": "EXP1", "targets": [
                     {"block": "Brit 2204 Custom", "param": "Master", "min": 0.0, "max": 0.7}]}])


def _kinds(blocks):
    return [("split" if "split" in b else "join" if "join" in b
             else "structural" if "structural" in b else b["block"], b.get("lane", 0))
            for b in blocks]


def _flow_blocks(body, pi=0):
    """Non-endpoint bNN dicts of path ``pi`` in lane-then-position order."""
    p = body["preset"]["flow"][pi]
    keys = sorted((k for k in p if k.startswith("b") and k[1:].isdigit()
                   and int(k[1:]) not in (0, 13, 14, 27)), key=lambda k: int(k[1:]))
    return [p[k] for k in keys]


@needs_sf
def test_headphones_adds_parallel_room_ir_and_disabled_tile(single_ir_body, lib, irs):
    r = venue.apply_venue(single_ir_body, "headphones", lib, irs)
    blocks = view(r.body, lib, irs=irs)["paths"][0]["blocks"]
    kinds = _kinds(blocks)
    assert [k for k, lane in kinds if lane == 0 and k != "structural"] == [
        "Scream 808", "Brit 2204 Custom", "split", "IR", "join", "Plate", "Tile"]
    # the room IR is the split's only B-lane block (listed between split and join)
    assert kinds.index(("split", 0)) < kinds.index(("IR", 1)) < kinds.index(("join", 0))
    assert [k for k, lane in kinds if lane == 1] == ["IR"]
    lane0 = [b for b in blocks if b.get("lane", 0) == 0 and "structural" not in b]
    close, room = lane0[3], blocks[kinds.index(("IR", 1))]
    assert close["ir"] == "YA MRSH 412 T75 Mix 12.wav"
    assert room["ir"] == "YA MRSH 412 T75 ROOM 2.wav"   # view prints a unique basename
    assert room["params"]["Level"] == pytest.approx(EXPECTED_LEVEL, abs=0.1)
    assert lane0[4]["join"]["params"] == {"A Level": 0.0, "B Level": 0.0,
                                          "A Pan": 0.5, "B Pan": 0.5, "Level": 0.0}
    tile = lane0[6]
    assert tile["enabled"] is False and tile["params"] == {"Decay": 0.2, "Mix": 0.2}
    assert r.venue == "headphones"
    assert r.venue_detail == {
        "treatment": "room-ir", "room_ir": ROOM,
        "room_level_db": pytest.approx(EXPECTED_LEVEL, abs=0.1),
        "room_switch": "FS2", "ambience_block": "Tile", "ambience_switch": "FS3",
        "fallback_reason": None}
    assert r.body["meta"] == single_ir_body["meta"]


@needs_sf
def test_room_switch_is_a_level_param_toggle_not_a_bypass(single_ir_body, lib, irs):
    r = venue.apply_venue(single_ir_body, "headphones", lib, irs)
    fss = view(r.body, lib, irs=irs)["footswitches"]
    fs = [f for f in fss if f.get("label") == "ROOM MIC"][0]
    assert fs["switch"] == "FS2" and fs["param"] == "Level" and fs["lane"] == 1
    assert fs["min"] == -60.0 and fs["max"] == pytest.approx(EXPECTED_LEVEL, abs=0.1)
    assert fs["color"] == "purple"
    tile = [f for f in fss if f.get("label") == "TILE"][0]
    assert tile["switch"] == "FS3" and tile["block"] == "Tile" and "param" not in tile
    assert tile["color"] == "turquoise"


def _treatment_keys(path):
    """bNN keys the headphones treatment added to a path: the room IR, its
    split and join, and the Tile."""
    path = {k: b for k, b in path.items() if k[1:].isdigit()}
    room = [k for k, b in path.items() if b.get("slot") and b["slot"][0].get("irhash") == ROOM]
    split = [k for k, b in path.items() if b.get("type") == "split" and b.get("branch") in room]
    join = [path[k]["endpoint"] for k in split]
    tile = [k for k, b in path.items() if b.get("slot") and b["slot"][0].get("model") == "HD2_RvbTile"]
    return set(room + split + join + tile)


def _kept(path, skip=()):
    """A path's bNN dicts in grid order, minus ``skip``, without the grid
    coordinates an insert legitimately moves (key, position, split pointers)."""
    keys = sorted((k for k in path if k[1:].isdigit() and k not in skip), key=lambda k: int(k[1:]))
    return [{f: v for f, v in path[k].items() if f not in ("position", "branch", "endpoint")}
            for k in keys]


def _sid(body, switch):
    return venue._sid(body, switch)


@needs_sf
def test_everything_else_is_unchanged(single_ir_body, lib, irs):
    r = venue.apply_venue(single_ir_body, "headphones", lib, irs)
    before, after = single_ir_body["preset"]["flow"][0], r.body["preset"]["flow"][0]
    assert _kept(after, _treatment_keys(after)) == _kept(before)
    rest = lambda b: {k: v for k, v in b["preset"].items() if k not in ("flow", "sources")}
    assert rest(r.body) == rest(single_ir_body) and r.body["meta"] == single_ir_body["meta"]
    src0, src1 = single_ir_body["preset"]["sources"], r.body["preset"]["sources"]
    assert {k: v for k, v in src1.items() if k in src0} == src0     # FS1 untouched
    assert set(src1) - set(src0) == {str(_sid(r.body, "FS2")), str(_sid(r.body, "FS3"))}


@pytest.fixture
def tapped_body(lib, irs):
    recipe = {"name": "Tapped", "paths": [
        {"blocks": _chain()},
        {"input": "mic", "blocks": [{"block": "Digital"}]}]}
    body = compose_preset(parse_spec(recipe), lib, source="t", irs=irs)
    assert usbtaps.apply(body, {"guitar": "3/4", "mic": "5/6"})["status"] == "added"
    return body


@needs_sf
def test_usb_taps_survive(tapped_body, lib, irs):
    r = venue.apply_venue(tapped_body, "headphones", lib, irs)
    assert r.venue_detail["treatment"] == "room-ir"
    f0, f1 = r.body["preset"]["flow"]
    assert usbtaps._find_tap(f0)[1] == "P35_OutputUSB3_4" and not usbtaps._stale(f0)
    assert usbtaps._find_tap(f1)[1] == "P35_OutputUSB5_6"
    # the mic path, tap and all, comes through untouched
    assert f1 == tapped_body["preset"]["flow"][1]
    assert usbtaps.apply(copy.deepcopy(r.body), {"guitar": "3/4", "mic": "5/6"})["status"] == "present"


def test_no_cab_means_no_treatment(lib, irs):
    di_body = _body(lib, irs, blocks=_chain(ir=None))
    r = venue.apply_venue(di_body, "headphones", lib, irs)
    assert r.venue_detail["treatment"] == "none"
    assert view(r.body, lib, irs=irs) == view(di_body, lib, irs=irs) and r.warnings


@needs_sf
@pytest.mark.parametrize("blocks,top,why", [
    (_chain({"block": "4x12 Greenback 25"}), {}, "cab"),
    (_chain(), {"snapshots": [{"name": "A"}, {"name": "B", "disable": ["IR"]}]}, "snapshot"),
], ids=["two-cabs", "snapshot-bypassed"])
def test_two_cabs_or_snapshot_bypassed_cab_fall_back_to_tile(lib, irs, blocks, top, why):
    body = _body(lib, irs, blocks=blocks, **top)
    r = venue.apply_venue(body, "headphones", lib, irs)
    d = r.venue_detail
    assert d["treatment"] == "tile" and why in d["fallback_reason"]
    assert d["room_ir"] is None and d["ambience_block"] == "Tile" and d["ambience_switch"] == "FS1"
    blocks = view(r.body, lib, irs=irs)["paths"][0]["blocks"]
    assert not any("split" in b for b in blocks)
    tile = [b for b in blocks if b.get("block") == "Tile"][0]
    assert "enabled" not in tile    # enabled at base


@needs_sf
@pytest.mark.parametrize("blocks,why", [
    (_chain({"block": "4x12 Greenback 25"}, ir=None), "IR"),
    (_chain(ir=LONER), "room IR"),
], ids=["stock-cab", "no-room-match"])
def test_stock_cab_and_no_matching_room_ir_fall_back_to_tile(lib, irs, blocks, why):
    r = venue.apply_venue(_body(lib, irs, blocks=blocks), "headphones", lib, irs)
    assert r.venue_detail["treatment"] == "tile" and why in r.venue_detail["fallback_reason"]


@needs_sf
def test_room_mode_tile_forces_fallback(single_ir_body, lib, irs):
    r = venue.apply_venue(single_ir_body, "headphones", lib, irs, room_mode="tile")
    assert r.venue_detail["treatment"] == "tile" and "tile" in r.venue_detail["fallback_reason"]


@needs_sf
def test_no_free_footswitch_still_adds_room_and_warns(lib, irs):
    params = [("Scream 808", p) for p in ("Drive", "Tone", "Level")] + [
        ("Brit 2204 Custom", p) for p in ("Drive", "Bass", "Mid", "Treble", "Presence",
                                          "Master", "ChVol")]
    switches = ["FS1", "FS2", "FS3", "FS4", "FS5", "FS7", "FS8", "FS9", "FS10", "FS11"]
    full_fs_body = _body(lib, irs, footswitches=[
        {"switch": s, "block": b, "param": p, "min": 0.0, "max": 1.0}
        for s, (b, p) in zip(switches, params)])
    r = venue.apply_venue(full_fs_body, "headphones", lib, irs)
    d = r.venue_detail
    assert d["treatment"] == "room-ir" and d["room_switch"] is None
    assert d["ambience_switch"] is None and d["ambience_block"] == "Tile"
    assert any("footswitch" in w for w in r.warnings)
    assert [x["slot"][0]["irhash"] for x in _flow_blocks(r.body)
            if x.get("path") == 1] == [ROOM]


@needs_sf
def test_per_snapshot_values_survive(lib, irs):
    body = _body(lib, irs, snapshots=[
        {"name": "A"}, {"name": "B", "params": {"IR": {"Level": -24.0},
                                                "Brit 2204 Custom": {"Drive": 0.9}}}])
    r = venue.apply_venue(body, "headphones", lib, irs)
    # room level uses the close IR's BASE Level (-18), not the snapshot override
    assert r.venue_detail["room_level_db"] == pytest.approx(EXPECTED_LEVEL, abs=0.1)
    ir_level = lambda b: [x for x in _flow_blocks(b)
                          if x["slot"][0].get("irhash") == CLOSE][0]["slot"][0]["params"]["Level"]
    assert ir_level(r.body)["snapshots"][:2] == ir_level(body)["snapshots"][:2] == [-18.0, -24.0]
    amp = lambda b: [x for x in _flow_blocks(b) if x.get("type") == "amp"][0]
    assert amp(r.body) == amp(body)


@pytest.fixture
def body_with_unknown_controller(single_ir_body):
    body = copy.deepcopy(single_ir_body)
    amp = [b for b in _flow_blocks(body) if b.get("type") == "amp"][0]
    amp["slot"][0]["params"]["Bass"]["controller"] = {
        "behavior": "continuous", "bypassed": False, "curve": "linear", "delay": 0,
        "goid": 0, "max": 1.0, "midisource": 0, "min": 0.0, "source": 99999999,
        "threshold": 0.0, "type": "param"}
    return body


@needs_sf
def test_unknown_controller_is_carried_verbatim(body_with_unknown_controller, lib, irs):
    """The old regenerate-from-`view` design had to refuse this preset; editing
    in place carries the controller it cannot represent through untouched."""
    r = venue.apply_venue(body_with_unknown_controller, "headphones", lib, irs)
    assert r.venue_detail["treatment"] == "room-ir"
    amp = lambda b: [x for x in _flow_blocks(b) if x.get("type") == "amp"][0]
    assert amp(r.body) == amp(body_with_unknown_controller)
    assert _to_direct(r, lib, irs).body["preset"]["flow"] == \
        body_with_unknown_controller["preset"]["flow"]


def _long(n_delays):
    """Scream, Brit, IR, n x Digital, Plate."""
    return _chain(*[{"block": "Digital"}] * n_delays)


@needs_sf
def test_tile_dropped_with_warning_when_lane_is_full(lib, irs):
    body = _body(lib, irs, blocks=_long(6),     # 10 blocks: room stage -> 12, Tile won't fit
                 footswitches=[{"switch": "FS1", "block": "Digital", "lane": 0, "pos": 5}])
    r = venue.apply_venue(body, "headphones", lib, irs)
    assert r.venue_detail["treatment"] == "room-ir"
    assert r.venue_detail["ambience_block"] is None and r.venue_detail["ambience_switch"] is None
    assert any("Tile" in w for w in r.warnings)
    # the ambiguous-name footswitch still targets the same (shifted) Digital
    fs = [f for f in view(r.body, lib, irs=irs)["footswitches"] if f["switch"] == "FS1"][0]
    assert (fs["block"], fs["pos"]) == ("Digital", 7)


@needs_sf
def test_no_room_for_the_split_falls_back_to_tile(lib, irs):
    r = venue.apply_venue(_body(lib, irs, blocks=_long(7)), "headphones", lib, irs)  # 11 blocks
    d = r.venue_detail
    assert d["treatment"] == "tile" and "no room" in d["fallback_reason"]
    assert d["room_ir"] is None and d["room_level_db"] is None and d["ambience_block"] == "Tile"
    assert not any(b.get("type") == "split" for b in _flow_blocks(r.body))


@needs_sf
def test_no_room_for_split_or_tile_is_an_error(lib, irs):
    with pytest.raises(venue.VenueError, match="no room"):
        venue.apply_venue(_body(lib, irs, blocks=_long(8)), "headphones", lib, irs)  # 12 blocks


@needs_sf
def test_tile_dropped_to_keep_the_usb_tap(lib, irs):
    body = _body(lib, irs, blocks=_long(5))     # 9 blocks: Tile at b12 leaves no cell for the tap
    assert usbtaps.apply(body, {"guitar": "3/4"})["status"] == "added"
    r = venue.apply_venue(body, "headphones", lib, irs)
    assert r.venue_detail["ambience_block"] is None
    assert usbtaps._find_tap(r.body["preset"]["flow"][0]) == ("b12", "P35_OutputUSB3_4")


# --- Task 5: direct reversal and amp-in -------------------------------------

def _to_direct(r, lib, irs):
    return venue.apply_venue(r.body, "direct", lib, irs,
                             source_venue="headphones", source_detail=r.venue_detail)


@needs_sf
@pytest.mark.parametrize("which", ["single_ir_body", "tapped_body"])
def test_headphones_then_direct_restores_the_original_chain(which, request, lib, irs):
    body = request.getfixturevalue(which)
    a = venue.apply_venue(body, "headphones", lib, irs)
    assert a.venue_detail["treatment"] == "room-ir"
    b = _to_direct(a, lib, irs)
    assert b.venue == "direct" and b.venue_detail == {}
    assert view(b.body, lib, irs=irs) == view(body, lib, irs=irs)
    # the flow comes back byte for byte; `sources` keeps a blank strip entry for
    # each released switch (this corpus chassis lacks them; real presets have one
    # for every switch -- see the real-shape round trip below)
    assert b.body["preset"]["flow"] == body["preset"]["flow"]


@needs_sf
def test_tile_fallback_and_none_reverse_too(lib, irs):
    for body in (_body(lib, irs, blocks=_chain({"block": "4x12 Greenback 25"})),
                 _body(lib, irs, blocks=_chain(ir=None))):
        a = venue.apply_venue(body, "headphones", lib, irs)
        assert view(_to_direct(a, lib, irs).body, lib, irs=irs) == view(body, lib, irs=irs)


def test_direct_without_detail_errors(single_ir_body, lib, irs):
    with pytest.raises(venue.VenueError, match="venue_detail"):
        venue.apply_venue(single_ir_body, "direct", lib, irs, source_venue="headphones")


def _hand_edit_swap_room(body):
    for b in _flow_blocks(body):
        if b["slot"][0].get("irhash") == ROOM:
            b["slot"][0]["irhash"] = LONER


def _hand_edit_drop_tile(body):
    flow = body["preset"]["flow"][0]
    for k in [k for k, b in flow.items() if isinstance(b, dict) and b.get("slot")
              and b["slot"][0].get("model") == "HD2_RvbTile"]:
        del flow[k]


@needs_sf
@pytest.mark.parametrize("hand_edit,named", [
    (_hand_edit_swap_room, "room_ir"), (_hand_edit_drop_tile, "Tile")])
def test_direct_after_hand_edit_errors_and_names_mismatch(single_ir_body, lib, irs,
                                                          hand_edit, named):
    a = venue.apply_venue(single_ir_body, "headphones", lib, irs)
    hand_edit(a.body)
    before = copy.deepcopy(a.body)
    with pytest.raises(venue.VenueError, match=named):
        _to_direct(a, lib, irs)
    assert a.body == before


@needs_sf
def test_direct_refuses_blocks_added_inside_the_split(single_ir_body, lib, irs):
    a = venue.apply_venue(single_ir_body, "headphones", lib, irs)
    flow0 = a.body["preset"]["flow"][0]          # a hand edit: a second block on the B lane
    flow0["b16"] = copy.deepcopy(flow0["b15"])
    flow0["b16"].update(position=2)
    flow0["b16"]["slot"][0]["irhash"] = LONER
    flow0["b05"]["branch"] = "b16"
    with pytest.raises(venue.VenueError, match="added inside the room IR split"):
        _to_direct(a, lib, irs)


def _eq_block():
    return {"block": "Parametric Mono", "params": {
        "LowCut": 90.0, "LowCutSlope": 2, "HighCut": 5500.0, "HighCutSlope": 2}}


def test_amp_in_removes_cabs_and_adds_eq_after_last_amp(single_ir_body, lib, irs):
    r = venue.apply_venue(single_ir_body, "amp-in", lib, irs)
    assert r.venue == "amp-in" and r.venue_detail == {"eq_block": "Parametric Mono"}
    v = view(r.body, lib, irs=irs)
    blocks = [{k: x for k, x in b.items() if k not in ("lane", "pos")}
              for b in v["paths"][0]["blocks"] if "structural" not in b]
    assert blocks == [{"block": "Scream 808"},
                      {"block": "Brit 2204 Custom", "params": {"Drive": 0.55}},
                      _eq_block(), {"block": "Plate"}]
    v0 = view(single_ir_body, lib, irs=irs)
    for k in ("snapshots", "footswitches", "expression"):
        assert v[k] == v0[k]


def test_amp_in_drops_controls_on_removed_cabs_with_a_warning(lib, irs):
    body = _body(lib, irs, blocks=_chain({"block": "4x12 Greenback 25"}),
                 snapshots=[{"name": "A"}, {"name": "B", "params": {"IR": {"Level": -20.0}}}],
                 footswitches=[{"switch": "FS2", "block": "4x12 Greenback 25"}])
    r = venue.apply_venue(body, "amp-in", lib, irs)
    v = view(r.body, lib, irs=irs)
    assert "footswitches" not in v and "params" not in v["snapshots"][1]
    assert len(r.warnings) == 2 and all("removed" in w for w in r.warnings)


@needs_sf
def test_amp_in_from_headphones_also_drops_room_stage(tapped_body, lib, irs):
    a = venue.apply_venue(tapped_body, "headphones", lib, irs)
    c = venue.apply_venue(a.body, "amp-in", lib, irs, source_venue="headphones",
                          source_detail=a.venue_detail)
    direct_amp_in = venue.apply_venue(tapped_body, "amp-in", lib, irs)
    assert view(c.body, lib, irs=irs) == view(direct_amp_in.body, lib, irs=irs)
    assert c.venue_detail == {"eq_block": "Parametric Mono"}
    assert usbtaps._find_tap(c.body["preset"]["flow"][0])[1] == "P35_OutputUSB3_4"


def test_amp_in_without_an_amp_errors(lib, irs):
    body = _body(lib, irs, blocks=[{"block": "Scream 808"}, {"block": "IR"}])
    with pytest.raises(venue.VenueError, match="no amp"):
        venue.apply_venue(body, "amp-in", lib, irs)


@pytest.mark.parametrize("target", ["direct", "headphones"])
def test_from_amp_in_to_anything_errors(single_ir_body, lib, irs, target):
    with pytest.raises(venue.VenueError, match="cannot restore a cab from an amp-in variant"):
        venue.apply_venue(single_ir_body, target, lib, irs, source_venue="amp-in")


@pytest.mark.parametrize("v", venue.VENUES)
def test_same_venue_errors(single_ir_body, lib, irs, v):
    with pytest.raises(venue.VenueError, match=f"already {v}"):
        venue.apply_venue(single_ir_body, v, lib, irs, source_venue=v, source_detail={})


def test_unspecified_to_direct_is_a_no_op(single_ir_body, lib, irs):
    r = venue.apply_venue(single_ir_body, "direct", lib, irs)
    assert r.body == single_ir_body and r.venue_detail == {}


# --- real-shape preset ------------------------------------------------------
#
# What the 126 library presets look like and the old regenerate-from-`view`
# design lost: a mic path, a wah on EXP1 + toe switch (EXP controller
# `bypassed: true`), per-snapshot values on UNNAMED snapshot slots (written
# into the body, as the device saves them), USB taps on both paths, and a
# blank `sources` entry for every switch.

@pytest.fixture
def real_shape(lib, irs):
    recipe = {
        "name": "Real", "paths": [
            {"blocks": [{"block": "UK Wah 846"}, *_chain()]},
            {"input": "mic", "blocks": [{"block": "Digital"}]}],
        "snapshots": [{"name": "Rhythm"},
                      {"name": "Lead", "params": {"Brit 2204 Custom": {"Drive": 0.85}}},
                      {"name": "Clean", "disable": ["Scream 808"]}],
        "footswitches": [{"switch": "FS1", "block": "Scream 808"},
                         {"switch": "EXP1Toe", "block": "UK Wah 846"}],
        "expression": [{"pedal": "EXP1", "targets": [
            {"block": "UK Wah 846", "param": "Pedal", "min": 0.0, "max": 1.0}]}]}
    body = compose_preset(parse_spec(recipe), lib, source="t", irs=irs)
    flow0 = body["preset"]["flow"][0]
    by_model = {b["slot"][0]["model"]: b for k, b in flow0.items() if k[1:].isdigit()}
    by_model["HD2_AmpBrit2204Custom"]["slot"][0]["params"]["Mid"] = {
        "value": 0.5, "snapshots": [0.5, 0.5, 0.5, 0.5, 0.7, 0.5, 0.2, 0.5]}
    ir = [b for b in by_model.values() if b["slot"][0].get("irhash") == CLOSE][0]
    ir["slot"][0]["params"]["Level"] = {
        "value": -18.0, "snapshots": [-18.0, -18.0, -18.0, -18.0, -18.0, -21.0, -18.0, -18.0]}
    flow0["b13"]["slot"][0]["params"]["gain"] = {
        "value": 0.0, "snapshots": [0.0, 0.0, 0.0, -2.0, -2.0, 0.0, 0.0, 0.0]}
    by_model["HD2_WahUKWah846"]["slot"][0]["params"]["Pedal"]["controller"]["bypassed"] = True
    assert usbtaps.apply(body, {"guitar": "3/4", "mic": "5/6"})["status"] == "added"
    for s in venue.FREE_SWITCHES:
        body["preset"]["sources"].setdefault(str(_sid(body, s)), {
            "bypass": False, "fs_color": "auto", "fs_label": "", "fs_topidx": 0})
    return body


def _rest(body):
    return {k: v for k, v in body["preset"].items() if k not in ("flow", "sources")}


@needs_sf
def test_real_shape_headphones_touches_nothing_else(real_shape, lib, irs):
    r = venue.apply_venue(real_shape, "headphones", lib, irs)
    assert r.venue_detail["treatment"] == "room-ir" and not r.warnings
    before, after = real_shape["preset"]["flow"], r.body["preset"]["flow"]
    assert len(_treatment_keys(after[0])) == 4
    assert _kept(after[0], _treatment_keys(after[0])) == _kept(before[0])
    assert after[1] == before[1]
    assert _rest(r.body) == _rest(real_shape) and r.body["meta"] == real_shape["meta"]
    src0, src1 = real_shape["preset"]["sources"], r.body["preset"]["sources"]
    assert {k for k in src0 if src0[k] != src1[k]} == {
        str(_sid(r.body, "FS2")), str(_sid(r.body, "FS3"))}
    assert set(src1) == set(src0)


@needs_sf
def test_real_shape_headphones_then_direct_is_the_original(real_shape, lib, irs):
    a = venue.apply_venue(real_shape, "headphones", lib, irs)
    assert _to_direct(a, lib, irs).body == real_shape     # grid keys included


def _model_chain(path):
    return [path[k]["slot"][0]["model"] for k in mutate._lane0_chain(path)]


def test_real_shape_amp_in(real_shape, lib, irs):
    r = venue.apply_venue(real_shape, "amp-in", lib, irs)
    flow0 = r.body["preset"]["flow"][0]
    assert _model_chain(flow0) == ["HD2_WahUKWah846", "HD2_DistScream808Mono", "HD2_AmpBrit2204Custom",
                                   "HD2_EQParametric", "HD2_RvbPlate", "P35_AppDSPSplitY"]
    before = real_shape["preset"]["flow"]
    ir = [k for k, b in before[0].items() if k[1:].isdigit() and b["slot"][0].get("irhash")]
    eq = [k for k, b in flow0.items() if k[1:].isdigit()
          and b["slot"][0]["model"] == "HD2_EQParametric"]
    assert _kept(flow0, eq) == _kept(before[0], ir)
    assert r.body["preset"]["flow"][1] == before[1]
    assert _rest(r.body) == _rest(real_shape)
    assert r.body["preset"]["sources"] == real_shape["preset"]["sources"]
    assert r.warnings == ["per-snapshot values on 'IR' removed with the cab"]


@needs_sf
def test_real_shape_amp_in_from_headphones(real_shape, lib, irs):
    a = venue.apply_venue(real_shape, "headphones", lib, irs)
    c = venue.apply_venue(a.body, "amp-in", lib, irs, source_venue="headphones",
                          source_detail=a.venue_detail)
    assert c.body == venue.apply_venue(real_shape, "amp-in", lib, irs).body


@needs_sf
def test_a_pushed_device_tap_comes_back_as_itself(lib, irs):
    """A device-pulled tap split carries a ``branch`` into the row-1 cell beneath
    it. Headphones pushes the tap from b11 to b12 (8 blocks + 3); the tap must
    move as its own dicts (branch following it), and direct must put it back."""
    body = _body(lib, irs, blocks=_long(4))
    assert usbtaps.apply(body, {"guitar": "3/4"})["status"] == "added"
    flow0 = body["preset"]["flow"][0]
    flow0["b11"]["branch"] = "b25"
    a = venue.apply_venue(body, "headphones", lib, irs)
    assert a.venue_detail["treatment"] == "room-ir" and a.venue_detail["ambience_block"]
    tap = a.body["preset"]["flow"][0]["b12"]
    assert (tap["type"], tap["position"], tap["branch"]) == ("split", 12, "b26")
    assert _to_direct(a, lib, irs).body["preset"]["flow"] == body["preset"]["flow"]
