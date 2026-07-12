# Path-Based MCP Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the five base64-shuttling MCP tools to take/return filesystem paths, so `.hsp` and WAV bytes never round-trip through agent context; then ship a release.

**Architecture:** The change is confined to the MCP boundary (`mcp_server/tools.py` handlers + `mcp_server/server.py` tool signatures/docs). helixgen core already works on bytes/paths — handlers reuse the existing `helixgen.hsp.read_hsp` / `write_hsp` / `dumps_hsp` helpers. Base64 encode/decode is removed. Ripple updates to the bundled `tone` skill, `CLAUDE.md`, and `tests/mcp_server/`.

**Tech Stack:** Python 3, FastMCP (`mcp` SDK), `click`, pytest. Pure stdlib otherwise.

## Global Constraints

- Run tests with `PYTHONPATH=$PWD/src python -m pytest` (editable install may shadow bundled code; always set PYTHONPATH). All commands run from the worktree root `/Users/michael.shea/git/helixgen/.claude/worktrees/feature-mcp-path-based`.
- Supported models: `"stadium"`, `"stadium_xl"` only — every handler calls `_validate_model(model)` first.
- Pure stdlib + `click` runtime deps only; `mcp` SDK + `click` for the server. No new runtime deps.
- TDD: failing test first, then minimal implementation, then commit. Match existing test style in `tests/mcp_server/`.
- Hard cut: no dual base64/path mode is retained. Remove `import base64` from `tools.py` once no handler uses it.
- `.hsp` magic header is `b"rpshnosj"` (`helixgen.hsp.HSP_MAGIC`), 8 bytes.
- Release is CI-automated on version bump — never move `stable` or push tags by hand.

---

### Task 1: `view_preset` → file path + shared read helper

**Files:**
- Modify: `mcp_server/tools.py` — replace `_decode_hsp_b64` with a path-based `_read_hsp_body`; rewrite `view_preset_handler`.
- Modify: `mcp_server/server.py:213-232` — `view_preset` signature + docstring.
- Test: `tests/mcp_server/test_patch_tools.py` — rewrite `test_view_preset_handler` to use a path.

**Interfaces:**
- Produces: `_read_hsp_body(hsp_path: str) -> dict[str, Any]` — reads a `.hsp` file, raises `ValueError` if missing or bad magic, returns the parsed JSON body. Reused by Tasks 2 and 5.
- Produces: `view_preset_handler(library, model, hsp_path, *, irs_dir=None) -> dict` — now takes a path.

- [ ] **Step 1: Write the failing test**

Replace `test_view_preset_handler` in `tests/mcp_server/test_patch_tools.py` (currently at lines 29-34) with:

```python
def test_view_preset_handler(hsp_library, tmp_path):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}), hsp_library, source="t")
    hsp_path = tmp_path / "m.hsp"
    hsp_path.write_bytes(dumps_hsp(preset))

    projection = tools.view_preset_handler(hsp_library, MODEL, str(hsp_path))
    assert projection["name"] == "M"
    assert projection["paths"][0]["blocks"][0]["block"] == "Tube Drive"


def test_view_preset_handler_missing_file_raises(hsp_library, tmp_path):
    import pytest
    with pytest.raises(ValueError, match="not found"):
        tools.view_preset_handler(hsp_library, MODEL, str(tmp_path / "nope.hsp"))


def test_view_preset_handler_bad_magic_raises(hsp_library, tmp_path):
    import pytest
    bad = tmp_path / "bad.hsp"
    bad.write_bytes(b"NOTMAGIC{}")
    with pytest.raises(ValueError, match="not a .hsp"):
        tools.view_preset_handler(hsp_library, MODEL, str(bad))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/test_patch_tools.py::test_view_preset_handler -v`
Expected: FAIL — `view_preset_handler` still expects a base64 blob (`binascii.Error` / wrong result).

- [ ] **Step 3: Replace `_decode_hsp_b64` with `_read_hsp_body`**

In `mcp_server/tools.py`, delete `_decode_hsp_b64` (lines 347-356) and add in its place:

```python
def _read_hsp_body(hsp_path: str) -> dict[str, Any]:
    """Read a `.hsp` file into its parsed JSON body dict.

    Raises ValueError with an actionable message if the path doesn't exist
    or the bytes don't start with the `.hsp` magic header.
    """
    p = Path(hsp_path).expanduser()
    if not p.is_file():
        raise ValueError(f".hsp not found: {hsp_path}")
    return read_hsp(p)
```

Update the imports at the top of `tools.py`: change
`from helixgen.hsp import HSP_MAGIC, dumps_hsp`
to
`from helixgen.hsp import dumps_hsp, is_hsp_bytes, read_hsp, write_hsp`
(`HSP_MAGIC` is no longer referenced; `is_hsp_bytes`/`write_hsp` are used in later tasks — add them now to avoid touching the import line repeatedly).

- [ ] **Step 4: Rewrite `view_preset_handler`**

Replace the body of `view_preset_handler` (lines 359-373) so its signature takes `hsp_path` and it calls `_read_hsp_body`:

```python
def view_preset_handler(
    library: Library, model: str, hsp_path: str, *, irs_dir: Path | None = None
) -> dict[str, Any]:
    """Read a `.hsp` file and return its read-only projection dict.

    Mirrors `helixgen view`: reads the magic-prefixed JSON body off disk, then
    projects it via `helixgen.view.view`. IRs are resolved against the mapping
    at `irs_dir` (or the default `$HELIXGEN_IRS`/`~/.helixgen/irs/`).
    """
    _validate_model(model)
    body = _read_hsp_body(hsp_path)
    irs = IrMapping.load(irs_dir)
    return view_projection(body, library, irs=irs)
```

- [ ] **Step 5: Update the server tool**

In `mcp_server/server.py`, replace `view_preset` (lines 213-232) with:

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/test_patch_tools.py -v -k view`
Expected: PASS (3 view tests).

- [ ] **Step 7: Commit**

```bash
git add mcp_server/tools.py mcp_server/server.py tests/mcp_server/test_patch_tools.py
git commit -m "feat(mcp): view_preset takes a file path, not base64"
```

---

### Task 2: `patch_preset` → in-place file edit

**Files:**
- Modify: `mcp_server/tools.py` — rewrite `patch_preset_handler` (lines 427-452).
- Modify: `mcp_server/server.py:257-286` — `patch_preset` signature + docstring.
- Test: `tests/mcp_server/test_patch_tools.py` — rewrite all `patch_preset_handler` tests to write a fixture file, patch it, and re-read the file.

**Interfaces:**
- Consumes: `_read_hsp_body` (Task 1), `write_hsp` (helixgen.hsp), `dumps_hsp`.
- Produces: `patch_preset_handler(library, model, hsp_path, operations) -> {"path": str, "warnings": list[str]}` — edits the file in place.

- [ ] **Step 1: Write the failing tests**

In `tests/mcp_server/test_patch_tools.py`, update the module docstring's second paragraph to describe in-place path editing, and replace the `_hsp_b64` / `_decode_body` helpers (lines 19-26) with a single helper:

```python
def _write_preset(tmp_path, preset: dict) -> str:
    p = tmp_path / "preset.hsp"
    p.write_bytes(dumps_hsp(preset))
    return str(p)
```

Then rewrite each patch test to pass a path and re-read the file. For the representative `set_param` test (lines 37-47):

```python
def test_patch_preset_handler_set_param(hsp_library, tmp_path):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [
            {"block": "Tube Drive", "params": {"Gain": 0.5}}]}]}), hsp_library, source="t")
    path = _write_preset(tmp_path, preset)

    res = tools.patch_preset_handler(hsp_library, MODEL, path,
        [{"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.9}])

    assert res == {"path": path, "warnings": []}
    body = read_hsp(path)
    assert body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Gain"]["value"] == 0.9
```

Apply the same transform (write file → pass path → `read_hsp(path)` to assert) to the other patch tests in this file: `test_patch_preset_handler_set_param_disambiguates_by_pos`, `test_patch_preset_handler_set_enabled_disambiguates_by_lane`, `test_patch_preset_handler_remove_block_disambiguates_by_pos`, `test_patch_preset_handler_add_block`, `test_patch_preset_handler_swap_model_disambiguates_by_pos`, and `test_patch_preset_handler_unknown_op_raises`. In each, replace `_hsp_b64(preset)` with `_write_preset(tmp_path, preset)`, add `tmp_path` to the signature, and replace `_decode_body(res["hsp_b64"])` with `read_hsp(path)`. Update the top-of-file imports to add `read_hsp`:

```python
from helixgen.hsp import dumps_hsp, read_hsp
```
(drop the now-unused `HSP_MAGIC`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/test_patch_tools.py -v -k patch`
Expected: FAIL — handler still base64-decodes its 3rd arg and returns `hsp_b64`.

- [ ] **Step 3: Rewrite the handler**

In `mcp_server/tools.py`, replace `patch_preset_handler` (lines 427-452) with:

```python
def patch_preset_handler(
    library: Library, model: str, hsp_path: str, operations: list
) -> dict[str, Any]:
    """Apply a sequence of surgical edits to a `.hsp` file, in place.

    Reads `hsp_path`, applies each `{"op": ...}` entry in `operations` via the
    matching `helixgen.mutate` verb (mutating the body in place — no spec
    round-trip), then writes the result back to the same path.

    Returns `{"path": <hsp_path>, "warnings": [<str>, ...]}`. `warnings`
    collects any `swap_model` messages about params/IRs that couldn't be
    carried over to the new block.
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
```

Note: the unknown-op check runs before `write_hsp`, so a bad op leaves the file untouched — matches the existing "no partial write on error" behavior.

- [ ] **Step 4: Update the server tool**

In `mcp_server/server.py`, replace `patch_preset` (lines 257-286). Change the signature to `def patch_preset(model: str, hsp_path: str, operations: list) -> dict[str, Any]:`, update the docstring's `hsp_b64` references to `hsp_path` (a filesystem path to a `.hsp` file), change the return-value line to `Returns {"path": <the same hsp_path, now edited in place>, "warnings": [...]}`, and update the final call to:

```python
    return _tools.patch_preset_handler(_resolve_library(), model, hsp_path, operations)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/test_patch_tools.py -v`
Expected: PASS (all patch + view tests).

- [ ] **Step 6: Commit**

```bash
git add mcp_server/tools.py mcp_server/server.py tests/mcp_server/test_patch_tools.py
git commit -m "feat(mcp): patch_preset edits a .hsp file in place, not base64"
```

---

### Task 3: `generate_preset` → write to out_path, return {path, warnings}

**Files:**
- Modify: `mcp_server/tools.py` — rewrite `generate_preset_handler` (lines 119-151); remove `_safe_filename` (lines 109-116) and the `_FILENAME_SAFE` regex (line 23) if unused elsewhere.
- Modify: `mcp_server/server.py:60-98` — `generate_preset` signature/return + drop `EmbeddedResource`/`BlobResourceContents` import (line 17) if unused elsewhere.
- Test: `tests/mcp_server/test_tools.py` — rewrite the three `generate_preset_handler` tests.

**Interfaces:**
- Consumes: `generate_from_recipe`, `parse_spec`, `dumps_hsp` (already imported).
- Produces: `generate_preset_handler(library, model, recipe, out_path, *, irs_dir=None) -> {"path": str, "warnings": list[str]}`.

- [ ] **Step 1: Write the failing tests**

In `tests/mcp_server/test_tools.py`, replace `test_generate_preset_handler_returns_base64_hsp` (lines 71-102) with:

```python
def test_generate_preset_handler_writes_hsp_file(mcp_library, tmp_path):
    """Writes a .hsp file at out_path whose bytes start with HSP_MAGIC; returns its path."""
    from helixgen.hsp import HSP_MAGIC
    from mcp_server.tools import generate_preset_handler

    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    assert amps and cabs, "fixture library missing amps/cabs"

    spec = {
        "name": "MCP Test Preset",
        "paths": [{"blocks": [
            {"block": amps[0].display_name},
            {"block": cabs[0].display_name},
        ]}],
    }
    out = tmp_path / "sub" / "mcp-test.hsp"   # parent dir does not exist yet

    result = generate_preset_handler(mcp_library, "stadium_xl", recipe=spec, out_path=str(out))

    assert result == {"path": str(out), "warnings": []}
    assert out.exists()
    assert out.read_bytes().startswith(HSP_MAGIC)
```

Update the two remaining generate tests to pass `out_path` (they assert on errors raised before any write, so the file needn't exist). In `test_generate_preset_handler_rejects_unknown_param` (line 119) and `test_generate_preset_handler_rejects_malformed_spec` (line 163) and `test_generate_preset_handler_with_pan_raises_generate_error` (line 197), add a `tmp_path` fixture param and pass `out_path=str(tmp_path / "x.hsp")` to each `generate_preset_handler(...)` call. Delete `test_generate_preset_handler_sanitizes_filename` (lines 123-138) entirely — filename sanitization is gone (the agent supplies the full path).

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/test_tools.py -v -k generate`
Expected: FAIL — handler has no `out_path` param (TypeError) / still returns a base64 dict.

- [ ] **Step 3: Rewrite the handler**

In `mcp_server/tools.py`, replace `generate_preset_handler` (lines 119-151) with:

```python
def generate_preset_handler(
    library: Library, model: str, recipe: dict[str, Any], out_path: str,
    *, irs_dir: Path | None = None,
) -> dict[str, Any]:
    """Generate a Helix Stadium .hsp preset from a recipe dict and write it to disk.

    Builds directly against the library's Stadium chassis via
    `helixgen.recipe.generate_from_recipe`, writes the `.hsp` bytes to
    `out_path` (creating parent directories), and returns
    `{"path": <out_path>, "warnings": []}`. The `.hsp` file is the sole source
    of truth — no sidecar spec is written.

    Underlying SpecError / ParamValidationError / GenerateError propagate; the
    MCP server boundary translates them to protocol errors (raised before any
    file is written).
    """
    _validate_model(model)
    spec = parse_spec(recipe, source="mcp:generate_preset")
    irs = IrMapping.load(irs_dir)
    chassis = library.load_chassis()
    raw = generate_from_recipe(
        spec, library, irs=irs, chassis=chassis, source="mcp:generate_preset"
    )
    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    return {"path": out_path, "warnings": []}
```

Then delete `_safe_filename` (lines 109-116) and the `_FILENAME_SAFE` module constant (line 23) — confirm neither is referenced elsewhere first:

Run: `grep -rn "_safe_filename\|_FILENAME_SAFE" mcp_server/`
Expected after deletion: no matches.

- [ ] **Step 4: Update the server tool**

In `mcp_server/server.py`, replace `generate_preset` (lines 60-98) with a path-returning version:

```python
@app.tool()
def generate_preset(model: str, recipe: dict[str, Any], out_path: str) -> dict[str, Any]:
    """Generate a Helix Stadium .hsp preset from a JSON recipe and write it to disk.

    Required `model`: `"stadium"` or `"stadium_xl"`. Confirm the user's device
    before calling — see the `setup` skill.

    `out_path` is the filesystem path to write the `.hsp` to (required; parent
    directories are created). The recipe follows the helixgen schema
    (see https://github.com/sheax0r/helixgen): a `name`, optional `author`,
    1-2 `paths` each with `blocks`, and optional
    `snapshots` / `footswitches` / `expression`.

    **IR usage:** `With Pan` blocks accept an `ir` field with either a basename
    (resolved via the local IR mapping) or a 32-char hex hash. For factory IRs,
    use a `Mic Ir_*` cab block. After generating with user IRs, remind the user
    the IRs must be loaded onto the device via the Librarian (Cab IRs → Import).

    **On param errors:** if the error says `Unknown param(s)`, call `show_block`
    with the offending block name for the correct case-sensitive param names,
    then retry.

    Returns `{"path": <out_path>, "warnings": [...]}`. Pass `out_path` to
    `view_preset` / `patch_preset` to inspect or edit the written file.
    """
    return _tools.generate_preset_handler(
        _resolve_library(), model, recipe=recipe, out_path=out_path)
```

Remove the now-unused import on line 17 (`from mcp.types import BlobResourceContents, EmbeddedResource`) — but first confirm it isn't used elsewhere in the file:

Run: `grep -n "EmbeddedResource\|BlobResourceContents" mcp_server/server.py`
Expected: only the import line remains → delete it. (If other matches exist, leave the import.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/test_tools.py -v -k generate`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcp_server/tools.py mcp_server/server.py tests/mcp_server/test_tools.py
git commit -m "feat(mcp): generate_preset writes to out_path, returns path"
```

---

### Task 4: `compute_irhash` → wav_path (drop 2 MB cap, keep magic check)

**Files:**
- Modify: `mcp_server/tools.py` — rewrite `compute_irhash_handler` (lines 170-212); the `_WAV_BYTES_LIMIT` constant (lines 29-32) becomes unused — remove it.
- Modify: `mcp_server/server.py:117-139` — `compute_irhash` signature + docstring.
- Test: `tests/mcp_server/test_tools.py` — rewrite the compute_irhash tests (lines 290-332).

**Interfaces:**
- Produces: `compute_irhash_handler(model, wav_path) -> {"irhash": str, "reminder": str}`.

- [ ] **Step 1: Write the failing tests**

In `tests/mcp_server/test_tools.py`, replace the compute_irhash test block (lines 290-332) with path-based versions. Keep using the existing `_write_synth_wav_file` helper (defined at line 338 — it's below this block, so it's in scope at call time):

```python
@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_compute_irhash_returns_hash_and_reminder(tmp_path):
    """Happy path: synth WAV file → 32-char hex hash + non-empty reminder."""
    from mcp_server.tools import compute_irhash_handler
    wav = tmp_path / "ir.wav"
    _write_synth_wav_file(wav, n_frames=64)
    result = compute_irhash_handler("stadium_xl", str(wav))
    assert set(result.keys()) == {"irhash", "reminder"}
    assert len(result["irhash"]) == 32
    assert all(c in "0123456789abcdef" for c in result["irhash"])
    assert "Librarian" in result["reminder"]


def test_compute_irhash_rejects_bad_model(tmp_path):
    """Bad model → ValueError; we never touch the file."""
    from mcp_server.tools import compute_irhash_handler
    with _pytest_top.raises(ValueError, match="unsupported model"):
        compute_irhash_handler("helix_floor", str(tmp_path / "any.wav"))


def test_compute_irhash_rejects_missing_file(tmp_path):
    """Nonexistent path → ValueError before libsndfile."""
    from mcp_server.tools import compute_irhash_handler
    with _pytest_top.raises(ValueError, match="not found"):
        compute_irhash_handler("stadium_xl", str(tmp_path / "missing.wav"))


def test_compute_irhash_rejects_non_riff(tmp_path):
    """A file without RIFF/WAVE magic → ValueError before libsndfile."""
    from mcp_server.tools import compute_irhash_handler
    fake = tmp_path / "fake.wav"
    fake.write_bytes(b"NOT A WAVE FILE AT ALL" + b"\x00" * 100)
    with _pytest_top.raises(ValueError, match="RIFF/WAVE magic"):
        compute_irhash_handler("stadium_xl", str(fake))
```

(The old `test_compute_irhash_rejects_oversize` and `test_compute_irhash_rejects_invalid_base64` are removed — no size cap, no base64.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/test_tools.py -v -k compute_irhash`
Expected: FAIL — handler still base64-decodes its 2nd arg.

- [ ] **Step 3: Rewrite the handler**

In `mcp_server/tools.py`, replace `compute_irhash_handler` (lines 170-212) with:

```python
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
```

Then remove the now-unused `_WAV_BYTES_LIMIT` block (lines 29-32). Confirm:

Run: `grep -rn "_WAV_BYTES_LIMIT\|tempfile\|base64" mcp_server/tools.py`
Expected: no `_WAV_BYTES_LIMIT`. `tempfile` is no longer needed by this handler — if `grep` shows `import tempfile` (line 10) is now unused, remove it. If `import base64` (line 6) is now unused (all four base64 sites gone across Tasks 1-4; Task 5 removes the last), remove it in Task 5 — for now it may still be referenced by `device_install_preset_handler`.

- [ ] **Step 4: Update the server tool**

In `mcp_server/server.py`, replace `compute_irhash` (lines 117-139):

```python
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
    Always surface the `reminder` — the hash is meaningless unless the matching
    WAV is also loaded onto the device's Cab IRs.
    """
    return _tools.compute_irhash_handler(model, wav_path)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/test_tools.py -v -k compute_irhash`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcp_server/tools.py mcp_server/server.py tests/mcp_server/test_tools.py
git commit -m "feat(mcp): compute_irhash takes a wav_path, not base64"
```

---

### Task 5: `device_install_preset` → hsp_path

**Files:**
- Modify: `mcp_server/tools.py` — rewrite `device_install_preset_handler` (lines 641-682); remove `import base64` (line 6) if now unused.
- Modify: `mcp_server/server.py:434-454` — `device_install_preset` signature + docstring.
- Test: `tests/mcp_server/test_tools.py` — add a stubbed-client unit test for the file-read path.

**Interfaces:**
- Consumes: `_read_hsp_body` (Task 1).
- Produces: `device_install_preset_handler(model, *, ip=..., hsp_path, name, pos, setlist="user", template_cid=None) -> {"ok": bool, "cid": int | None}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp_server/test_tools.py`:

```python
# -- device_install_preset (file-read path) ------------------------------


def test_device_install_preset_rejects_missing_file(tmp_path):
    """Nonexistent .hsp path → ValueError before any device connection."""
    from mcp_server.tools import device_install_preset_handler
    with _pytest_top.raises(ValueError, match="not found"):
        device_install_preset_handler(
            "stadium_xl", hsp_path=str(tmp_path / "nope.hsp"), name="X", pos=1)


def test_device_install_preset_rejects_non_hsp(tmp_path):
    """A file without .hsp magic → ValueError before any device connection."""
    from mcp_server.tools import device_install_preset_handler
    bad = tmp_path / "bad.hsp"
    bad.write_bytes(b"NOTMAGIC{}")
    with _pytest_top.raises(ValueError, match="not a .hsp"):
        device_install_preset_handler(
            "stadium_xl", hsp_path=str(bad), name="X", pos=1)


def test_device_install_preset_reads_file_and_installs(tmp_path, monkeypatch, mcp_library):
    """Happy path with the device client + bridge stubbed: the handler reads the
    .hsp body off disk and forwards it to bridge.install_recipe, returning the cid."""
    import mcp_server.tools as tools_mod
    from helixgen.generate import compose_preset
    from helixgen.hsp import dumps_hsp
    from helixgen.spec import parse_spec

    preset = compose_preset(parse_spec(
        {"name": "D", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}),
        mcp_library, source="t")
    hsp = tmp_path / "d.hsp"
    hsp.write_bytes(dumps_hsp(preset))

    seen = {}

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def find_by_pos(self, container, pos): return None
        def load_preset(self, cid): pass
        def get_edit_buffer(self): return b"TEMPLATE"

    def _fake_install_recipe(client, body, container, pos, name, template_blob, strict):
        seen["body_name"] = body["preset"]["meta"]["name"] if "meta" in body.get("preset", {}) else None
        seen["template_blob"] = template_blob
        return 4242

    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", lambda **kw: _FakeClient())
    monkeypatch.setattr(device_mod.bridge, "install_recipe", _fake_install_recipe)

    result = tools_mod.device_install_preset_handler(
        "stadium_xl", hsp_path=str(hsp), name="D", pos=3)

    assert result == {"ok": True, "cid": 4242}
    assert seen["template_blob"] == b"TEMPLATE"
```

Note: `HelixClient` and `bridge` are imported *inside* the handler from `helixgen.device`, so patch them on the `helixgen.device` module (as above), not on `mcp_server.tools`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/test_tools.py -v -k device_install`
Expected: FAIL — handler takes `hsp_b64`, not `hsp_path` (TypeError).

- [ ] **Step 3: Rewrite the handler**

In `mcp_server/tools.py`, replace `device_install_preset_handler` (lines 641-682). Change the keyword-only `hsp_b64` to `hsp_path` and read the body via `_read_hsp_body` instead of base64-decoding:

```python
def device_install_preset_handler(
    model: str,
    *,
    ip: str = _DEFAULT_DEVICE_IP,
    hsp_path: str,
    name: str,
    pos: int,
    setlist: str = "user",
    template_cid: int | None = None,
) -> dict[str, Any]:
    """Author a helixgen .hsp file onto the device as a new preset.

    Reads the `.hsp` off `hsp_path`, maps its blocks onto a device template's
    same-category slots (v2.2: single serial chain), and installs it.
    ``template_cid`` picks a device preset to use as the chain template
    (defaults to the current edit buffer). Returns
    ``{"ok": <bool>, "cid": <new cid or None>}``. EXPERIMENTAL.
    """
    _validate_model(model)
    from helixgen.device import HelixClient, HelixError, bridge

    body = _read_hsp_body(hsp_path)
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
```

(The `import json as _json` and `from helixgen.hsp import is_hsp_bytes` lines inside the old handler are dropped — `_read_hsp_body` handles both.)

Now remove `import base64` at the top of `tools.py` (line 6) — it was the last user. Confirm:

Run: `grep -n "base64" mcp_server/tools.py`
Expected: no matches → delete the `import base64` line.

- [ ] **Step 4: Update the server tool**

In `mcp_server/server.py`, replace `device_install_preset` (lines 434-454). Change the `hsp_b64: str` parameter to `hsp_path: str`, update the docstring to say it reads a `.hsp` file off disk, and update the call:

```python
    return _tools.device_install_preset_handler(
        model, ip=ip, hsp_path=hsp_path, name=name, pos=pos,
        setlist=setlist, template_cid=template_cid,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/test_tools.py -v -k device_install`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add mcp_server/tools.py mcp_server/server.py tests/mcp_server/test_tools.py
git commit -m "feat(mcp): device_install_preset reads a .hsp path, not base64"
```

---

### Task 6: Fix protocol tests for the new signatures

**Files:**
- Modify: `tests/mcp_server/test_protocol.py` — update the module docstring, `test_generate_preset_via_server_*`, and `test_view_then_patch_preset_via_server`.

**Interfaces:**
- Consumes: all five converted tools via the FastMCP `_tool_manager.get_tool(...).fn(...)` dispatch.

- [ ] **Step 1: Rewrite the affected protocol tests**

In `tests/mcp_server/test_protocol.py`, replace `test_generate_preset_via_server_returns_embedded_resource` (lines 97-130) with a path-based version:

```python
def test_generate_preset_via_server_writes_file(mcp_library, monkeypatch, tmp_path):
    """Server-level generate_preset writes a .hsp file and returns its path."""
    from helixgen.hsp import HSP_MAGIC
    from mcp_server import server as srv

    monkeypatch.setattr(srv, "_resolve_library", lambda: mcp_library)

    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    spec = {
        "name": "Protocol Test",
        "paths": [{"blocks": [{"block": amps[0].display_name}, {"block": cabs[0].display_name}]}],
    }
    out = tmp_path / "protocol.hsp"

    if not hasattr(srv.app, "_tool_manager"):
        pytest.skip("FastMCP._tool_manager not available on this SDK version")
    tool = srv.app._tool_manager.get_tool("generate_preset")
    result = tool.fn(model="stadium_xl", recipe=spec, out_path=str(out))

    assert result == {"path": str(out), "warnings": []}
    assert out.read_bytes().startswith(HSP_MAGIC)
```

Replace `test_view_then_patch_preset_via_server` (lines 133-169) with a path round-trip:

```python
def test_view_then_patch_preset_via_server(mcp_library, monkeypatch, tmp_path):
    """generate → view → patch round-trip through the server dispatch on a
    .hsp file path — no base64 anywhere in the loop."""
    from helixgen.hsp import read_hsp
    from mcp_server import server as srv

    monkeypatch.setattr(srv, "_resolve_library", lambda: mcp_library)

    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    recipe = {
        "name": "Protocol View/Patch",
        "paths": [{"blocks": [{"block": amps[0].display_name}, {"block": cabs[0].display_name}]}],
    }
    out = tmp_path / "vp.hsp"

    if not hasattr(srv.app, "_tool_manager"):
        pytest.skip("FastMCP._tool_manager not available on this SDK version")

    gen_tool = srv.app._tool_manager.get_tool("generate_preset")
    gen_tool.fn(model="stadium_xl", recipe=recipe, out_path=str(out))

    view_tool = srv.app._tool_manager.get_tool("view_preset")
    projection = view_tool.fn(model="stadium_xl", hsp_path=str(out))
    assert projection["name"] == "Protocol View/Patch"
    assert projection["paths"][0]["blocks"][0]["block"] == amps[0].display_name

    patch_tool = srv.app._tool_manager.get_tool("patch_preset")
    patched = patch_tool.fn(
        model="stadium_xl", hsp_path=str(out),
        operations=[{"op": "set_enabled", "block": amps[0].display_name, "enabled": False}],
    )
    assert patched == {"path": str(out), "warnings": []}
    # The file was edited in place — re-read and confirm the block is bypassed.
    body = read_hsp(out)
    assert body["preset"]["flow"][0]["b01"]["slot"][0]["@enabled"]["value"] is False
```

Update the module docstring (lines 1-4) to drop "four tools" / "content shapes" wording — say the tools now operate on file paths. The `test_server_registers_documented_tools` set is unchanged (tool names are identical) — leave it.

- [ ] **Step 2: Run the full MCP suite**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/mcp_server/ -v`
Expected: PASS (all tests; count ≈ prior 78 minus the 3 deleted base64-specific tests plus the new file/error tests).

- [ ] **Step 3: Run the whole test suite to catch collateral**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS. (The 211-export round-trip and golden tests exercise core, not the MCP boundary, and are unaffected.)

- [ ] **Step 4: Commit**

```bash
git add tests/mcp_server/test_protocol.py
git commit -m "test(mcp): protocol round-trip on file paths, not base64 blobs"
```

---

### Task 7: Update docs — CLAUDE.md + tone skill

**Files:**
- Modify: `CLAUDE.md:388-394` — the MCP-tools paragraph.
- Modify: `skills/tone/SKILL.md` — the tool table (line 31), the generate step (lines 379-389), the save-location note (line 410), and the "Adjusting an existing tone" section (lines 477-496).

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update CLAUDE.md**

Replace the MCP-tools paragraph at `CLAUDE.md:388-394` with:

```markdown
MCP tools mirror the CLI for agent-driven edits, operating on `.hsp` **file
paths** (no base64): `generate_preset(model, recipe, out_path)` writes a `.hsp`
to `out_path` and returns `{path, warnings}`; `patch_preset(model, hsp_path,
operations)` applies a list of `{op, ...}` operations (`set_param`,
`set_enabled`, `add_block`, `remove_block`, `swap_model`) to the file **in
place** and returns `{path, warnings}`; `view_preset(model, hsp_path)` returns
the read-only recipe-shape projection. The agent edit loop is a single
`patch_preset` call on the file — no decompile/regenerate round-trip, no blob
in context.
```

- [ ] **Step 2: Update the tone skill tool table**

In `skills/tone/SKILL.md` line 31, change the `generate_preset` row's args + return column:

```markdown
| `generate_preset` | `model`, `recipe` (inline JSON dict — full helixgen schema), `out_path` | `{path, warnings}` — the `.hsp` is written to `out_path` |
```

- [ ] **Step 3: Update the generate step (skill lines ~379-389)**

Replace the paragraph beginning "Call `generate_preset(...)`" and the following base64-extract code block (lines 379-389) with:

```markdown
Call `generate_preset(model, recipe=<the dict you built in step 5>, out_path="<dir>/<slug>.hsp")` (`model` is the device model string, e.g. `"stadium_xl"`). It writes the `.hsp` directly to `out_path` and returns `{"path": ..., "warnings": [...]}` — no base64, no manual file extraction. Surface any `warnings` to the user.
```

- [ ] **Step 4: Update the save-location note (skill line ~410)**

The note still applies, but drop any implication that `generate_preset` returns a blob you write yourself — the tool writes the file. Keep the `<slug>` naming convention and Finder-reveal guidance; just ensure the `.hsp` is produced by passing `out_path` (the companion `.md` is still written by the skill itself).

- [ ] **Step 5: Update "Adjusting an existing tone" (skill lines ~477-496)**

Replace the numbered base64 steps with the path-based loop:

```markdown
1. The `.hsp` you saved is the source of truth — you already have its path.
2. Call `patch_preset(model, hsp_path="<dir>/<slug>.hsp", operations=[...])` with
   the smallest set of ops that addresses the feedback. It edits the file in
   place and returns `{"path": ..., "warnings": [...]}`.
3. To inspect the result in recipe shape, call `view_preset(model, hsp_path)` on
   the same path (read-only).
4. Surface any `warnings` (e.g. dropped params on a swap) to the user.

Prefer one `patch_preset` call with multiple `operations` over several edits.
The `.hsp` file is the thing you mutate — the recipe/spec dict is not re-read as
truth.
```

- [ ] **Step 6: Verify no stale base64 references remain in docs**

Run: `grep -rn "hsp_b64\|base64\|resource.blob\|EmbeddedResource" CLAUDE.md skills/tone/SKILL.md`
Expected: no matches (the `device pull ... blob` line in CLAUDE.md at line 44 is about `.sbe` content, not base64 — that's fine; confirm the only remaining "blob" hit is that CLI line).

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md skills/tone/SKILL.md
git commit -m "docs: MCP tools take file paths, not base64 (CLAUDE.md + tone skill)"
```

---

### Task 8: Release

**Files:**
- Modify: `.claude-plugin/plugin.json` — bump `version`.
- Modify: `.claude-plugin/marketplace.json` — bump `version` (must match plugin.json).
- Modify: `pyproject.toml` — bump the lib version.
- Modify: `src/helixgen/__init__.py` — bump `__version__`.

**Interfaces:** none.

- [ ] **Step 1: Read current versions**

Run: `grep -n "version" .claude-plugin/plugin.json .claude-plugin/marketplace.json pyproject.toml src/helixgen/__init__.py`
Note the current plugin version (main is at release 2.9.0) and lib version (separate `0.1.x` line). This is a breaking MCP-signature change → bump the plugin **minor** to the next `2.x.0` (e.g. `2.10.0`), and bump the lib patch/minor per its own line.

- [ ] **Step 2: Bump plugin + marketplace versions**

Set the same new version (e.g. `2.10.0`) in both `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`. The release workflow fails the build if they disagree.

- [ ] **Step 3: Bump lib version**

Bump the version in `pyproject.toml` and the matching `__version__` in `src/helixgen/__init__.py` (the `0.1.x` line — increment its minor or patch).

- [ ] **Step 4: Final full-suite green check**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS (0 failures).

- [ ] **Step 5: Commit the release bump**

```bash
git add .claude-plugin/plugin.json .claude-plugin/marketplace.json pyproject.toml src/helixgen/__init__.py
git commit -m "release 2.10.0: path-based MCP tools (no base64 in agent context)"
```

- [ ] **Step 6: Open a PR and merge to main**

Push the branch and open a PR (title `release 2.10.0: path-based MCP tools`, body summarizing the breaking MCP-signature change). After review, merge to `main`. The `.github/workflows/release.yml` workflow then auto-creates the tag `helixgen--v2.10.0` and fast-forwards `stable`. **Do not** move `stable` or push tags by hand. The release is live once the workflow has run.

---

## Self-Review

**Spec coverage:**
- generate_preset → out_path + {path,warnings}: Task 3 ✓
- view_preset → path: Task 1 ✓
- patch_preset → in-place path: Task 2 ✓
- device_install_preset → hsp_path: Task 5 ✓
- compute_irhash → wav_path, drop 2 MB cap, keep magic check: Task 4 ✓
- Error handling (missing path / bad magic → ValueError): Tasks 1, 4, 5 ✓
- Ripple: tone skill: Task 7 ✓ · CLAUDE.md: Task 7 ✓ · tests/mcp_server/: Tasks 1-6 ✓
- Hard cut (remove base64 import, EmbeddedResource import, _safe_filename, _WAV_BYTES_LIMIT): Tasks 3, 4, 5 ✓
- Release: Task 8 ✓
- Out of scope (device-read tools, CLI, .sbe pull): not touched ✓

**Placeholder scan:** No TBD/TODO. Every code step shows the full code. Version numbers use "e.g. 2.10.0" because the exact current version is read in Task 8 Step 1 — the engineer picks the next minor from the observed value; this is an instruction, not a placeholder.

**Type consistency:** `_read_hsp_body(hsp_path: str) -> dict` defined in Task 1, consumed in Tasks 2 & 5. Return shapes `{"path": str, "warnings": list}` consistent across generate_preset (Task 3) and patch_preset (Task 2). `compute_irhash_handler` return `{"irhash", "reminder"}` unchanged. Server-tool call sites updated in the same task as each handler.
