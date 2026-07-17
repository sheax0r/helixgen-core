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
@pytest.fixture(autouse=True)
def _configured_device_ip(monkeypatch):
    """#74: device verbs no longer have a built-in default IP. These tests
    exercise verb logic against fakes, so simulate a configured user."""
    monkeypatch.setenv("HELIXGEN_HELIX_IP", "10.0.0.99")


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
    """Stand-in for HelixClient: records calls, steers the subscriber, and
    models the device's ACTIVE-preset identity (cid + name) so the normalize
    identity guard is exercised offline."""

    calls = []
    names = {}                        # cid -> device preset display name
    active = {"cid": 7, "name": None}

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
        type(self).active = {"cid": cid, "name": type(self).names.get(cid)}
        return True

    def active_preset(self):
        a = type(self).active
        return {"cid": a["cid"], "name": a["name"], "posi": 0,
                "slot": "1A", "ccid": -2}

    def product_info(self):
        return {"serial": "FAKE123"}


def _patch(monkeypatch, script, active_name="Snapshots Corpus", names=None):
    import helixgen.device as device_mod
    from helixgen.device import subscribe as sub_mod

    ScriptedSubscriber.script = dict(script)
    ScriptedSubscriber.state = {"key": None}
    FakeClient.calls = []
    FakeClient.names = dict(names or {})
    FakeClient.active = {"cid": 7, "name": active_name}
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
    assert payload["target_total_db"] == pytest.approx(27.96, abs=0.01)
    by_name = {t["name"]: t for t in payload["targets"]}
    assert by_name["Lead"]["output_level_db"] == 0.0
    assert by_name["Lead"]["total_db"] == pytest.approx(33.98, abs=0.01)
    assert by_name["Lead"]["trim_db"] == -6.0
    assert by_name["Clean"]["trim_db"] == 0.0
    assert all(t["applied"] is False for t in payload["targets"])
    assert payload["written"] == []


def test_normalize_snapshots_yes_rerun_is_noop(monkeypatch, preset):
    # C1: the meters tap upstream of output gain, so a re-run measures the
    # SAME gains — sizing trims from TOTAL loudness (gain + output level)
    # makes the second --yes pass a dead-band no-op, not a doubled trim.
    _patch(monkeypatch, GAINS)
    args = ["device", "normalize", str(preset), "--seconds", "6", "--yes"]
    assert CliRunner().invoke(cli, args).exit_code == 0
    assert _gain(preset)["snapshots"][1] == -6.0
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "nothing to write" in result.output
    assert _gain(preset)["snapshots"][1] == -6.0   # NOT -12


def test_normalize_snapshots_preserves_hand_balanced_overrides(
        monkeypatch, preset):
    # C1: a pre-existing hand-balanced override that already equalizes total
    # loudness (Lead measures +6 dB hotter, its output is already -6 dB)
    # must be left alone, not re-trimmed toward -12.
    from helixgen.hsp import write_hsp
    body = read_hsp(preset)
    for fl in (0, 1):
        body["preset"]["flow"][fl]["b13"]["slot"][0]["params"]["gain"][
            "snapshots"] = [0.0, -6.0] + [0.0] * 6
    write_hsp(preset, body)
    _patch(monkeypatch, GAINS)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--seconds", "6", "--yes"])
    assert result.exit_code == 0, result.output
    assert "nothing to write" in result.output
    assert _gain(preset)["snapshots"][1] == -6.0


def test_normalize_snapshots_aborts_on_active_preset_mismatch(
        monkeypatch, preset):
    # I1: the device's ACTIVE tone is verified against the .hsp's name
    # BEFORE anything is measured — a mismatch aborts the whole run.
    _patch(monkeypatch, GAINS, active_name="Some Other Tone")
    before = preset.read_bytes()
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--seconds", "6", "--yes"])
    assert result.exit_code != 0
    assert "Some Other Tone" in result.output
    assert "Snapshots Corpus" in result.output
    assert not any(c[0] == "activate_snapshot" for c in FakeClient.calls)
    assert preset.read_bytes() == before


def test_normalize_snapshots_unverifiable_active_name_warns_and_proceeds(
        monkeypatch, preset):
    # an unresolvable active-preset name (e.g. edit buffer never saved) is
    # not proof of a mismatch: warn and continue
    _patch(monkeypatch, GAINS, active_name=None)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--seconds", "6", "--yes"])
    assert result.exit_code == 0, result.output
    assert "could not verify" in result.output
    assert _gain(preset)["snapshots"][1] == -6.0


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
    # level (-4.5 dB) so total-loudness sizing has to account for it
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


def _patch_gig(monkeypatch, gig_setlist, script=None, names=None):
    name_a, name_b = gig_setlist["names"]
    _patch(monkeypatch,
           script if script is not None else {("cid", 101): 0.5,
                                              ("cid", 102): 1.0},
           names=names if names is not None else {101: name_a, 102: name_b})


def test_normalize_setlist_trims_equalize_total_loudness(
        monkeypatch, gig_setlist):
    _patch_gig(monkeypatch, gig_setlist)
    result = CliRunner().invoke(
        cli, ["device", "normalize", "--setlist", "Gig",
              "--seconds", "6", "--yes"])
    assert result.exit_code == 0, result.output
    assert ("load_preset", 101) in FakeClient.calls
    assert ("load_preset", 102) in FakeClient.calls
    a, b = gig_setlist["paths"]
    assert _gain(a)["value"] == 0.0     # anchor untouched
    # C1: ToneB measures +6.02 dB hotter but already carries a -4.5 dB base,
    # so its TOTAL loudness is only 1.52 dB above the anchor's — the whole
    # preset shifts -1.5 dB (uniform, preserving intra-preset balance),
    # ending equalized in total loudness (not shifted the full -6)
    assert _gain(b)["value"] == -6.0
    assert _gain(b, flow=1)["value"] == -1.5
    # M7: the player's previously ACTIVE preset (cid 7) is restored
    assert FakeClient.calls[-1] == ("load_preset", 7)


def test_normalize_setlist_yes_rerun_is_noop(monkeypatch, gig_setlist):
    _patch_gig(monkeypatch, gig_setlist)
    args = ["device", "normalize", "--setlist", "Gig", "--seconds", "6",
            "--yes"]
    assert CliRunner().invoke(cli, args).exit_code == 0
    _patch_gig(monkeypatch, gig_setlist)   # fresh call log, same telemetry
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "nothing to write" in result.output
    b = gig_setlist["paths"][1]
    assert _gain(b)["value"] == -6.0       # NOT -7.5


def test_normalize_setlist_skips_tone_with_mismatched_device_name(
        monkeypatch, gig_setlist):
    # I1: a stale observed CID that now points at a DIFFERENT preset must
    # not be silently measured — the tone is skipped into the exit-1 path.
    name_a, name_b = gig_setlist["names"]
    _patch_gig(monkeypatch, gig_setlist,
               names={101: name_a, 102: "Renamed On Device"})
    b_before = gig_setlist["paths"][1].read_bytes()
    result = CliRunner().invoke(
        cli, ["device", "normalize", "--setlist", "Gig",
              "--seconds", "6", "--yes", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    by_tone = {t["tone"]: t for t in payload["targets"]}
    assert by_tone[name_b]["ok"] is False
    assert "Renamed On Device" in by_tone[name_b]["reason"]
    assert gig_setlist["paths"][1].read_bytes() == b_before


def test_normalize_setlist_write_failure_reports_written_files(
        monkeypatch, gig_setlist, tmp_path):
    # M6: a mid-run write failure must say which files were ALREADY written
    from helixgen import hsp as hsp_mod
    from helixgen.device.manifest import SetlistManifest
    from helixgen.device import observations

    c = tmp_path / "ToneC.hsp"
    shutil.copy(harness.CORPUS_DIR / "snapshots.hsp", c)
    m = SetlistManifest.load()
    name_c = m.add_tone("Gig", c)
    m.save()
    obs = observations.load_observations("FAKE123")
    obs.tones[name_c] = {"cid": 103, "posi": 2}
    observations.save_observations(obs)

    name_a, name_b = gig_setlist["names"]
    _patch_gig(monkeypatch, gig_setlist,
               script={("cid", 101): 0.5, ("cid", 102): 1.0,
                       ("cid", 103): 1.0},
               names={101: name_a, 102: name_b, 103: name_c})
    real_write = hsp_mod.write_hsp

    def failing_write(path, body):
        if str(path).endswith("ToneC.hsp"):
            raise OSError("disk full")
        return real_write(path, body)

    monkeypatch.setattr(hsp_mod, "write_hsp", failing_write)
    result = CliRunner().invoke(
        cli, ["device", "normalize", "--setlist", "Gig",
              "--seconds", "6", "--yes"])
    assert result.exit_code != 0
    assert "ToneC.hsp" in result.output          # the failure itself
    assert str(gig_setlist["paths"][1]) in result.output  # already written


def test_normalize_setlist_skips_tone_without_placement(
        monkeypatch, gig_setlist):
    from helixgen.device import observations
    name_a, name_b = gig_setlist["names"]
    obs = observations.load_observations("FAKE123")
    del obs.tones[name_b]
    observations.save_observations(obs)
    _patch_gig(monkeypatch, gig_setlist)
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


# --- library metadata recording (`normalized` on the tone's variant) ---------


@pytest.fixture
def _isolated_git_env(tmp_path, monkeypatch):
    """save_tone_meta advisory-commits under the tmp home -- keep git hermetic
    (mirrors tests/test_library_group.py)."""
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    monkeypatch.delenv("HELIXGEN_GIT_COMMIT_TONES", raising=False)
    fake_home = tmp_path / "_fake_home_for_git"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL",
                       str(fake_home / "gitconfig-does-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def library_preset(tmp_path, _isolated_git_env):
    """The snapshots corpus preset registered as a LIBRARY variant: its .hsp
    lives under library/tones/ and a tone metadata JSON points at it."""
    from helixgen import home, tone_meta

    tones = home.tones_dir()
    tones.mkdir(parents=True, exist_ok=True)
    dst = tones / "snapshots-corpus.hsp"
    shutil.copy(harness.CORPUS_DIR / "snapshots.hsp", dst)
    meta = tone_meta.upsert_variant(
        None, descriptor="Snapshots Corpus", guitar_slug=None,
        guitar_short=None, hsp_path=dst)
    tone_meta.save_tone_meta(meta)
    return dst


def _library_normalized(variant_key="generic"):
    from helixgen import tone_meta
    meta = tone_meta.load_tone_meta("snapshots-corpus")
    return meta.variants[variant_key].normalized


def test_normalize_yes_records_normalized_on_library_variant(
        monkeypatch, library_preset):
    from helixgen import __version__
    _patch(monkeypatch, GAINS)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(library_preset),
              "--seconds", "6", "--yes"])
    assert result.exit_code == 0, result.output
    rec = _library_normalized()
    assert rec is not None
    assert rec["scope"] == "snapshots"
    assert rec["target_total_db"] == pytest.approx(27.96, abs=0.01)
    assert rec["tolerance_db"] == 1.0
    assert rec["seconds"] == 6.0                 # the --seconds used
    assert rec["helixgen_version"] == __version__
    assert rec["at"].startswith("20")            # ISO timestamp
    # every named snapshot is in targets, with the FULL measurement
    # telemetry -- in-band zero trims included
    by_name = {t["name"]: t for t in rec["targets"]}
    assert set(by_name) == {"Rhythm", "Lead", "Clean"}
    lead = by_name["Lead"]
    assert lead["snapshot"] == 1
    assert lead["ok"] is True
    assert lead["gain_db"] == pytest.approx(33.98, abs=0.01)
    assert lead["output_db"] == pytest.approx(0.0, abs=0.05)  # chain-out dBFS
    # #64d: playing_seconds now follows the OBSERVED sample rate; the
    # scripted subscriber replays its window instantly, so it reports ~0.0
    # (the ok flag above is the measurement-trust signal, not this field)
    assert lead["playing_seconds"] >= 0.0
    assert lead["output_level_db"] == 0.0
    assert lead["total_db"] == pytest.approx(33.98, abs=0.01)
    assert lead["trim_db"] == -6.0
    assert lead["applied"] is True
    assert by_name["Rhythm"]["trim_db"] == 0.0   # the anchor, in band
    assert by_name["Rhythm"]["applied"] is False
    assert "recorded in library" in result.output


def test_normalize_dry_run_never_writes_library_metadata(
        monkeypatch, library_preset):
    _patch(monkeypatch, GAINS)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(library_preset), "--seconds", "6"])
    assert result.exit_code == 0, result.output
    assert _library_normalized() is None


def test_normalize_all_in_band_still_records_confirmation(
        monkeypatch, library_preset):
    # a --yes run whose trims are ALL in band writes no .hsp but still
    # records: zero trims confirm the tone is level-matched
    _patch(monkeypatch, {("snap", 0): 0.5, ("snap", 1): 0.5,
                         ("snap", 2): 0.5})
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(library_preset),
              "--seconds", "6", "--yes"])
    assert result.exit_code == 0, result.output
    assert "nothing to write" in result.output
    rec = _library_normalized()
    assert rec is not None
    assert {t["name"]: t["trim_db"] for t in rec["targets"]} == {
        "Rhythm": 0.0, "Lead": 0.0, "Clean": 0.0}
    assert all(t["applied"] is False for t in rec["targets"])


def test_normalize_partial_run_records_nothing(monkeypatch, library_preset):
    # a SKIPPED snapshot means the tone was NOT fully level-matched
    _patch(monkeypatch, {**GAINS, ("snap", 2): "hum"})
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(library_preset),
              "--seconds", "6", "--yes"])
    assert result.exit_code == 1
    assert _library_normalized() is None


def test_normalize_rerun_overwrites_record(monkeypatch, library_preset):
    _patch(monkeypatch, GAINS)
    args = ["device", "normalize", str(library_preset), "--seconds", "6",
            "--yes"]
    assert CliRunner().invoke(cli, args).exit_code == 0
    first = _library_normalized()
    assert {t["name"]: t["trim_db"] for t in first["targets"]}["Lead"] == -6.0
    # second run: same telemetry, trims now in band -> record OVERWRITTEN
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    rec = _library_normalized()
    assert {t["name"]: t["trim_db"] for t in rec["targets"]} == {
        "Rhythm": 0.0, "Lead": 0.0, "Clean": 0.0}


def test_normalize_non_library_hsp_records_nothing(monkeypatch, preset):
    from helixgen import tone_meta
    _patch(monkeypatch, GAINS)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(preset), "--seconds", "6", "--yes"])
    assert result.exit_code == 0, result.output
    assert tone_meta.load_all_tone_metas() == []
    assert "recorded in library" not in result.output


def test_normalize_json_reports_library_recorded(monkeypatch, library_preset):
    _patch(monkeypatch, GAINS)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(library_preset),
              "--seconds", "6", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["library_recorded"] == [
        {"tone": "snapshots-corpus", "variant": "generic",
         "preset_name": "Snapshots Corpus", "path": str(library_preset)}]
    # the pre-existing shape is intact
    assert payload["scope"] == "snapshots"
    assert payload["written"] == [str(library_preset)]


def test_normalize_json_dry_run_library_recorded_empty(
        monkeypatch, library_preset):
    _patch(monkeypatch, GAINS)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(library_preset),
              "--seconds", "6", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["library_recorded"] == []


def test_normalize_save_failure_warns_and_reports_empty_recorded(
        monkeypatch, library_preset):
    # review pin: a save_tone_meta failure during recording is advisory --
    # the run still exits 0, warns to stderr, and --json reports
    # library_recorded: [] (the written trims are the real outcome)
    from helixgen import tone_meta
    _patch(monkeypatch, GAINS)

    def boom(meta):
        raise OSError("disk full")

    monkeypatch.setattr(tone_meta, "save_tone_meta", boom)
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(library_preset),
              "--seconds", "6", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    assert "could not record" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["library_recorded"] == []
    assert payload["written"] == [str(library_preset)]


def test_normalize_corrupt_sibling_tone_meta_never_breaks_the_run(
        monkeypatch, library_preset):
    # regression (review finding 1): a corrupt-but-PARSEABLE tone JSON
    # anywhere in library/tones/ -- valid JSON, invalid shape -- used to
    # crash normalize --yes AFTER the trims were written, killing the
    # --json report. Shape-invalid files must be skipped with a stderr
    # warning; the matching tone still records.
    from helixgen import home
    _patch(monkeypatch, GAINS)
    tones = home.tones_dir()
    # variant value is a string -> AttributeError in _variant_from_dict
    (tones / "corrupt-a.json").write_text(
        json.dumps({"variants": {"generic": "not-a-dict"}}))
    # variant dict missing "hsp" -> KeyError in _variant_from_dict
    (tones / "corrupt-b.json").write_text(
        json.dumps({"variants": {"generic": {"preset_name": "X"}}}))
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(library_preset),
              "--seconds", "6", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    assert "corrupt-a.json" in result.stderr
    assert "corrupt-b.json" in result.stderr
    payload = json.loads(result.stdout)          # the report stays intact
    assert payload["written"] == [str(library_preset)]
    assert payload["library_recorded"] == [
        {"tone": "snapshots-corpus", "variant": "generic",
         "preset_name": "Snapshots Corpus", "path": str(library_preset)}]
    assert _library_normalized() is not None


def test_normalize_invalid_identity_meta_warns_and_completes(
        monkeypatch, library_preset):
    # regression (review finding 2): a matching meta whose IDENTITY is
    # broken (artist set, song null) loads fine but save_tone_meta raises
    # ValueError from meta_path(meta.logical_slug) -- and the old warning
    # handler evaluated meta.logical_slug!r AGAIN, so the same ValueError
    # escaped during exception handling. The warning must use a safe label
    # and the run must complete with its --json report intact.
    from helixgen import home
    _patch(monkeypatch, GAINS)
    p = home.tones_dir() / "snapshots-corpus.json"
    data = json.loads(p.read_text())
    data["artist"] = "Somebody"                  # song stays null -> invalid
    p.write_text(json.dumps(data))
    result = CliRunner().invoke(
        cli, ["device", "normalize", str(library_preset),
              "--seconds", "6", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    assert "could not record" in result.stderr
    assert "Snapshots Corpus" in result.stderr   # the safe label
    payload = json.loads(result.stdout)
    assert payload["written"] == [str(library_preset)]
    assert payload["library_recorded"] == []


def test_normalize_setlist_mixed_ok_and_skip_records_ok_variants(
        monkeypatch, library_preset, tmp_path):
    # review pin: setlist scope with MIXED ok/skipped targets still records
    # the measured-ok tones' variants (per-tone granularity) AND still
    # exits 1 for the partial run
    from helixgen.device import observations
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    name = m.add_tone("LibGig", library_preset)
    other = tmp_path / "OtherTone.hsp"
    shutil.copy(harness.CORPUS_DIR / "goldfinger.hsp", other)
    name_other = m.add_tone("LibGig", other)
    m.save()
    obs = observations.load_observations("FAKE123")
    obs.tones[name] = {"cid": 201, "posi": 0}
    # name_other gets NO observed placement -> SKIPPED
    observations.save_observations(obs)
    _patch(monkeypatch, {("cid", 201): 0.5}, names={201: name})
    result = CliRunner().invoke(
        cli, ["device", "normalize", "--setlist", "LibGig",
              "--seconds", "6", "--target-db", "30", "--yes", "--json"])
    assert result.exit_code == 1                 # partial: a target skipped
    payload = json.loads(result.stdout)
    by_tone = {t["tone"]: t for t in payload["targets"]}
    assert by_tone[name]["ok"] is True
    assert by_tone[name_other]["ok"] is False
    assert [r["tone"] for r in payload["library_recorded"]] == [
        "snapshots-corpus"]
    rec = _library_normalized()
    assert rec is not None
    assert rec["scope"] == "setlist"
    assert len(rec["targets"]) == 1
    assert rec["targets"][0]["tone"] == name


def test_normalize_setlist_records_base_trim_on_library_variant(
        monkeypatch, library_preset, _isolated_git_env):
    from helixgen.device import observations
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    name = m.add_tone("LibGig", library_preset)
    m.save()
    obs = observations.load_observations("FAKE123")
    obs.tones[name] = {"cid": 201, "posi": 0}
    observations.save_observations(obs)
    _patch(monkeypatch, {("cid", 201): 0.5}, names={201: name})
    result = CliRunner().invoke(
        cli, ["device", "normalize", "--setlist", "LibGig",
              "--seconds", "6", "--target-db", "30", "--yes"])
    assert result.exit_code == 0, result.output
    rec = _library_normalized()
    assert rec is not None
    assert rec["scope"] == "setlist"
    assert rec["target_total_db"] == 30.0
    assert rec["seconds"] == 6.0
    # setlist scope shifts the whole preset -> ONE target entry (this tone)
    assert len(rec["targets"]) == 1
    t = rec["targets"][0]
    assert t["tone"] == name
    assert t["ok"] is True
    assert t["gain_db"] == pytest.approx(27.96, abs=0.01)
    assert t["output_db"] == pytest.approx(-6.02, abs=0.05)  # chain-out dBFS
    assert t["total_db"] == pytest.approx(27.96, abs=0.01)
    assert t["trim_db"] == pytest.approx(2.0)
    assert t["applied"] is True
