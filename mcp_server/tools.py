"""Pure-Python handlers for MCP tools. No MCP types; FastMCP wraps these at
registration time. Importable + directly testable.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from helixgen import mutate
from helixgen.hsp import read_hsp, write_hsp
from helixgen.ir import IrMapping, compute_stadium_irhash
from helixgen.irhash_cache import IrHashCache, cached_irhash
from helixgen.library import Library
from helixgen.recipe import generate_from_recipe
from helixgen.spec import parse_spec
from helixgen.view import view as view_projection


# Required `model` parameter on every tool. Allow-list; everything else errors.
# Soft gate (agents can misreport); the setup skill is the real gate.
_SUPPORTED_MODELS = frozenset({"stadium", "stadium_xl"})

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


def generate_preset_handler(
    library: Library, model: str, recipe: dict[str, Any], out_path: str,
    *, irs_dir: Path | None = None,
) -> dict[str, Any]:
    """Generate a Helix Stadium .hsp preset from a recipe dict and write it to disk.

    Builds directly against the library's Stadium chassis via
    `helixgen.recipe.generate_from_recipe`, writes the `.hsp` bytes to
    `out_path` (creating parent directories), and returns
    `{"path": <out_path>, "warnings": []}`. The `.hsp` file is the sole source
    of truth -- no sidecar spec is written.

    Underlying SpecError / ParamValidationError / GenerateError propagate; the
    MCP server boundary translates them to protocol errors (raised before any
    file is written).
    """
    _validate_model(model)
    # Parse+validate the recipe before touching the chassis, so a malformed
    # recipe reports its own error rather than being masked by a
    # missing-chassis error (mirrors `helixgen generate`'s error ordering).
    spec = parse_spec(recipe, source="mcp:generate_preset")
    irs = IrMapping.load(irs_dir)
    chassis = library.load_chassis()
    # Generate-time diagnostics (unshowable scribble labels, >12-char labels,
    # unregistered IR hashes, ...) are stderr prints in the CLI; capture them
    # here so the MCP caller sees them in the returned `warnings` instead of
    # a stderr stream it cannot read. They are re-emitted to the real stderr.
    import contextlib
    import io
    import sys as _sys

    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        raw = generate_from_recipe(
            spec, library, irs=irs, chassis=chassis, source="mcp:generate_preset"
        )
    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    warnings: list[str] = []
    for line in captured.getvalue().splitlines():
        print(line, file=_sys.stderr)
        line = line.strip()
        if line:
            warnings.append(line[len("warning: "):] if line.startswith("warning: ") else line)
    try:
        from helixgen.device.manifest import SetlistManifest

        m = SetlistManifest.load()
        m.register_tone(out, source="authored")
        m.save()
    except Exception as e:  # noqa: BLE001 — registration is advisory
        warnings.append(f"could not register tone in library: {e}")
    return {"path": out_path, "warnings": warnings}


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


def compute_irhash_handler(model: str, wav_path: str) -> dict[str, str]:
    """Compute Stadium's IR hash for a WAV file on disk.

    Reads the first 12 bytes to check RIFF/WAVE magic (cheap defense-in-depth
    before libsndfile, which has had CVEs), then calls `compute_stadium_irhash`.
    Returns the 32-char hex hash plus an upload-to-device reminder.

    Raises ValueError on bad model / missing file / non-WAV magic; FastMCP
    translates these to an `isError` text content block.
    """
    _validate_model(model)
    wav = Path(wav_path).expanduser()
    if not wav.is_file():
        raise ValueError(f"wav file not found: {wav_path}")
    with wav.open("rb") as fh:
        head = fh.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        raise ValueError(
            "WAV bytes don't look valid (missing RIFF/WAVE magic). "
            "Make sure this is a .wav file, not another format."
        )
    irhash = compute_stadium_irhash(wav)
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


def device_info_handler(model: str, *, ip: str = _DEFAULT_DEVICE_IP) -> dict[str, Any]:
    """Query the device's product info (``/ProductInfoGet`` -- a read).

    Returns model, device_id, helixgen_model, serial, firmware (+build/date),
    sd storage totals, and the full 4CC-decoded reply under ``raw``.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            return client.product_info()
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_settings_list_handler(
    *, page: str | None = None, values: bool = False, ip: str = _DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """List Global-Settings property keys grouped by page.

    Without ``values`` this is offline (the bundled catalog). With
    ``values=True`` it fetches each key's live value + range from the device.
    """
    from helixgen.device import settings as S

    try:
        catalog = {page: S.keys_for_page(page)} if page else S.pages()
    except KeyError:
        raise ValueError(
            f"unknown page {page!r}; choose from {', '.join(S.page_names())}")
    if not values:
        return {"pages": catalog}

    from helixgen.device import HelixClient, HelixError
    rows: list[dict[str, Any]] = []
    try:
        with HelixClient(ip=ip) as client:
            for pg in sorted(catalog):
                for k in catalog[pg]:
                    try:
                        d = client.get_property_def(k)
                        v = client.get_property(k)
                        rows.append({"page": pg, "key": k, "name": d.name,
                                     "value": v.value, "type": d.type,
                                     "min": d.vmin, "max": d.vmax,
                                     "enum": d.enum})
                    except (HelixError, ValueError) as e:
                        rows.append({"page": pg, "key": k, "error": str(e)})
                    if client.sock is None:  # connection lost — stop cleanly
                        return {"settings": rows, "aborted_at": k}
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"settings": rows}


def device_settings_get_handler(
    key: str, *, ip: str = _DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Read one Global-Settings value with its definition (name/range/enum)."""
    from helixgen.device import settings as S
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            d = client.get_property_def(key)
            v = client.get_property(key)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"key": key, "name": d.name, "value": v.value,
            "display": S.render_value(d, v.value), "type": d.type,
            "min": d.vmin, "max": d.vmax, "default": d.default,
            "enum": d.enum, "page": S.page_for_key(key)}


def device_settings_set_handler(
    key: str, value: str, *, ip: str = _DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Write one Global-Settings value. ``value`` may be a number or (for enum
    properties) a label like ``"Strobe"`` or its index. Validated against the
    property's range/enum before sending. Returns ``{ok, key, value, display}``.
    """
    from helixgen.device import settings as S
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            d = client.get_property_def(key)
            coerced = S.coerce_value(d, str(value))
            ok = client.set_property(key, d.type, coerced)
            readback = client.get_property(key)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"ok": bool(ok), "key": key, "value": readback.value,
            "display": S.render_value(d, readback.value), "name": d.name}


def device_globaleq_list_handler() -> dict[str, Any]:
    """List the Global EQ outputs (qtr/xlr/pho), bands, and valid params.

    Offline catalog. Global EQ is **write-only** over the network (no read-back),
    so there is no globaleq "get" tool.
    """
    from helixgen.device import globaleq as G

    return {"outputs": G.OUTPUTS, "catalog": G.catalog()}


def device_globaleq_set_handler(
    output: str, band: str, param: str, value: str,
    *, ip: str = _DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Write one **Global EQ** band parameter over the network.

    ``output`` ∈ qtr/xlr/pho; ``band`` ∈ lowcut/lowshelf/low/mid/high/highshelf/
    highcut (or "" with ``param="level"`` for the output level); ``param`` ∈
    enable/freq/gain/q/slope/level. Validated against the band's param set before
    sending. Returns ``{ok, key, value}``.
    """
    from helixgen.device import globaleq as G
    from helixgen.device import HelixClient, HelixError

    band_arg = "" if str(band).strip() in ("-", "") else band
    try:
        key = G.key_for(output, band_arg, param)  # validate before connecting
        with HelixClient(ip=ip) as client:
            ok = client.set_globaleq(output, band_arg, param, value)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"ok": bool(ok), "key": key, "value": value}


def device_tuner_handler(
    *, seconds: float = 3.0, ip: str = _DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Sample the device's live pitch detector for ``seconds`` and return the
    latest reading.

    Reads the always-on background pitch telemetry on port 2003 (no Stadium app,
    no tuner-engage needed). Returns ``{signal, note, cents, hz, midi, samples}``
    — ``signal`` False means no note was detected (silence) in the window. Play a
    note before/while calling.
    """
    from helixgen.device.subscribe import HelixSubscriber
    from helixgen.device import HelixError
    from helixgen.device import tuner as T

    last = None
    samples = 0
    try:
        with HelixSubscriber(ip=ip) as sub:
            for ev in sub.stream(duration=seconds, filter_addrs={"/dspEvent"},
                                 include_noise=True):
                r = T.reading_from_event_args(ev.args)
                if r is None:
                    continue
                samples += 1
                if r.signal:
                    last = r  # keep the most recent pitched reading
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    if last is None:
        return {"signal": False, "note": "—", "cents": None, "hz": None,
                "midi": None, "samples": samples}
    return {"signal": True, "note": last.name, "cents": last.cents,
            "hz": round(last.hz, 2), "midi": round(last.midi, 3),
            "samples": samples}


def device_snapshot_handler(
    index: int, *, ip: str = _DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Recall a snapshot (0-based, 0..7) on the live device. Changes the ACTIVE
    tone's snapshot immediately (`/activateSnapshot`). Returns ``{ok, index}``."""
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            ok = client.activate_snapshot(index)
    except (HelixError, ValueError) as e:
        raise ValueError(f"device error: {e}") from e
    return {"ok": bool(ok), "index": int(index)}


def device_blocks_handler(*, ip: str = _DEFAULT_DEVICE_IP) -> dict[str, Any]:
    """List the live edit buffer's blocks with (path, block) coordinates + model
    + on/off state — the coordinates device_bypass/device_model/device_set_param
    address. Reads only (does not change the tone). Returns ``{blocks: [...]}``."""
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            return {"blocks": client.edit_buffer_blocks()}
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_bypass_handler(
    path: int, block: int, enable: bool, *, ip: str = _DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Enable (``enable=True``) or bypass (``False``) a block in the live edit
    buffer (`/BlockEnableSet`). Coordinates from device_blocks. Changes the
    ACTIVE tone. Returns ``{ok, path, block, enabled}``."""
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            ok = client.set_block_enable(path, block, bool(enable))
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"ok": bool(ok), "path": int(path), "block": int(block),
            "enabled": bool(enable)}


def device_model_handler(
    path: int, block: int, model: str, *, ip: str = _DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Set a block's model in the live edit buffer (`/ModelSet`). ``model`` is a
    numeric model id or a model-id string (e.g. ``HD2_AmpBritPlexiNrm``). The
    device rejects a cross-category swap. Changes the ACTIVE tone. Returns
    ``{ok, path, block, model, model_id}``."""
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import defs as _defs

    if str(model).lstrip("-").isdigit():
        model_id = int(model)
    else:
        model_id = _defs.model_id_for(model)
        if model_id is None:
            raise ValueError(
                f"unknown model {model!r}; pass a numeric id or exact model-id "
                "string (see list_blocks)")
    try:
        with HelixClient(ip=ip) as client:
            ok = client.set_block_model(path, block, model_id)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"ok": bool(ok), "path": int(path), "block": int(block),
            "model": model, "model_id": model_id}


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
            new_cid = client._raw.create_from(src_cid, container, pos)
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
            return {"ok": bool(client._raw.delete(container, [cid]))}
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
    hsp_path: str,
    name: str,
    pos: int,
    setlist: str = "user",
) -> dict[str, Any]:
    """Author a helixgen .hsp file onto the device as a new preset.

    Reads the `.hsp` off ``hsp_path``, transcodes it straight into the device's
    native content format (any block chain, full fidelity, no template), and
    installs it into the empty slot ``pos``. Returns
    ``{"ok": <bool>, "cid": <new cid or None>}``. EXPERIMENTAL.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError, bridge, transcode

    body = _read_hsp_body(hsp_path)
    container = _device_container(setlist)
    try:
        with HelixClient(ip=ip) as client:
            if client.find_by_pos(container, pos) is not None:
                raise ValueError(f"{setlist} slot {pos} is not empty")
            try:
                blob = transcode.hsp_to_sbepgsm(body, strict=True)
            except (bridge.UnresolvedModel, ValueError) as e:
                raise ValueError(str(e)) from e
            with client.mutating():
                cid = client._raw.push_to_slot(container, pos, name, blob)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    if cid is not None:
        try:
            from helixgen.device.manifest import SetlistManifest, _posi_to_slot

            m = SetlistManifest.load()
            if name not in m.tones:
                m.register_tone(hsp_path, source="import-local")
            slot = _posi_to_slot(pos)
            if slot:
                m.mark_on_device(name, slot)
            m.tones[name]["device"] = {"cid": cid, "posi": pos}
            if setlist and setlist != "user":
                m.add_to_setlist(setlist, name)
            m.save()
        except Exception:  # noqa: BLE001 — ledger/manifest record is advisory
            pass
    return {"ok": cid is not None, "cid": cid}


# ---------------------------------------------------------------------------
# setlist-manifest handlers (local, no device) + reference-based sync
# ---------------------------------------------------------------------------


def device_setlist_list_handler(model: str) -> dict[str, Any]:
    """Return the local setlist manifest as a dict (desired membership +
    observed device placement).

    Mirrors ``helixgen device setlist list``: reads
    ``~/.helixgen/setlists.json`` (via :meth:`SetlistManifest.load`) and returns
    its full ``to_dict()`` document (``{version, tones, setlists, observed}``).
    Local-only — never touches the device.
    """
    _validate_model(model)
    from helixgen.device.manifest import SetlistManifest

    return SetlistManifest.load().to_dict()


def device_setlist_add_handler(
    model: str, setlist: str, hsp_path: str, *, pos: int | None = None
) -> dict[str, Any]:
    """Register an authored ``.hsp`` tone and add it to ``setlist``'s membership.

    Reads the file's ``meta.name`` as the tone name, records its path +
    content hash in the manifest, and appends the name to ``setlist`` (at ``pos``
    if given; the setlist is auto-created in the manifest if new). Local-only.
    Returns ``{ok, setlist, tone, tones}`` (``tones`` = the setlist's new
    ordered membership). A duplicate name bound to a different path raises
    ValueError.
    """
    _validate_model(model)
    from helixgen.device.manifest import SetlistManifest, ManifestError

    m = SetlistManifest.load()
    try:
        name = m.add_tone(setlist, hsp_path, pos=pos)
    except ManifestError as e:
        raise ValueError(str(e)) from e
    m.save()
    return {"ok": True, "setlist": setlist, "tone": name,
            "tones": m.tones_in(setlist)}


def device_setlist_remove_handler(
    model: str, setlist: str, tone_name: str
) -> dict[str, Any]:
    """Drop ``tone_name`` from ``setlist``'s membership in the local manifest.

    Keeps the tone in the registry if another setlist still references it or
    it carries an explicit device mark (`device add` / concrete slot); an
    implicit synced-setlist auto-stamp dies with its last membership
    (add-then-remove is a no-op). Local-only. Returns ``{ok, setlist, tone,
    tones}`` — ``ok`` is False if the tone wasn't in that setlist.
    """
    _validate_model(model)
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    removed = m.remove_tone(setlist, tone_name)
    m.save()
    return {"ok": removed, "setlist": setlist, "tone": tone_name,
            "tones": m.tones_in(setlist)}


def device_sync_setlist_handler(
    model: str,
    setlist: str,
    *,
    ip: str = _DEFAULT_DEVICE_IP,
    exclude_irs: bool = False,
) -> dict[str, Any]:
    """Sync ONE manifest setlist onto the device (pool-first, reference rebuild).

    Reconciles the pool for the tones ``setlist`` needs (install/update/skip),
    then rebuilds that setlist's references to match manifest order — never
    orphaning a still-referenced pool preset. A single-setlist sync never
    garbage-collects. Returns the engine result dict (``{ok, setlists, pool,
    references, gc, irs, errors}``). EXPERIMENTAL.
    """
    _validate_model(model)
    from helixgen.device import HelixError
    from helixgen.device.manifest import SetlistManifest
    from helixgen.device.setlist_sync import sync_setlists

    try:
        return sync_setlists(
            SetlistManifest.load(), ip=ip, setlists=[setlist],
            exclude_irs=exclude_irs,
        )
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_sync_all_handler(
    model: str,
    *,
    ip: str = _DEFAULT_DEVICE_IP,
    gc: bool = False,
    exclude_irs: bool = False,
) -> dict[str, Any]:
    """Sync ALL manifest setlists onto the device (the whole-library reconcile).

    Reconciles the pool for the union of every setlist's tones, rebuilds each
    setlist's references, and — only when ``gc=True`` — garbage-collects pool
    presets no setlist references any more (never orphaning). Returns the engine
    result dict (``{ok, setlists, pool, references, gc, irs, errors}``).
    EXPERIMENTAL.
    """
    _validate_model(model)
    from helixgen.device import HelixError
    from helixgen.device.manifest import SetlistManifest
    from helixgen.device.setlist_sync import sync_setlists

    try:
        return sync_setlists(
            SetlistManifest.load(), ip=ip, setlists=None, gc=gc,
            exclude_irs=exclude_irs,
        )
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


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
            new_cid = client._raw.save_edit_buffer_to(container, pos, name)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"ok": new_cid is not None, "cid": new_cid}


# --- IR maintenance + preset info + device-side setlist ops (parity #20) ----


def device_delete_ir_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, name_or_hash: str,
    force_wedge: bool = False
) -> dict[str, Any]:
    """Delete ONE user IR from the device, matched by name or 32-hex hash.

    Removes the registry entry AND the backing .wav on the device
    (best-effort — ``file_removed`` in the result says whether the file is
    gone). Presets that referenced it show a silent cab until it is
    re-imported. ``force_wedge=True`` additionally allows cleaning the
    "wedged" state (a 32-hex hash with no registry entry but a still-resolving
    device file, left by a delete → quick re-import); the result then has
    ``cid: None``. Never pass ``force_wedge`` for an IR that was just
    imported — its listing may merely be lagging. Returns
    ``{ok, cid, name, hash, file_removed}``. Raises ValueError when nothing
    (or more than one name) matches.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import maintenance as mt

    try:
        with HelixClient(ip=ip) as client:
            return mt.delete_device_ir(client, name_or_hash, ip=ip,
                                       force_wedge=force_wedge)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_rename_ir_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, name_or_hash: str, new_name: str
) -> dict[str, Any]:
    """Rename a user IR on the device (matched by name or 32-hex hash).

    Display-name only — the IR's hash (which presets reference) is untouched,
    so no preset breaks. Returns ``{ok, cid, name, hash}`` (``name`` = the new
    name).
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import maintenance as mt

    try:
        with HelixClient(ip=ip) as client:
            target = mt.resolve_device_ir_live(client, name_or_hash)
            ok = client.rename(target["cid_"], new_name)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"ok": bool(ok), "cid": target.get("cid_"), "name": new_name,
            "hash": target.get("hash")}


def device_ir_prune_handler(
    model: str,
    *,
    ip: str = _DEFAULT_DEVICE_IP,
    execute: bool = False,
    force: bool = False,
    ignore_warnings: bool = False,
    only: str | None = None,
) -> dict[str, Any]:
    """Delete device IRs no preset references any more (**dry-run by default**).

    Diffs the device's user IRs against every IR hash referenced by presets ON
    the device (non-activating content reads across the pool) and by local
    tone-library ``.hsp`` files. Nothing is deleted unless ``execute=True``; an
    IR referenced on the device is never a candidate; an IR referenced only by
    a local off-device tone is "protected" and needs ``force=True`` too. Local
    tones whose recorded ``.hsp`` can't be read surface in ``warnings`` —
    executing over warnings needs ``ignore_warnings=True`` (a SEPARATE consent
    from ``force``). ``only`` narrows deletion to a single IR (name-or-hash).
    Returns ``{ok, dry_run, device_irs, referenced, protected, orphans,
    deleted, warnings, errors}``.
    """
    _validate_model(model)
    from helixgen.device import HelixError
    from helixgen.device import maintenance as mt

    try:
        return mt.ir_prune(ip=ip, execute=execute, force=force,
                           ignore_warnings=ignore_warnings, only=only)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_set_info_handler(
    model: str,
    *,
    ip: str = _DEFAULT_DEVICE_IP,
    cids: list[int],
    color: str | int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Set preset color and/or notes on one or more CIDs (batch-capable).

    ``color`` is a palette name (auto, white, red, dark orange, light orange,
    yellow, green, turquoise, blue, violet, pink, off) or raw index 0-11 —
    written as the ``colr`` content attr. ``notes`` is the Preset Info text,
    written via a NON-activating content round-trip (the device's live tone is
    never disturbed). At least one of ``color``/``notes`` is required. Returns
    ``{ok, results: [{cid, color?, notes?}]}``.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import maintenance as mt

    if color is None and notes is None:
        raise ValueError("nothing to set: give color and/or notes")
    if color is not None:
        mt.color_index(color)  # validate once, before touching any preset
    results: list[dict[str, Any]] = []
    try:
        with HelixClient(ip=ip) as client:
            for cid in cids:
                try:
                    out = mt.set_preset_info(client, int(cid), color=color,
                                             notes=notes)
                except HelixError as e:
                    results.append({"cid": int(cid), "error": str(e)})
                    continue
                results.append({"cid": int(cid), **out})
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    ok = all("error" not in r
             and all(r.get(k, True) for k in ("color", "notes"))
             for r in results)
    return {"ok": ok, "results": results}


def device_setlist_create_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, name: str
) -> dict[str, Any]:
    """Create a new EMPTY setlist ON THE DEVICE (no Stadium app needed).

    Device-side creation (backlog #8): sends the device's own create command
    and records the setlist in the local manifest too. Errors if a setlist
    with that name already exists on the device. Returns ``{ok, cid, name}``.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            if client.resolve_setlist_cid(name) is not None:
                raise ValueError(f"setlist {name!r} already exists on the device")
            cid = client.create_setlist(name)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    if cid is None:
        raise ValueError(f"device refused to create setlist {name!r}")
    try:
        from helixgen.device.manifest import SetlistManifest

        m = SetlistManifest.load()
        m.create_setlist(name)
        m.save()
    except Exception:  # noqa: BLE001 — advisory; the device write succeeded
        pass
    return {"ok": True, "cid": cid, "name": name}


def device_setlist_rename_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, name: str, new_name: str
) -> dict[str, Any]:
    """Rename a setlist ON THE DEVICE (and in the local manifest, if tracked).

    Resolves the setlist by (case-insensitive) name. Errors if ``name`` isn't
    on the device or ``new_name`` already is. Returns ``{ok, cid, name}``
    (``name`` = the new name).
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            cid = client.resolve_setlist_cid(name)
            if cid is None:
                raise ValueError(f"setlist {name!r} not found on the device")
            if client.resolve_setlist_cid(new_name) is not None:
                raise ValueError(
                    f"a setlist named {new_name!r} already exists on the device")
            ok = client.rename(cid, new_name)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    try:
        from helixgen.device.manifest import SetlistManifest

        m = SetlistManifest.load()
        if m.rename_setlist(name, new_name):
            m.save()
    except Exception:  # noqa: BLE001 — advisory
        pass
    return {"ok": bool(ok), "cid": cid, "name": new_name}


def device_setlist_delete_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, name: str
) -> dict[str, Any]:
    """Delete a setlist ON THE DEVICE. Its references die with it — the pool
    presets they point at are NEVER deleted (never-orphan guarantee).

    A local manifest setlist of the same name is kept as a local-only draft
    (marked unsynced). Returns ``{ok, cid, name}``.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            cid = client.resolve_setlist_cid(name)
            if cid is None:
                raise ValueError(f"setlist {name!r} not found on the device")
            ok = client.delete_setlist(cid)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    try:
        from helixgen.device.manifest import SetlistManifest

        m = SetlistManifest.load()
        if name in m.setlists_map:
            m.set_setlist_synced(name, False)
            m.save()
    except Exception:  # noqa: BLE001 — advisory
        pass
    return {"ok": bool(ok), "cid": cid, "name": name}


def device_setlist_duplicate_handler(
    model: str, *, ip: str = _DEFAULT_DEVICE_IP, src: str, dst: str
) -> dict[str, Any]:
    """Duplicate a setlist ON THE DEVICE: copy ``src``'s references into ``dst``.

    ``dst`` is created on the device when absent; if it exists it must be
    EMPTY. References are pointers — the pool presets are shared, not copied
    (editing a pool preset changes it in both setlists). Returns
    ``{ok, src_cid, dst_cid, created, copied}``.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip=ip) as client:
            src_cid = client.resolve_setlist_cid(src)
            if src_cid is None:
                raise ValueError(f"setlist {src!r} not found on the device")
            dst_cid = client.resolve_setlist_cid(dst)
            created = False
            if dst_cid is None:
                dst_cid = client.create_setlist(dst)
                created = True
                if dst_cid is None:
                    raise ValueError(f"device refused to create setlist {dst!r}")
            copied = client.duplicate_setlist_refs(src_cid, dst_cid)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    if created:
        try:
            from helixgen.device.manifest import SetlistManifest

            m = SetlistManifest.load()
            m.create_setlist(dst)
            m.save()
        except Exception:  # noqa: BLE001 — advisory; the device write succeeded
            pass
    return {"ok": True, "src_cid": src_cid, "dst_cid": dst_cid,
            "created": created, "copied": copied}


def device_reorder_handler(
    setlist: str, target: str, to_index: int, *, ip: str = _DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Move a preset to a new position within a setlist via
    `/ReorderContainerContent`.

    ``setlist`` is a setlist display name (resolved the way `device_setlist_*`
    resolve setlists), a literal container cid (``-2`` = the pool, whose
    presets also resolve by name), or the literal ``"setlists"`` to instead
    reorder the top-level setlist list itself (``target`` is then a setlist
    name/cid; a real setlist literally named "setlists" must be addressed by
    its container cid). ``target`` is a preset display name or a literal cid
    within that setlist; ``to_index`` is bounds-validated against the
    container's current length. Numeric ``target``/``setlist`` values are
    cid-first: a display-name collision yields a ``warnings`` entry when the
    cid resolves in the container, and an error (naming the item's real cid)
    when it doesn't.
    Direct, immediate DEVICE-side write — distinct from the local-manifest
    reorder path (`device_slots reorder` CLI verb + `device_sync_setlist`),
    which only takes effect on the device on the next sync. Returns
    ``{ok, container, moved_cid, new_pos, items, warnings}``.
    """
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import reorder as R

    try:
        with HelixClient(ip=ip) as client:
            return R.reorder_setlist_item(client, setlist, target, to_index)
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e


def device_meters_handler(
    *, seconds: float = 3.0, ip: str = _DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Sample the device's live level-meter telemetry for ``seconds`` and
    return the latest reading per meter stream.

    Reads the grid-level meter arrays (`/dspEvent` eid_=1, mid_=796/800 — 128
    floats each) that ride the same port-2003 burst as the network tuner (no
    Stadium app needed). Returns ``{"meters": [{"mid", "peak", "values"}, …],
    "samples": N}`` — ``meters`` holds the most recent reading seen per mid
    (0, 1, or 2 entries depending on what arrived in the window).
    """
    from helixgen.device.subscribe import HelixSubscriber
    from helixgen.device import HelixError
    from helixgen.device import meters as M

    last: dict[int, Any] = {}
    samples = 0
    try:
        with HelixSubscriber(ip=ip) as sub:
            for ev in sub.stream(duration=seconds, filter_addrs={"/dspEvent"},
                                 include_noise=True):
                for r in M.readings_from_event_args(ev.args):
                    samples += 1
                    last[r.mid] = r
    except HelixError as e:
        raise ValueError(f"device error: {e}") from e
    return {"meters": [
                {"mid": mid, "peak": round(r.peak, 4),
                 "values": [round(v, 4) for v in r.values]}
                for mid, r in sorted(last.items())],
            "samples": samples}
