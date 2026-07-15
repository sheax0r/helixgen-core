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
def generate_preset(model: str, recipe: dict[str, Any], out_path: str) -> dict[str, Any]:
    """Generate a Helix Stadium .hsp preset from a JSON recipe and write it to disk.

    Required `model`: `"stadium"` or `"stadium_xl"`. Confirm the user's
    device before calling — see the `setup` skill.

    `out_path` is the filesystem path to write the `.hsp` to (required; parent
    directories are created). The recipe follows the helixgen schema
    (see https://github.com/sheax0r/helixgen): a `name`, optional `author`,
    1-2 `paths` each with `blocks`, and optional
    `snapshots` / `footswitches` / `expression`. It is built directly against
    the library's Stadium chassis — no sidecar spec file is written; the
    written `.hsp` file is the sole source of truth.

    **Signal-flow depth:** each path's `input` may be a mode string
    (`"inst1"`/`"inst2"`/`"both"`/`"none"`) OR an object adding `impedance`
    (`"FirstBlock"`/`"FirstEnabled"`/`"10K"`…`"1M"`), `pad` (bool), `trim`
    (dB), `gate` (bool or `{enabled, threshold, decay}`), and (stereo only)
    `link`; a path may carry `output: {level, pan}` (lane-0 endpoint).
    A `split` entry takes a friendly `type` (`y`/`ab`/`crossover`/`dynamic`)
    with per-type params (e.g. crossover `Frequency`/`Reverse`, dynamic
    `Threshold`/`Attack`/`Decay`); a `join` entry takes the merge-mixer params
    (`"A Level"`, `"A Pan"`, `"B Level"`, `"B Pan"`, `"B Polarity"`,
    `"Level"` — literal names with spaces). Delay/reverb/FX-Loop blocks accept
    `trails` (spillover on bypass).

    **MIDI CC control (EXPERIMENTAL, #33):** an optional top-level `midi` list
    binds incoming MIDI Control Change messages — each entry is `{"cc": 0-127,
    "targets": [...]}` where a target sweeps a param
    (`{"block", "param", "min", "max"}`) or toggles a block's bypass
    (`{"block", "bypass": true}`). CC-only (MIDI Note is out of scope). The
    binding is realized on `device install`/`sync` (transcoded into the device
    content); it is not written as a device-native `.hsp` controller. One param
    is driven by at most one controller across footswitch/expression/MIDI.

    **Command Center (EXPERIMENTAL, #16):** an optional top-level `commands`
    list binds a footswitch or Instant slot to a command sent on press — a MIDI
    message or a snapshot action (targets the device / external gear, not
    a block). Each entry is `{"switch": "FS1".."FS11"|"Instant1".."Instant6",
    "command": <family>, ...fields}`: `midi_cc` (`cc`,`value`,`channel`),
    `midi_pc` (`program`,`channel`,`bank_msb`/`bank_lsb`), `midi_note`
    (`note`,`velocity`,`channel`,`note_off`), `midi_mmc` (`message`,`channel`),
    `snapshot` (`snapshot` 0-7); plus optional `behavior`/`toggle`/`label`/
    `color`. Same-switch entries merge (max 2). A recall-preset family is
    deferred (unanchored). Authored NATIVELY into `preset.commands` (the
    encoding real exports carry) and transcoded to the device `cg__.entt` on
    install/sync. Storage HW-validated; audible response uncharacterized.
    Reserved `FS6`/`FS12` and a switch shared with a `footswitches` entry are
    rejected.

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

    Returns `{"path": <out_path>, "warnings": [...]}`. Pass `out_path` to
    `view_preset` / `patch_preset` to inspect or edit the written file.
    """
    return _tools.generate_preset_handler(
        _resolve_library(), model, recipe=recipe, out_path=out_path)


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
def compute_irhash(model: str, wav_path: str) -> dict[str, str]:
    """Compute Helix Stadium's IR hash for a WAV file on disk.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    `wav_path` is a filesystem path to a `.wav` file. Runs it through Stadium's
    exact import-preprocessing pipeline and returns the 32-char hex hash that
    would appear in a generated preset's `irhash` field. Embed the returned
    hash in the `ir` field of a `With Pan` block in a subsequent
    `generate_preset` call.

    **48 kHz sources only.** Non-48 kHz raises a clear error suggesting
    `sox in.wav -r 48000 out.wav`. Stereo input is reduced to the left channel
    (matches Stadium's import). Rejects files without `RIFF`/`WAVE` magic before
    calling libsndfile.

    Returns `{"irhash": "<32-char hex>", "reminder": "<upload-to-device note>"}`.
    Always surface the `reminder` text to the user — the hash is meaningless
    unless the matching WAV is also loaded onto their device's Cab IRs.
    """
    return _tools.compute_irhash_handler(model, wav_path)


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
def view_preset(model: str, hsp_path: str) -> dict[str, Any]:
    """Project a Stadium `.hsp` file into a readable dict for agents/humans.

    Use this to inspect an orphan/ingested preset's blocks, params, snapshots,
    footswitches, and expression wiring before deciding what to edit with
    `patch_preset`. Read-only — never writes; the `.hsp` file remains the sole
    source of truth.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    `hsp_path` is a filesystem path to a `.hsp` file (the file written by
    `generate_preset`, edited by `patch_preset`, or a user-supplied export).

    Returns a spec-shaped dict (`name`, `paths[*].blocks`, `snapshots`,
    `footswitches`, `expression`, ...) for comprehension only — it is NOT
    accepted back into `patch_preset` or `generate_preset`; edit the `.hsp`
    file itself via `patch_preset`'s `operations`.
    """
    return _tools.view_preset_handler(_resolve_library(), model, hsp_path)


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
def patch_preset(model: str, hsp_path: str, operations: list) -> dict[str, Any]:
    """Apply surgical edits to a `.hsp` file, in place.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    `hsp_path` is a filesystem path to a `.hsp` file (written by
    `generate_preset`, a prior `patch_preset` call, or a user-supplied orphan
    export). Each op in `operations` mutates the file's body directly (no spec
    round-trip) via the matching `helixgen.mutate` verb. Supported ops:
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

    **Signal-flow pseudo-blocks:** `set_param` also accepts the block names
    `input` / `output` / `split` / `join` (`merge` = alias), which address
    the path's endpoints / split / merge mixer instead of a library block
    (`path` selects the DSP, default 0; `pos` disambiguates two splits).
    Input params use the recipe vocabulary (`impedance`, `pad`, `trim`,
    `gate`, `threshold`, `decay`, `link`); output params are `level`/`pan`;
    split/join params are the literal wire names (`BalanceA`, `Frequency`,
    `"A Level"`, …).

    Returns `{"path": <the same hsp_path, now edited in place>, "warnings":
    [<str>, ...]}`. `warnings` collects any `swap_model` messages about
    params/IRs that couldn't be carried over. Call `patch_preset` again on the
    same path to keep editing, or `view_preset` to inspect the result.
    """
    return _tools.patch_preset_handler(_resolve_library(), model, hsp_path, operations)


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
def device_info(model: str, ip: str = _tools._DEFAULT_DEVICE_IP) -> dict[str, Any]:
    """Show the connected Helix device's identity: model, firmware, serial,
    storage.

    Required `model`: `"stadium"` or `"stadium_xl"`.

    Read-only (`/ProductInfoGet` -- part of the editor's own connect
    handshake); never touches presets or the edit buffer. Returns
    `{"model", "device_id", "helixgen_model", "serial", "firmware",
    "firmware_build", "firmware_date", "sd_total_bytes",
    "sd_available_bytes", "raw"}` -- `helixgen_model` is the chassis key
    (`"stadium_xl"`) when the numeric device id is recognized. CLI mirror:
    `helixgen device info`.
    """
    return _tools.device_info_handler(model, ip=ip)


@app.tool()
def device_settings_list(
    page: str | None = None,
    values: bool = False,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """List the device's **Global Settings** property keys, grouped by page
    (ins-outs, switches-pedals, displays, preferences, songs, tempo-click,
    midi, date-time, tuner, wireless).

    Offline by default (bundled catalog). Pass `values=True` to also fetch each
    key's live value + range from the device. `page` narrows to one page.
    Returns `{"pages": {...}}` or, with values, `{"settings": [...]}`.
    """
    return _tools.device_settings_list_handler(page=page, values=values, ip=ip)


@app.tool()
def device_settings_get(
    key: str,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Read one Global-Settings value over the network, with its definition.

    `key` is a device property key like `global.tuner.reference.pitch` or
    `global.midi.channel` (see `device_settings_list`). Returns the current
    value plus name, type, min/max, default, and enum labels.
    """
    return _tools.device_settings_get_handler(key, ip=ip)


@app.tool()
def device_settings_set(
    key: str,
    value: str,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Write one Global-Settings value over the network (no Stadium app needed).

    `key` is a device property key (see `device_settings_list`). `value` may be
    a number, or for enum settings a label (e.g. `"Strobe"`) or its index. The
    value is validated against the property's range/enum before sending.
    Returns `{"ok", "key", "value", "display", "name"}`.
    """
    return _tools.device_settings_set_handler(key, value, ip=ip)


@app.tool()
def device_globaleq_list() -> dict[str, Any]:
    """List the device's **Global EQ** outputs, bands, and valid params (offline).

    The Stadium has three Global EQs — `qtr` (1/4"), `xlr`, `pho` (Phones) — each
    a 7-band EQ (lowcut, lowshelf, low, mid, high, highshelf, highcut) plus an
    output level. Returns `{"outputs": {...}, "catalog": [...]}`. Global EQ is
    write-only over the network (no read-back), so there is no globaleq get tool.
    """
    return _tools.device_globaleq_list_handler()


@app.tool()
def device_globaleq_set(
    output: str,
    band: str,
    param: str,
    value: str,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Write one **Global EQ** parameter over the network (no Stadium app needed).

    `output` ∈ qtr/xlr/pho. `band` ∈ lowcut/lowshelf/low/mid/high/highshelf/
    highcut (or "" with `param="level"` for the output level). `param` ∈
    enable/freq/gain/q/slope/level. Value is validated against the band's param
    set before sending. Returns `{"ok", "key", "value"}`.
    """
    return _tools.device_globaleq_set_handler(output, band, param, value, ip=ip)


@app.tool()
def device_tuner(
    seconds: float = 3.0,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Read the Stadium's live pitch (network tuner) — no Stadium app needed.

    Samples the device's always-on background pitch detector on port 2003 for
    `seconds` and returns the latest reading `{signal, note, cents, hz, midi,
    samples}`. `signal=False` means silence (no note detected). Have a note
    ringing when you call. The hardware tuner does NOT need to be engaged.
    """
    return _tools.device_tuner_handler(seconds=seconds, ip=ip)


@app.tool()
def device_snapshot(
    index: int,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Recall a snapshot (0-based, 0..7) on the live device — changes the ACTIVE
    tone's snapshot now (`/activateSnapshot`). Returns `{ok, index}`."""
    return _tools.device_snapshot_handler(index, ip=ip)


@app.tool()
def device_blocks(
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """List the live edit buffer's blocks with `(path, block)` coordinates, model,
    and on/off state — the coordinates `device_bypass` / `device_model` /
    `device_set_param` address. Read-only. Returns `{blocks: [...]}`."""
    return _tools.device_blocks_handler(ip=ip)


@app.tool()
def device_bypass(
    path: int,
    block: int,
    enable: bool,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Enable (`enable=True`) or bypass (`False`) a block in the live edit buffer
    (`/BlockEnableSet`). Coordinates from `device_blocks`. Changes the ACTIVE
    tone. Returns `{ok, path, block, enabled}`."""
    return _tools.device_bypass_handler(path, block, enable, ip=ip)


@app.tool()
def device_model(
    path: int,
    block: int,
    model: str,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Set a block's model in the live edit buffer (`/ModelSet`). `model` is a
    numeric model id or a model-id string (e.g. `HD2_AmpBritPlexiNrm`). The
    device rejects a cross-category swap. Changes the ACTIVE tone. Returns
    `{ok, path, block, model, model_id}`."""
    return _tools.device_model_handler(path, block, model, ip=ip)


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


@app.tool()
def device_install_preset(
    model: str,
    hsp_path: str,
    name: str,
    pos: int,
    setlist: str = "user",
    auto_irs: bool = True,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Author a helixgen `.hsp` file onto the device as a new preset.

    Required `model`: `"stadium"` or `"stadium_xl"`. `hsp_path` is a filesystem
    path to a `.hsp` file; it is read off disk, transcoded straight into the
    device's native content format (any block chain, full fidelity, no
    template), and installed into `setlist` slot `pos` (must be empty).

    `auto_irs` (default `True`): any IRs the preset references that aren't
    already on the device are uploaded FIRST, resolved via the local IR
    mapping.json then pushed (instant registration) — the same shared core
    `device sync` / CLI `device install --auto-irs` use — so the installed
    preset's cabs resolve immediately. Set `auto_irs=False` to skip this.

    EXPERIMENTAL. Returns `{"ok": <bool>, "cid": <new cid>, "irs": [...]}`.
    `irs` is `[]` when the preset references no IRs (or none were missing);
    otherwise one result dict per IR: `{hash, ok, outcome, note, ...}` — a
    non-ok entry (unregistered locally, hash mismatch, upload failure, or,
    with `auto_irs=False`, `outcome: "skipped_auto_irs_off"`) means that cab
    may come up silent until it's imported.
    """
    return _tools.device_install_preset_handler(
        model, ip=ip, hsp_path=hsp_path, name=name, pos=pos,
        setlist=setlist, auto_irs=auto_irs,
    )


@app.tool()
def device_import_hss(
    model: str,
    hss_path: str,
    setlist: str | None = None,
    list_only: bool = False,
    dry_run: bool = False,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Import a `.hss` setlist-bundle export (backlog #31, READ side). EXPERIMENTAL.

    Required `model`: `"stadium"` or `"stadium_xl"`. `hss_path` is a
    filesystem path to a `.hss` file (path-based, no base64). A `.hss` is the
    Stadium app's "export setlist" file.

    `list_only=True` decodes the bundle fully offline (no device needed) and
    returns `{ok, name, device_id, device_version, mtime, slots: [{pos, filled,
    name, format}]}` — use this first to see what's inside before importing.
    `format` is `"hsp"` (a real export's payload), `"sbepgsm"` (a content
    blob), or `"unknown"`.

    Otherwise each filled slot is installed into the device pool
    (non-activating) and referenced into a device setlist (`setlist`, or the
    bundle's own name if omitted; created if absent) in the bundle's slot
    order. `dry_run=True` returns the plan (`would_install`, each entry with
    a `would_skip` flag for unrecognizable payloads) without writing.
    Per-slot failures are reported in `errors` without aborting the rest.
    Returns `{ok, setlist, cid, created, installed, warnings, errors}` (plus
    `manifest_warnings` on local name conflicts). `warnings` carries per-slot
    manifest-type/payload disagreements.

    Imported presets ARE recorded in the local tone library as pathless
    tones (source `import-hss`) with membership in the destination setlist,
    so a later `device_sync_setlist` keeps their references. NOT idempotent
    on retry: re-running after a partial failure DUPLICATES the
    already-succeeded slots (pool presets + references) — delete the setlist
    and orphaned pool presets, or import into a fresh setlist, before
    retrying.

    Both the container and the filled-slot framing are pinned against real
    captured exports (a filled slot embeds the preset's `.hsp`, transcoded to
    device content on import) — see `src/helixgen/device/hss.py`.
    """
    return _tools.device_import_hss_handler(
        model, ip=ip, hss_path=hss_path, setlist=setlist,
        list_only=list_only, dry_run=dry_run,
    )


@app.tool()
def device_export_hss(
    model: str,
    setlist: str,
    out_path: str,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Export a DEVICE setlist to a `.hss` bundle (backlog #31). EXPERIMENTAL.

    Required `model`: `"stadium"` or `"stadium_xl"`. Reads the named device
    setlist's references and writes a `.hss` to `out_path` (path-based, no
    base64), embedding each referenced preset's local `.hsp` (resolved by
    preset name via the tone library) verbatim — mirroring how the Stadium app
    embeds a `.hsp` per preset. The container framing (24-byte header, gzip
    header, ustar layout) is byte-faithful to a real app export; the gzip
    DEFLATE stream differs (non-zlib app encoder), and helixgen embeds
    compact-JSON `.hsp` where the app pretty-prints — same format family,
    re-importable.

    A referenced preset with NO local `.hsp` (device-born or untracked by the
    tone library) is SKIPPED — helixgen has no device-content → `.hsp`
    converter (backlog #31 residual). The `.hss` is still written with the
    presets that resolved. Returns `{ok, setlist, path, embedded, skipped,
    bytes}` (`ok` is `not skipped`).
    """
    return _tools.device_export_hss_handler(
        model, ip=ip, setlist=setlist, out_path=out_path,
    )


@app.tool()
def device_setlist_list(model: str) -> dict[str, Any]:
    """Return the local setlist manifest (desired membership + observed state).

    Required `model`: `"stadium"` or `"stadium_xl"`. Reads
    `~/.helixgen/setlists.json` and returns its full document
    (`{version, tones, setlists, observed}`). Local-only — never touches the
    device. Use it to see which authored tones belong to which setlist before
    calling `device_sync_setlist` / `device_sync_all`.
    """
    return _tools.device_setlist_list_handler(model)


@app.tool()
def device_setlist_add(
    model: str, setlist: str, hsp_path: str, pos: int | None = None
) -> dict[str, Any]:
    """Register an authored `.hsp` tone and add it to a setlist's membership.

    Required `model`: `"stadium"` or `"stadium_xl"`. `hsp_path` is a filesystem
    path to a `.hsp` file (path-based, no base64); its `meta.name` becomes the
    tone name. Appends the tone to `setlist` (at `pos` if given; the setlist is
    auto-created in the manifest if new).

    Membership semantics (safe to call in bulk without pre-checking):
    - **The same tone may belong to many setlists** — adding a tone that already
      exists (e.g. to both `library` and `Sarah`) is expected and correct; it is
      referenced once in the device pool and shared. NOT a duplicate error.
    - **Idempotent within a setlist** — re-adding a tone already in `setlist` is a
      no-op (never duplicated); re-adding the same file refreshes its content
      hash.
    - The ONLY rejection is a name collision on a *different* file: if the tone's
      `meta.name` is already registered to a different `.hsp` path, it raises
      (names must be unique in the manifest) — rename the tone or reuse the entry.

    Local-only — writes `~/.helixgen/setlists.json`; run `device_sync_setlist`
    to push it to the device. Returns `{ok, setlist, tone, tones}`.
    """
    return _tools.device_setlist_add_handler(model, setlist, hsp_path, pos=pos)


@app.tool()
def device_setlist_remove(
    model: str, setlist: str, tone_name: str
) -> dict[str, Any]:
    """Drop a tone from a setlist's membership in the local manifest.

    Required `model`: `"stadium"` or `"stadium_xl"`. Removes `tone_name` from
    `setlist`. The tone stays in the registry if another setlist still uses
    it, or if it carries an explicit device mark (`device add` / a concrete
    slot). An implicit mark (auto-stamped when it joined a synced setlist)
    dies with its last membership, so add-then-remove is a no-op. Local-only. Returns `{ok, setlist, tone, tones}` — `ok` is False if the
    tone wasn't in that setlist.
    """
    return _tools.device_setlist_remove_handler(model, setlist, tone_name)


@app.tool()
def device_sync_setlist(
    model: str,
    setlist: str,
    ip: str = _tools._DEFAULT_DEVICE_IP,
    exclude_irs: bool = False,
    repush: bool = False,
) -> dict[str, Any]:
    """Sync ONE manifest setlist onto the device (pool-first, reference rebuild).

    Required `model`: `"stadium"` or `"stadium_xl"`. Reconciles the preset pool
    for the tones `setlist` needs PLUS every slot-marked tone (a `device add`ed
    tone installs even with no setlist membership; pathless save/create tones
    absent from the pool are left alone), then rebuilds that setlist's
    references to match manifest order — never orphaning a still-referenced
    pool preset. Unsynced manifest tones (slot=null) that helixgen previously
    placed (and only those — a same-named preset helixgen didn't place is never
    touched) are deleted from the device, kept in the library; never-orphan
    skips are reported in `pool.delete_skipped`. Targeting a setlist marks it
    `synced` (mirrored by future `device_sync_all` runs). Uploads each tone's
    IRs unless `exclude_irs=True`. `repush=True` forces every in-scope tone
    already in the pool to be re-transcoded + re-pushed (via the same
    non-activating existing-cid content update a normal hash-triggered update
    uses) even when its recorded `.hsp` hash still matches — use this once
    after a helixgen transcoder upgrade to refresh tones a plain sync would
    otherwise skip as unchanged. A single-setlist sync never
    garbage-collects. EXPERIMENTAL. Returns the engine result dict
    (`{ok, setlists, pool:{installed,updated,skipped,deleted,delete_skipped},
    references, gc, irs, errors}`).
    """
    return _tools.device_sync_setlist_handler(
        model, setlist, ip=ip, exclude_irs=exclude_irs, repush=repush,
    )


@app.tool()
def device_sync_all(
    model: str,
    ip: str = _tools._DEFAULT_DEVICE_IP,
    gc: bool = False,
    exclude_irs: bool = False,
    repush: bool = False,
) -> dict[str, Any]:
    """Sync ALL manifest setlists onto the device (the whole-library reconcile).

    Required `model`: `"stadium"` or `"stadium_xl"`. Reconciles the preset pool
    for the union of every **synced** setlist's tones (local-only drafts are
    never touched on the device) plus every slot-marked tone, rebuilds each
    synced setlist's references, deletes unsynced (slot=null) manifest tones
    that helixgen previously placed from the pool, and — only when `gc=True` —
    garbage-collects pool presets no setlist references and no slot-marked
    tone wants (never orphaning). Uploads IRs unless `exclude_irs=True`.
    `repush=True` forces every in-scope tone already in the pool to be
    re-pushed even when its recorded hash matches — see `device_sync_setlist`
    for why (a transcoder upgrade a hash comparison can't see). EXPERIMENTAL.
    Returns the engine result dict (`{ok, setlists, pool,
    references, gc, irs, errors}`).
    """
    return _tools.device_sync_all_handler(
        model, ip=ip, gc=gc, exclude_irs=exclude_irs, repush=repush,
    )


@app.tool()
def device_delete_ir(
    model: str, name_or_hash: str, ip: str = _tools._DEFAULT_DEVICE_IP,
    force_wedge: bool = False
) -> dict[str, Any]:
    """Delete ONE user IR from the device, matched by name or 32-hex hash.

    Required `model`: `"stadium"` or `"stadium_xl"`. Removes the IR's registry
    entry AND its backing .wav (best-effort — `file_removed` in the result
    says whether the file is gone; presets that referenced it show a silent
    cab until re-import). Errors when nothing (or more than one name)
    matches — use the hash from `helixgen device list-irs` to disambiguate.
    `force_wedge=True` additionally cleans the "wedged" state (a 32-hex hash
    with no registry entry but a still-resolving device file, left by a
    delete then quick re-import); the result then has `cid: None`. NEVER pass
    `force_wedge` for an IR that was just imported — its listing may merely
    be lagging behind the write. To clean up ALL unreferenced IRs at once,
    use `device_ir_prune` instead. Returns
    `{ok, cid, name, hash, file_removed}`.
    """
    return _tools.device_delete_ir_handler(
        model, ip=ip, name_or_hash=name_or_hash, force_wedge=force_wedge)


@app.tool()
def device_rename_ir(
    model: str, name_or_hash: str, new_name: str,
    ip: str = _tools._DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Rename a user IR on the device (matched by name or 32-hex hash).

    Required `model`: `"stadium"` or `"stadium_xl"`. Display-name only — the
    IR's hash (which presets reference) is untouched, so no preset breaks.
    Returns `{ok, cid, name, hash}` (`name` = the new name).
    """
    return _tools.device_rename_ir_handler(
        model, ip=ip, name_or_hash=name_or_hash, new_name=new_name)


@app.tool()
def device_ir_prune(
    model: str,
    ip: str = _tools._DEFAULT_DEVICE_IP,
    execute: bool = False,
    force: bool = False,
    ignore_warnings: bool = False,
    only: str | None = None,
) -> dict[str, Any]:
    """Delete device IRs that no preset references any more — DRY-RUN by default.

    Required `model`: `"stadium"` or `"stadium_xl"`. Diffs the device's user
    IRs against every IR hash referenced by the presets ON the device
    (non-activating content reads — the live tone is never disturbed), by the
    live edit buffer, and by the local tone library's .hsp files. Nothing is
    deleted unless `execute=True`. An IR referenced by any on-device preset
    is never a candidate. An IR referenced only by a local off-device tone is
    "protected" and needs `force=True` as well. Local tones whose recorded
    .hsp is missing/unreadable surface in `warnings` — executing over
    warnings requires `ignore_warnings=True` (a SEPARATE consent from
    `force`). `only` narrows deletion to a single IR (name-or-hash). Execute
    mode re-scans immediately before deleting and aborts if the device
    listings changed (nothing deleted; just re-run). Always run the dry-run
    first and show the user the orphans/protected lists (and any warnings)
    before executing. Returns `{ok, dry_run, device_irs, referenced,
    protected, orphans, deleted, warnings, errors}`.
    """
    return _tools.device_ir_prune_handler(
        model, ip=ip, execute=execute, force=force,
        ignore_warnings=ignore_warnings, only=only)


@app.tool()
def device_set_info(
    model: str,
    cids: list[int],
    color: str | None = None,
    notes: str | None = None,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Set preset color and/or notes on one or more preset `cids` (batch-capable).

    Required `model`: `"stadium"` or `"stadium_xl"`. `color` is a palette name
    (`auto`, `white`, `red`, `dark orange`, `light orange`, `yellow`, `green`,
    `turquoise`, `blue`, `violet`, `pink`, `off`) or a raw index 0-11. `notes`
    is the Preset Info panel text. At least one of the two is required.
    Notes are written via a NON-activating content round-trip — the device's
    live tone is never disturbed. Passing several cids batch-applies the same
    color/notes to each (the librarian's "batch color"); a failing cid does
    NOT stop the batch — it is reported as `{cid, error}` in `results` and
    `ok` goes false. Returns `{ok, results: [{cid, color?, notes?, error?}]}`.
    """
    return _tools.device_set_info_handler(
        model, ip=ip, cids=cids, color=color, notes=notes)


@app.tool()
def device_setlist_create(
    model: str, name: str, ip: str = _tools._DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Create a new EMPTY setlist ON THE DEVICE (no Stadium app needed).

    Required `model`: `"stadium"` or `"stadium_xl"`. Uses the device's own
    create command and records the setlist in the local manifest too, so a
    following `device_sync_setlist` can target it immediately. Errors if a
    setlist with that name already exists on the device. (This is the
    device-side counterpart of the local-manifest `device_setlist_add`
    family.) Returns `{ok, cid, name}`.
    """
    return _tools.device_setlist_create_handler(model, ip=ip, name=name)


@app.tool()
def device_setlist_rename(
    model: str, name: str, new_name: str, ip: str = _tools._DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Rename a setlist ON THE DEVICE (and in the local manifest, if tracked).

    Required `model`: `"stadium"` or `"stadium_xl"`. Resolves the setlist by
    case-insensitive name; errors if `name` isn't on the device or `new_name`
    already is. Returns `{ok, cid, name}` (`name` = the new name).
    """
    return _tools.device_setlist_rename_handler(
        model, ip=ip, name=name, new_name=new_name)


@app.tool()
def device_setlist_delete(
    model: str, name: str, ip: str = _tools._DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Delete a setlist ON THE DEVICE. Never deletes presets.

    Required `model`: `"stadium"` or `"stadium_xl"`. The setlist's references
    die with it, but the pool presets they point at are NEVER deleted
    (never-orphan guarantee) — every tone stays available to other setlists.
    A local manifest setlist of the same name is kept as a local-only draft.
    Confirm with the user before calling — there is no undo. Returns
    `{ok, cid, name}`.
    """
    return _tools.device_setlist_delete_handler(model, ip=ip, name=name)


@app.tool()
def device_setlist_duplicate(
    model: str, src: str, dst: str, ip: str = _tools._DEFAULT_DEVICE_IP
) -> dict[str, Any]:
    """Duplicate a setlist ON THE DEVICE: copy `src`'s references into `dst`.

    Required `model`: `"stadium"` or `"stadium_xl"`. `dst` is created on the
    device when absent (and then recorded in the local manifest, like
    `device_setlist_create`); if it already exists it must be EMPTY.
    References are pointers — the pool presets are shared, not copied, so
    editing a preset changes it in both setlists. Returns `{ok, src_cid,
    dst_cid, created, copied}`.
    """
    return _tools.device_setlist_duplicate_handler(model, ip=ip, src=src, dst=dst)


@app.tool()
def device_reorder(
    setlist: str,
    target: str,
    to_index: int,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Move a preset to a new position within a setlist (`/ReorderContainerContent`).

    `setlist` is a setlist display name (e.g. `"throwaway"`), a literal
    container cid (`-2` = the pool, whose presets also resolve by name), or the
    literal `"setlists"` to instead reorder the top-level setlist list itself
    (`target` is then a setlist name/cid; a real setlist literally named
    "setlists" must be addressed by its container cid). `target` is a preset
    display name or a literal cid; `to_index` is the new 0-based position,
    bounds-validated against the container's current length. Numeric
    `target`/`setlist` values are **cid-first**: if an item is display-named
    that digit string, the cid reading wins with an entry in the result's
    `warnings` when the cid resolves in the container, and errors (naming the
    item's real cid) when it doesn't. Direct, immediate DEVICE-side
    write — distinct from the local-manifest reorder path (`device slots
    reorder` CLI + `device_sync_setlist`), which only takes effect on the
    device on the next sync (and could reorder things right back). Returns
    `{ok, container, moved_cid, new_pos, items, warnings}`.
    """
    return _tools.device_reorder_handler(setlist, target, to_index, ip=ip)


@app.tool()
def device_meters(
    seconds: float = 3.0,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Read the Stadium's live level meters — no Stadium app needed.

    Samples the grid-level meter telemetry on port 2003 (`/dspEvent` eid_=1,
    mid_=796/800 — 128 floats each) for `seconds`, riding the same burst as
    the network tuner. Read-only. Returns `{meters: [{mid, peak, values}, …],
    samples}` — the latest reading seen per mid.
    """
    return _tools.device_meters_handler(seconds=seconds, ip=ip)


@app.tool()
def device_measure(
    seconds: float = 20.0,
    min_playing: int = 40,
    ip: str = _tools._DEFAULT_DEVICE_IP,
) -> dict[str, Any]:
    """Measure how loud the Stadium's ACTIVE tone is while the player plays.

    Read-only: samples the port-2003 telemetry for `seconds` and reduces the
    playing-gated readings (real pitch + non-silent input; hum and silence
    are ignored) to robust dB stats. TELL THE PLAYER TO PLAY STEADILY during
    the window. Returns `{seconds, n_samples, n_playing, playing_seconds,
    input_db, output_db, output_db_p75, gain_db, ok, reason}` — `gain_db`
    (chain out/in, input-invariant) is the number to compare across
    snapshots/presets when level-matching; `ok=False` + `reason` means the
    window had too little actual playing to trust (re-run it).
    """
    return _tools.device_measure_handler(
        seconds=seconds, min_playing=min_playing, ip=ip)
