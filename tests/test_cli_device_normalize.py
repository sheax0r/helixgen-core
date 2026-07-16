"""CLI tests for `device normalize` (loudness phase 2, backlog #62).

Never touch a real device: HelixClient is a fake that records snapshot
recalls / preset loads, and HelixSubscriber replays a scripted telemetry
stream whose loudness depends on which target the fake client last selected
— so the closed loop (recall -> measure -> trim -> write .hsp) runs fully
offline.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.hsp import read_hsp
from tests.golden import harness

msgpack = pytest.importorskip("msgpack")

# in=0.02 with outs 0.5 / 1.0 / 0.52 -> chain gains +27.96 / +33.98 / +28.30 dB
IN_LEVEL = 0.02
GAINS = {("snap", 0): 0.5, ("snap", 1): 1.0, ("snap", 2): 0.52}


class _Ev:
    def __init__(self, args):
        self.args = args


def _grid(cells):
    vals = [0.0] * 128
    for i, v in cells.items():
        vals[i] = v
    return vals


def _bursts(pitch, inp, out, n=60):
    events = []
    for _ in range(n):
        events += [
            _Ev([{"id__": {"eid_": 10, "mid_": 796}, "vals": [pitch]}]),
            _Ev([{"id__": {"eid_": 1, "mid_": 796},
                  "vals": _grid({0: inp, 1: inp})}]),
            _Ev([{"id__": {"eid_": 1, "mid_": 800},
                  "vals": _grid({108: out, 109: out})}]),
        ]
    return events


class ScriptedSubscriber:
    """Yields a telemetry window for whatever target the fake client last
    selected. A target scripted as "hum" yields pitchless (gated-out) data."""

    state = {"key": None}
    script = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, duration=None, filter_addrs=None, include_noise=False):
        out = type(self).script.get(type(self).state["key"])
        if out is None:
            return
        if out == "hum":
            yield from _bursts(-1.0, 0.03, 0.5)
        else:
            yield from _bursts(40.0, IN_LEVEL, out)


class FakeClient:
    """Stand-in for HelixClient: records calls and steers the subscriber."""

    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def activate_snapshot(self, index):
        type(self).calls.append(("activate_snapshot", index))
        ScriptedSubscriber.state["key"] = ("snap", index)
        return True

    def load_preset(self, cid):
        type(self).calls.append(("load_preset", cid))
        ScriptedSubscriber.state["key"] = ("cid", cid)
        return True

    def product_info(self):
        return {"serial": "FAKE123"}


def _patch(monkeypatch, script):
    import helixgen.device as device_mod
    from helixgen.device import subscribe as sub_mod

    ScriptedSubscriber.script = dict(script)
    ScriptedSubscriber.state = {"key": None}
    FakeClient.calls = []
    monkeypatch.setattr(sub_mod, "HelixSubscriber", ScriptedSubscriber)
    monkeypatch.setattr(device_mod, "HelixClient", FakeClient)


@pytest.fixture
def preset(tmp_path):
    dst = tmp_path / "snapshots.hsp"
    shutil.copy(harness.CORPUS_DIR / "snapshots.hsp", dst)
    return dst


def _gain(path: Path, flow=0):
    return read_hsp(path)["preset"]["flow"][flow]["b13"]["slot"][0][
        "params"]["gain"]


# --- snapshot scope ----------------------------------------------------------

def test_normalize_snapshots_dry_run_reports_and_writes_nothing(
        monkeypatch, preset):
    _patch(monkeypatch, GAINS)
    before = preset.read_bytes()
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--seconds", "6"])
    assert result.exit_code == 0, result.output
    # every named snapshot was recalled, then the on-load snapshot restored
    assert FakeClient.calls == [("activate_snapshot", 0),
                                ("activate_snapshot", 1),
                                ("activate_snapshot", 2),
                                ("activate_snapshot", 0)]
    assert "dry-run" in result.output and "--yes" in result.output
    assert "-6.0" in result.output          # Lead's proposed trim
    assert preset.read_bytes() == before    # nothing written


def test_normalize_snapshots_yes_writes_per_snapshot_trims(
        monkeypatch, preset):
    _patch(monkeypatch, GAINS)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--seconds", "6", "--yes"])
    assert result.exit_code == 0, result.output
    w = _gain(preset)
    # Lead (snapshot 1) trimmed -6.0 dB toward the Rhythm anchor; Clean's
    # +0.34 dB delta is inside the +-1 dB band -> untouched (densified base)
    assert w["snapshots"] == [0.0, -6.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert w["value"] == 0.0
    # both DSP paths' outputs move together
    assert _gain(preset, flow=1)["snapshots"][1] == -6.0
    # the .hsp is the source of truth; the device copy comes via sync
    assert "device sync" in result.output


def test_normalize_snapshots_target_db_is_absolute(monkeypatch, preset):
    _patch(monkeypatch, GAINS)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--seconds", "6",
              "--target-db", "30", "--yes"])
    assert result.exit_code == 0, result.output
    w = _gain(preset)
    assert w["snapshots"] == [2.0, -4.0, 1.7, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_normalize_snapshots_skips_unmeasurable_target(monkeypatch, preset):
    _patch(monkeypatch, {**GAINS, ("snap", 2): "hum"})
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--seconds", "6", "--yes"])
    assert result.exit_code == 1  # a skipped target is a partial result
    assert "SKIPPED" in result.output
    w = _gain(preset)
    assert w["snapshots"][1] == -6.0   # the measurable trim still landed
    assert w["snapshots"][2] == 0.0


def test_normalize_snapshots_aborts_when_no_anchor(monkeypatch, preset):
    _patch(monkeypatch, {k: "hum" for k in GAINS})
    before = preset.read_bytes()
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--seconds", "6", "--yes"])
    assert result.exit_code != 0
    assert preset.read_bytes() == before


def test_normalize_snapshots_json(monkeypatch, preset):
    _patch(monkeypatch, GAINS)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--seconds", "6", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)  # progress goes to stderr under --json
    assert payload["scope"] == "snapshots"
    assert payload["dry_run"] is True
    assert payload["anchor"] == {"snapshot": 0, "name": "Rhythm"}
    assert payload["target_gain_db"] == pytest.approx(27.96, abs=0.01)
    by_name = {t["name"]: t for t in payload["targets"]}
    assert by_name["Lead"]["trim_db"] == -6.0
    assert by_name["Clean"]["trim_db"] == 0.0
    assert all(t["applied"] is False for t in payload["targets"])
    assert payload["written"] == []


def test_normalize_requires_named_snapshots(monkeypatch, tmp_path):
    _patch(monkeypatch, GAINS)
    dst = tmp_path / "flat.hsp"
    shutil.copy(harness.CORPUS_DIR / "goldfinger.hsp", dst)
    result = CliRunner().invoke(cli, ["device", "normalize", str(dst)])
    assert result.exit_code != 0
    assert "named snapshot" in result.output


def test_normalize_requires_exactly_one_scope(monkeypatch, preset):
    _patch(monkeypatch, GAINS)
    result = CliRunner().invoke(cli, ["device", "normalize"])
    assert result.exit_code != 0
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--setlist", "Gig"])
    assert result.exit_code != 0


# --- setlist scope -----------------------------------------------------------

@pytest.fixture
def gig_setlist(tmp_path):
    """A local manifest setlist 'Gig' with two tones that have local .hsp
    sources and observed placements on the fake device (serial FAKE123)."""
    from helixgen.device import observations
    from helixgen.device.manifest import SetlistManifest

    # two DIFFERENT corpus presets (tone names come from meta.name and must
    # be unique in the manifest); flow_params carries a non-zero base output
    # level (-4.5 dB) so the shift compounds on it
    a = tmp_path / "ToneA.hsp"
    b = tmp_path / "ToneB.hsp"
    shutil.copy(harness.CORPUS_DIR / "goldfinger.hsp", a)
    shutil.copy(harness.CORPUS_DIR / "flow_params.hsp", b)
    m = SetlistManifest.load()
    name_a = m.add_tone("Gig", a)
    name_b = m.add_tone("Gig", b)
    m.save()
    obs = observations.load_observations("FAKE123")
    obs.tones[name_a] = {"cid": 101, "posi": 0}
    obs.tones[name_b] = {"cid": 102, "posi": 1}
    observations.save_observations(obs)
    return {"names": (name_a, name_b), "paths": (a, b)}


def test_normalize_setlist_trims_toward_anchor(monkeypatch, gig_setlist):
    _patch(monkeypatch, {("cid", 101): 0.5, ("cid", 102): 1.0})
    result = CliRunner().invoke(
        cli, ["device", "normalize", "--setlist", "Gig",
              "--seconds", "6", "--yes"])
    assert result.exit_code == 0, result.output
    assert ("load_preset", 101) in FakeClient.calls
    assert ("load_preset", 102) in FakeClient.calls
    a, b = gig_setlist["paths"]
    assert _gain(a)["value"] == 0.0     # anchor untouched
    assert _gain(b)["value"] == -10.5   # -4.5 base shifted -6.0 dB
    assert _gain(b, flow=1)["value"] == -6.0


def test_normalize_setlist_skips_tone_without_placement(
        monkeypatch, gig_setlist):
    from helixgen.device import observations
    name_a, name_b = gig_setlist["names"]
    obs = observations.load_observations("FAKE123")
    del obs.tones[name_b]
    observations.save_observations(obs)
    _patch(monkeypatch, {("cid", 101): 0.5, ("cid", 102): 1.0})
    result = CliRunner().invoke(
        cli, ["device", "normalize", "--setlist", "Gig",
              "--seconds", "6", "--yes", "--json"])
    assert result.exit_code == 1  # partial: a target was skipped
    payload = json.loads(result.stdout)
    by_tone = {t["tone"]: t for t in payload["targets"]}
    assert by_tone[name_b]["ok"] is False
    assert not any(c == ("load_preset", 102) for c in FakeClient.calls)


def test_normalize_setlist_unknown_setlist(monkeypatch, gig_setlist):
    _patch(monkeypatch, {})
    result = CliRunner().invoke(
        cli, ["device", "normalize", "--setlist", "Nope"])
    assert result.exit_code != 0
    assert "Nope" in result.output
