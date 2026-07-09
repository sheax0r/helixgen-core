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

from helixgen import mutate
from helixgen.hsp import HSP_MAGIC, dumps_hsp
from helixgen.ir import IrMapping, compute_stadium_irhash
from helixgen.library import Library
from helixgen.recipe import generate_from_recipe
from helixgen.spec import parse_spec
from helixgen.view import view as view_projection


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


def generate_preset_handler(
    library: Library, model: str, recipe: dict[str, Any], *, irs_dir: Path | None = None
) -> dict[str, Any]:
    """Generate a Helix Stadium .hsp preset from an inline recipe dict.

    Builds directly against the library's Stadium chassis via
    `helixgen.recipe.generate_from_recipe` -- no temp files, no sidecar spec
    is written (the `.hsp` itself is the sole source of truth post-redesign).

    Returns a dict suitable for an MCP EmbeddedResource:
      - mimeType: application/octet-stream
      - name:     safe basename ending in .hsp
      - hsp_b64:  base64-encoded .hsp bytes (magic header + compact JSON)

    Underlying SpecError / ParamValidationError / GenerateError propagate;
    the MCP server boundary translates them to protocol errors.
    """
    _validate_model(model)
    # Parse+validate the recipe before touching the chassis, so a malformed
    # recipe reports its own error rather than being masked by a
    # missing-chassis error (mirrors `helixgen generate`'s error ordering).
    spec = parse_spec(recipe, source="mcp:generate_preset")
    irs = IrMapping.load(irs_dir)
    chassis = library.load_chassis()
    raw = generate_from_recipe(
        spec, library, irs=irs, chassis=chassis, source="mcp:generate_preset"
    )

    return {
        "mimeType": "application/octet-stream",
        "name":     _safe_filename(recipe.get("name", "preset")),
        "hsp_b64":  base64.b64encode(raw).decode("ascii"),
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


def controller_mapping_handler(model: str) -> list[dict[str, Any]]:
    """Return the device's canonical controller mapping (identifier ↔ English).

    Mirrors `helixgen controllers --json`: a JSON-serialisable list of records,
    one per assignable controller (FS1–FS5, FS7–FS11, EXP1, EXP2, EXP1Toe),
    each carrying the identifier, source id (hex + int), kind, grid position,
    canonical name, position phrase, rendered English, and aliases. Feeds the
    skill's English rendering and the English→identifier translation sub-agent.
    Reserved switches (FS6 = MODE, FS12 = TAP/Tuner) are intentionally excluded.
    """
    _validate_model(model)
    from helixgen import controllers
    # helixgen keys its tables by "stadium_xl"; the standard Stadium shares the
    # same FS grid, so resolve both supported models against that table.
    device = "stadium_xl" if model in _SUPPORTED_MODELS else model
    return controllers.controller_mapping(device)


def _decode_hsp_b64(hsp_b64: str) -> dict[str, Any]:
    """Decode a base64 `.hsp` blob into its parsed JSON body dict.

    Raises ValueError if the decoded bytes don't start with the `.hsp` magic
    header.
    """
    raw = base64.b64decode(hsp_b64)
    if raw[:len(HSP_MAGIC)] != HSP_MAGIC:
        raise ValueError("payload is not a .hsp blob (missing magic header)")
    return json.loads(raw[len(HSP_MAGIC):].decode("utf-8"))


def view_preset_handler(
    library: Library, model: str, hsp_b64: str, *, irs_dir: Path | None = None
) -> dict[str, Any]:
    """Decode a base64-encoded .hsp blob into its read-only projection dict.

    Mirrors `helixgen view`: unwraps the magic-prefixed JSON body, then
    projects it via `helixgen.view.view` -- never reads/writes disk, no
    sidecar spec is produced. IRs are resolved against the mapping at
    `irs_dir` (or the default `$HELIXGEN_IRS`/`~/.helixgen/irs/` location)
    so a registered IR block's `irhash` can be reported by wav basename.
    """
    _validate_model(model)
    body = _decode_hsp_b64(hsp_b64)
    irs = IrMapping.load(irs_dir)
    return view_projection(body, library, irs=irs)


# Each entry takes (body, library, op_dict) and mutates `body` in place,
# returning a list[str] of warnings (empty for ops that never warn).
def _op_set_param(body: dict, library: Library, o: dict) -> list[str]:
    mutate.set_param(
        body, o["block"], o["param"], o["value"], library,
        path=o.get("path"), lane=o.get("lane"), pos=o.get("pos"),
    )
    return []


def _op_set_enabled(body: dict, library: Library, o: dict) -> list[str]:
    mutate.set_enabled(
        body, o["block"], o["enabled"], library,
        snapshot=o.get("snapshot"),
        path=o.get("path"), lane=o.get("lane"), pos=o.get("pos"),
    )
    return []


def _op_add_block(body: dict, library: Library, o: dict) -> list[str]:
    mutate.add_block(
        body, o["block"], library,
        path=o.get("path", 0), after=o.get("after"), params=o.get("params"),
    )
    return []


def _op_remove_block(body: dict, library: Library, o: dict) -> list[str]:
    mutate.remove_block(
        body, o["block"], library,
        path=o.get("path"), lane=o.get("lane"), pos=o.get("pos"),
    )
    return []


def _op_swap_model(body: dict, library: Library, o: dict) -> list[str]:
    return mutate.swap_model(
        body, o["old"], o["new"], library,
        path=o.get("path"), lane=o.get("lane"), pos=o.get("pos"),
    )


_PATCH_OPS = {
    "set_param": _op_set_param,
    "set_enabled": _op_set_enabled,
    "add_block": _op_add_block,
    "remove_block": _op_remove_block,
    "swap_model": _op_swap_model,
}


def patch_preset_handler(
    library: Library, model: str, hsp_b64: str, operations: list
) -> dict[str, Any]:
    """Apply a sequence of surgical edits directly to a base64-encoded .hsp blob.

    Decodes `hsp_b64` to a body dict, applies each `{"op": ...}` entry in
    `operations` via the matching `helixgen.mutate` verb (mutating the body
    in place -- no spec round-trip), then re-encodes via
    `helixgen.hsp.dumps_hsp`.

    Returns `{"hsp_b64": <base64 .hsp bytes>, "warnings": [<str>, ...]}`.
    `warnings` collects any `swap_model` messages about params/IRs that
    couldn't be carried over to the new block.
    """
    _validate_model(model)
    body = _decode_hsp_b64(hsp_b64)
    warnings: list[str] = []
    for o in operations:
        op = o.get("op")
        if op not in _PATCH_OPS:
            raise ValueError(f"unknown patch op {op!r}; valid: {sorted(_PATCH_OPS)}")
        warnings.extend(_PATCH_OPS[op](body, library, o))
    return {
        "hsp_b64": base64.b64encode(dumps_hsp(body)).decode("ascii"),
        "warnings": warnings,
    }
