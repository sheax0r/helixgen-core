"""hgc-es5 venue-drop guard + hgc-3am in-place room-lane level knob (CLI)."""
import json

import pytest
from click.testing import CliRunner

from helixgen import home, tone_meta
from helixgen.cli import cli
from helixgen.hsp import read_hsp
from tests.test_venue import CLOSE, ROOM, _chain, irs, lib, needs_sf  # noqa: F401


def _run(*args):
    return CliRunner().invoke(cli, list(args))


def _write(tmp_path, blocks, name="r.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"name": "Probe", "paths": [{"blocks": blocks}]}))
    return p


def _gen(lib, irs, recipe, *extra, descriptor="Probe"):
    return _run("generate", str(recipe), "--library", str(lib.root),
                "--irs-dir", str(irs.irs_dir), "--descriptor", descriptor, *extra)


@pytest.fixture(autouse=True)
def _no_default_venue(monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_VENUE", raising=False)


def _meta(slug="probe"):
    return tone_meta.load_tone_meta(slug)


def _room_block(hp):
    """(bNN, slot0) of the parallel room IR lane in path 0, or (None, None)."""
    for k, b in hp["preset"]["flow"][0].items():
        if (k[1:].isdigit() and int(k[1:]) >= 14 and isinstance(b, dict)
                and b.get("slot") and b["slot"][0].get("irhash") == ROOM):
            return k, b["slot"][0]
    return None, None


def _room_level(hp):
    _, s = _room_block(hp)
    w = (s.get("params") or {}).get("Level") if s else None
    return w["value"] if isinstance(w, dict) else w


def _room_switch_max(hp):
    _, s = _room_block(hp)
    ctrl = ((s.get("params") or {}).get("Level") or {}).get("controller") if s else None
    return ctrl.get("max") if isinstance(ctrl, dict) else None


def _structure(hp):
    """Block keys + model/type per path — changes iff a block was added/removed/moved."""
    return [sorted((k, (b.get("slot", [{}])[0].get("model") if isinstance(b, dict) else None),
                    b.get("type") if isinstance(b, dict) else None)
                   for k, b in flow.items() if isinstance(b, dict))
            for flow in hp["preset"]["flow"]]


# --- Phase 0: provenance recorded on generate -------------------------------

@needs_sf
def test_generate_headphones_records_room_ir_treatment(lib, irs, tmp_path):
    irs.save()
    assert _gen(lib, irs, _write(tmp_path, _chain()), "--venue", "headphones").exit_code == 0
    d = _meta().variants["generic"].venue_detail
    assert d["treatment"] == "room-ir" and d["room_ir"] == ROOM


# --- hgc-es5: the guard -----------------------------------------------------

@needs_sf
def test_venue_to_direct_refuses_to_drop_a_room_ir_lane(lib, irs, tmp_path):
    irs.save()
    assert _gen(lib, irs, _write(tmp_path, _chain()), "--venue", "headphones").exit_code == 0
    probe = home.tones_dir() / "probe.hsp"
    before = probe.read_bytes()
    r = _run("library", "venue", str(probe), "direct",
             "--library", str(lib.root), "--irs-dir", str(irs.irs_dir))
    assert r.exit_code != 0
    assert "room-mic lane" in r.output and "room-level" in r.output
    assert probe.read_bytes() == before                      # nothing written
    assert _meta().variants["generic"].venue_detail["treatment"] == "room-ir"


@needs_sf
def test_venue_force_overrides_the_guard(lib, irs, tmp_path):
    irs.save()
    assert _gen(lib, irs, _write(tmp_path, _chain()), "--venue", "headphones").exit_code == 0
    probe = home.tones_dir() / "probe.hsp"
    r = _run("library", "venue", str(probe), "direct", "--force",
             "--library", str(lib.root), "--irs-dir", str(irs.irs_dir))
    assert r.exit_code == 0, r.output
    assert _meta().variants["generic"].venue == "direct"


@needs_sf
def test_venue_dry_run_previews_without_tripping_the_guard(lib, irs, tmp_path):
    irs.save()
    assert _gen(lib, irs, _write(tmp_path, _chain()), "--venue", "headphones").exit_code == 0
    probe = home.tones_dir() / "probe.hsp"
    r = _run("library", "venue", str(probe), "direct", "--dry-run",
             "--library", str(lib.root), "--irs-dir", str(irs.irs_dir))
    assert r.exit_code == 0, r.output
    assert _meta().variants["generic"].venue == "headphones"  # unchanged


@needs_sf
def test_guard_silent_when_recorded_treatment_is_not_room_ir(lib, irs, tmp_path):
    """A direct variant converting TO headphones (producing room-ir) is fine:
    the guard only protects an already-recorded room-ir lane."""
    irs.save()
    assert _gen(lib, irs, _write(tmp_path, _chain()),
                "--venue", "direct").exit_code == 0
    probe = home.tones_dir() / "probe.hsp"
    r = _run("library", "venue", str(probe), "headphones",
             "--library", str(lib.root), "--irs-dir", str(irs.irs_dir))
    assert r.exit_code == 0, r.output
    assert _meta().variants["generic"].venue_detail["treatment"] == "room-ir"


# --- hgc-3am: in-place room-lane level knob ---------------------------------

@needs_sf
def test_room_level_tames_the_lane_in_place(lib, irs, tmp_path):
    irs.save()
    assert _gen(lib, irs, _write(tmp_path, _chain()), "--venue", "headphones").exit_code == 0
    probe = home.tones_dir() / "probe.hsp"
    before = read_hsp(probe)
    old_level = _room_level(before)
    assert old_level is not None and _room_switch_max(before) == pytest.approx(old_level)

    r = _run("library", "room-level", str(probe), "--library", str(lib.root),
             "--", "-45")
    assert r.exit_code == 0, r.output

    after = read_hsp(probe)
    assert _room_level(after) == pytest.approx(-45.0)
    assert _room_switch_max(after) == pytest.approx(-45.0)      # footswitch toe tracks
    # treatment + structure unchanged, only the room lane got quieter
    assert _structure(after) == _structure(before)
    assert _meta().variants["generic"].venue_detail["treatment"] == "room-ir"
    assert _meta().variants["generic"].venue_detail["room_level_db"] == pytest.approx(-45.0)


@needs_sf
def test_room_level_refuses_a_non_room_ir_tone(lib, irs, tmp_path):
    irs.save()
    assert _gen(lib, irs, _write(tmp_path, _chain()), "--venue", "direct").exit_code == 0
    probe = home.tones_dir() / "probe.hsp"
    r = _run("library", "room-level", str(probe), "--library", str(lib.root),
             "--", "-45")
    assert r.exit_code != 0 and "room-IR" in r.output


@needs_sf
def test_room_level_rejects_non_finite(lib, irs, tmp_path):
    irs.save()
    assert _gen(lib, irs, _write(tmp_path, _chain()), "--venue", "headphones").exit_code == 0
    probe = home.tones_dir() / "probe.hsp"
    for bad in ("nan", "inf", "-inf"):
        r = _run("library", "room-level", str(probe), "--library", str(lib.root),
                 "--", bad)
        assert r.exit_code != 0 and "finite" in r.output, (bad, r.output)
