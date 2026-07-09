"""Shared machinery for the golden-output contract (Phase 0 of the
.hsp-canonical redesign).

This module has two jobs:

1. Build a fully self-contained, deterministic Stadium (.hsp) `Library` —
   a synthetic chassis + a handful of blocks — so the corpus never depends
   on the user's real `~/.helixgen/library` or on gitignored `data/*.hsp`
   exports. Every corpus recipe resolves its blocks against this library.

2. Provide `run_current_pipeline`, the ONE call site that turns a spec dict
   into `.hsp` bytes using whatever the current authoring entry point is.
   When the authoring entry point moves (the eventual `recipe.apply_recipe`
   from the redesign plan), only this function's body needs to change —
   the goldens, the recipes, and the comparison logic all stay put.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from helixgen.chassis import extract_chassis_from_hsp
from helixgen.hsp import HSP_MAGIC_LEN, dumps_hsp
from helixgen.library import Block, Library

CORPUS_DIR = Path(__file__).parent / "corpus"

# Stadium XL device_id, matching real exports (see controllers.py /
# STADIUM_XL_DEVICE_IDS) so footswitch/expression source-id resolution
# exercises the real table instead of the "unknown device" warning path.
_DEVICE_ID = 2490368

_CHASSIS_PAYLOAD: dict[str, Any] = {
    "meta": {
        "name": "corpus-chassis",
        "color": "auto",
        "device_id": _DEVICE_ID,
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
                    "type": "input", "position": 0, "path": 0,
                    "slot": [{"model": "P35_InputInst1_2", "params": {}, "version": 0}],
                },
                "b13": {
                    "type": "output", "position": 13, "path": 0,
                    "slot": [{"model": "P35_OutputMatrix", "params": {}, "version": 0}],
                },
            },
            {
                "@enabled": True,
                "b00": {
                    "type": "input", "position": 0, "path": 1,
                    "slot": [{"model": "P35_InputNone", "params": {}, "version": 0}],
                },
                "b13": {
                    "type": "output", "position": 13, "path": 1,
                    "slot": [{"model": "P35_OutputMatrix", "params": {}, "version": 0}],
                },
            },
        ],
    },
}

_SRC = {"preset": "corpus", "firmware": "test", "date": "2026-07-08"}


def _corpus_blocks() -> list[Block]:
    """The fixed block set every corpus recipe resolves against.

    Drive/amp/cab/delay/reverb display names deliberately match
    `tests/fixtures/specs/goldfinger.json` so that fixture can be reused
    verbatim as a corpus recipe (see corpus/goldfinger.recipe.json).
    """
    return [
        Block(
            model_id="HD2_DrvScream808", category="drive", display_name="Scream 808",
            params={
                "Drive": {"type": "float"}, "Tone": {"type": "float"},
                "Level": {"type": "float"},
            },
            exemplar={"@model": "HD2_DrvScream808", "@type": "fx", "@enabled": True,
                      "Drive": 0.1, "Tone": 0.5, "Level": 0.6},
            first_seen=_SRC,
        ),
        Block(
            model_id="HD2_AmpBrit2204Custom", category="amp", display_name="Brit 2204 Custom",
            params={
                "Drive": {"type": "float"}, "Bass": {"type": "float"},
                "Mid": {"type": "float"}, "Treble": {"type": "float"},
                "Presence": {"type": "float"}, "Master": {"type": "float"},
                "ChVol": {"type": "float"},
            },
            exemplar={"@model": "HD2_AmpBrit2204Custom", "@type": "amp", "@enabled": True,
                      "Drive": 0.6, "Bass": 0.5, "Mid": 0.5, "Treble": 0.5,
                      "Presence": 0.5, "Master": 0.5, "ChVol": 0.5},
            first_seen=_SRC,
        ),
        Block(
            model_id="HD2_Cab4x12Greenback25", category="cab", display_name="4x12 Greenback 25",
            params={
                "Distance": {"type": "int"}, "HighCut": {"type": "float"},
                "LowCut": {"type": "float"},
            },
            exemplar={"@model": "HD2_Cab4x12Greenback25", "@type": "cab", "@enabled": True,
                      "Distance": 3, "HighCut": 8000.0, "LowCut": 80.0},
            first_seen=_SRC,
        ),
        Block(
            model_id="HD2_DlyDigital", category="delay", display_name="Digital",
            params={
                "Mix": {"type": "float"}, "Time": {"type": "float"},
                "Feedback": {"type": "float"},
            },
            exemplar={"@model": "HD2_DlyDigital", "@type": "fx", "@enabled": True,
                      "Mix": 0.3, "Time": 0.4, "Feedback": 0.4},
            first_seen=_SRC,
        ),
        Block(
            model_id="HD2_RvbPlate", category="reverb", display_name="Plate",
            params={
                "Mix": {"type": "float"}, "Decay": {"type": "float"},
                "PreDelay": {"type": "float"},
            },
            exemplar={"@model": "HD2_RvbPlate", "@type": "fx", "@enabled": True,
                      "Mix": 0.1, "Decay": 1.2, "PreDelay": 0.01},
            first_seen=_SRC,
        ),
        # IR block — carries a canonical default_irhash so recipes can omit
        # the spec `ir` field entirely (no IrMapping / registered wav needed).
        Block(
            model_id="HX2_ImpulseResponseWithPan", category="cab", display_name="With Pan",
            params={
                "HighCut": {"type": "float"}, "LowCut": {"type": "float"},
                "Mix": {"type": "float"}, "Pan": {"type": "float"},
                "Level": {"type": "float"}, "Delay": {"type": "float"},
                "IrData": {"type": "int"}, "Polarity": {"type": "bool"},
            },
            exemplar={"@model": "HX2_ImpulseResponseWithPan", "@type": "cab", "@enabled": True,
                      "HighCut": 20100.0, "LowCut": 19.9, "Mix": 1.0, "Pan": 0.5,
                      "Level": -18.0, "Delay": 0.0, "IrData": 0, "Polarity": False},
            first_seen=_SRC,
            default_irhash="ad8182e1ebe9fd95dffde5dd54b6d89c",
        ),
    ]


def build_corpus_library(root: Path) -> Library:
    """Build the deterministic, self-contained Stadium library used by
    every corpus recipe. `root` is a fresh, writable directory (a tmp_path
    in tests; a throwaway dir when re-capturing goldens).
    """
    library = Library(root=Path(root))
    library.save_chassis(extract_chassis_from_hsp(copy.deepcopy(_CHASSIS_PAYLOAD)))
    for block in _corpus_blocks():
        library.save_block(block)
    library.rebuild_index()
    return library


def run_current_pipeline(spec_dict: dict[str, Any], library: Library) -> bytes:
    """THE single repointable call site: spec dict -> `.hsp` bytes via
    whatever the current authoring entry point is.

    Today that's `parse_spec` + `compose_preset` + `dumps_hsp`. When Task 3
    of the redesign moves the authoring entry point to `recipe.apply_recipe`,
    update only this function's body — every golden, every recipe.json, and
    `test_golden_parity`'s comparison logic stay unchanged.
    """
    from helixgen.recipe import generate_from_recipe

    chassis = library.load_chassis()
    return generate_from_recipe(
        spec_dict, library, chassis=chassis, source="golden-corpus"
    )


def parsed_dict(hsp_bytes: bytes) -> dict[str, Any]:
    """Strip the 8-byte magic header and parse the JSON payload."""
    return json.loads(hsp_bytes[HSP_MAGIC_LEN:].decode("utf-8"))


def normalize(hsp_bytes: bytes) -> dict[str, Any]:
    """Parse `.hsp` bytes to a dict and drop volatile fields (the
    `meta.helixgen.generated_at` timestamp) so two runs of the same
    pipeline compare equal regardless of when each ran.
    """
    d = parsed_dict(hsp_bytes)
    helixgen_meta = d.get("meta", {}).get("helixgen", {})
    helixgen_meta.pop("generated_at", None)
    helixgen_meta.pop("version", None)  # release provenance, not tone data
    return d


def list_corpus_names() -> list[str]:
    """Every `<name>` with both a `<name>.recipe.json` and `<name>.hsp` in
    `corpus/`, sorted for stable parametrize ordering.
    """
    names = sorted(
        p.name.removesuffix(".recipe.json") for p in CORPUS_DIR.glob("*.recipe.json")
    )
    missing = [n for n in names if not (CORPUS_DIR / f"{n}.hsp").exists()]
    if missing:
        raise FileNotFoundError(
            f"corpus recipe(s) with no matching golden .hsp: {missing}"
        )
    return names


def load_recipe(name: str) -> dict[str, Any]:
    return json.loads((CORPUS_DIR / f"{name}.recipe.json").read_text())


def load_golden(name: str) -> bytes:
    return (CORPUS_DIR / f"{name}.hsp").read_bytes()
