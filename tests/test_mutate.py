"""Tests for helixgen.mutate — the .hsp-canonical body-mutation verbs.

These operate directly on a parsed `.hsp` body dict (`preset.flow[*].bNN`),
not on a spec.json. See docs/superpowers/plans/2026-07-08-hsp-canonical-redesign.md
Tasks 1b/1c.
"""
from __future__ import annotations

import pytest

from helixgen import mutate
from helixgen.hsp import read_hsp
from tests.golden import harness


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    root = tmp_path_factory.mktemp("mutate-test-library")
    return harness.build_corpus_library(root)


@pytest.fixture
def goldfinger_body():
    return read_hsp(harness.CORPUS_DIR / "goldfinger.hsp")


# --- resolve_slot ------------------------------------------------------

def test_resolve_slot_unique_match(goldfinger_body, library):
    assert mutate.resolve_slot(goldfinger_body, "Brit 2204 Custom", library) == (0, "b02", 0)


def test_resolve_slot_finds_each_placed_block(goldfinger_body, library):
    # Sanity: every block placed by the goldfinger recipe resolves to a
    # distinct (flow_index, bnn_key, slot_index).
    assert mutate.resolve_slot(goldfinger_body, "Scream 808", library) == (0, "b01", 0)
    assert mutate.resolve_slot(goldfinger_body, "4x12 Greenback 25", library) == (0, "b03", 0)
    assert mutate.resolve_slot(goldfinger_body, "Digital", library) == (0, "b04", 0)
    assert mutate.resolve_slot(goldfinger_body, "Plate", library) == (0, "b05", 0)


def test_resolve_slot_missing_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError) as exc:
        mutate.resolve_slot(goldfinger_body, "Nope Amp", library)
    msg = str(exc.value)
    assert "Nope Amp" in msg
    # Helpful message lists the blocks that ARE placed.
    assert "Scream 808" in msg
    assert "Brit 2204 Custom" in msg


def test_resolve_slot_ambiguous_raises(library):
    body = {
        "preset": {
            "flow": [
                {
                    "b01": {
                        "type": "amp", "position": 1, "path": 0,
                        "@enabled": {"value": True},
                        "slot": [{"model": "HD2_AmpBrit2204Custom", "@enabled": {"value": True}, "params": {}}],
                    },
                    "b02": {
                        "type": "amp", "position": 2, "path": 0,
                        "@enabled": {"value": True},
                        "slot": [{"model": "HD2_AmpBrit2204Custom", "@enabled": {"value": True}, "params": {}}],
                    },
                }
            ]
        }
    }
    with pytest.raises(mutate.MutateError) as exc:
        mutate.resolve_slot(body, "Brit 2204 Custom", library)
    assert "Brit 2204 Custom" in str(exc.value)
    # Disambiguated by pos resolves cleanly.
    assert mutate.resolve_slot(body, "Brit 2204 Custom", library, pos=1) == (0, "b01", 0)
    assert mutate.resolve_slot(body, "Brit 2204 Custom", library, pos=2) == (0, "b02", 0)


def test_resolve_slot_by_model_id(goldfinger_body, library):
    assert mutate.resolve_slot(goldfinger_body, "HD2_AmpBrit2204Custom", library) == (0, "b02", 0)


def test_resolve_slot_skips_endpoints(goldfinger_body, library):
    # b00/b13 endpoints (P35_ models) never match a library block name.
    with pytest.raises(mutate.MutateError):
        mutate.resolve_slot(goldfinger_body, "P35_InputInst1_2", library)
