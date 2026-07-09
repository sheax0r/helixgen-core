"""In-place mutation verbs for a parsed `.hsp` body dict.

This is the heart of the `.hsp`-canonical redesign
(`docs/superpowers/plans/2026-07-08-hsp-canonical-redesign.md`): instead of
compiling a spec into a fresh `.hsp` body, we address a block already placed
in `preset.flow[*].bNN` and mutate its `slot` dict directly, in place.

Block addressing mirrors `patch.resolve_block`'s disambiguation semantics
(display name, optionally narrowed by `path`/`lane`/`pos`) but resolves
against the `.hsp` body's `preset.flow` structure instead of a spec dict, and
returns a `(flow_index, bnn_key, slot_index)` coordinate rather than a
`(path_index, block_index)` one. `slot_index` is always `0` today — the only
addressable slot in a `bNN` entry is `slot[0]`; a dual-cab's second physical
slot (`slot[1]`) is opaque verbatim state (see `decompile._block_entry`'s
`raw.slots`) and is not independently addressable here.

More verbs (`set_param`, `set_enabled`, `add_block`, controller wiring, ...)
land in this same module in later phases of the redesign; keep additions
here rather than spawning new modules per verb.
"""
from __future__ import annotations

from typing import Any

from helixgen.hsp import CHASSIS_MODEL_PREFIX, ENDPOINT_KEYS, _translate_model_id
from helixgen.library import Block, Library

__all__ = ["MutateError", "resolve_slot"]


class MutateError(ValueError):
    """A `.hsp` body-level mutation could not be applied (bad address, etc.)."""


def _bnn_keys(path_dict: dict[str, Any]) -> list[str]:
    """Sorted user-block keys (`b01`..`b12`) in a flow path dict, endpoints excluded."""
    return sorted(
        k for k in path_dict
        if isinstance(k, str) and k.startswith("b") and k not in ENDPOINT_KEYS and k[1:].isdigit()
    )


def _lane_pos(key: str) -> tuple[int, int]:
    """Decode a `bNN` key into (lane, pos): lane 1 starts at b14, lane 0 is b01-b13."""
    num = int(key[1:])
    lane = 1 if num >= 14 else 0
    return lane, num - 14 * lane


def _iter_slots(
    body: dict[str, Any], library: Library
) -> list[tuple[int, str, int, Block, int, int]]:
    """Walk `preset.flow[*]` and return every resolvable user block's primary
    (index-0) slot as `(flow_index, bnn_key, slot_index, block, lane, pos)`.

    Skips `b00`/`b13` endpoints, split/join/input/output structural slots,
    `P35_` chassis-routing models, and any slot whose model the library
    cannot resolve (mirrors `decompile._name_index`'s skip-on-KeyError).
    """
    flow = (body.get("preset") or {}).get("flow") or []
    out: list[tuple[int, str, int, Block, int, int]] = []
    for fi, path_dict in enumerate(flow):
        if not isinstance(path_dict, dict):
            continue
        for key in _bnn_keys(path_dict):
            bnn = path_dict.get(key)
            if not isinstance(bnn, dict) or bnn.get("type") in ("split", "join", "input", "output"):
                continue
            slots = bnn.get("slot")
            if not slots or not isinstance(slots, list):
                continue
            slot0 = slots[0]
            if not isinstance(slot0, dict) or "model" not in slot0:
                continue
            model = slot0["model"]
            if isinstance(model, str) and model.startswith(CHASSIS_MODEL_PREFIX):
                continue
            try:
                block = library.load_block(_translate_model_id(model))
            except KeyError:
                continue
            lane, pos = _lane_pos(key)
            out.append((fi, key, 0, block, lane, pos))
    return out


def resolve_slot(
    body: dict[str, Any],
    name: str,
    library: Library,
    *,
    path: int | None = None,
    lane: int | None = None,
    pos: int | None = None,
) -> tuple[int, str, int]:
    """Resolve a display name (or model_id) to a `(flow_index, bnn_key, slot_index)`
    coordinate in `body`, mirroring `patch.resolve_block`'s disambiguation.

    `name` matches a placed block's `display_name` or `model_id`. `path`/
    `lane`/`pos` narrow the match when the name is ambiguous. Raises
    `MutateError` if no block matches (message lists every placed block) or
    if more than one does (message says to disambiguate).
    """
    placed = _iter_slots(body, library)
    name_matches = [t for t in placed if name in (t[3].display_name, t[3].model_id)]

    matches = name_matches
    if path is not None:
        matches = [t for t in matches if t[0] == path]
    if lane is not None:
        matches = [t for t in matches if t[4] == lane]
    if pos is not None:
        matches = [t for t in matches if t[5] == pos]

    if not matches:
        placed_names = [t[3].display_name for t in placed]
        raise MutateError(
            f"Block {name!r} is not in the preset (with the given path/lane/pos). "
            f"Placed blocks: {placed_names}."
        )
    if len(matches) > 1:
        raise MutateError(
            f"Block {name!r} matches {len(matches)} placements; "
            f"disambiguate with path=/lane=/pos=."
        )
    fi, key, si, _block, _lane, _pos = matches[0]
    return (fi, key, si)
