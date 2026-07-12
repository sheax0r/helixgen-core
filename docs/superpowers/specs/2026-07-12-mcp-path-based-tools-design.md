# Design: path-based MCP tools (remove base64 from agent context)

**Date:** 2026-07-12
**Branch:** `worktree-feature-mcp-path-based`
**Status:** approved, ready for implementation plan

## Problem

Several helixgen MCP tools shuttle whole `.hsp` (and one WAV) payloads through
the agent as base64 strings. A `.hsp` blob round-trips through the model's
context on every generate → view → patch cycle, and the bundled `tone` skill has
to perform base64 gymnastics to compensate (extract the returned blob to
`/tmp/<slug>.hsp`, then re-read + re-encode the file to feed `patch_preset`).
This burns large amounts of context for no benefit: the MCP server runs locally
on the same machine as the files.

The CLI is already fully path-based (zero base64). This work brings the MCP
boundary in line: **base64 becomes an implementation detail the MCP server
hides.** Agents pass and receive filesystem paths; the server reads and writes
the bytes.

## Non-goals

- **Remote MCP servers.** A path-based API assumes server and files share a
  filesystem. That's the only mode in use here, and it is explicitly accepted.
  This is a hard cut — no dual base64/path mode is kept.
- **Device-read tools.** The `device_*` MCP tools (`device_read_preset`,
  `device_list_presets`, …) already return only small metadata (cids, names,
  `ok` flags), never raw content. The one large-blob device read — pulling a
  preset's `.sbe` content — is CLI-only (`device pull <cid> <outfile.sbe>`) and
  already writes to a file. Nothing pulls device content into agent memory
  today; no change needed.
- **The CLI.** Already path-based.
- **helixgen core** (`recipe`, `mutate`, `view`, `hsp`, `ir`, `device.bridge`).
  These already operate on bytes/paths. The change is confined to the MCP
  boundary.

## Scope: five MCP tools

| Tool | Today | Target |
|------|-------|--------|
| `generate_preset` | returns base64 blob in an `EmbeddedResource` | writes the `.hsp` to `out_path`, returns `{path, warnings}` |
| `view_preset` | takes `hsp_b64` | takes `hsp_path`, reads the file |
| `patch_preset` | takes + returns `hsp_b64` | takes `hsp_path`, edits **in place**, returns `{path, warnings}` |
| `device_install_preset` | takes `hsp_b64` | takes `hsp_path`, reads the `.hsp` off disk |
| `compute_irhash` | takes `wav_b64` | takes `wav_path`, reads the WAV off disk |

### 1. `generate_preset(model, recipe, out_path)`

- `out_path` is **required** (mirrors the CLI's required `-o` flag). The agent
  controls the location per the `tone` skill's naming convention.
- Writes the generated `.hsp` bytes to `out_path`.
- Returns `{"path": <out_path>, "warnings": [...]}`.
- Drops the `EmbeddedResource` / `BlobResourceContents` return type and the
  `base64.b64encode` call.
- The existing `_safe_filename` helper is no longer needed for the return value;
  the agent supplies the full path. (Keep or remove per the implementation —
  it's dead once the blob return is gone.)

### 2. `view_preset(model, hsp_path)`

- Reads `hsp_path` from disk, validates the `.hsp` magic header, projects via
  `helixgen.view.view`.
- Return shape is unchanged (the spec-shaped projection dict).

### 3. `patch_preset(model, hsp_path, operations)`

- Reads `hsp_path`, applies each op via the matching `helixgen.mutate` verb
  (mutating the body in place), writes the result back to the **same path**.
- Returns `{"path": <hsp_path>, "warnings": [...]}`.
- Matches the CLI mutate-verb mental model exactly: load `.hsp` → mutate → save
  in place. No blob round-trip.

### 4. `device_install_preset(model, hsp_path, ...)`

- Reads the `.hsp` off `hsp_path` instead of decoding a base64 blob, then hands
  the bytes to the existing `device.bridge` install path. All other params
  (`name`, `pos`, `ip`, template, auto-irs, …) unchanged.

### 5. `compute_irhash(model, wav_path)`

- Reads the WAV off `wav_path`.
- **Drops the 2 MB size cap.** That cap guarded an in-memory decode of untrusted
  base64 over the JSON-RPC channel; a local file the user points at doesn't need
  it. (`register_ir` / `discover_irs`, already path-based, impose no such cap.)
- **Keeps the RIFF/WAVE magic check** (cheap defense-in-depth before libsndfile,
  which has had CVEs).
- Returns `{"irhash": <hex>, "reminder": <upload-note>}` (unchanged shape).

## Error handling

- Missing / unreadable path → `ValueError` with an actionable message
  (`".hsp not found: <path>"`, `"wav file not found: <path>"`), matching
  `register_ir`'s existing not-found check. FastMCP renders `ValueError` as an
  MCP `isError` text content block.
- Non-`.hsp` magic → the existing `"payload is not a .hsp blob (missing magic
  header)"` error, now raised after the file read rather than after a base64
  decode.

## Ripple updates (part of this work)

- **Bundled `tone` skill** (`skills/tone/SKILL.md`) — remove the base64
  extract-to-`/tmp` / re-encode-to-patch dance. Call
  `generate_preset(out_path=…)` and `patch_preset(hsp_path=…)` directly. The
  skill gets *simpler* — this is the payoff. Update the tool-signature table and
  the "Adjusting an existing tone" section.
- **`CLAUDE.md`** — the MCP tool-description block that documents the base64
  flow (`hsp_b64`, `resource.blob`).
- **`tests/mcp_server/`** — `test_tools.py`, `test_patch_tools.py`,
  `test_protocol.py` rewritten to pass paths and assert on written files.

## Testing (TDD per repo convention)

Failing test first, then minimal implementation, per tool:

- `generate_preset`: build a recipe → call with `out_path` in a tmp dir → assert
  the file exists and starts with the `.hsp` magic; assert the returned `path`
  matches.
- `patch_preset`: copy a fixture `.hsp` to a tmp path → apply a `set_param` op →
  re-read the file and assert the value changed in place; assert returned
  `path`.
- `view_preset`: point at a fixture `.hsp` path → assert the projection dict
  shape.
- `compute_irhash`: point at a 48 kHz WAV fixture → assert the known hash.
- `device_install_preset`: unit-test the handler's file-read + arg-forwarding
  with the device client stubbed (as the existing device tests do); no live
  device.
- Error cases: nonexistent path and non-`.hsp` file each raise `ValueError`.

The 211-export round-trip and golden-output tests are unaffected — they exercise
core, not the MCP boundary.

## Release

Per `CLAUDE.md` releasing rules: bump the plugin version in both
`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` (and
conventionally the lib version in `pyproject.toml` + `src/helixgen/__init__.py`),
commit `release X.Y.Z`, PR, merge to `main`; the workflow tags and advances
`stable`. This is a **breaking** MCP-signature change, so a minor bump
(next `2.x.0`) is appropriate. Do not move `stable` or push tags by hand.
