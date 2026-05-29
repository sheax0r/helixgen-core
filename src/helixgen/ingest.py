"""Ingest module: parse exported .hlx and single-block JSON, extract schemas."""
from __future__ import annotations

import re
from enum import Enum
from typing import Any

from helixgen.ir import IR_MODEL_PREFIX


# ---------------------------------------------------------------------------
# Wire-format constants for the real Helix .hlx export shape.
# Confirmed against tests/fixtures/presets/possum.hlx (a real device export).
# User blocks live as direct children of data.tone.dsp{0,1} keyed
# `block0`, `block1`, ... and cabs as siblings keyed `cab0`, `cab1`, ...
# Cabs are linked from amp blocks via the `@cab` metadata key.
# Other dsp children are routing/endpoint infrastructure that we never
# catalog as user blocks: inputA, inputB, outputA, outputB, split, join.
# ---------------------------------------------------------------------------
RAW_BLOCK_MODEL_KEY = "@model"          # block JSON: model identifier
RAW_BLOCK_CATEGORY_KEY = "@category"    # block JSON: optional category override
RAW_BLOCK_NAME_KEY = "@name"            # block JSON: optional human-readable name
RAW_BLOCK_CAB_LINK_KEY = "@cab"         # amp block: name of paired cab sibling
RAW_BLOCK_SYSTEM_KEY_PREFIX = "@"       # any key starting with this is metadata, not a param

# Slot-level metadata fields (not user-tunable params) that share the raw block dict
# with the actual params. They must be filtered from extract_schema.
RAW_BLOCK_NON_PARAM_KEYS = frozenset({"irhash"})

PRESET_TONE_KEY = ("data", "tone")      # full preset: path to dsp0/dsp1 root
PRESET_DSP_KEYS = ("dsp0", "dsp1")
DSP_BLOCK_KEY_PREFIX = "block"          # user block sibling keys: block0, block1, ...
DSP_CAB_KEY_PREFIX = "cab"              # cab sibling keys: cab0, cab1, ...
DSP_INFRASTRUCTURE_KEYS = frozenset(    # never catalog these as user blocks
    {"inputA", "inputB", "outputA", "outputB", "split", "join"}
)


# ---------------------------------------------------------------------------
# Category inference from model_id prefix.
# Order matters: most specific first. Add new prefixes here as discovered.
# Both HD2_ (older Helix) and HX2_ (Stadium) are observed in the wild.
# ---------------------------------------------------------------------------
_CATEGORY_PREFIXES: list[tuple[str, str]] = [
    ("HD2_Amp", "amp"),
    ("HD2_Preamp", "amp"),
    ("HD2_Cab", "cab"),
    ("HD2_Drv", "drive"),
    ("HD2_Dist", "drive"),
    ("HD2_Rvb", "reverb"),
    ("HD2_Reverb", "reverb"),
    ("HD2_Dly", "delay"),
    ("HD2_Delay", "delay"),
    ("HD2_RetroReel", "delay"),
    ("HD2_EQ", "eq"),
    ("HD2_CaliQ", "eq"),
    ("HD2_Dynamics", "dynamics"),
    ("HD2_Dyn", "dynamics"),
    ("HD2_Mod", "modulation"),
    ("HD2_Chorus", "modulation"),
    ("HD2_Tremolo", "modulation"),
    ("HD2_Phaser", "modulation"),
    ("HD2_Flanger", "modulation"),
    ("HD2_Vibrato", "modulation"),
    ("HD2_Rotary", "modulation"),
    ("HD2_RingMod", "modulation"),
    ("HD2_Pitch", "pitch"),
    ("HD2_Wah", "filter"),
    ("HD2_VolPan", "volume"),
    ("HD2_Send", "send"),
    ("HD2_Return", "send"),
    ("HX2_Amp", "amp"),
    ("HX2_Cab", "cab"),
    ("HX2_ImpulseResponse", "cab"),
    ("HX2_Drv", "drive"),
    ("HX2_Dist", "drive"),
    ("HX2_Rvb", "reverb"),
    ("HX2_Reverb", "reverb"),
    ("HX2_Dly", "delay"),
    ("HX2_Delay", "delay"),
    ("HX2_EQ", "eq"),
    ("HX2_Gate", "dynamics"),
    ("HX2_Comp", "dynamics"),
    ("HX2_Dyn", "dynamics"),
    ("HX2_Mod", "modulation"),
    ("HX2_Pitch", "pitch"),
    ("HX2_Wah", "filter"),
    ("HX2_Filter", "filter"),
    ("Agoura_Amp", "amp"),
    ("VIC_Amp", "amp"),
    ("VIC_Reverb", "reverb"),
    ("VIC_DynPlate", "reverb"),   # plate reverbs; must precede any VIC_Dyn rule
    ("VIC_Delay", "delay"),
    ("VIC_Pitch", "pitch"),
    ("L6SPB_Poly", "pitch"),
    ("L6SPB_AcousGtrSim", "filter"),
    # Line 6 legacy stompbox modelers — DL4 (delay), DM4 (distortion), FM4
    # (filter/synth), MM4 (modulation). Exceptions match before the family
    # default: DL4AutoVol is a volume swell, DM4BassOctaver is a pitch effect.
    ("HD2_DL4AutoVol", "volume"),
    ("HD2_DL4", "delay"),
    ("HD2_DM4BassOctaver", "pitch"),
    ("HD2_DM4", "drive"),
    ("HD2_FM4", "filter"),
    ("HD2_MM4", "modulation"),
    # Stadium repackages the DM4 compressor models under HX2_.
    ("HX2_DM4", "dynamics"),
    # Single-word legacy effects (no namespace prefix at all).
    ("TapeEater", "drive"),
]


def infer_category(model_id: str) -> str:
    """Return the category for a model_id, or 'uncategorized' if unknown."""
    for prefix, category in _CATEGORY_PREFIXES:
        if model_id.startswith(prefix):
            return category
    return "uncategorized"


# Strip a known category prefix; insert a space before any uppercase letter
# that follows a lowercase letter or digit; insert a space before digits that
# follow a lowercase letter (except 'x' for unit specs like "4x12").
# Finally collapse whitespace.
_HUMANIZE_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<![x])(?<=[a-z])(?=[0-9])")


def humanize_model_id(model_id: str) -> str:
    """Turn a model_id like 'HD2_AmpBrit2204Custom' into 'Brit 2204 Custom'."""
    body = model_id
    for prefix, _ in _CATEGORY_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    else:
        # No known prefix: also strip any leading "HD2_"/"HX2_"/"P35_" if present
        for raw_prefix in ("HD2_", "HX2_", "P35_"):
            if body.startswith(raw_prefix):
                body = body[len(raw_prefix):]
                break
    spaced = _HUMANIZE_SPLIT_RE.sub(" ", body)
    return " ".join(spaced.split())


class Shape(Enum):
    PRESET = "preset"
    SINGLE_BLOCK = "single_block"
    UNKNOWN = "unknown"


def detect_shape(data: Any) -> Shape:
    """Detect whether a parsed JSON value is a full preset, a single block, or neither."""
    if not isinstance(data, dict):
        return Shape.UNKNOWN

    if (
        "version" in data
        and "schema" in data
        and isinstance(data.get("data"), dict)
        and isinstance(data["data"].get("tone"), dict)
        and any(dsp in data["data"]["tone"] for dsp in PRESET_DSP_KEYS)
    ):
        return Shape.PRESET

    if RAW_BLOCK_MODEL_KEY in data:
        return Shape.SINGLE_BLOCK

    return Shape.UNKNOWN


def _value_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def extract_schema(raw_block: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract a per-parameter schema from a raw block JSON."""
    schema: dict[str, dict[str, Any]] = {}
    for key, value in raw_block.items():
        if isinstance(key, str) and key.startswith(RAW_BLOCK_SYSTEM_KEY_PREFIX):
            continue
        if key in RAW_BLOCK_NON_PARAM_KEYS:
            continue
        type_name = _value_type_name(value)
        entry: dict[str, Any] = {"type": type_name, "default": value}
        if type_name in ("int", "float"):
            entry["observed_range"] = [value, value]
        schema[key] = entry
    return schema


def _is_user_block_key(key: str) -> bool:
    """True if `key` is a user-block slot (`block0`, `block1`, ...)."""
    return (
        isinstance(key, str)
        and key.startswith(DSP_BLOCK_KEY_PREFIX)
        and key[len(DSP_BLOCK_KEY_PREFIX):].isdigit()
    )


def _is_cab_key(key: str) -> bool:
    """True if `key` is a cab slot (`cab0`, `cab1`, ...)."""
    return (
        isinstance(key, str)
        and key.startswith(DSP_CAB_KEY_PREFIX)
        and key[len(DSP_CAB_KEY_PREFIX):].isdigit()
    )


def _slot_index(key: str, prefix: str) -> int:
    return int(key[len(prefix):])


def extract_blocks_from_preset(preset: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk dsp0 + dsp1 user-block + cab slots and return raw block dicts.

    Order: for each dsp in PRESET_DSP_KEYS, return user blocks in slot-index
    order, then cabs in slot-index order. Infrastructure (inputA/outputA/etc.)
    is skipped — those are catalogued via the chassis, not the library.
    """
    tone = preset.get("data", {}).get("tone", {})
    blocks: list[dict[str, Any]] = []
    for dsp_key in PRESET_DSP_KEYS:
        dsp = tone.get(dsp_key)
        if not isinstance(dsp, dict):
            continue
        block_keys = sorted(
            (k for k in dsp.keys() if _is_user_block_key(k)),
            key=lambda k: _slot_index(k, DSP_BLOCK_KEY_PREFIX),
        )
        cab_keys = sorted(
            (k for k in dsp.keys() if _is_cab_key(k)),
            key=lambda k: _slot_index(k, DSP_CAB_KEY_PREFIX),
        )
        for k in block_keys:
            blocks.append(dsp[k])
        for k in cab_keys:
            blocks.append(dsp[k])
    return blocks


def extract_block_from_single(raw: dict[str, Any]) -> dict[str, Any]:
    """A single-block JSON file is already a raw block; return it."""
    return raw


from helixgen.library import Block


def block_from_raw(raw: dict[str, Any], source_info: dict[str, str]) -> Block:
    """Build a Block dataclass from a single raw block dict + source provenance."""
    model_id = raw[RAW_BLOCK_MODEL_KEY]
    category = raw.get(RAW_BLOCK_CATEGORY_KEY) or infer_category(model_id)
    display_name = raw.get(RAW_BLOCK_NAME_KEY) or humanize_model_id(model_id)
    params = extract_schema(raw)
    default_irhash = raw.get("irhash") if str(model_id).startswith(IR_MODEL_PREFIX) else None
    return Block(
        model_id=model_id,
        category=category,
        display_name=display_name,
        aliases=[],
        params=params,
        exemplar=raw,
        first_seen=dict(source_info),
        default_irhash=default_irhash,
    )


import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

from helixgen.library import IngestStatus, Library


@dataclass
class IngestSummary:
    new: int = 0
    matched: int = 0
    conflicted: int = 0
    skipped: int = 0
    chassis_extracted: bool = False
    skipped_files: list[str] = field(default_factory=list)

    def add(self, other: "IngestSummary") -> None:
        self.new += other.new
        self.matched += other.matched
        self.conflicted += other.conflicted
        self.skipped += other.skipped
        self.chassis_extracted = self.chassis_extracted or other.chassis_extracted
        self.skipped_files.extend(other.skipped_files)


def _today() -> str:
    return datetime.date.today().isoformat()


def _firmware(preset: dict[str, Any]) -> str:
    """Best-effort firmware/version stamp for provenance."""
    data = preset.get("data", {})
    version = data.get("device_version")
    if version is not None:
        return str(version)
    device = data.get("device")
    if isinstance(device, dict):
        return str(device.get("fw", "unknown"))
    return "unknown"


def ingest_file(path: Path, library: Library) -> IngestSummary:
    """Ingest a single file: detect format (.hsp / .hlx / single block JSON),
    extract blocks, write to library. .hsp ingest is block-only (no chassis).
    """
    summary = IngestSummary()

    try:
        raw_bytes = Path(path).read_bytes()
    except OSError:
        summary.skipped += 1
        summary.skipped_files.append(str(path))
        return summary

    # .hsp Stadium export: 8-byte ASCII magic header, then JSON.
    from helixgen.hsp import is_hsp_bytes, read_hsp, extract_blocks_from_hsp
    if is_hsp_bytes(raw_bytes):
        try:
            hsp_data = read_hsp(path)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            summary.skipped += 1
            summary.skipped_files.append(str(path))
            return summary
        if not library.has_chassis():
            from helixgen.chassis import extract_chassis_from_hsp
            library.save_chassis(extract_chassis_from_hsp(hsp_data))
            summary.chassis_extracted = True
        raw_blocks = extract_blocks_from_hsp(hsp_data)
        firmware = str(hsp_data.get("meta", {}).get("device_version", "unknown"))
        return _ingest_blocks(path, raw_blocks, firmware, library, summary)

    # .hlx / .json: plain JSON.
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        summary.skipped += 1
        summary.skipped_files.append(str(path))
        return summary

    shape = detect_shape(data)
    if shape == Shape.UNKNOWN:
        summary.skipped += 1
        summary.skipped_files.append(str(path))
        return summary

    if shape == Shape.PRESET:
        raw_blocks = extract_blocks_from_preset(data)
        firmware = _firmware(data)
        if not library.has_chassis():
            from helixgen.chassis import extract_chassis
            library.save_chassis(extract_chassis(data))
            summary.chassis_extracted = True
    else:
        raw_blocks = [extract_block_from_single(data)]
        firmware = "unknown"

    return _ingest_blocks(path, raw_blocks, firmware, library, summary)


def _ingest_blocks(
    path: Path,
    raw_blocks: list[dict[str, Any]],
    firmware: str,
    library: Library,
    summary: IngestSummary,
) -> IngestSummary:
    """Catalog a list of raw block dicts into the library and tally outcomes."""
    source_info = {
        "preset": str(path),
        "firmware": firmware,
        "date": _today(),
    }
    for raw in raw_blocks:
        if not isinstance(raw, dict) or RAW_BLOCK_MODEL_KEY not in raw:
            continue
        block = block_from_raw(raw, source_info)
        status = library.save_block_with_dedup(block)
        if status == IngestStatus.NEW:
            summary.new += 1
        elif status == IngestStatus.MATCH:
            summary.matched += 1
        elif status == IngestStatus.CONFLICT:
            summary.conflicted += 1
    return summary


INGEST_EXTENSIONS = {".hlx", ".hsp", ".json"}


def ingest_path(path: Path, library: Library) -> IngestSummary:
    """Ingest a file or recursively all .hlx/.json files in a directory."""
    path = Path(path)
    summary = IngestSummary()

    if path.is_file():
        summary.add(ingest_file(path, library))
    elif path.is_dir():
        for entry in sorted(path.rglob("*")):
            if entry.is_file() and entry.suffix.lower() in INGEST_EXTENSIONS:
                summary.add(ingest_file(entry, library))
    else:
        raise FileNotFoundError(f"Path does not exist: {path}")

    library.rebuild_index()
    return summary
