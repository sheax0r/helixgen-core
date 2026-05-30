# helixgen MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a public, unauthenticated MCP server (`mcp_server/`) that wraps the existing `helixgen` package and runs on Render's free tier, exposing three tools — `list_blocks`, `show_block`, `generate_preset` — to claude.ai web users via the Streamable HTTP transport.

**Architecture:** New sibling package `mcp_server/` at repo root (not nested in `helixgen.*`) keeps the MCP SDK dependency out of the core CLI. Pure-function handlers in `tools.py` are unit-tested directly; the MCP `FastMCP` server in `server.py` wraps them; `__main__.py` binds the Streamable HTTP transport to `$PORT`. A scrubbed `chassis.json` fixture (committed under `mcp_server/data/`) replaces the "ingest a real `.hsp`" step at deploy time, so no device exports are committed.

**Tech Stack:** Python 3.11+, official `mcp` SDK from PyPI (`mcp>=1.0,<2.0`), `helixgen` (this repo, editable install), `pytest`. No new runtime deps for `helixgen` itself; `mcp` is in a `[mcp]` optional extra.

**Spec:** `docs/superpowers/specs/2026-05-30-helixgen-mcp-server-design.md`

---

## File structure

| File                                       | Status   | Responsibility                                                          |
|--------------------------------------------|----------|-------------------------------------------------------------------------|
| `pyproject.toml`                           | modify   | Add `[project.optional-dependencies] mcp = ["mcp>=1.0,<2.0"]`           |
| `mcp_server/__init__.py`                   | **new**  | `__version__ = "0.1.0"`. Nothing else.                                  |
| `mcp_server/tools.py`                      | **new**  | Pure handlers: `list_blocks_handler`, `show_block_handler`, `generate_preset_handler`. Each takes a `Library` + args and returns plain Python (str or dict). No MCP types here. |
| `mcp_server/server.py`                     | **new**  | `FastMCP("helixgen")` app, three tools registered via decorators, each delegating to the corresponding `tools.py` handler. |
| `mcp_server/__main__.py`                   | **new**  | Entry point: reads `PORT` env, runs `app` with Streamable HTTP transport. |
| `mcp_server/data/chassis.json`             | **new**  | Scrubbed Stadium chassis. Committed binary-equivalent JSON, ~5 KB.       |
| `mcp_server/data/__init__.py`              | **new**  | Empty marker so `importlib.resources` works against `mcp_server.data`.  |
| `render.yaml`                              | **new**  | Render service definition (native Python, build cmd, start cmd).        |
| `mcp_server/DEPLOY.md`                     | **new**  | User-facing setup: Render deploy + claude.ai connector wiring.          |
| `tests/mcp_server/__init__.py`             | **new**  | Empty.                                                                  |
| `tests/mcp_server/conftest.py`             | **new**  | `mcp_library` fixture — Library against `$HELIXGEN_LIBRARY` or `~/.helixgen/library/`; pytest.skip if no chassis. |
| `tests/mcp_server/test_tools.py`           | **new**  | Direct calls to the three handlers, asserting return shape + error handling. |
| `tests/mcp_server/test_protocol.py`        | **new**  | In-process MCP roundtrip via the SDK's in-memory transport — verifies tool registration + result shapes match the protocol. |

---

## Style conventions to follow

- pytest plain functions, no classes. Use `monkeypatch` for env vars.
- One responsibility per test.
- Handlers in `tools.py` are pure Python (no `async`, no MCP types) — easier to test, and `FastMCP` wraps them at registration time.
- Error handling: handlers raise the underlying `helixgen` exceptions (`KeyError`, `LookupError`, `SpecError`, `ParamValidationError`, `GenerateError`). `server.py` is responsible for translating to MCP errors at the registration boundary if needed (FastMCP defaults handle this for sync handlers — verify behavior in Task 7).
- Match the existing helixgen test style: `tests/conftest.py` already provides `tmp_library`, `sample_serial_preset_hsp`, etc. Reuse where appropriate.
- Skip-if-not-present pattern for tests requiring a populated library — `pytest.skip("...")` inside the fixture, matching how `test_generate_input.py` skips when `data/` is empty.
- No comments inside code unless explaining a non-obvious WHY. Don't narrate what the code does.

---

## Task 1 — Add `mcp` extra and `mcp_server/__init__.py` skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `mcp_server/__init__.py`
- Create: `mcp_server/data/__init__.py`

- [ ] **Step 1.1: Modify `pyproject.toml` to add the `mcp` extra and include `mcp_server` in the build.**

Current `[project.optional-dependencies]` block (around line 16):

```toml
[project.optional-dependencies]
dev = [
    "pytest>=7.4",
]
```

Replace with:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=7.4",
]
mcp = [
    "mcp>=1.0,<2.0",
]
```

And the `[tool.setuptools.packages.find]` block at the end currently reads:

```toml
[tool.setuptools.packages.find]
where = ["src"]
```

Replace with (so the build picks up the sibling `mcp_server/` package too):

```toml
[tool.setuptools.packages.find]
where = ["src", "."]
include = ["helixgen*", "mcp_server*"]
```

- [ ] **Step 1.2: Create `mcp_server/__init__.py`**

```python
"""helixgen MCP server — public connector for claude.ai web."""

__version__ = "0.1.0"
```

- [ ] **Step 1.3: Create `mcp_server/data/__init__.py`** (empty marker file)

```python
```

- [ ] **Step 1.4: Reinstall editable with the new extra and verify the import.**

Run:
```bash
pip install -e .[mcp]
python -c "import mcp; import mcp_server; print(mcp_server.__version__)"
```

Expected output: `0.1.0` (no import errors).

- [ ] **Step 1.5: Verify existing tests still pass.**

Run: `pytest -q`
Expected: 268 passed, 18 skipped (the project's existing baseline in this worktree).

- [ ] **Step 1.6: Commit.**

```bash
git add pyproject.toml mcp_server/__init__.py mcp_server/data/__init__.py
git commit -m "feat(mcp): scaffold mcp_server package and add [mcp] extra

Empty sibling package at repo root, version 0.1.0. mcp>=1.0 added as an
optional dependency; not pulled in by the default helixgen install.
"
```

---

## Task 2 — Scrubbed `chassis.json` fixture

**Files:**
- Create: `mcp_server/data/chassis.json`

This is a one-off data-prep step. The chassis is mechanical (path layout, default param shapes, device_id for FS/EXP resolution) — not user content. We derive it from the maintainer's local `~/.helixgen/library/chassis.json`, nulling user-attributable fields.

- [ ] **Step 2.1: Run the scrub script as a one-liner.**

The chassis carries user-attributable strings in *four* places that all need
scrubbing, not just `meta`. A real ingest captures snapshot names,
footswitch labels, and the source preset's clip filename — all of which
must be replaced with generic / empty values before publishing.

```bash
python -c "
import json
from pathlib import Path

src = Path.home() / '.helixgen' / 'library' / 'chassis.json'
dst = Path('mcp_server/data/chassis.json')

chassis = json.loads(src.read_text())

chassis['meta']['name'] = ''
chassis['meta']['info'] = ''
chassis['meta'].pop('author', None)

for i, snap in enumerate(chassis.get('preset', {}).get('snapshots', [])):
    snap['name'] = f'Snap {i + 1}'

for src_entry in chassis.get('preset', {}).get('sources', {}).values():
    if isinstance(src_entry, dict) and 'fs_label' in src_entry:
        src_entry['fs_label'] = ''

clip = chassis.get('preset', {}).get('clip')
if isinstance(clip, dict):
    clip['filename'] = ''
    clip['path'] = ''

dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(chassis, indent=2))
print(f'wrote {dst} ({dst.stat().st_size} bytes)')
"
```

Expected output: `wrote mcp_server/data/chassis.json (<size> bytes)` — size varies (~8-10 KB typical).

- [ ] **Step 2.2: Verify the scrubbed file has the right shape.**

Run:
```bash
python -c "
import json
from pathlib import Path
c = json.loads(Path('mcp_server/data/chassis.json').read_text())

assert c['meta']['name'] == '', f'name not scrubbed: {c[\"meta\"][\"name\"]!r}'
assert c['meta']['info'] == '', f'info not scrubbed: {c[\"meta\"][\"info\"]!r}'
assert 'author' not in c['meta'], 'author still present'
assert c['_helixgen_chassis_shape'] == 'hsp', 'wrong chassis shape'
assert c['meta'].get('device_id'), 'device_id missing — FS/EXP wiring will fail'

snaps = c.get('preset', {}).get('snapshots', [])
for i, s in enumerate(snaps):
    assert s['name'] == f'Snap {i + 1}', f'snapshot {i}: {s[\"name\"]!r}'

for sid, entry in c.get('preset', {}).get('sources', {}).items():
    if isinstance(entry, dict) and 'fs_label' in entry:
        assert entry['fs_label'] == '', f'source {sid} fs_label: {entry[\"fs_label\"]!r}'

clip = c.get('preset', {}).get('clip')
if isinstance(clip, dict):
    assert clip.get('filename', '') == ''
    assert clip.get('path', '') == ''

print('ok')
"
```
Expected: `ok`.

- [ ] **Step 2.3: Commit.**

```bash
git add mcp_server/data/chassis.json
git commit -m "feat(mcp): bundle scrubbed Stadium chassis fixture

Derived from a real .hsp ingest with meta.name, meta.info, meta.author
nulled or removed. Preserves device_id (required for FS/EXP source
resolution) and the mechanical preset.flow structure. Deploy build copies
this into \$HELIXGEN_LIBRARY/chassis.json so no .hsp needs to be committed.
"
```

---

## Task 3 — Test fixture: `mcp_library` in `tests/mcp_server/conftest.py`

The fixture resolves a `Library` against `$HELIXGEN_LIBRARY` (or `~/.helixgen/library/` if unset), and `pytest.skip`s if the library has no chassis or no blocks. Matches the existing "skip-if-not-present" pattern used by `tests/test_generate_input.py`.

**Files:**
- Create: `tests/mcp_server/__init__.py`
- Create: `tests/mcp_server/conftest.py`
- Create: `tests/mcp_server/test_tools.py` (will be added to in Tasks 4–6; this task adds a sanity test only)

- [ ] **Step 3.1: Create `tests/mcp_server/__init__.py`** (empty)

```python
```

- [ ] **Step 3.2: Create `tests/mcp_server/conftest.py`**

```python
"""Shared fixtures for mcp_server tests."""
from __future__ import annotations

import pytest

from helixgen.library import Library, default_library_path


@pytest.fixture(scope="session")
def mcp_library() -> Library:
    """A populated Library to drive the MCP tools.

    Resolves via the same env-var path as the CLI. Skips the test if the
    library has no chassis or no blocks, matching the project's existing
    skip-if-not-present convention for tests that need real ingest data.
    """
    library = Library(default_library_path())
    if not library.has_chassis():
        pytest.skip(
            f"No chassis at {library.chassis_path}. "
            "Run `helixgen bootstrap && helixgen ingest <real.hsp>` "
            "to populate, or set HELIXGEN_LIBRARY to an existing library."
        )
    if not library.list_blocks():
        pytest.skip(f"Library at {library.root} has no blocks.")
    return library
```

- [ ] **Step 3.3: Write a sanity test in `tests/mcp_server/test_tools.py`** (this is the file we'll add to in the next tasks)

```python
"""Tests for mcp_server.tools handler functions."""
from __future__ import annotations


def test_library_fixture_loads(mcp_library):
    """Smoke test: fixture resolves to a populated Library."""
    assert mcp_library.has_chassis()
    assert mcp_library.list_blocks()
```

- [ ] **Step 3.4: Run the test.**

Run: `pytest tests/mcp_server/test_tools.py -v`
Expected: `test_library_fixture_loads PASSED` (or `SKIPPED` if `~/.helixgen/library/` is empty in your dev env — that's fine, it confirms the skip path works).

- [ ] **Step 3.5: Commit.**

```bash
git add tests/mcp_server/__init__.py tests/mcp_server/conftest.py tests/mcp_server/test_tools.py
git commit -m "test(mcp): add mcp_library fixture and conftest skeleton

Session-scoped fixture resolves via HELIXGEN_LIBRARY or default path; skips
when no chassis/blocks present (matches existing skip-if-not-present pattern
in tests/test_generate_input.py).
"
```

---

## Task 4 — `list_blocks_handler` (TDD)

**Files:**
- Create: `mcp_server/tools.py`
- Modify: `tests/mcp_server/test_tools.py`

- [ ] **Step 4.1: Write the failing test.**

Append to `tests/mcp_server/test_tools.py`:

```python
def test_list_blocks_handler_returns_grouped_text(mcp_library):
    """Returns text grouped by category, one block per line."""
    from mcp_server.tools import list_blocks_handler

    result = list_blocks_handler(mcp_library, category=None)

    assert isinstance(result, str)
    assert result.strip()
    # Format: lines beginning "<category>:" followed by indented "  <name>  [<model_id>]" lines.
    lines = result.splitlines()
    assert any(line.endswith(":") for line in lines), "expected at least one category header"
    assert any(line.startswith("  ") and "[" in line and line.endswith("]") for line in lines), (
        "expected at least one indented '<name>  [<model_id>]' line"
    )


def test_list_blocks_handler_filters_by_category(mcp_library):
    """When category given, only blocks from that category appear."""
    from mcp_server.tools import list_blocks_handler

    result = list_blocks_handler(mcp_library, category="amp")
    lines = result.splitlines()
    headers = [line[:-1] for line in lines if line.endswith(":")]
    assert headers == ["amp"], f"expected only 'amp:' header, got {headers}"


def test_list_blocks_handler_unknown_category_returns_empty(mcp_library):
    """An unknown category returns an empty string, not an error."""
    from mcp_server.tools import list_blocks_handler
    assert list_blocks_handler(mcp_library, category="nonexistent") == ""
```

- [ ] **Step 4.2: Run to verify failure.**

Run: `pytest tests/mcp_server/test_tools.py::test_list_blocks_handler_returns_grouped_text -v`
Expected: `ImportError` / `ModuleNotFoundError: No module named 'mcp_server.tools'`.

- [ ] **Step 4.3: Create `mcp_server/tools.py` with the handler.**

```python
"""Pure-Python handlers for MCP tools. No MCP types; FastMCP wraps these at
registration time. Importable + directly testable.
"""
from __future__ import annotations

from helixgen.library import Library


def list_blocks_handler(library: Library, category: str | None = None) -> str:
    """Return library blocks grouped by category, matching `helixgen list-blocks`.

    Format mirrors the CLI: one `<category>:` header per category, followed
    by indented `  <display_name>  [<model_id>]` lines sorted by name.
    Unknown category returns an empty string (not an error) so callers can
    distinguish "no such category" from "library empty."
    """
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
```

- [ ] **Step 4.4: Run tests to verify they pass.**

Run: `pytest tests/mcp_server/test_tools.py -v`
Expected: all 4 tests pass (1 sanity + 3 new), or skip if library not populated.

- [ ] **Step 4.5: Commit.**

```bash
git add mcp_server/tools.py tests/mcp_server/test_tools.py
git commit -m "feat(mcp): list_blocks_handler

Pure handler returning CLI-style grouped text. Empty string on unknown
category (lets callers distinguish 'no such category' from 'library empty').
"
```

---

## Task 5 — `show_block_handler` (TDD)

**Files:**
- Modify: `mcp_server/tools.py`
- Modify: `tests/mcp_server/test_tools.py`

- [ ] **Step 5.1: Write the failing tests.**

Append to `tests/mcp_server/test_tools.py`:

```python
def test_show_block_handler_returns_schema_text(mcp_library):
    """Returns header + category + params lines, matching CLI format."""
    from mcp_server.tools import show_block_handler

    # Pick any block in the library; assume at least one amp.
    amps = mcp_library.list_blocks(category="amp")
    assert amps, "fixture library has no amps to show"
    target = amps[0]

    result = show_block_handler(mcp_library, name_or_id=target.model_id)

    assert isinstance(result, str)
    lines = result.splitlines()
    # First line: "<display_name>  [<model_id>]"
    assert lines[0] == f"{target.display_name}  [{target.model_id}]"
    # Must include the category line and a params: section.
    assert any(line.startswith("category:") for line in lines)
    assert any(line == "params:" for line in lines)


def test_show_block_handler_unknown_name_raises_keyerror(mcp_library):
    """Unknown block bubbles up the underlying KeyError unchanged."""
    import pytest as _pytest
    from mcp_server.tools import show_block_handler
    with _pytest.raises(KeyError):
        show_block_handler(mcp_library, name_or_id="ThisBlockDoesNotExist")
```

- [ ] **Step 5.2: Run to verify failure.**

Run: `pytest tests/mcp_server/test_tools.py::test_show_block_handler_returns_schema_text -v`
Expected: `ImportError: cannot import name 'show_block_handler'`.

- [ ] **Step 5.3: Add the handler to `mcp_server/tools.py`.**

Append to `mcp_server/tools.py`:

```python
def show_block_handler(library: Library, name_or_id: str) -> str:
    """Return a block's schema (params, defaults, ranges) as text.

    Format mirrors `helixgen show-block`: header, category, aliases (if any),
    then one indented line per param with type, default, and observed-range
    or values where present. KeyError / LookupError propagate to the caller
    (FastMCP translates these to MCP errors at the registration boundary).
    """
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
```

- [ ] **Step 5.4: Run tests to verify they pass.**

Run: `pytest tests/mcp_server/test_tools.py -v`
Expected: all tests pass (or skip if library empty).

- [ ] **Step 5.5: Commit.**

```bash
git add mcp_server/tools.py tests/mcp_server/test_tools.py
git commit -m "feat(mcp): show_block_handler

Mirrors helixgen show-block output. KeyError/LookupError propagate; FastMCP
converts them to MCP errors at the protocol boundary.
"
```

---

## Task 6 — `generate_preset_handler` (TDD)

This is the only tool that produces binary output. It writes the spec to a tmp file, runs `helixgen.generate.generate_preset`, reads the resulting `.hsp` bytes, and returns a base64-encoded payload in a shape suitable for an MCP `EmbeddedResource`.

**Files:**
- Modify: `mcp_server/tools.py`
- Modify: `tests/mcp_server/test_tools.py`

- [ ] **Step 6.1: Write the failing tests.**

Append to `tests/mcp_server/test_tools.py`:

```python
def test_generate_preset_handler_returns_base64_hsp(mcp_library):
    """Returns a dict with mimeType, name, and base64 blob whose bytes start with HSP_MAGIC."""
    import base64
    from helixgen.hsp import HSP_MAGIC
    from mcp_server.tools import generate_preset_handler

    # Pick the first amp and first cab from the library to build a minimal spec.
    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    assert amps and cabs, "fixture library missing amps/cabs"

    spec = {
        "name": "MCP Test Preset",
        "paths": [
            {
                "blocks": [
                    {"block": amps[0].display_name},
                    {"block": cabs[0].display_name},
                ]
            }
        ],
    }

    result = generate_preset_handler(mcp_library, spec=spec)

    assert isinstance(result, dict)
    assert result["mimeType"] == "application/octet-stream"
    assert result["name"].endswith(".hsp")
    decoded = base64.b64decode(result["blob"])
    assert decoded.startswith(HSP_MAGIC), (
        f"expected HSP_MAGIC prefix; got {decoded[:8]!r}"
    )


def test_generate_preset_handler_rejects_unknown_param(mcp_library):
    """Bad spec surfaces ParamValidationError unchanged."""
    import pytest as _pytest
    from helixgen.generate import ParamValidationError
    from mcp_server.tools import generate_preset_handler

    amps = mcp_library.list_blocks(category="amp")
    assert amps
    spec = {
        "name": "broken",
        "paths": [
            {"blocks": [{"block": amps[0].display_name, "params": {"NoSuchParam": 0.5}}]}
        ],
    }
    with _pytest.raises(ParamValidationError):
        generate_preset_handler(mcp_library, spec=spec)


def test_generate_preset_handler_sanitizes_filename(mcp_library):
    """Spec names with path separators or unsafe chars yield safe filenames."""
    from mcp_server.tools import generate_preset_handler

    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    spec = {
        "name": "../../etc/passwd",
        "paths": [{"blocks": [{"block": amps[0].display_name}, {"block": cabs[0].display_name}]}],
    }
    result = generate_preset_handler(mcp_library, spec=spec)
    # No path traversal, no slashes, no null bytes.
    assert "/" not in result["name"]
    assert "\\" not in result["name"]
    assert ".." not in result["name"]
    assert result["name"].endswith(".hsp")
```

- [ ] **Step 6.2: Run to verify failure.**

Run: `pytest tests/mcp_server/test_tools.py::test_generate_preset_handler_returns_base64_hsp -v`
Expected: `ImportError: cannot import name 'generate_preset_handler'`.

- [ ] **Step 6.3: Add the handler to `mcp_server/tools.py`.**

Append to `mcp_server/tools.py`:

```python
import base64
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from helixgen.generate import generate_preset


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    """Convert an arbitrary preset name to a safe basename for the .hsp blob.

    Strips path separators, collapses unsafe characters to underscores,
    and falls back to 'preset' when the result would be empty.
    """
    cleaned = _FILENAME_SAFE.sub("_", name).strip("._-")
    return f"{cleaned or 'preset'}.hsp"


def generate_preset_handler(library: Library, spec: dict[str, Any]) -> dict[str, Any]:
    """Generate a Helix Stadium .hsp from an inline spec dict.

    Returns a dict suitable for an MCP EmbeddedResource:
      - mimeType: application/octet-stream
      - name:     safe basename ending in .hsp
      - blob:     base64-encoded .hsp bytes (magic header + JSON body)

    Underlying SpecError / ParamValidationError / GenerateError propagate;
    the MCP server boundary translates them to protocol errors.
    """
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
```

- [ ] **Step 6.4: Run tests to verify they pass.**

Run: `pytest tests/mcp_server/test_tools.py -v`
Expected: all tests pass (or skip if library empty).

- [ ] **Step 6.5: Commit.**

```bash
git add mcp_server/tools.py tests/mcp_server/test_tools.py
git commit -m "feat(mcp): generate_preset_handler returning base64 .hsp blob

Writes spec to a tmp file, runs generate_preset(), returns the bytes as
base64 in an MCP EmbeddedResource-shaped dict. Filenames are sanitized
to defeat path-traversal in spec.name.
"
```

---

## Task 7 — `server.py` with FastMCP + protocol-level test

**Files:**
- Create: `mcp_server/server.py`
- Create: `tests/mcp_server/test_protocol.py`

The protocol test verifies that the registered tools have the right names and that calling them via the MCP server yields the documented content shapes. We use the SDK's in-memory transport (preferred) or, if that API is awkward in the installed `mcp` version, call the server's tool-list / call-tool methods directly. Either way, no HTTP layer is exercised.

- [ ] **Step 7.1: Write the failing protocol test.**

Create `tests/mcp_server/test_protocol.py`:

```python
"""Protocol-level smoke tests: verify the FastMCP server registers the three
tools with the right names + arg schemas, and that calling them returns the
expected content shapes. Does not exercise the HTTP transport.
"""
from __future__ import annotations

import pytest


def _get_tool_names(server) -> set[str]:
    """Best-effort extraction of registered tool names from a FastMCP app.

    The FastMCP API exposes tools via several paths depending on SDK version;
    try the common ones in order.
    """
    if hasattr(server, "list_tools"):
        # Most current FastMCP versions: synchronous accessor.
        try:
            return {t.name for t in server.list_tools()}
        except TypeError:
            pass
    if hasattr(server, "_tool_manager"):
        tm = server._tool_manager
        if hasattr(tm, "list_tools"):
            return {t.name for t in tm.list_tools()}
    raise AssertionError(
        "Could not locate registered tools on the FastMCP server — check SDK version."
    )


def test_server_registers_three_tools():
    """The server registers exactly the three documented tools."""
    from mcp_server.server import app

    names = _get_tool_names(app)
    assert names == {"list_blocks", "show_block", "generate_preset"}, (
        f"unexpected tool set: {names}"
    )


def test_server_tools_have_descriptions():
    """Every tool has a non-empty description so Claude knows when to call it."""
    from mcp_server.server import app

    if hasattr(app, "list_tools"):
        tools = list(app.list_tools())
    else:
        tools = list(app._tool_manager.list_tools())
    for t in tools:
        assert getattr(t, "description", None), f"tool {t.name} has no description"


def test_list_blocks_via_server(mcp_library, monkeypatch):
    """Invoking the list_blocks tool through the server returns grouped text."""
    from mcp_server import server as srv

    # Point the server's library resolver at our test library.
    monkeypatch.setattr(srv, "_resolve_library", lambda: mcp_library)

    # Get the registered handler. FastMCP exposes via tool_manager.get_tool.
    if hasattr(srv.app, "_tool_manager"):
        tool = srv.app._tool_manager.get_tool("list_blocks")
        result = tool.fn(category=None)
    else:
        # Fallback: import the underlying handler directly.
        from mcp_server.tools import list_blocks_handler
        result = list_blocks_handler(mcp_library, category=None)

    assert isinstance(result, str)
    assert result  # non-empty
```

- [ ] **Step 7.2: Run to verify failure.**

Run: `pytest tests/mcp_server/test_protocol.py -v`
Expected: `ImportError: cannot import name 'app' from 'mcp_server.server'`.

- [ ] **Step 7.3: Create `mcp_server/server.py`.**

```python
"""FastMCP server wiring: registers the three helixgen tools.

Each tool delegates to the corresponding pure-Python handler in
`mcp_server.tools`. The library is resolved per-request via the standard
`helixgen.library.default_library_path()` (overridable via HELIXGEN_LIBRARY).
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
def list_blocks(category: str | None = None) -> str:
    """List Helix blocks in the library, optionally filtered to one category.

    Categories: amp, cab, drive, delay, reverb, modulation, filter, eq,
    dynamics, pitch, volume, send. Output is grouped by category with one
    block per line as `<display_name>  [<model_id>]`.
    """
    return _tools.list_blocks_handler(_resolve_library(), category=category)


@app.tool()
def show_block(name_or_id: str) -> str:
    """Show a Helix block's parameter schema: types, defaults, observed ranges.

    Accepts the display name (e.g. "Brit Plexi Brt"), the model id
    (e.g. "HD2_AmpBritPlexiBrt"), or an alias. **Always call this before
    writing params for a block** — param names are case-sensitive and the
    generator rejects unknown ones.
    """
    return _tools.show_block_handler(_resolve_library(), name_or_id=name_or_id)


@app.tool()
def generate_preset(spec: dict[str, Any]) -> dict[str, Any]:
    """Generate a Helix Stadium .hsp preset from an inline JSON spec.

    The spec follows the helixgen schema (see https://github.com/sheax0r/helixgen):
    a `name`, optional `author`, 1-2 `paths` each with `blocks`, and optional
    `snapshots` / `footswitches` / `expression`. The `ir` field on IR blocks
    is accepted but ignored server-side (no IR registry in this deployment).

    Returns a dict with `mimeType` (application/octet-stream), `name`
    (safe filename ending in .hsp), and `blob` (base64-encoded .hsp bytes).
    """
    return _tools.generate_preset_handler(_resolve_library(), spec=spec)
```

- [ ] **Step 7.4: Run tests to verify they pass.**

Run: `pytest tests/mcp_server/test_protocol.py -v`
Expected: all 3 pass (`test_list_blocks_via_server` may skip if library empty).

- [ ] **Step 7.5: Run the full suite to make sure nothing else broke.**

Run: `pytest -q`
Expected: previous baseline (268+) passes, plus the new MCP tests pass-or-skip.

- [ ] **Step 7.6: Commit.**

```bash
git add mcp_server/server.py tests/mcp_server/test_protocol.py
git commit -m "feat(mcp): FastMCP server with list_blocks, show_block, generate_preset

Three tools registered via the FastMCP decorator API. Library resolved
per-request from default_library_path() (HELIXGEN_LIBRARY-aware). Tool
docstrings double as MCP descriptions read by Claude.
"
```

---

## Task 8 — `__main__.py` entry point + manual smoke check

**Files:**
- Create: `mcp_server/__main__.py`

- [ ] **Step 8.1: Create `mcp_server/__main__.py`.**

```python
"""Entry point for `python -m mcp_server`.

Binds the FastMCP app over Streamable HTTP on 0.0.0.0:$PORT (Render injects
PORT at runtime; defaults to 10000 locally). No other configuration.
"""
from __future__ import annotations

import os

from mcp_server.server import app


def main() -> None:
    port = int(os.environ.get("PORT", "10000"))
    # FastMCP's run() dispatches by transport name; "streamable-http" is the
    # claude.ai-compatible HTTP transport (successor to SSE).
    app.run(transport="streamable-http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 8.2: Smoke-check that the server starts and binds.**

```bash
PORT=10000 python -m mcp_server &
SERVER_PID=$!
sleep 2
# Verify it's listening; the MCP endpoint expects POST, so GET should give a 4xx (which means it's alive).
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:10000/mcp
kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null
```

Expected: a 3-digit HTTP status (likely 405 Method Not Allowed or 400). Anything other than `000` (connection refused) confirms the server bound the port. If you see `000`, the server failed to start — check stderr.

- [ ] **Step 8.3: Commit.**

```bash
git add mcp_server/__main__.py
git commit -m "feat(mcp): __main__ entry point binding Streamable HTTP on \$PORT

PORT defaults to 10000 locally; Render injects it at runtime. Bare app.run()
on the streamable-http transport — no extra config.
"
```

---

## Task 9 — `render.yaml` deployment config

**Files:**
- Create: `render.yaml`

- [ ] **Step 9.1: Create `render.yaml` at the repo root.**

```yaml
services:
  - type: web
    name: helixgen-mcp
    runtime: python
    plan: free
    pythonVersion: "3.11"
    buildCommand: |
      pip install -e .[mcp]
      helixgen bootstrap
      mkdir -p "$HOME/.helixgen/library"
      cp mcp_server/data/chassis.json "$HOME/.helixgen/library/chassis.json"
    startCommand: python -m mcp_server
    healthCheckPath: /mcp
```

- [ ] **Step 9.2: Verify the build steps locally on a scratch HELIXGEN_LIBRARY.**

```bash
SCRATCH=$(mktemp -d)
HELIXGEN_LIBRARY="$SCRATCH/library" helixgen bootstrap 2>&1 | tail -3
mkdir -p "$SCRATCH/library"
cp mcp_server/data/chassis.json "$SCRATCH/library/chassis.json"
HELIXGEN_LIBRARY="$SCRATCH/library" python -c "
from helixgen.library import Library
lib = Library('$SCRATCH/library')
assert lib.has_chassis()
blocks = lib.list_blocks()
print(f'ok: {len(blocks)} blocks, chassis present')
"
rm -rf "$SCRATCH"
```

Expected: `ok: <N> blocks, chassis present` (N typically 200-400 depending on what phelix has at HEAD).

If the bootstrap fails (e.g. network issue), the same failure would happen on Render — investigate before continuing.

- [ ] **Step 9.3: Commit.**

```bash
git add render.yaml
git commit -m "feat(mcp): render.yaml — native Python free-tier deploy

Build runs phelix bootstrap then copies the bundled scrubbed chassis into
\$HOME/.helixgen/library/. Start: python -m mcp_server. healthCheckPath
points at /mcp.
"
```

---

## Task 10 — `DEPLOY.md` user-facing setup guide

**Files:**
- Create: `mcp_server/DEPLOY.md`

- [ ] **Step 10.1: Create `mcp_server/DEPLOY.md`.**

```markdown
# Deploying helixgen-mcp

Public, unauthenticated MCP server wrapping the helixgen CLI. Hosts on
Render's free tier; integrates with claude.ai as a custom connector.

## What you get

Three tools exposed to any Claude client that connects to your server URL:

- `list_blocks(category?)` — browse the block catalog.
- `show_block(name_or_id)` — inspect a block's params.
- `generate_preset(spec)` — turn an inline JSON tone spec into a `.hsp`
  Stadium preset, returned as a base64 blob.

The full spec schema is in `CLAUDE.md` at the repo root (paths, snapshots,
footswitches, expression). The `ir` field on IR blocks is ignored
server-side — this deployment ships only canonical IRs from the bundled
chassis, no user-IR registry.

## Step 1: Deploy to Render

1. Sign in to [render.com](https://render.com).
2. **New +** → **Web Service** → connect this GitHub repo.
3. Pick branch `main` (or whichever branch holds `render.yaml`).
4. Render detects `render.yaml` automatically. Confirm the suggested
   service name (`helixgen-mcp`) and click **Create Web Service**.
5. First build takes ~2 min (mostly the `helixgen bootstrap` step which
   clones `sensorium/phelix`). Watch the build log.
6. Once **Live**, copy the URL — something like
   `https://helixgen-mcp-xxxx.onrender.com`. Your MCP endpoint is that
   URL + `/mcp`.

## Step 2: Add as a custom connector in claude.ai

1. claude.ai → **Settings** → **Connectors** → **Add custom connector**.
2. Name: `helixgen`. URL: `https://helixgen-mcp-xxxx.onrender.com/mcp`
   (your URL from Step 1).
3. Save. Claude should report the connector handshake succeeded and list
   three available tools.

## Step 3: Smoke test

In a new claude.ai chat:

> List the available helixgen amp blocks.

Claude should call `list_blocks(category="amp")` and return a categorized
list. If you see a timeout (~45s) on the first request after a quiet
period, that's Render's free-tier cold start — subsequent requests are fast.

## Known limitations (v1)

- **No auth.** Anyone with the URL can use it. If traffic gets noisy, take
  the service down via Render's dashboard.
- **No rate limiting.** Same caveat.
- **Cold starts.** Render free tier suspends after 15 min of idle; first
  request takes 30–60s to wake. Mitigations (UptimeRobot keepalive,
  upgrading off free tier) are out of scope for v1.
- **No IR support.** The `ir` field in specs is silently ignored. IR
  blocks use whatever canonical hash the bundled library carries.
- **Stateless.** Every generate call rebuilds the library handle. No
  per-user storage, no preset history.

## Updating the deployment

Render auto-deploys on push to the connected branch. To redeploy without
a code change (e.g. to pick up a new phelix snapshot), use Render's
**Manual Deploy** → **Clear build cache & deploy**.
```

- [ ] **Step 10.2: Commit.**

```bash
git add mcp_server/DEPLOY.md
git commit -m "docs(mcp): DEPLOY.md — Render setup + claude.ai connector wiring

Three-step user guide: deploy to Render, add custom connector in claude.ai,
smoke test. Documents the known v1 limitations (no auth, cold starts,
no IRs).
"
```

---

## Final verification

- [ ] **Run the full test suite.**

```bash
pytest -q
```

Expected: previous baseline + new mcp tests passing (or skipping cleanly).

- [ ] **List the new files.**

```bash
git diff --name-only main..HEAD
```

Expected files (in some order):
- `pyproject.toml`
- `mcp_server/__init__.py`
- `mcp_server/data/__init__.py`
- `mcp_server/data/chassis.json`
- `mcp_server/tools.py`
- `mcp_server/server.py`
- `mcp_server/__main__.py`
- `mcp_server/DEPLOY.md`
- `render.yaml`
- `tests/mcp_server/__init__.py`
- `tests/mcp_server/conftest.py`
- `tests/mcp_server/test_tools.py`
- `tests/mcp_server/test_protocol.py`
- `docs/superpowers/plans/2026-05-30-helixgen-mcp-server.md` (this file)

- [ ] **End-to-end manual check.**

```bash
PORT=10000 python -m mcp_server &
sleep 2
curl -sS -X POST http://localhost:10000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | head -c 500
kill %1 2>/dev/null
```

Expected: JSON or SSE response containing `"name":"list_blocks"`,
`"name":"show_block"`, `"name":"generate_preset"`. If the response is
plaintext or HTML, something is wrong with the transport wiring —
check that `app.run(transport="streamable-http", ...)` is correct for
the installed `mcp` version.
