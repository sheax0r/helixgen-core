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
from helixgen.hsp import dumps_hsp, is_hsp_bytes, read_hsp, write_hsp
from helixgen.ir import IrMapping, compute_stadium_irhash
from helixgen.irhash_cache import IrHashCache, cached_irhash
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
    irhash = cached_irhash(wav)
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
    cache = IrHashCache.load()
    registered: list[str] = []
    already: list[str] = []
    conflicts: list[str] = []
    failed: list[dict[str, str]] = []

    for wav in sorted(root.rglob("*")):
        if not wav.is_file() or wav.suffix.lower() != ".wav":
            continue
        try:
            h = cached_irhash(wav, cache=cache)
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
    cache.save()
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
    cache = IrHashCache.load()
    for wav in sorted(root.rglob("*")):
        if not wav.is_file() or wav.suffix.lower() != ".wav":
            continue
        try:
            h = cached_irhash(wav, cache=cache)
        except (NotImplementedError, RuntimeError, FileNotFoundError):
            continue
        out.append({"hash": h, "path": str(wav), "basename": wav.name})
    cache.save()
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


def _read_hsp_body(hsp_path: str) -> dict[str, Any]:
    """Read a `.hsp` file into its parsed JSON body dict.

    Raises ValueError with an actionable message if the path doesn't exist
    or the bytes don't start with the `.hsp` magic header.
    """
    p = Path(hsp_path).expanduser()
    if not p.is_file():
        raise ValueError(f".hsp not found: {hsp_path}")
    return read_hsp(p)


def view_preset_handler(
    library: Library, model: str, hsp_path: str, *, irs_dir: Path | None = None
) -> dict[str, Any]:
    """Read a `.hsp` file and return its read-only projection dict.

    Mirrors `helixgen view`: reads the magic-prefixed JSON body off disk, then
    projects it via `helixgen.view.view`. IRs are resolved against the mapping
    at `irs_dir` (or the default `$HELIXGEN_IRS`/`~/.helixgen/irs/`) so a
    registered IR block's `irhash` can be reported by wav basename.
    """
    _validate_model(model)
    body = _read_hsp_body(hsp_path)
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
    library: Library, model: str, hsp_path: str, operations: list
) -> dict[str, Any]:
    """Apply a sequence of surgical edits to a `.hsp` file, in place.

    Reads `hsp_path`, applies each `{"op": ...}` entry in `operations` via the
    matching `helixgen.mutate` verb (mutating the body in place -- no spec
    round-trip), then writes the result back to the same path.

    Returns `{"path": <hsp_path>, "warnings": [<str>, ...]}`. `warnings`
    collects any `swap_model` messages about params/IRs that couldn't be
    carried over to the new block. An unknown op raises before any write, so a
    bad op leaves the file untouched.
    """
    _validate_model(model)
    body = _read_hsp_body(hsp_path)
    warnings: list[str] = []
    for o in operations:
        op = o.get("op")
        if op not in _PATCH_OPS:
            raise ValueError(f"unknown patch op {op!r}; valid: {sorted(_PATCH_OPS)}")
        warnings.extend(_PATCH_OPS[op](body, library, o))
    write_hsp(hsp_path, body)
    return {"path": hsp_path, "warnings": warnings}


# ---------------------------------------------------------------------------
# device_* handlers — drive a networked Line 6 Helix Stadium over the LAN.
#
# These delegate to `helixgen.device.HelixClient`, which speaks the editor's
# ZeroMQ/OSC protocol and needs the optional `device` extra (pyzmq + msgpack).
# The client is imported LAZILY inside each handler so merely importing
# `mcp_server.tools` never requires the device extra — only actually calling a
# device_* tool does. `HelixError` (device/RPC failure) is wrapped as
# `ValueError` so FastMCP renders it as an MCP `isError` text block.
# ---------------------------------------------------------------------------

# Default LAN address of the user's Helix Stadium (override per-call via `ip`).
_DEFAULT_DEVICE_IP = os.environ.get("HELIXGEN_HELIX_IP") or "192.168.4.84"


def _device_container(setlist: str) -> int:
    """Map a setlist name (``"user"``/``"factory"``/``"throwaway"``) to a container id.

    Returns the virtual-container constant from `helixgen.device`. Raises
    ValueError for any other name so a typo reports itself rather than
    silently targeting the wrong container.
    """
    from helixgen.device import USER, FACTORY, THROWAWAY

    key = (setlist or "user").strip().lower()
    mapping = {"user": USER, "factory": FACTORY, "throwaway": THROWAWAY}
    if key not in mapping:
        raise ValueError(
            f"unknown setlist {setlist!r}; valid: {sorted(mapping)}"
        )
    return mapping[key]


def device_list_presets_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, setlist: str = "user"
) -> list[dict[str, Any]]:
    """List presets in a setlist on the networked device.

    Returns the raw preset dicts as reported by the device (each carries
    ``cid_``, ``name``, ``cctp``, ``posi``), sorted by slot position.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    container = _device_container(setlist)
    try:
        with HelixClient(ip=ip) as client:
            return client.list_presets(container=container)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_list_setlists_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP
) -> list[dict[str, Any]]:
    """List the device's virtual setlist containers that currently resolve."""
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            return client.list_setlists()
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_read_preset_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, cid: int
) -> dict[str, Any]:
    """Read a single preset's content reference (attributes) by its ``cid``.

    Raises ValueError if the device has no content at that ``cid``.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            ref = client.get_ref(cid)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    if ref is None:
        raise ValueError(f"no content at cid {cid!r}")
    return ref


def device_load_preset_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, cid: int
) -> dict[str, Any]:
    """Load a preset (by ``cid``) into the device's edit buffer.

    Returns ``{"ok": <bool>}`` — the device's acknowledgement status.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            return {"ok": bool(client.load_preset(cid))}
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_create_preset_handler(
    model: str,
    *,
    ip: str = _DEFAULT_DEVICE_IP,
    src_cid: int,
    setlist: str = "user",
    pos: int,
) -> dict[str, Any]:
    """Create a preset by copying ``src_cid`` into ``setlist`` at slot ``pos``.

    Returns ``{"ok": <bool>, "cid": <new cid or None>}``. ``ok`` is False when
    the device did not report a new cid for the copy.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    container = _device_container(setlist)
    try:
        with HelixClient(ip=ip) as client:
            new_cid = client.create_from(src_cid, container, pos)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"ok": new_cid is not None, "cid": new_cid}


def device_rename_preset_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, cid: int, name: str
) -> dict[str, Any]:
    """Rename the preset at ``cid`` to ``name``. Returns ``{"ok": <bool>}``."""
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            return {"ok": bool(client.rename(cid, name))}
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_delete_preset_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, cid: int, setlist: str = "user"
) -> dict[str, Any]:
    """Delete the preset at ``cid`` from ``setlist``. Returns ``{"ok": <bool>}``."""
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    container = _device_container(setlist)
    try:
        with HelixClient(ip=ip) as client:
            return {"ok": bool(client.delete(container, [cid]))}
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_set_param_handler(
    model: str,
    *,
    ip: str = _DEFAULT_DEVICE_IP,
    path: int,
    block: int,
    param_id: int,
    value: float,
) -> dict[str, Any]:
    """Set one param in the device's edit buffer. Returns ``{"ok": <bool>}``.

    ``path``/``block``/``param_id`` are the device's numeric coordinates for
    the target param; ``value`` is the normalized float.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            return {"ok": bool(client.set_param(path, block, param_id, value))}
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_install_preset_handler(
    model: str,
    *,
    ip: str = _DEFAULT_DEVICE_IP,
    hsp_b64: str,
    name: str,
    pos: int,
    setlist: str = "user",
    template_cid: int | None = None,
) -> dict[str, Any]:
    """Author a helixgen .hsp (base64) onto the device as a new preset.

    Maps the preset's blocks onto a device template's same-category slots
    (v2.2: single serial chain) and installs it. ``template_cid`` picks a device
    preset to use as the chain template (defaults to the current edit buffer).
    Returns ``{"ok": <bool>, "cid": <new cid or None>}``. EXPERIMENTAL.
    """
    _validate_model(model)
    import json as _json
    from helixgen.hsp import is_hsp_bytes
    from helixgen.device import HelixClient, HelixError, bridge

    raw = base64.b64decode(hsp_b64)
    if not is_hsp_bytes(raw):
        raise ValueError("not a .hsp document (bad magic)")
    body = _json.loads(raw[8:].decode("utf-8"))
    container = _device_container(setlist)
    try:
        with HelixClient(ip=ip) as client:
            if client.find_by_pos(container, pos) is not None:
                raise ValueError(f"{setlist} slot {pos} is not empty")
            if template_cid is not None:
                client.load_preset(template_cid)
            template_blob = client.get_edit_buffer()
            try:
                cid = bridge.install_recipe(client, body, container, pos, name,
                                            template_blob, strict=True)
            except (bridge.UnresolvedModel, ValueError) as e:
                raise ValueError(str(e)) from e
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"ok": cid is not None, "cid": cid}


def device_save_preset_handler(
    model: str,
    *,
    ip: str = _DEFAULT_DEVICE_IP,
    name: str,
    setlist: str = "user",
    pos: int,
) -> dict[str, Any]:
    """Save the device's CURRENT edit buffer as a new preset at ``pos``.

    Mirrors the editor's "Save As New". The target slot must be empty. Returns
    ``{"ok": <bool>, "cid": <new cid or None>}``.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    container = _device_container(setlist)
    try:
        with HelixClient(ip=ip) as client:
            if client.find_by_pos(container, pos) is not None:
                raise ValueError(f"{setlist} slot {pos} is not empty")
            new_cid = client.save_edit_buffer_to(container, pos, name)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"ok": new_cid is not None, "cid": new_cid}
