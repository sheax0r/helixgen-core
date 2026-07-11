"""FastMCP server wiring: registers the helixgen tools.

Each tool delegates to the corresponding pure-Python handler in
`mcp_server.tools`. The library is resolved per-request via the standard
`helixgen.library.default_library_path()` (overridable via HELIXGEN_LIBRARY).

All tools take a required `model` parameter — `"stadium"` or `"stadium_xl"`.
Anything else raises `ValueError` (FastMCP renders this as an MCP isError
text content block). The param is a soft gate; the `setup` skill is
the real guarantee that the device is confirmed per session.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import BlobResourceContents, EmbeddedResource

from helixgen.library import Library, default_library_path
from mcp_server import tools as _tools


def _resolve_library() -> Library:
    """Construct a Library at the configured path. Cheap; no caching for v1."""
    return Library(default_library_path())


app = FastMCP("helixgen")


@app.tool()
def list_blocks(model: str, category: str | None = None) -> str:
    """List Helix blocks in the library, optionally filtered to one category.

    Required `model`: `"stadium"` or `"stadium_xl"`. Ask the user which they
    have if you don't know — never guess.

    Categories: amp, cab, drive, delay, reverb, modulation, filter, eq,
    dynamics, pitch, volume, send. Output is grouped by category with one
    block per line as `<display_name>  [<model_id>]`.
    """
    return _tools.list_blocks_handler(_resolve_library(), model, category=category)


@app.tool()
def show_block(model: str, name_or_id: str) -> str:
    """Show a Helix block's parameter schema: types, defaults, observed ranges.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    Accepts the display name (e.g. "Brit Plexi Brt"), the model id
    (e.g. "HD2_AmpBritPlexiBrt"), or an alias. **Always call this before
    writing params for a block** — param names are case-sensitive and the
    generator rejects unknown ones.
    """
    return _tools.show_block_handler(_resolve_library(), model, name_or_id=name_or_id)


@app.tool()
def generate_preset(model: str, recipe: dict[str, Any]) -> EmbeddedResource:
    """Generate a Helix Stadium .hsp preset from an inline JSON recipe.

    Required `model`: `"stadium"` or `"stadium_xl"`. Confirm the user's
    device before calling — see the `setup` skill.

    The recipe follows the helixgen schema (see https://github.com/sheax0r/helixgen):
    a `name`, optional `author`, 1-2 `paths` each with `blocks`, and optional
    `snapshots` / `footswitches` / `expression`. It is built directly against
    the library's Stadium chassis — no sidecar spec file is written; the
    returned `.hsp` blob is the sole source of truth.

    **IR usage:** `With Pan` blocks accept an `ir` field with either a
    basename (resolved via the local IR mapping) or a 32-char hex hash
    (used literally). For factory IRs, use a `Mic Ir_*` cab block.

    **After generating with user IRs:** remind the user the IRs must be
    loaded onto the device via the Helix Stadium app's Librarian (Cab IRs →
    Import) before the preset will load correctly.

    **On param errors:** if the error message says `Unknown param(s)`, call
    `show_block` with the offending block name to retrieve the correct param
    names, then retry `generate_preset` with the corrected recipe. Param
    names are case-sensitive.

    Returns an MCP `EmbeddedResource` whose `resource.blob` is the base64-
    encoded `.hsp` bytes; `resource.uri` is `file:///<sanitized-name>.hsp`.
    To apply further surgical edits, pass `resource.blob` as `hsp_b64` to
    `patch_preset`.
    """
    result = _tools.generate_preset_handler(_resolve_library(), model, recipe=recipe)
    return EmbeddedResource(
        type="resource",
        resource=BlobResourceContents(
            uri=f"file:///{result['name']}",
            mimeType=result["mimeType"],
            blob=result["hsp_b64"],
        ),
    )


@app.tool()
def list_irs(model: str) -> str:
    """List user impulse responses (IRs) registered with helixgen.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    Returns text with one line per IR: `<hash>  <wav-path>`, or an empty
    string when no IRs are registered. Call this before deciding between
    an IR cab (`With Pan` + `ir`) vs. a stock cab (`Mic Ir_*`): empty
    result → use a stock cab; non-empty → an IR is available and can be
    referenced by basename (e.g. `"YA VX30 212 BLU Mix 01.wav"`).
    """
    return _tools.list_irs_handler(model)


@app.tool()
def compute_irhash(model: str, wav_b64: str) -> dict[str, str]:
    """Compute Helix Stadium's IR hash for a base64-encoded WAV file.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    Stateless. Takes the WAV bytes as base64 (drag-and-drop friendly), runs
    them through Stadium's exact import-preprocessing pipeline, and returns
    the 32-char hex hash that would appear in a generated preset's `irhash`
    field. Embed the returned hash in the `ir` field of a `With Pan` block
    in a subsequent `generate_preset` call.

    **Validation (security):** rejects files larger than 2 MB and files
    that don't start with `RIFF`/`WAVE` magic before calling libsndfile.

    **48 kHz sources only.** Non-48 kHz raises a clear error suggesting
    `sox in.wav -r 48000 out.wav`. Stereo input is reduced to the left
    channel (matches Stadium's import).

    Returns `{"irhash": "<32-char hex>", "reminder": "<upload-to-device note>"}`.
    Always surface the `reminder` text to the user — the hash is meaningless
    unless the matching WAV is also loaded onto their device's Cab IRs.
    """
    return _tools.compute_irhash_handler(model, wav_b64)


@app.tool()
def discover_irs(model: str, ir_directory: str) -> list[dict[str, str]]:
    """Walk a local directory and return Stadium hashes for every WAV in it.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    Walks the server's filesystem under `ir_directory` for `.wav` files.

    Returns a list of `{"hash", "path", "basename"}` dicts, sorted by
    basename. Files that fail to hash (non-48 kHz, libsndfile errors)
    are skipped silently — callers get the successful subset only.
    Equivalent to `helixgen ir-scan` from the CLI, but stateless (does
    not write to `mapping.json`).
    """
    return _tools.discover_irs_handler(model, ir_directory)


@app.tool()
def register_ir(model: str, wav_path: str, force: bool = False) -> dict[str, str]:
    """Compute the Stadium hash for a local WAV and persist it to `mapping.json`.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    Writes to the server's `mapping.json` (under `$HELIXGEN_IRS` or
    `~/.helixgen/irs/`). Idempotent for the same `(hash, canonical_path)` pair. If the hash is
    already mapped to a different path, raises unless `force=True`.

    Equivalent to `helixgen register-irs <wav>` from the CLI. After this
    call, the same WAV can be referenced by basename in a `generate_preset`
    spec's `ir` field.

    Returns `{"hash": "<32-char hex>", "path": "<canonical>",
    "reminder": "<upload-to-device note>"}`. Always surface the `reminder`
    text to the user — the mapping only matters if the same WAV is also
    loaded onto their device's Cab IRs.
    """
    return _tools.register_ir_handler(model, wav_path, force=force)


@app.tool()
def register_irs(model: str, ir_directory: str, force: bool = False) -> dict[str, list]:
    """Bulk-register every WAV under a local directory to `mapping.json`.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    Recursive. Reads each `*.wav` under `ir_directory`, computes its
    Stadium hash, and registers it. Single `mapping.json` write at the
    end — far cheaper than calling `register_ir` per file when the
    library is large.

    Returns a per-category summary so the agent can report counts and
    surface anything notable to the user::

        {
          "registered":          ["new1.wav", "new2.wav", ...],
          "already_registered":  ["was-here.wav", ...],
          "conflicts":           ["dup.wav", ...],
          "failed":              [{"basename": "bad.wav", "reason": "..."}, ...],
        }

    `conflicts` is files whose hash already maps to a different path; with
    `force=True` they go into `registered` instead. `failed` is per-file
    hashing errors (non-48 kHz, libsndfile errors) — those don't abort the
    bulk run, so the partial successful subset is still persisted.

    Equivalent to `helixgen ir-scan <dir>` from the CLI.
    """
    return _tools.register_irs_handler(model, ir_directory, force=force)


@app.tool()
def view_preset(model: str, hsp_b64: str) -> dict[str, Any]:
    """Project a base64-encoded Stadium .hsp into a readable dict for agents/humans.

    Use this to inspect an orphan/ingested preset's blocks, params,
    snapshots, footswitches, and expression wiring before deciding what to
    edit with `patch_preset`. Read-only — never writes a sidecar file; the
    `.hsp` blob itself remains the sole source of truth.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    `hsp_b64` is the base64-encoded bytes of a `.hsp` file (the same blob
    returned by `generate_preset`'s `resource.blob`, or `patch_preset`'s
    `hsp_b64`).

    Returns a spec-shaped dict (`name`, `paths[*].blocks`, `snapshots`,
    `footswitches`, `expression`, ...) for comprehension only — it is NOT
    accepted back into `patch_preset` or `generate_preset`; edit the `.hsp`
    blob itself via `patch_preset`'s `operations`.
    """
    return _tools.view_preset_handler(_resolve_library(), model, hsp_b64)


@app.tool()
def controller_mapping(model: str) -> list[dict[str, Any]]:
    """Return the device's assignable controllers with English names + positions.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    Returns a JSON list of records — one per assignable control (FS1–FS5,
    FS7–FS11, EXP1, EXP2, EXP1Toe) — each with the identifier (`id`), source id
    (`source` hex + `source_id` int), `kind`, grid `row`/`col`, canonical
    `name`, `position` phrase, a rendered `english` string (e.g.
    `"Footswitch 5 (top row, 5th from left)"`), and `aliases`.

    Use this to (a) render any controller to the user in plain English rather
    than a bare `FS#`, and (b) feed the English→identifier translation
    sub-agent when a user describes a switch in free text. Reserved switches
    (FS6 = MODE, FS12 = TAP/Tuner) are intentionally NOT assignable and are
    excluded from the list.
    """
    return _tools.controller_mapping_handler(model)


@app.tool()
def patch_preset(model: str, hsp_b64: str, operations: list) -> dict[str, Any]:
    """Apply surgical edits directly to a base64-encoded .hsp blob.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    `hsp_b64` is the base64-encoded bytes of a `.hsp` file (from
    `generate_preset`'s `resource.blob`, a prior `patch_preset` call's
    `hsp_b64`, or a user-supplied orphan export). Each op in `operations`
    mutates the decoded body directly (no spec round-trip) via the matching
    `helixgen.mutate` verb. Supported ops:
    - `set_param` — `{op, block, param, value, [path], [lane], [pos]}`
    - `set_enabled` — `{op, block, enabled, [path], [lane], [pos], [snapshot]}`
    - `add_block` — `{op, block, [path], [after], [params]}`
    - `remove_block` — `{op, block, [path], [lane], [pos]}`
    - `swap_model` — `{op, old, new, [path], [lane], [pos]}`

    `[lane]`/`[pos]` disambiguate a block address when more than one placed
    block shares the same display name (e.g. a dual-cab block or a block
    duplicated across a parallel split) — same semantics as the CLI's
    `--lane`/`--pos` flags. An address that cannot be resolved uniquely
    raises a clear "matches N placements" error listing the candidates.
    Call `show_block` first to confirm exact, case-sensitive param names.

    Returns `{"hsp_b64": <base64 .hsp bytes reflecting every op>, "warnings":
    [<str>, ...]}`. `warnings` collects any `swap_model` messages about
    params/IRs that couldn't be carried over. Pass the returned `hsp_b64`
    into another `patch_preset` call to keep editing, or `view_preset` to
    inspect the result.
    """
    return _tools.patch_preset_handler(_resolve_library(), model, hsp_b64, operations)


# ---------------------------------------------------------------------------
# device_* tools — drive a networked Line 6 Helix Stadium over the LAN.
#
# Thin wrappers over the `device_*_handler` functions in `mcp_server.tools`,
# which lazily import `helixgen.device.HelixClient` (the optional `device`
# extra: pyzmq + msgpack). `ip` defaults to the user's device; pass it to
# target another. Device/RPC failures surface as MCP errors (ValueError).
# ---------------------------------------------------------------------------


@app.tool()
def device_list_presets(
    model: str, ip: str = _tools._DEFAULT_DEVICE_IP, setlist: str = "user"
) -> list[dict[str, Any]]:
    """List presets in a setlist on the networked Helix Stadium.

    Required `model`: `"stadium"` or `"stadium_xl"`. `ip` is the device's LAN
    address; `setlist` is one of `"user"` (default), `"factory"`,
    `"throwaway"`.

    Returns the device's raw preset records — each with `cid_` (content id,
    used by the other `device_*` tools), `name`, `cctp` (content type), and
    `posi` (slot position) — sorted by slot.
    """
    return _tools.device_list_presets_handler(model, ip=ip, setlist=setlist)


@app.tool()
def device_list_setlists(
    model: str, ip: str = _tools._DEFAULT_DEVICE_IP
) -> list[dict[str, Any]]:
    """List the device's virtual setlist containers (user/factory/throwaway).

    Required `model`: `"stadium"` or `"stadium_xl"`. Returns one record per
    setlist container that currently resolves on the device.
    """
    return _tools.device_list_setlists_handler(model, ip=ip)


@app.tool()
def device_read_preset(
    model: str, cid: int, ip: str = _tools._DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Read a single preset's attributes (content reference) by its `cid`.

    Required `model`: `"stadium"` or `"stadium_xl"`. `cid` is a content id
    from `device_list_presets`. Errors if the device has no content at `cid`.
    """
    return _tools.device_read_preset_handler(model, ip=ip, cid=cid)


@app.tool()
def device_load_preset(
    model: str, cid: int, ip: str = _tools._DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Load a preset (by `cid`) into the device's edit buffer.

    Required `model`: `"stadium"` or `"stadium_xl"`. Returns `{"ok": <bool>}`
    reflecting the device's acknowledgement.
    """
    return _tools.device_load_preset_handler(model, ip=ip, cid=cid)


@app.tool()
def device_create_preset(
    model: str,
    src_cid: int,
    pos: int,
    ip: str = _tools._DEFAULT_DEVICE_IP,
    setlist: str = "user",
) -> dict[str, Any]:
    """Create a preset by copying `src_cid` into `setlist` at slot `pos`.

    Required `model`: `"stadium"` or `"stadium_xl"`. `setlist` is one of
    `"user"` (default), `"factory"`, `"throwaway"`. Returns
    `{"ok": <bool>, "cid": <new content id or null>}`.
    """
    return _tools.device_create_preset_handler(
        model, ip=ip, src_cid=src_cid, setlist=setlist, pos=pos
    )


@app.tool()
def device_rename_preset(
    model: str, cid: int, name: str, ip: str = _tools._DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Rename the preset at `cid` to `name` on the device.

    Required `model`: `"stadium"` or `"stadium_xl"`. Returns `{"ok": <bool>}`.
    """
    return _tools.device_rename_preset_handler(model, ip=ip, cid=cid, name=name)


@app.tool()
def device_delete_preset(
    model: str, cid: int, ip: str = _tools._DEFAULT_DEVICE_IP, setlist: str = "user"
) -> dict[str, Any]:
    """Delete the preset at `cid` from `setlist` on the device.

    Required `model`: `"stadium"` or `"stadium_xl"`. `setlist` is one of
    `"user"` (default), `"factory"`, `"throwaway"`. Returns `{"ok": <bool>}`.
    """
    return _tools.device_delete_preset_handler(model, ip=ip, cid=cid, setlist=setlist)


@app.tool()
def device_set_param(
    model: str,
    path: int,
    block: int,
    param_id: int,
    value: float,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Set one param in the device's live edit buffer.

    Required `model`: `"stadium"` or `"stadium_xl"`. `path`/`block`/`param_id`
    are the device's numeric coordinates for the target param; `value` is the
    normalized float. Returns `{"ok": <bool>}`.
    """
    return _tools.device_set_param_handler(
        model, ip=ip, path=path, block=block, param_id=param_id, value=value
    )


@app.tool()
def device_save_preset(
    model: str,
    name: str,
    pos: int,
    setlist: str = "user",
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Save the device's CURRENT edit buffer as a new preset (Save As New).

    Required `model`: `"stadium"` or `"stadium_xl"`. Saves the live edit buffer
    into `setlist` at slot `pos` (which must be empty) under `name`. Returns
    `{"ok": <bool>, "cid": <new cid>}`.
    """
    return _tools.device_save_preset_handler(
        model, ip=ip, name=name, setlist=setlist, pos=pos
    )
