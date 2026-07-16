"""Shared pytest fixtures for helixgen tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
