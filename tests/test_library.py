import os
from pathlib import Path

import pytest

from helixgen.library import Block, default_library_path


def test_block_round_trips_through_dict():
    block = Block(
        model_id="HD2_AmpBrit2204Custom",
        category="amp",
        display_name="Brit 2204",
        aliases=["JCM800"],
        params={
            "Drive": {"type": "float", "default": 0.5, "observed_range": [0, 1]},
        },
        exemplar={"@model": "HD2_AmpBrit2204Custom", "Drive": 0.5},
        first_seen={"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"},
    )
    as_dict = block.to_dict()
    assert as_dict["model_id"] == "HD2_AmpBrit2204Custom"
    assert as_dict["display_name"] == "Brit 2204"
    assert as_dict["aliases"] == ["JCM800"]
    restored = Block.from_dict(as_dict)
    assert restored == block


def test_default_library_path_uses_home(monkeypatch):
    monkeypatch.delenv("HELIXGEN_LIBRARY", raising=False)
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    assert default_library_path() == Path("/tmp/fake-home/.helixgen/library")


def test_default_library_path_honors_env_var(monkeypatch):
    monkeypatch.setenv("HELIXGEN_LIBRARY", "/custom/lib")
    assert default_library_path() == Path("/custom/lib")
