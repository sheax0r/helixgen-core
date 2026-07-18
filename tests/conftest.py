"""Shared pytest fixtures for helixgen tests."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# The real helixgen home as resolved from the *launching* environment,
# captured at import — before any per-test autouse fixture redirects
# $HELIXGEN_HOME at a tmp dir. This is the one dir the offline suite must
# never mutate; the guard below snapshots it. Mirrors helixgen.home
# .helixgen_home() so a shell that sets $HELIXGEN_HOME is honored too.
_REAL_HOME = (
    Path(os.environ["HELIXGEN_HOME"]).expanduser()
    if os.environ.get("HELIXGEN_HOME")
    else Path.home() / ".helixgen"
)


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    """Map each file under ``root`` to ``(size, mtime_ns)``.

    Stat-only walk (no byte reads) so it stays cheap even over a large real
    home (IR audio, git objects, device backups) and across every xdist
    worker. Any creation, deletion, or in-place write shifts a file's path,
    size, or mtime_ns and so is caught. Missing ``root`` snapshots empty (a
    test that creates the real home from scratch is itself the regression).
    """
    snap: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return snap
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            snap[str(p)] = (st.st_size, st.st_mtime_ns)
    return snap


@pytest.fixture(scope="session", autouse=True)
def _real_home_untouched_guard():
    """FAIL the run if any offline test mutated the real ``~/.helixgen``.

    Defense-in-depth for backlog #79(k): the per-test autouse fixtures below
    already redirect every home-derived path (manifest, slots, IR-hash cache,
    locks, and — via ``$HELIXGEN_HOME`` — the library/prefs/cache defaults) at
    a tmp dir, so nothing should reach the real home. This snapshots the real
    home at session start and asserts it byte-count/mtime-identical at session
    end, so a future isolation regression fails the run immediately instead of
    silently polluting real state. Ports the live suite's real-home guard
    (``tests/live/conftest.py`` ``_real_dotfiles_guard``) to the offline suite.

    xdist-safe: session fixtures are per-worker, so each worker snapshots and
    asserts its own view of the (global) real home — no cross-worker state.
    """
    before = _snapshot_tree(_REAL_HOME)
    yield
    after = _snapshot_tree(_REAL_HOME)
    if before != after:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in before.keys() & after.keys()
                         if before[k] != after[k])
        raise AssertionError(
            "ISOLATION REGRESSION: the offline suite mutated the user's real "
            f"helixgen home at {_REAL_HOME}.\n"
            f"  added:   {added}\n"
            f"  removed: {removed}\n"
            f"  changed: {changed}\n"
            "Every offline test must operate on tmp state only — check for a "
            "helixgen path resolved without honoring the autouse env redirects "
            "(or a stray per-area $HELIXGEN_* override in the launching shell).")


@pytest.fixture(autouse=True)
def _isolate_irhash_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the IR-hash cache at a per-test tmp file for the whole suite, so
    no test ever reads or writes the real `~/.helixgen/cache/irhash.json`.
    Tests that assert on cache-path resolution override this env themselves.
    """
    monkeypatch.setenv("HELIXGEN_IRHASH_CACHE", str(tmp_path / "_irhash_cache.json"))


@pytest.fixture(autouse=True)
def _isolate_device_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the helixgen home, device slot ledger, and setlist manifest at
    per-test tmp files, so no test ever reads or writes the real
    `~/.helixgen/setlists.json`, the legacy `~/.helixgen/device-slots.json`, or
    the per-device `~/.helixgen/devices/<serial>.json` observation files. The
    ledger is now folded into the manifest file (`$HELIXGEN_SETLISTS`); the
    legacy `$HELIXGEN_DEVICE_SLOTS` path is only read for migration.
    `$HELIXGEN_HOME` isolation also captures `devices/` (observations, manifest
    v3). Tests that assert on path resolution override these envs themselves.
    """
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path))
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "_setlists.json"))
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(tmp_path / "_device_slots.json"))


@pytest.fixture(autouse=True)
def _isolate_device_ip(monkeypatch: pytest.MonkeyPatch):
    """Drop any inherited $HELIXGEN_HELIX_IP so the #74 resolution chain is
    deterministic in the offline suite (a developer shell's env must not
    change fail-fast behavior). Tests that assert on env resolution set it
    themselves via monkeypatch."""
    monkeypatch.delenv("HELIXGEN_HELIX_IP", raising=False)


@pytest.fixture(autouse=True)
def _isolate_device_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the advisory device-lock root at a per-test tmp dir, so no test
    ever reads/writes the real `~/.helixgen/locks/` (a developer's live
    session lease must not block the suite, nor the suite leak leases), and
    drop any inherited lock token/timeout from the invoking shell.
    """
    monkeypatch.setenv("HELIXGEN_LOCKS", str(tmp_path / "_locks"))
    monkeypatch.delenv("HELIXGEN_LOCK_TOKEN", raising=False)
    monkeypatch.delenv("HELIXGEN_LOCK_TIMEOUT", raising=False)


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp `~/.helixgen` home with the *default* manifest/devices layout in
    force (no `$HELIXGEN_SETLISTS` override), so migration tests exercise the
    real `manifest_path()` / `legacy_manifest_path()` / `devices_dir()` paths.
    """
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path))
    monkeypatch.delenv("HELIXGEN_SETLISTS", raising=False)
    monkeypatch.delenv("HELIXGEN_DEVICE_SLOTS", raising=False)
    return tmp_path


@pytest.fixture
def tmp_library(tmp_path: Path) -> Path:
    """Empty library directory in a tmp dir."""
    lib = tmp_path / "library"
    lib.mkdir()
    return lib


@pytest.fixture
def sample_amp_block() -> dict:
    """Synthetic single-block JSON for an amp."""
    return json.loads((FIXTURES_DIR / "blocks" / "sample_amp.json").read_text())


@pytest.fixture
def sample_cab_block() -> dict:
    """Synthetic single-block JSON for a cab."""
    return json.loads((FIXTURES_DIR / "blocks" / "sample_cab.json").read_text())


@pytest.fixture
def sample_serial_preset() -> dict:
    """Synthetic full-preset JSON, single serial DSP path."""
    return json.loads((FIXTURES_DIR / "presets" / "sample_serial.json").read_text())


@pytest.fixture
def sample_serial_preset_hsp() -> dict:
    """Minimal Stadium-chassis (.hsp) preset body for tests.

    Shape matches what extract_chassis_from_hsp expects: top-level `meta` +
    `preset.flow` list of path dicts with b00/b13 endpoint stubs.
    """
    return {
        "meta": {
            "name": "t",
            "color": "auto",
            "device_id": 2490368,
            "device_version": 318833973,
            "info": "",
        },
        "preset": {
            "clip": {"end": 0.0, "filename": "", "path": "", "start": 0.0},
            "cursor": {"flow": 0, "path": 0, "position": 0},
            "flow": [
                {
                    "@enabled": True,
                    "b00": {
                        "type": "input",
                        "position": 0,
                        "path": 0,
                        "slot": [{"model": "P35_InputInst1", "params": {}, "version": 0}],
                    },
                    "b13": {
                        "type": "output",
                        "position": 13,
                        "path": 0,
                        "slot": [{"model": "P35_OutputMatrix", "params": {}, "version": 0}],
                    },
                },
                {
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


@pytest.fixture
def hsp_library(tmp_path: Path, sample_serial_preset_hsp: dict):
    """A Stadium (.hsp) Library: chassis from the hsp fixture + two synthetic
    blocks (a drive `Tube Drive` and an amp `Brit Amp`). Exemplars are
    .hlx-normalized (params unwrapped). Shared by decompile/patch/CLI tests.
    """
    from helixgen.hsp import HSP_MAGIC
    from helixgen.ingest import ingest_path
    from helixgen.library import Block, Library

    chassis = tmp_path / "chassis.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(chassis, lib)
    lib.save_block(Block(
        model_id="HD2_DistTube", category="drive", display_name="Tube Drive",
        params={"Gain": {"type": "float"}, "Tone": {"type": "float"}},
        exemplar={"@model": "HD2_DistTube", "@type": "fx", "@enabled": True,
                  "Gain": 0.5, "Tone": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))
    lib.save_block(Block(
        model_id="HD2_AmpBrit", category="amp", display_name="Brit Amp",
        params={"Drive": {"type": "float"}, "Master": {"type": "float"}},
        exemplar={"@model": "HD2_AmpBrit", "@type": "amp", "@enabled": True,
                  "Drive": 0.5, "Master": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))
    return lib


@pytest.fixture
def strip_provenance():
    """Return a function that deep-copies a preset and drops the volatile
    `meta.helixgen.generated_at` stamp, so round-trip comparisons are stable.
    """
    def _strip(preset: dict) -> dict:
        p = json.loads(json.dumps(preset))
        p.get("meta", {}).get("helixgen", {}).pop("generated_at", None)
        return p
    return _strip
