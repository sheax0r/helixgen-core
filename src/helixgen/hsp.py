"""Read Helix Stadium .hsp exports and re-shape them into .hlx-style block dicts.

`.hsp` is the Stadium export format. The file is `rpshnosj` (8-byte ASCII magic)
followed by JSON. The interesting payload lives at `preset.flow`, a length-2
list of path dicts keyed `b00..b13`:

    b00            input endpoint (skipped)
    b01..b12       user blocks
    b13            output endpoint (skipped)

Each block has a `slot` array (cabs are dual-slot stereo, others mono) plus
`type`, `position`, `path`, `@enabled`, etc. Within a slot, `params` is a
flat dict and each value is wrapped in `{"value": ...}` (or, for controlled
params, `{"controller": {...}, "value": ...}`; or for stereo, a `{"1": {...},
"2": {...}}` pair). We unwrap to plain scalars.

Model IDs in `.hsp` use a Stadium-specific namespace that diverges from
`.hlx` for some models (e.g. `HD2_DistCompulsiveDriveMono` vs
`HD2_DistCompulsiveDrive`). We apply a small known-translation table; other
model IDs pass through unchanged. As the library accretes coverage, the
translation table grows.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


HSP_MAGIC = b"rpshnosj"
HSP_MAGIC_LEN = 8

# Block keys to skip (input/output endpoints, not user blocks).
ENDPOINT_KEYS = frozenset({"b00", "b13"})

# Stadium chassis-level models that appear in `bNN` slots but are not
# user-arrangeable effects: inputs, outputs, splits/joins, loopers, etc.
# These are routing/IO infrastructure — they belong in the chassis, not the
# block library. Any model_id starting with this prefix is filtered at
# extraction time.
CHASSIS_MODEL_PREFIX = "P35_"


# Stadium model-id → Helix model-id translations we know about.
# Add entries here as we observe new divergences during bulk ingest.
HSP_TO_HLX_MODEL_ID: dict[str, str] = {
    "HD2_DistCompulsiveDriveMono": "HD2_DistCompulsiveDrive",
    "HD2_DistScream808Mono": "HD2_DrvScream808",
    "HD2_VolPanVolMono": "HD2_VolPanVol",
}

# Reverse mapping for generation (.hlx id → Stadium id). Derived from the
# forward table; collisions in the inverse direction would be a programming
# error and surface immediately when this dict is built.
HLX_TO_HSP_MODEL_ID: dict[str, str] = {v: k for k, v in HSP_TO_HLX_MODEL_ID.items()}
assert len(HLX_TO_HSP_MODEL_ID) == len(HSP_TO_HLX_MODEL_ID), (
    "HSP→HLX translation table has duplicate values; reverse mapping is ambiguous."
)


def translate_to_hsp(model_id: str) -> str:
    """Inverse of `_translate_model_id`: library id → Stadium id (if known)."""
    return HLX_TO_HSP_MODEL_ID.get(model_id, model_id)


def is_hsp_bytes(raw: bytes) -> bool:
    return raw[:HSP_MAGIC_LEN] == HSP_MAGIC


def read_hsp(path: Path | str) -> dict[str, Any]:
    """Parse the JSON payload of a .hsp file. Raises ValueError if magic is wrong."""
    raw = Path(path).read_bytes()
    if not is_hsp_bytes(raw):
        raise ValueError(
            f"{path}: not a .hsp file (missing {HSP_MAGIC!r} magic header)"
        )
    return json.loads(raw[HSP_MAGIC_LEN:].decode("utf-8"))


def _unwrap_value(wrapped: Any) -> Any:
    """Unwrap a .hsp param value.

    Cases observed in real exports:
      {"value": x}                       — plain scalar
      {"controller": {...}, "value": x}  — controlled, take the value
      {"1": {"value": x}, "2": {...}}    — stereo, take channel 1 (mono fallback)
    Anything else falls through unchanged.
    """
    if not isinstance(wrapped, dict):
        return wrapped
    if "value" in wrapped:
        return wrapped["value"]
    if "1" in wrapped and isinstance(wrapped["1"], dict) and "value" in wrapped["1"]:
        return wrapped["1"]["value"]
    return wrapped


def _translate_model_id(model_id: str) -> str:
    return HSP_TO_HLX_MODEL_ID.get(model_id, model_id)


def _slot_to_hlx_block(
    slot: dict[str, Any], block_meta: dict[str, Any]
) -> dict[str, Any]:
    """Reshape a single .hsp slot dict into a .hlx-style raw block dict.

    `slot` is one element of a block's `slot` array. `block_meta` carries the
    surrounding block-level metadata (type, position, path) that becomes
    `@type`, `@position`, `@path` on the .hlx side.
    """
    out: dict[str, Any] = {
        "@model": _translate_model_id(slot.get("model", "")),
        "@enabled": _unwrap_value(slot.get("@enabled", True)),
    }
    if "type" in block_meta:
        out["@type"] = block_meta["type"]
    if "position" in block_meta:
        out["@position"] = block_meta["position"]
    if "path" in block_meta:
        out["@path"] = block_meta["path"]
    if "version" in slot and slot["version"] not in (None, 0):
        out["@version"] = slot["version"]
    if "irhash" in slot:
        out["irhash"] = slot["irhash"]

    for name, wrapped in (slot.get("params") or {}).items():
        out[name] = _unwrap_value(wrapped)

    return out


def extract_blocks_from_hsp(hsp_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk preset.flow paths and return a flat list of .hlx-shaped raw blocks.

    For each path in `preset.flow`, iterate b01..b12 in numeric key order and
    yield each slot as a separate raw block. Skips b00/b13 endpoints. Cabs
    arrive as dual-slot blocks in .hsp; we yield each slot independently so
    every catalogued cab becomes its own library entry.
    """
    flow = hsp_data.get("preset", {}).get("flow")
    if not isinstance(flow, list):
        return []

    blocks: list[dict[str, Any]] = []
    for path in flow:
        if not isinstance(path, dict):
            continue
        block_keys = sorted(
            k for k in path
            if isinstance(k, str)
            and k.startswith("b")
            and k not in ENDPOINT_KEYS
            and k[1:].isdigit()
        )
        for key in block_keys:
            raw_block = path[key]
            if not isinstance(raw_block, dict):
                continue
            slots = raw_block.get("slot", [])
            block_meta = {k: v for k, v in raw_block.items() if k != "slot"}
            for slot in slots:
                if not isinstance(slot, dict) or "model" not in slot:
                    continue
                if slot["model"].startswith(CHASSIS_MODEL_PREFIX):
                    continue
                blocks.append(_slot_to_hlx_block(slot, block_meta))

    return blocks


def read_hsp_blocks(path: Path | str) -> list[dict[str, Any]]:
    """Convenience: read a .hsp file and return its .hlx-shaped raw blocks."""
    return extract_blocks_from_hsp(read_hsp(path))
