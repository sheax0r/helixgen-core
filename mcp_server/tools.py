"""Pure-Python handlers for MCP tools. No MCP types; FastMCP wraps these at
registration time. Importable + directly testable.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from helixgen.decompile import decompile_body
from helixgen.generate import generate_preset
from helixgen.hsp import HSP_MAGIC
from helixgen.ir import IrMapping, compute_stadium_irhash
from helixgen.library import Library
from helixgen import patch as _patch


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Required `model` parameter on every tool. Allow-list; everything else errors.
# Soft gate (agents can misreport); the setup skill is the real gate.
_SUPPORTED_MODELS = frozenset({"stadium", "stadium_xl"})

# 2 MB cap on incoming WAV bytes for compute_irhash. Real IRs are ≤200 KB
# typically; the cap is well above realistic usage and well below
# MCP/JSON-RPC per-message budgets.
_WAV_BYTES_LIMIT = 2 * 1024 * 1024

# Upload-to-device reminder returned alongside every computed irhash.
_UPLOAD_REMINDER = (
    "This hash will only resolve on the device if the same WAV is loaded "
    "onto your Helix Stadium via the Librarian's Cab IRs → Import. "
    "Drag it in if you haven't already."
)


def _validate_model(model: str) -> None:
    """Reject any model outside the supported allow-list with an actionable error.

    FastMCP translates ValueError into an MCP `isError` text content block.
    The message tells the agent what to do (ask the user) — necessary
    because the param is a soft gate: a confused agent can still pass an
    allowed value for the wrong device.
    """
    if model not in _SUPPORTED_MODELS:
        raise ValueError(
            f"unsupported model: {model!r}. helixgen currently supports only "
            f"{sorted(_SUPPORTED_MODELS)}. Ask the user to confirm their device."
        )


def list_blocks_handler(library: Library, model: str, category: str | None = None) -> str:
    """Return library blocks grouped by category, matching `helixgen list-blocks`.

    Format mirrors the CLI: one `<category>:` header per category, followed
    by indented `  <display_name>  [<model_id>]` lines sorted by name.
    Unknown category returns an empty string (not an error) so callers can
    distinguish "no such category" from "library empty."
    """
    _validate_model(model)
    blocks = library.list_blocks(category=category)
    if not blocks:
        return ""

    by_category: dict[str, list] = {}
    for b in blocks:
        by_category.setdefault(b.category, []).append(b)

    lines: list[str] = []
    for cat in sorted(by_category):
        lines.append(f"{cat}:")
        for b in sorted(by_category[cat], key=lambda x: x.display_name):
            lines.append(f"  {b.display_name}  [{b.model_id}]")
    return "\n".join(lines)


def show_block_handler(library: Library, model: str, name_or_id: str) -> str:
    """Return a block's schema (params, defaults, ranges) as text.

    Format mirrors `helixgen show-block`: header, category, aliases (if any),
    then one indented line per param with type, default, and observed-range
    or values where present. KeyError / LookupError propagate to the caller
    (FastMCP translates these to MCP errors at the registration boundary).
    """
    _validate_model(model)
    block = library.find_block(name_or_id)

    lines: list[str] = []
    lines.append(f"{block.display_name}  [{block.model_id}]")
    lines.append(f"category: {block.category}")
    if block.aliases:
        lines.append(f"aliases: {', '.join(block.aliases)}")
    lines.append("params:")
    for name, schema in block.params.items():
        meta_bits = [schema["type"], f"default={schema.get('default')!r}"]
        if "observed_range" in schema:
            meta_bits.append(f"observed={schema['observed_range']}")
        if "values" in schema:
            meta_bits.append(f"values={schema['values']}")
        lines.append(f"  {name}  ({', '.join(meta_bits)})")
    return "\n".join(lines)


def _safe_filename(name: str) -> str:
    """Convert an arbitrary preset name to a safe basename for the .hsp blob.

    Strips path separators, collapses unsafe characters to underscores,
    and falls back to 'preset' when the result would be empty.
    """
    cleaned = _FILENAME_SAFE.sub("_", name).strip("._-")
    return f"{cleaned or 'preset'}.hsp"


def generate_preset_handler(library: Library, model: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Generate a Helix Stadium .hsp from an inline spec dict.

    Returns a dict suitable for an MCP EmbeddedResource:
      - mimeType: application/octet-stream
      - name:     safe basename ending in .hsp
      - blob:     base64-encoded .hsp bytes (magic header + JSON body)

    Underlying SpecError / ParamValidationError / GenerateError propagate;
    the MCP server boundary translates them to protocol errors.
    """
    _validate_model(model)
    with tempfile.TemporaryDirectory(prefix="helixgen-mcp-") as tmp_dir:
        tmp = Path(tmp_dir)
        spec_path = tmp / "spec.json"
        out_path = tmp / "preset.hsp"
        spec_path.write_text(json.dumps(spec))
        generate_preset(spec_path, out_path, library)
        raw = out_path.read_bytes()

    return {
        "mimeType": "application/octet-stream",
        "name":     _safe_filename(spec.get("name", "preset")),
        "blob":     base64.b64encode(raw).decode("ascii"),
    }


def list_irs_handler(model: str, irs_dir: Path | None = None) -> str:
    """Return registered user IRs as text, matching `helixgen list-irs`.

    One line per IR: `<hash>  <wav-path>`, sorted by hash. Empty string when
    no IRs are registered or the mapping file is absent — callers branch on
    truthiness to decide whether to use IRs vs. stock cabs.
    """
    _validate_model(model)
    mapping = IrMapping.load(irs_dir)
    if not mapping.entries:
        return ""
    return "\n".join(
        f"{h}  {mapping.entries[h]}" for h in sorted(mapping.entries)
    )


def compute_irhash_handler(model: str, wav_b64: str) -> dict[str, str]:
    """Compute Stadium's IR hash for a base64-encoded WAV file.

    Stateless. Decodes the bytes, validates the size and WAV magic, writes
    to a NamedTemporaryFile, and calls `compute_stadium_irhash`. Returns the
    32-char hex hash plus an upload-to-device reminder.

    Validation (defense in depth — libsndfile has had CVEs):
      1. Model in the supported allow-list
      2. Decoded size ≤ 2 MB (config: `_WAV_BYTES_LIMIT`)
      3. First 4 bytes = `RIFF`, bytes 8–12 = `WAVE` (basic magic check
         before libsndfile sees the input)

    Returns: `{"irhash": "<hex>", "reminder": "<upload-to-device message>"}`.
    Raises ValueError on any validation failure; FastMCP translates to an
    `isError` text content block.
    """
    _validate_model(model)
    try:
        data = base64.b64decode(wav_b64, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        raise ValueError(f"wav_b64 is not valid base64: {e}") from e
    if len(data) > _WAV_BYTES_LIMIT:
        raise ValueError(
            f"WAV is {len(data)} bytes; max {_WAV_BYTES_LIMIT} (2 MB). "
            "Real IRs are typically under 200 KB."
        )
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(
            "WAV bytes don't look valid (missing RIFF/WAVE magic). "
            "Make sure the user dragged a .wav file, not another format."
        )
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tf.write(data)
        tmp_path = tf.name
    try:
        irhash = compute_stadium_irhash(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return {"irhash": irhash, "reminder": _UPLOAD_REMINDER}


def register_ir_handler(
    model: str,
    wav_path: str,
    *,
    force: bool = False,
    irs_dir: Path | None = None,
) -> dict[str, str]:
    """Compute the Stadium hash for `wav_path` and persist it to `mapping.json`.

    Returns: `{"hash": "<hex>", "path": "<canonical>", "reminder": "<upload-to-device message>"}`.
    Raises ValueError on bad model / missing file / mapping conflict.
    """
    _validate_model(model)
    wav = Path(wav_path).expanduser().resolve()
    if not wav.is_file():
        raise ValueError(f"wav file not found: {wav_path}")
    irhash = compute_stadium_irhash(wav)
    mapping = IrMapping.load(irs_dir)
    mapping.register(irhash, wav, force=force)
    mapping.save()
    canonical = mapping.entries[irhash]
    return {"hash": irhash, "path": canonical, "reminder": _UPLOAD_REMINDER}


def register_irs_handler(
    model: str,
    ir_directory: str,
    *,
    force: bool = False,
    irs_dir: Path | None = None,
) -> dict[str, list]:
    """Walk a directory, hash every WAV, batch-register all to `mapping.json`.

    Returns a summary dict::

        {
          "registered":          ["new1.wav", "new2.wav", ...],
          "already_registered":  ["was-here.wav", ...],
          "conflicts":           ["dup.wav", ...],
          "failed":              [{"basename": "bad.wav", "reason": "..."}, ...],
        }

    `conflicts` happens when a hash already maps to a different canonical path
    and `force=False`. With `force=True` those go into `registered`.

    `failed` collects per-file errors (non-48 kHz, libsndfile error) without
    aborting the bulk run — the partial successful subset is still persisted.
    """
    _validate_model(model)
    root = Path(ir_directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {ir_directory}")

    from helixgen.ir import IrMappingError

    mapping = IrMapping.load(irs_dir)
    registered: list[str] = []
    already: list[str] = []
    conflicts: list[str] = []
    failed: list[dict[str, str]] = []

    for wav in sorted(root.rglob("*")):
        if not wav.is_file() or wav.suffix.lower() != ".wav":
            continue
        try:
            h = compute_stadium_irhash(wav)
        except (NotImplementedError, RuntimeError, FileNotFoundError) as e:
            failed.append({"basename": wav.name, "reason": str(e)})
            continue
        existing = mapping.entries.get(h)
        canonical = str(wav.resolve())
        try:
            mapping.register(h, wav, force=force)
        except IrMappingError:
            conflicts.append(wav.name)
            continue
        if existing == canonical:
            already.append(wav.name)
        else:
            registered.append(wav.name)

    mapping.save()
    return {
        "registered": registered,
        "already_registered": already,
        "conflicts": conflicts,
        "failed": failed,
    }


def discover_irs_handler(model: str, ir_directory: str) -> list[dict[str, str]]:
    """Walk a server-side filesystem path and return (hash, path, basename) for each WAV.

    Returns: list of `{"hash", "path", "basename"}` dicts, sorted by basename.
    Files that fail to hash (non-48 kHz, libsndfile errors) are skipped with
    no error — callers get the successful subset. Returns an empty list if
    the directory has no WAVs.
    """
    _validate_model(model)
    root = Path(ir_directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {ir_directory}")
    out: list[dict[str, str]] = []
    for wav in sorted(root.rglob("*")):
        if not wav.is_file() or wav.suffix.lower() != ".wav":
            continue
        try:
            h = compute_stadium_irhash(wav)
        except (NotImplementedError, RuntimeError, FileNotFoundError):
            continue
        out.append({"hash": h, "path": str(wav), "basename": wav.name})
    return out


def decompile_preset_handler(library: Library, model: str, hsp_b64: str) -> dict:
    """Decompile a base64-encoded .hsp blob into a spec dict."""
    _validate_model(model)
    raw = base64.b64decode(hsp_b64)
    if raw[:len(HSP_MAGIC)] != HSP_MAGIC:
        raise ValueError("payload is not a .hsp blob (missing magic header)")
    body = json.loads(raw[len(HSP_MAGIC):].decode("utf-8"))
    return decompile_body(body, library)


_PATCH_OPS = {
    "set_param": lambda lib, spec, o: (
        _patch.set_param(spec, o["block"], o["param"], o["value"],
                         path=o.get("path"), index=o.get("index"),
                         lane=o.get("lane"), pos=o.get("pos")), []),
    "set_enabled": lambda lib, spec, o: (
        _patch.set_enabled(spec, o["block"], o["enabled"],
                           path=o.get("path"), index=o.get("index"),
                           lane=o.get("lane"), pos=o.get("pos"),
                           snapshot=o.get("snapshot")), []),
    "add_block": lambda lib, spec, o: (
        _patch.add_block(spec, o["block"], path=o.get("path", 0),
                         after=o.get("after"), params=o.get("params"),
                         lane=o.get("lane"), pos=o.get("pos")), []),
    "remove_block": lambda lib, spec, o: (
        _patch.remove_block(spec, o["block"],
                            path=o.get("path"), index=o.get("index"),
                            lane=o.get("lane"), pos=o.get("pos")), []),
    "swap_model": lambda lib, spec, o: _patch.swap_model(
        spec, o["old"], o["new"], lib, path=o.get("path"), index=o.get("index"),
        lane=o.get("lane"), pos=o.get("pos")),
}


def patch_preset_handler(library: Library, model: str, spec: dict, operations: list) -> dict:
    """Apply a sequence of patch ops to a spec dict. Returns {spec, warnings}."""
    _validate_model(model)
    warnings: list[str] = []
    current = spec
    for o in operations:
        op = o.get("op")
        if op not in _PATCH_OPS:
            raise ValueError(f"unknown patch op {op!r}; valid: {sorted(_PATCH_OPS)}")
        current, warns = _PATCH_OPS[op](library, current, o)
        warnings.extend(warns)
    return {"spec": current, "warnings": warnings}
