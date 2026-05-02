"""Shared pytest fixtures for helixgen tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
