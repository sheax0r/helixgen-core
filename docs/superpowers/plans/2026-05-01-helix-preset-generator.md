# Helix Preset Generator v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python CLI (`helixgen`) that ingests real Helix `.hlx` exports into a reusable block-schema library and generates new `.hlx` presets from a strict JSON tone spec.

**Architecture:** Two-mode tool. `ingest` parses exports, extracts per-block schemas + a one-time chassis (an empty preset shell), and writes them to a filesystem library at `~/.helixgen/library/`. `generate` consumes a JSON spec, looks up blocks in the library, deep-copies the chassis, places blocks into the chassis's discovered position-key slots, overlays user params, and writes a new `.hlx`. A `bootstrap` subcommand seeds the library from `sensorium/phelix`.

**Tech Stack:** Python 3.11+, `click` (CLI), `pytest` (tests). Standard library for everything else (`json`, `pathlib`, `dataclasses`, `subprocess`, `re`).

**Source spec:** `docs/superpowers/specs/2026-05-01-helix-preset-generator-design.md`
**Deferred features:** `docs/features/parallel-paths.md`

---

## Conventions used throughout this plan

- All commands run from the repo root (`~/git/helixgen`) unless stated otherwise.
- Every code change is preceded by a failing test. After implementation, run the test to confirm it passes, then commit.
- Commit messages follow Conventional Commits: `feat:`, `test:`, `chore:`, `docs:`, `fix:`.
- The library directory in tests is always a `tmp_path` fixture — never the user's real `~/.helixgen/library/`.
- Tests use **synthetic** fixture JSON that encodes the *assumed* shape of Helix exports. Real exports are validated in Task 34. If real exports differ, fixtures and code update together.
- Type hints are used throughout. mypy is **not** wired up — we want strictness in code style without adding a CI gate.
- **Documented assumptions** about the wire format are concentrated in `src/helixgen/ingest.py` and `src/helixgen/chassis.py` near the top of the file as module-level constants, so they're easy to update once we have a real export to compare against.

---

## Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `src/helixgen/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `.gitignore`

- [ ] **Step 1: Write `.gitignore`**

```
# Python
__pycache__/
*.py[cod]
*.egg-info/
build/
dist/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/

# Editors
.vscode/
.idea/
*.swp

# OS
.DS_Store

# Venv
.venv/
venv/
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "helixgen"
version = "0.1.0"
description = "Generate Line 6 Helix .hlx preset files from JSON tone specs."
requires-python = ">=3.11"
dependencies = [
    "click>=8.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4",
]

[project.scripts]
helixgen = "helixgen.cli:cli"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
```

- [ ] **Step 3: Write `src/helixgen/__init__.py`**

```python
__version__ = "0.1.0"
```

- [ ] **Step 4: Write `tests/__init__.py`** (empty file)

```python
```

- [ ] **Step 5: Write `tests/conftest.py`**

```python
"""Shared pytest fixtures for helixgen tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def tmp_library(tmp_path: Path) -> Path:
    """Empty library directory in a tmp dir."""
    lib = tmp_path / "library"
    lib.mkdir()
    return lib


@pytest.fixture
def sample_amp_block() -> dict:
    """Synthetic single-block JSON for an amp."""
    return json.loads((FIXTURES_DIR / "blocks" / "sample_amp.json").read_text())


@pytest.fixture
def sample_cab_block() -> dict:
    """Synthetic single-block JSON for a cab."""
    return json.loads((FIXTURES_DIR / "blocks" / "sample_cab.json").read_text())


@pytest.fixture
def sample_serial_preset() -> dict:
    """Synthetic full-preset JSON, single serial DSP path."""
    return json.loads((FIXTURES_DIR / "presets" / "sample_serial.json").read_text())
```

- [ ] **Step 6: Install in editable mode and verify pytest runs**

Run:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```
Expected: `no tests ran in ... s` (exit 0). If pytest can't import `helixgen`, the package install failed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/ tests/ .gitignore
git commit -m "chore: scaffold helixgen package, click+pytest deps, conftest"
```

---

## Task 2: Synthetic test fixtures

These encode our *assumptions* about the Helix export wire format. They are the contract that drives all subsequent TDD. Task 34 validates them against a real export.

**Files:**
- Create: `tests/fixtures/blocks/sample_amp.json`
- Create: `tests/fixtures/blocks/sample_cab.json`
- Create: `tests/fixtures/presets/sample_serial.json`
- Create: `tests/fixtures/README.md`

- [ ] **Step 1: Write `tests/fixtures/README.md`**

```markdown
# Test fixtures

These files encode the *hypothesized* shape of Helix `.hlx` exports and
`sensorium/phelix` block-export JSON. They are synthetic, not real exports.

If real exports differ from these shapes, update the fixtures and the code
that consumes them together. The hot spots are:

- Block model identification: assumed at top-level key `"@model"`
- Block enabled flag: assumed at `"@enabled"`
- Param keys: assumed top-level on the block JSON, excluding any key
  starting with `@`
- Preset top-level: `version`, `schema`, `data.device`, `data.meta`,
  `data.tone.dsp0.blocks`, `data.tone.dsp1.blocks`
- Block position keys inside a `blocks` dict: assumed `dsp0_block_0`,
  `dsp0_block_1`, ... — but the chassis preserves whatever the source
  preset uses, so deviations are tolerated as long as the keys are stable.

Real exports go in `tests/fixtures/presets/real/` and are gitignored if
they contain identifying info. The Goldfinger spec lives at
`tests/fixtures/specs/goldfinger.json`.
```

- [ ] **Step 2: Write `tests/fixtures/blocks/sample_amp.json`**

```json
{
  "@model": "HD2_AmpBrit2204Custom",
  "@enabled": true,
  "Drive": 0.6,
  "Bass": 0.5,
  "Mid": 0.75,
  "Treble": 0.55,
  "Presence": 0.55,
  "Master": 0.6,
  "Ch Vol": 0.5
}
```

- [ ] **Step 3: Write `tests/fixtures/blocks/sample_cab.json`**

```json
{
  "@model": "HD2_Cab4x12Greenback25",
  "@enabled": true,
  "Mic": "57 Dynamic",
  "Distance": 0.1,
  "Axis": "12° off",
  "High Cut": 8000,
  "Low Cut": 80
}
```

- [ ] **Step 4: Write `tests/fixtures/presets/sample_serial.json`**

```json
{
  "version": 6,
  "schema": "L6Preset",
  "data": {
    "device": { "name": "Helix", "fw": "3.71" },
    "meta": { "name": "Sample Serial Preset", "author": "" },
    "tone": {
      "dsp0": {
        "input": "Multi",
        "output": "Multi",
        "blocks": {
          "dsp0_block_0": {
            "@model": "HD2_DynamicsNoiseGate",
            "@enabled": true,
            "Threshold": 0.4,
            "Decay": 0.3
          },
          "dsp0_block_1": {
            "@model": "HD2_DrvScream808",
            "@enabled": true,
            "Drive": 0.1,
            "Tone": 0.5,
            "Level": 0.6
          },
          "dsp0_block_2": {
            "@model": "HD2_AmpBrit2204Custom",
            "@enabled": true,
            "Drive": 0.6,
            "Bass": 0.5,
            "Mid": 0.75,
            "Treble": 0.55,
            "Presence": 0.55,
            "Master": 0.6,
            "Ch Vol": 0.5
          },
          "dsp0_block_3": {
            "@model": "HD2_Cab4x12Greenback25",
            "@enabled": true,
            "Mic": "57 Dynamic",
            "Distance": 0.1,
            "Axis": "12° off",
            "High Cut": 8000,
            "Low Cut": 80
          }
        }
      },
      "dsp1": {
        "input": "None",
        "output": "Multi",
        "blocks": {}
      }
    }
  }
}
```

- [ ] **Step 5: Sanity check fixtures are valid JSON**

Run:
```bash
python -c "import json, pathlib; [json.loads(p.read_text()) for p in pathlib.Path('tests/fixtures').rglob('*.json')]; print('ok')"
```
Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/
git commit -m "test: add synthetic block and preset fixtures encoding format assumptions"
```

---

## Task 3: Block dataclass

The `Block` is the in-memory representation of a library entry. It serializes to and from the on-disk JSON shape described in the spec.

**Files:**
- Create: `src/helixgen/library.py`
- Create: `tests/test_library.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_library.py`:
```python
from helixgen.library import Block


def test_block_round_trips_through_dict():
    block = Block(
        model_id="HD2_AmpBrit2204Custom",
        category="amp",
        display_name="Brit 2204",
        aliases=["JCM800"],
        params={
            "Drive": {"type": "float", "default": 0.5, "observed_range": [0, 1]},
        },
        exemplar={"@model": "HD2_AmpBrit2204Custom", "Drive": 0.5},
        first_seen={"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"},
    )
    as_dict = block.to_dict()
    assert as_dict["model_id"] == "HD2_AmpBrit2204Custom"
    assert as_dict["display_name"] == "Brit 2204"
    assert as_dict["aliases"] == ["JCM800"]
    restored = Block.from_dict(as_dict)
    assert restored == block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_library.py::test_block_round_trips_through_dict -v`
Expected: FAIL — `ImportError: cannot import name 'Block' from 'helixgen.library'`

- [ ] **Step 3: Write minimal implementation**

In `src/helixgen/library.py`:
```python
"""Helix block library: filesystem read/write, indexing, lookup."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Block:
    """A single Helix block, as stored in the library."""

    model_id: str
    category: str
    display_name: str
    params: dict[str, dict[str, Any]]
    exemplar: dict[str, Any]
    first_seen: dict[str, str]
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Block":
        return cls(
            model_id=data["model_id"],
            category=data["category"],
            display_name=data["display_name"],
            aliases=list(data.get("aliases", [])),
            params=dict(data.get("params", {})),
            exemplar=dict(data.get("exemplar", {})),
            first_seen=dict(data.get("first_seen", {})),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_library.py::test_block_round_trips_through_dict -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/library.py tests/test_library.py
git commit -m "feat: add Block dataclass with dict round-trip"
```

---

## Task 4: Default library path + env-var override

**Files:**
- Modify: `src/helixgen/library.py`
- Modify: `tests/test_library.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_library.py`:
```python
import os
from pathlib import Path

import pytest

from helixgen.library import default_library_path


def test_default_library_path_uses_home(monkeypatch):
    monkeypatch.delenv("HELIXGEN_LIBRARY", raising=False)
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    assert default_library_path() == Path("/tmp/fake-home/.helixgen/library")


def test_default_library_path_honors_env_var(monkeypatch):
    monkeypatch.setenv("HELIXGEN_LIBRARY", "/custom/lib")
    assert default_library_path() == Path("/custom/lib")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_library.py -v -k default_library_path`
Expected: FAIL — `ImportError: cannot import name 'default_library_path'`.

- [ ] **Step 3: Implement**

Add to `src/helixgen/library.py` (top of file after imports):
```python
import os
from pathlib import Path


def default_library_path() -> Path:
    """Return the library path, honoring HELIXGEN_LIBRARY env var."""
    env = os.environ.get("HELIXGEN_LIBRARY")
    if env:
        return Path(env)
    return Path(os.environ["HOME"]) / ".helixgen" / "library"


def default_cache_path() -> Path:
    """Return the cache path used for cloned upstream repos."""
    return Path(os.environ["HOME"]) / ".helixgen" / ".cache"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_library.py -v -k default_library_path`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/library.py tests/test_library.py
git commit -m "feat: add default_library_path and default_cache_path with env override"
```

---

## Task 5: humanize_model_id and infer_category

These are pure helpers used during ingest. They live in `ingest.py` because they're ingest-time concerns.

**Files:**
- Create: `src/helixgen/ingest.py`
- Create: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_ingest.py`:
```python
import pytest

from helixgen.ingest import humanize_model_id, infer_category


@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("HD2_AmpBrit2204Custom", "Brit 2204 Custom"),
        ("HD2_Cab4x12Greenback25", "4x12 Greenback 25"),
        ("HD2_DrvScream808", "Scream 808"),
        ("HD2_DynamicsNoiseGate", "Noise Gate"),
        ("HD2_RvbPlate", "Plate"),
        ("UnknownPrefixThing", "Unknown Prefix Thing"),
    ],
)
def test_humanize_model_id(model_id, expected):
    assert humanize_model_id(model_id) == expected


@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("HD2_AmpBrit2204Custom", "amp"),
        ("HD2_Cab4x12Greenback25", "cab"),
        ("HD2_DrvScream808", "drive"),
        ("HD2_DistFuzz", "drive"),
        ("HD2_RvbPlate", "reverb"),
        ("HD2_DlyDigital", "delay"),
        ("HD2_EQParametric", "eq"),
        ("HD2_DynamicsNoiseGate", "dynamics"),
        ("HD2_ModChorus", "modulation"),
        ("HD2_PitchShift", "pitch"),
        ("HD2_WahCryBaby", "filter"),
        ("HD2_TotallyNewThing", "uncategorized"),
        ("WeirdNoPrefix", "uncategorized"),
    ],
)
def test_infer_category(model_id, expected):
    assert infer_category(model_id) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

In `src/helixgen/ingest.py`:
```python
"""Ingest module: parse exported .hlx and single-block JSON, extract schemas."""
from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# DOCUMENTED ASSUMPTIONS about the Helix export wire format.
# Verify against a real exported .hlx in Task 34. If the real shape differs,
# update these constants and the synthetic fixtures together.
# ---------------------------------------------------------------------------
RAW_BLOCK_MODEL_KEY = "@model"          # block JSON: model identifier
RAW_BLOCK_CATEGORY_KEY = "@category"    # block JSON: optional category override
RAW_BLOCK_NAME_KEY = "@name"            # block JSON: optional human-readable name
RAW_BLOCK_SYSTEM_KEY_PREFIX = "@"       # any key starting with this is metadata, not a param

PRESET_TONE_KEY = ("data", "tone")      # full preset: path to dsp0/dsp1 root
PRESET_DSP_KEYS = ("dsp0", "dsp1")
PRESET_BLOCKS_KEY = "blocks"            # within each dsp, the block dict


# ---------------------------------------------------------------------------
# Category inference from model_id prefix.
# Order matters: most specific first. Add new prefixes here as discovered.
# ---------------------------------------------------------------------------
_CATEGORY_PREFIXES: list[tuple[str, str]] = [
    ("HD2_Amp", "amp"),
    ("HD2_Cab", "cab"),
    ("HD2_Drv", "drive"),
    ("HD2_Dist", "drive"),
    ("HD2_Rvb", "reverb"),
    ("HD2_Dly", "delay"),
    ("HD2_EQ", "eq"),
    ("HD2_Dynamics", "dynamics"),
    ("HD2_Dyn", "dynamics"),
    ("HD2_Mod", "modulation"),
    ("HD2_Pitch", "pitch"),
    ("HD2_Wah", "filter"),
]


def infer_category(model_id: str) -> str:
    """Return the category for a model_id, or 'uncategorized' if unknown."""
    for prefix, category in _CATEGORY_PREFIXES:
        if model_id.startswith(prefix):
            return category
    return "uncategorized"


# Strip a known category prefix; insert a space before any uppercase letter
# that follows a lowercase letter or digit; collapse whitespace.
_HUMANIZE_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def humanize_model_id(model_id: str) -> str:
    """Turn a model_id like 'HD2_AmpBrit2204Custom' into 'Brit 2204 Custom'."""
    body = model_id
    for prefix, _ in _CATEGORY_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    else:
        # No known prefix: also strip any leading "HD2_" if present
        if body.startswith("HD2_"):
            body = body[4:]
    spaced = _HUMANIZE_SPLIT_RE.sub(" ", body)
    return " ".join(spaced.split())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ingest.py -v`
Expected: PASS for all parametrized cases.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ingest.py tests/test_ingest.py
git commit -m "feat: add humanize_model_id and infer_category helpers"
```

---

## Task 6: Library — save_block and load_block

**Files:**
- Modify: `src/helixgen/library.py`
- Modify: `tests/test_library.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_library.py`:
```python
from helixgen.library import Library


def make_block(**overrides):
    """Helper: build a minimal Block with overrideable fields."""
    defaults = dict(
        model_id="HD2_AmpBrit2204Custom",
        category="amp",
        display_name="Brit 2204",
        aliases=[],
        params={"Drive": {"type": "float", "default": 0.5, "observed_range": [0, 1]}},
        exemplar={"@model": "HD2_AmpBrit2204Custom", "Drive": 0.5},
        first_seen={"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"},
    )
    defaults.update(overrides)
    return Block(**defaults)


def test_save_block_writes_to_category_subdir(tmp_library):
    lib = Library(tmp_library)
    block = make_block()
    lib.save_block(block)
    expected_path = tmp_library / "blocks" / "amp" / "HD2_AmpBrit2204Custom.json"
    assert expected_path.exists()


def test_load_block_round_trip(tmp_library):
    lib = Library(tmp_library)
    block = make_block()
    lib.save_block(block)
    loaded = lib.load_block("HD2_AmpBrit2204Custom")
    assert loaded == block


def test_load_block_missing_raises(tmp_library):
    lib = Library(tmp_library)
    with pytest.raises(KeyError):
        lib.load_block("HD2_NotPresent")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_library.py -v -k "save_block or load_block"`
Expected: FAIL — `ImportError: cannot import name 'Library'`.

- [ ] **Step 3: Implement**

Append to `src/helixgen/library.py`:
```python
import json


class Library:
    """Filesystem-backed block library at a given root directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.blocks_dir = self.root / "blocks"

    def block_path(self, model_id: str, category: str) -> Path:
        return self.blocks_dir / category / f"{model_id}.json"

    def save_block(self, block: Block) -> Path:
        path = self.block_path(block.model_id, block.category)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(block.to_dict(), indent=2, sort_keys=False))
        return path

    def load_block(self, model_id: str) -> Block:
        # Search all category subdirs for the model_id
        if self.blocks_dir.exists():
            for category_dir in self.blocks_dir.iterdir():
                if not category_dir.is_dir():
                    continue
                candidate = category_dir / f"{model_id}.json"
                if candidate.exists():
                    return Block.from_dict(json.loads(candidate.read_text()))
        raise KeyError(f"No block with model_id {model_id!r} in library at {self.root}")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_library.py -v`
Expected: PASS for all tests.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/library.py tests/test_library.py
git commit -m "feat: Library.save_block and load_block with category subdirs"
```

---

## Task 7: Library — list_blocks and find_block

**Files:**
- Modify: `src/helixgen/library.py`
- Modify: `tests/test_library.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_library.py`:
```python
def test_list_blocks_returns_all(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(model_id="HD2_AmpBrit2204Custom", category="amp"))
    lib.save_block(make_block(model_id="HD2_Cab4x12", category="cab", display_name="4x12"))
    blocks = sorted(lib.list_blocks(), key=lambda b: b.model_id)
    assert [b.model_id for b in blocks] == ["HD2_AmpBrit2204Custom", "HD2_Cab4x12"]


def test_list_blocks_filters_by_category(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(model_id="HD2_AmpBrit2204Custom", category="amp"))
    lib.save_block(make_block(model_id="HD2_Cab4x12", category="cab", display_name="4x12"))
    blocks = list(lib.list_blocks(category="amp"))
    assert [b.model_id for b in blocks] == ["HD2_AmpBrit2204Custom"]


def test_find_block_by_display_name(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(model_id="HD2_AmpBrit2204Custom", display_name="Brit 2204"))
    found = lib.find_block("Brit 2204")
    assert found.model_id == "HD2_AmpBrit2204Custom"


def test_find_block_by_alias(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(
        model_id="HD2_AmpBrit2204Custom",
        display_name="Brit 2204",
        aliases=["JCM800"],
    ))
    assert lib.find_block("JCM800").model_id == "HD2_AmpBrit2204Custom"


def test_find_block_by_model_id(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block())
    assert lib.find_block("HD2_AmpBrit2204Custom").model_id == "HD2_AmpBrit2204Custom"


def test_find_block_missing_raises_keyerror(tmp_library):
    lib = Library(tmp_library)
    with pytest.raises(KeyError):
        lib.find_block("Nonexistent Block")


def test_find_block_ambiguous_raises_with_candidates(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(
        model_id="HD2_RvbPlate", category="reverb", display_name="Plate Reverb"
    ))
    lib.save_block(make_block(
        model_id="HD2_LegacyPlateReverb",
        category="reverb",
        display_name="Plate Reverb",
    ))
    with pytest.raises(LookupError) as excinfo:
        lib.find_block("Plate Reverb")
    msg = str(excinfo.value)
    assert "HD2_RvbPlate" in msg
    assert "HD2_LegacyPlateReverb" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_library.py -v -k "list_blocks or find_block"`
Expected: FAIL — methods missing.

- [ ] **Step 3: Implement**

Append to the `Library` class in `src/helixgen/library.py`:
```python
    def list_blocks(self, category: str | None = None) -> list[Block]:
        """Return all blocks, optionally filtered to one category."""
        if not self.blocks_dir.exists():
            return []
        results: list[Block] = []
        category_dirs = (
            [self.blocks_dir / category] if category else list(self.blocks_dir.iterdir())
        )
        for cat_dir in category_dirs:
            if not cat_dir.is_dir():
                continue
            for entry in cat_dir.glob("*.json"):
                results.append(Block.from_dict(json.loads(entry.read_text())))
        return results

    def find_block(self, name_or_id: str) -> Block:
        """Resolve a display name, alias, or model_id to a single Block.

        Raises KeyError if no match. Raises LookupError if multiple matches.
        """
        all_blocks = self.list_blocks()
        # Exact model_id match wins over name match, since model_id is unique.
        for block in all_blocks:
            if block.model_id == name_or_id:
                return block
        # Then collect all blocks whose display_name or aliases match.
        matches = [
            b for b in all_blocks
            if b.display_name == name_or_id or name_or_id in b.aliases
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(b.model_id for b in matches)
            raise LookupError(
                f"Block name {name_or_id!r} matches multiple library entries: {ids}. "
                f"Use the model_id explicitly."
            )
        raise KeyError(
            f"Block {name_or_id!r} not found in library at {self.root}. "
            f"Try `helixgen ingest <export.hlx>` or `helixgen bootstrap`."
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_library.py -v`
Expected: PASS for all tests.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/library.py tests/test_library.py
git commit -m "feat: Library.list_blocks and find_block with ambiguity errors"
```

---

## Task 8: Library — index rebuild

The index is *derived* from the block files; rebuilding it is idempotent and can be triggered at any time.

**Files:**
- Modify: `src/helixgen/library.py`
- Modify: `tests/test_library.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_library.py`:
```python
def test_rebuild_index_writes_json(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(
        model_id="HD2_AmpBrit2204Custom",
        display_name="Brit 2204",
        aliases=["JCM800"],
        category="amp",
    ))
    lib.save_block(make_block(
        model_id="HD2_Cab4x12Greenback25",
        category="cab",
        display_name="4x12 Greenback 25",
    ))

    lib.rebuild_index()

    index_path = tmp_library / "index.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text())

    # name → model_id resolution
    assert index["names"]["Brit 2204"] == ["HD2_AmpBrit2204Custom"]
    assert index["names"]["JCM800"] == ["HD2_AmpBrit2204Custom"]
    assert index["names"]["4x12 Greenback 25"] == ["HD2_Cab4x12Greenback25"]

    # model_id → category
    assert index["categories"]["HD2_AmpBrit2204Custom"] == "amp"
    assert index["categories"]["HD2_Cab4x12Greenback25"] == "cab"


def test_rebuild_index_records_ambiguity(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(
        model_id="HD2_RvbPlate", category="reverb", display_name="Plate Reverb"
    ))
    lib.save_block(make_block(
        model_id="HD2_LegacyPlateReverb",
        category="reverb",
        display_name="Plate Reverb",
    ))

    lib.rebuild_index()

    index = json.loads((tmp_library / "index.json").read_text())
    assert sorted(index["names"]["Plate Reverb"]) == [
        "HD2_LegacyPlateReverb",
        "HD2_RvbPlate",
    ]


def test_rebuild_index_on_empty_library(tmp_library):
    lib = Library(tmp_library)
    lib.rebuild_index()
    index = json.loads((tmp_library / "index.json").read_text())
    assert index == {"names": {}, "categories": {}}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_library.py -v -k rebuild_index`
Expected: FAIL — `rebuild_index` missing.

- [ ] **Step 3: Implement**

Append to the `Library` class:
```python
    def rebuild_index(self) -> dict[str, Any]:
        """Re-derive index.json from the on-disk block files."""
        names: dict[str, list[str]] = {}
        categories: dict[str, str] = {}

        for block in self.list_blocks():
            categories[block.model_id] = block.category
            for name in [block.display_name, *block.aliases]:
                if not name:
                    continue
                names.setdefault(name, []).append(block.model_id)

        index = {"names": names, "categories": categories}
        index_path = self.root / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index, indent=2, sort_keys=True))
        return index
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_library.py -v -k rebuild_index`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/library.py tests/test_library.py
git commit -m "feat: Library.rebuild_index derives names+categories from block files"
```

---

## Task 9: Library — chassis save/load

**Files:**
- Modify: `src/helixgen/library.py`
- Modify: `tests/test_library.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_library.py`:
```python
def test_chassis_save_load_round_trip(tmp_library):
    lib = Library(tmp_library)
    chassis = {
        "version": 6,
        "schema": "L6Preset",
        "data": {"meta": {"name": ""}, "tone": {"dsp0": {"blocks": {}}}},
        "_helixgen": {"position_keys": {"dsp0": ["dsp0_block_0"], "dsp1": []}},
    }
    assert not lib.has_chassis()
    lib.save_chassis(chassis)
    assert lib.has_chassis()
    assert lib.load_chassis() == chassis


def test_load_chassis_missing_raises(tmp_library):
    lib = Library(tmp_library)
    with pytest.raises(FileNotFoundError):
        lib.load_chassis()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_library.py -v -k chassis`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to the `Library` class:
```python
    @property
    def chassis_path(self) -> Path:
        return self.root / "chassis.json"

    def has_chassis(self) -> bool:
        return self.chassis_path.exists()

    def save_chassis(self, chassis: dict[str, Any]) -> Path:
        self.chassis_path.parent.mkdir(parents=True, exist_ok=True)
        self.chassis_path.write_text(json.dumps(chassis, indent=2))
        return self.chassis_path

    def load_chassis(self) -> dict[str, Any]:
        if not self.chassis_path.exists():
            raise FileNotFoundError(
                f"No chassis at {self.chassis_path}. Run `helixgen ingest <export.hlx>` first."
            )
        return json.loads(self.chassis_path.read_text())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_library.py -v -k chassis`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/library.py tests/test_library.py
git commit -m "feat: Library.save_chassis, load_chassis, has_chassis"
```

---

## Task 10: Library — schema diffing and conflict detection

When the same `model_id` is ingested twice, we compare param schemas. Same → skip. Different → write `<model_id>.v2.json`.

**Files:**
- Modify: `src/helixgen/library.py`
- Modify: `tests/test_library.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_library.py`:
```python
from helixgen.library import IngestStatus


def test_save_block_first_time_returns_new(tmp_library):
    lib = Library(tmp_library)
    status = lib.save_block_with_dedup(make_block())
    assert status == IngestStatus.NEW


def test_save_block_same_schema_returns_match(tmp_library):
    lib = Library(tmp_library)
    lib.save_block_with_dedup(make_block())
    # Same schema (same param keys + types), different exemplar values
    block_v2 = make_block(
        exemplar={"@model": "HD2_AmpBrit2204Custom", "Drive": 0.99},
    )
    status = lib.save_block_with_dedup(block_v2)
    assert status == IngestStatus.MATCH


def test_save_block_different_schema_writes_v2_file(tmp_library):
    lib = Library(tmp_library)
    lib.save_block_with_dedup(make_block())
    # Schema mismatch: a new param appears
    new_params = {
        "Drive": {"type": "float", "default": 0.5, "observed_range": [0, 1]},
        "NewParam": {"type": "float", "default": 0.0, "observed_range": [0, 1]},
    }
    block_changed = make_block(params=new_params)
    status = lib.save_block_with_dedup(block_changed)
    assert status == IngestStatus.CONFLICT
    v2_path = tmp_library / "blocks" / "amp" / "HD2_AmpBrit2204Custom.v2.json"
    assert v2_path.exists()


def test_save_block_third_conflict_writes_v3(tmp_library):
    lib = Library(tmp_library)
    lib.save_block_with_dedup(make_block())
    lib.save_block_with_dedup(make_block(params={
        "Drive": {"type": "float", "default": 0.5, "observed_range": [0, 1]},
        "NewParam": {"type": "float", "default": 0.0, "observed_range": [0, 1]},
    }))
    lib.save_block_with_dedup(make_block(params={
        "TotallyDifferent": {"type": "float", "default": 0.0, "observed_range": [0, 1]},
    }))
    v3_path = tmp_library / "blocks" / "amp" / "HD2_AmpBrit2204Custom.v3.json"
    assert v3_path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_library.py -v -k save_block`
Expected: FAIL — `IngestStatus` and `save_block_with_dedup` missing.

- [ ] **Step 3: Implement**

Add at the top of `library.py`, after imports:
```python
from enum import Enum


class IngestStatus(Enum):
    NEW = "new"
    MATCH = "match"
    CONFLICT = "conflict"
```

Then append to the `Library` class:
```python
    def save_block_with_dedup(self, block: Block) -> IngestStatus:
        """Save a block, deduplicating by model_id and detecting conflicts."""
        path = self.block_path(block.model_id, block.category)
        if not path.exists():
            self.save_block(block)
            return IngestStatus.NEW

        existing = Block.from_dict(json.loads(path.read_text()))
        if _schemas_match(existing.params, block.params):
            return IngestStatus.MATCH

        # Conflict: find next vN suffix and write there.
        v = 2
        while True:
            conflict_path = path.with_name(f"{block.model_id}.v{v}.json")
            if not conflict_path.exists():
                conflict_path.write_text(
                    json.dumps(block.to_dict(), indent=2, sort_keys=False)
                )
                return IngestStatus.CONFLICT
            v += 1


def _schemas_match(a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]]) -> bool:
    """Two schemas match iff they have the same param keys and the same types per key."""
    if set(a.keys()) != set(b.keys()):
        return False
    for key in a:
        if a[key].get("type") != b[key].get("type"):
            return False
    return True
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_library.py -v -k save_block`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/library.py tests/test_library.py
git commit -m "feat: save_block_with_dedup with NEW/MATCH/CONFLICT statuses"
```

---

## Task 11: ingest.detect_shape

**Files:**
- Modify: `src/helixgen/ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingest.py`:
```python
from helixgen.ingest import Shape, detect_shape


def test_detect_full_preset(sample_serial_preset):
    assert detect_shape(sample_serial_preset) == Shape.PRESET


def test_detect_single_block(sample_amp_block):
    assert detect_shape(sample_amp_block) == Shape.SINGLE_BLOCK


def test_detect_unknown_shape():
    assert detect_shape({"foo": "bar"}) == Shape.UNKNOWN
    assert detect_shape([]) == Shape.UNKNOWN
    assert detect_shape("just a string") == Shape.UNKNOWN
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -v -k detect`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/ingest.py`:
```python
from enum import Enum


class Shape(Enum):
    PRESET = "preset"
    SINGLE_BLOCK = "single_block"
    UNKNOWN = "unknown"


def detect_shape(data: Any) -> Shape:
    """Detect whether a parsed JSON value is a full preset, a single block, or neither."""
    if not isinstance(data, dict):
        return Shape.UNKNOWN

    # Full preset: has top-level version + schema + data.tone with at least one DSP
    if (
        "version" in data
        and "schema" in data
        and isinstance(data.get("data"), dict)
        and isinstance(data["data"].get("tone"), dict)
        and any(dsp in data["data"]["tone"] for dsp in PRESET_DSP_KEYS)
    ):
        return Shape.PRESET

    # Single block: has the model identifier at the top level
    if RAW_BLOCK_MODEL_KEY in data:
        return Shape.SINGLE_BLOCK

    return Shape.UNKNOWN
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ingest.py -v -k detect`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ingest.py tests/test_ingest.py
git commit -m "feat: ingest.detect_shape for preset vs single-block JSON"
```

---

## Task 12: ingest.extract_schema

Given one raw block JSON, infer a per-param schema (type + default) by walking the top-level keys, excluding system keys (`@*`).

**Files:**
- Modify: `src/helixgen/ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest.py`:
```python
from helixgen.ingest import extract_schema


def test_extract_schema_floats(sample_amp_block):
    schema = extract_schema(sample_amp_block)
    assert "Drive" in schema
    assert schema["Drive"]["type"] == "float"
    assert schema["Drive"]["default"] == 0.6
    assert schema["Drive"]["observed_range"] == [0.6, 0.6]


def test_extract_schema_skips_system_keys(sample_amp_block):
    schema = extract_schema(sample_amp_block)
    assert "@model" not in schema
    assert "@enabled" not in schema


def test_extract_schema_int_and_string(sample_cab_block):
    schema = extract_schema(sample_cab_block)
    assert schema["High Cut"]["type"] == "int"
    assert schema["High Cut"]["default"] == 8000
    assert schema["Mic"]["type"] == "str"
    assert schema["Mic"]["default"] == "57 Dynamic"


def test_extract_schema_handles_bool():
    schema = extract_schema({"@model": "X", "Loop": True})
    assert schema["Loop"]["type"] == "bool"
    assert schema["Loop"]["default"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -v -k extract_schema`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/ingest.py`:
```python
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
    """Extract a per-parameter schema from a raw block JSON.

    Rules:
    - Top-level keys starting with `@` are system metadata, not params.
    - Each remaining key becomes a schema entry whose type is inferred from
      the value, default is the value itself, and observed_range starts as
      [value, value] for numerics and is omitted for non-numerics.
    """
    schema: dict[str, dict[str, Any]] = {}
    for key, value in raw_block.items():
        if isinstance(key, str) and key.startswith(RAW_BLOCK_SYSTEM_KEY_PREFIX):
            continue
        type_name = _value_type_name(value)
        entry: dict[str, Any] = {"type": type_name, "default": value}
        if type_name in ("int", "float"):
            entry["observed_range"] = [value, value]
        schema[key] = entry
    return schema
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ingest.py -v -k extract_schema`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ingest.py tests/test_ingest.py
git commit -m "feat: ingest.extract_schema infers per-param type+default+range"
```

---

## Task 13: chassis.extract_chassis

Strip blocks out of a full preset; record original position keys so generation can reuse them.

**Files:**
- Create: `src/helixgen/chassis.py`
- Create: `tests/test_chassis.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_chassis.py`:
```python
from helixgen.chassis import extract_chassis


def test_extract_chassis_strips_blocks(sample_serial_preset):
    chassis = extract_chassis(sample_serial_preset)
    assert chassis["data"]["tone"]["dsp0"]["blocks"] == {}
    assert chassis["data"]["tone"]["dsp1"]["blocks"] == {}


def test_extract_chassis_records_position_keys(sample_serial_preset):
    chassis = extract_chassis(sample_serial_preset)
    keys = chassis["_helixgen"]["position_keys"]
    assert keys["dsp0"] == ["dsp0_block_0", "dsp0_block_1", "dsp0_block_2", "dsp0_block_3"]
    assert keys["dsp1"] == []


def test_extract_chassis_preserves_meta_and_routing(sample_serial_preset):
    chassis = extract_chassis(sample_serial_preset)
    assert chassis["version"] == 6
    assert chassis["schema"] == "L6Preset"
    assert chassis["data"]["device"]["name"] == "Helix"
    assert chassis["data"]["tone"]["dsp0"]["input"] == "Multi"


def test_extract_chassis_does_not_mutate_input(sample_serial_preset):
    original = json.loads(json.dumps(sample_serial_preset))  # deep copy
    extract_chassis(sample_serial_preset)
    assert sample_serial_preset == original


import json
```

(Move the `import json` line to the top of the file with the other imports — left here for clarity of what's needed.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_chassis.py -v`
Expected: FAIL — `helixgen.chassis` module missing.

- [ ] **Step 3: Implement**

In `src/helixgen/chassis.py`:
```python
"""Chassis: empty-preset shell extracted from a real export, used as generation template."""
from __future__ import annotations

import copy
from typing import Any

from helixgen.ingest import PRESET_DSP_KEYS, PRESET_BLOCKS_KEY


def extract_chassis(preset: dict[str, Any]) -> dict[str, Any]:
    """Return a chassis: a deep copy of `preset` with all blocks removed.

    Records the original position keys (the keys of each dsp's `blocks` dict)
    under `_helixgen.position_keys.{dsp0, dsp1}` so generation can reuse them.
    """
    chassis = copy.deepcopy(preset)
    tone = chassis.setdefault("data", {}).setdefault("tone", {})

    position_keys: dict[str, list[str]] = {}
    for dsp_key in PRESET_DSP_KEYS:
        dsp = tone.get(dsp_key)
        if dsp is None:
            position_keys[dsp_key] = []
            continue
        blocks = dsp.get(PRESET_BLOCKS_KEY, {})
        position_keys[dsp_key] = list(blocks.keys())
        dsp[PRESET_BLOCKS_KEY] = {}

    chassis.setdefault("_helixgen", {})["position_keys"] = position_keys
    return chassis
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_chassis.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/chassis.py tests/test_chassis.py
git commit -m "feat: chassis.extract_chassis with position-key preservation"
```

---

## Task 14: ingest — extract blocks from a full preset, and from a single-block file

**Files:**
- Modify: `src/helixgen/ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest.py`:
```python
from helixgen.ingest import extract_blocks_from_preset, extract_block_from_single


def test_extract_blocks_from_preset(sample_serial_preset):
    blocks = extract_blocks_from_preset(sample_serial_preset)
    model_ids = [b["@model"] for b in blocks]
    assert model_ids == [
        "HD2_DynamicsNoiseGate",
        "HD2_DrvScream808",
        "HD2_AmpBrit2204Custom",
        "HD2_Cab4x12Greenback25",
    ]


def test_extract_blocks_from_preset_handles_empty_dsp1(sample_serial_preset):
    # dsp1 is empty in the fixture; should not error
    blocks = extract_blocks_from_preset(sample_serial_preset)
    assert len(blocks) == 4


def test_extract_block_from_single(sample_amp_block):
    block = extract_block_from_single(sample_amp_block)
    assert block["@model"] == "HD2_AmpBrit2204Custom"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -v -k extract_blocks`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/ingest.py`:
```python
def extract_blocks_from_preset(preset: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk dsp0 + dsp1 blocks and return a flat list of raw block dicts in order."""
    tone = preset.get("data", {}).get("tone", {})
    blocks: list[dict[str, Any]] = []
    for dsp_key in PRESET_DSP_KEYS:
        dsp = tone.get(dsp_key, {})
        for block in dsp.get(PRESET_BLOCKS_KEY, {}).values():
            blocks.append(block)
    return blocks


def extract_block_from_single(raw: dict[str, Any]) -> dict[str, Any]:
    """A single-block JSON file is already a raw block; return it."""
    return raw
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ingest.py -v -k extract_blocks`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ingest.py tests/test_ingest.py
git commit -m "feat: ingest.extract_blocks_from_preset and extract_block_from_single"
```

---

## Task 15: ingest.block_from_raw — assemble a Block from a raw block dict

**Files:**
- Modify: `src/helixgen/ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingest.py`:
```python
from helixgen.ingest import block_from_raw


def test_block_from_raw_uses_humanized_name_when_no_explicit_name(sample_amp_block):
    source_info = {"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"}
    block = block_from_raw(sample_amp_block, source_info)
    assert block.model_id == "HD2_AmpBrit2204Custom"
    assert block.category == "amp"
    assert block.display_name == "Brit 2204 Custom"
    assert "Drive" in block.params
    assert block.exemplar == sample_amp_block
    assert block.first_seen == source_info


def test_block_from_raw_prefers_explicit_name_field():
    raw = {"@model": "HD2_AmpBrit2204Custom", "@name": "Brit JCM 800", "Drive": 0.5}
    block = block_from_raw(raw, {"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"})
    assert block.display_name == "Brit JCM 800"


def test_block_from_raw_prefers_explicit_category_field():
    raw = {"@model": "HD2_TotallyNewThing", "@category": "amp", "Drive": 0.5}
    block = block_from_raw(raw, {"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"})
    assert block.category == "amp"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -v -k block_from_raw`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/ingest.py`:
```python
from helixgen.library import Block


def block_from_raw(raw: dict[str, Any], source_info: dict[str, str]) -> Block:
    """Build a Block dataclass from a single raw block dict + source provenance."""
    model_id = raw[RAW_BLOCK_MODEL_KEY]
    category = raw.get(RAW_BLOCK_CATEGORY_KEY) or infer_category(model_id)
    display_name = raw.get(RAW_BLOCK_NAME_KEY) or humanize_model_id(model_id)
    params = extract_schema(raw)
    return Block(
        model_id=model_id,
        category=category,
        display_name=display_name,
        aliases=[],
        params=params,
        exemplar=raw,
        first_seen=dict(source_info),
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ingest.py -v -k block_from_raw`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ingest.py tests/test_ingest.py
git commit -m "feat: ingest.block_from_raw assembles a Block dataclass"
```

---

## Task 16: ingest.ingest_file — single-file orchestrator

**Files:**
- Modify: `src/helixgen/ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest.py`:
```python
import json
from dataclasses import dataclass
from pathlib import Path

from helixgen.ingest import ingest_file, IngestSummary
from helixgen.library import Library, IngestStatus


def test_ingest_file_full_preset(tmp_library, sample_serial_preset, tmp_path):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    summary = ingest_file(preset_path, lib)

    # 4 blocks in the fixture preset
    assert summary.new == 4
    assert summary.matched == 0
    assert summary.conflicted == 0
    # Library should now have 4 block files
    assert len(lib.list_blocks()) == 4


def test_ingest_file_single_block(tmp_library, sample_amp_block, tmp_path):
    block_path = tmp_path / "amp.json"
    block_path.write_text(json.dumps(sample_amp_block))
    lib = Library(tmp_library)

    summary = ingest_file(block_path, lib)

    assert summary.new == 1
    assert len(lib.list_blocks()) == 1


def test_ingest_file_idempotent(tmp_library, sample_serial_preset, tmp_path):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    first = ingest_file(preset_path, lib)
    second = ingest_file(preset_path, lib)

    assert first.new == 4
    assert second.new == 0
    assert second.matched == 4


def test_ingest_file_unparseable_returns_skipped(tmp_library, tmp_path):
    bad_path = tmp_path / "bad.hlx"
    bad_path.write_text("not json {{{")
    lib = Library(tmp_library)

    summary = ingest_file(bad_path, lib)
    assert summary.skipped == 1
    assert summary.new == 0


def test_ingest_file_unknown_shape_returns_skipped(tmp_library, tmp_path):
    weird = tmp_path / "weird.json"
    weird.write_text(json.dumps({"foo": "bar"}))
    lib = Library(tmp_library)

    summary = ingest_file(weird, lib)
    assert summary.skipped == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -v -k ingest_file`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/ingest.py`:
```python
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
    return preset.get("data", {}).get("device", {}).get("fw", "unknown")


def ingest_file(path: Path, library: Library) -> IngestSummary:
    """Ingest a single file: parse, detect shape, extract blocks, write to library."""
    summary = IngestSummary()

    try:
        data = json.loads(Path(path).read_text())
    except (json.JSONDecodeError, OSError):
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
    else:
        raw_blocks = [extract_block_from_single(data)]
        firmware = "unknown"

    source_info = {
        "preset": str(path),
        "firmware": firmware,
        "date": _today(),
    }

    for raw in raw_blocks:
        block = block_from_raw(raw, source_info)
        status = library.save_block_with_dedup(block)
        if status == IngestStatus.NEW:
            summary.new += 1
        elif status == IngestStatus.MATCH:
            summary.matched += 1
        elif status == IngestStatus.CONFLICT:
            summary.conflicted += 1

    return summary
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ingest.py -v -k ingest_file`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ingest.py tests/test_ingest.py
git commit -m "feat: ingest.ingest_file orchestrates parse + extract + save"
```

---

## Task 17: First-run chassis extraction inside ingest

When ingest sees its first full preset and the library has no chassis, extract one.

**Files:**
- Modify: `src/helixgen/ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingest.py`:
```python
def test_ingest_extracts_chassis_on_first_full_preset(
    tmp_library, sample_serial_preset, tmp_path
):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    assert not lib.has_chassis()
    summary = ingest_file(preset_path, lib)

    assert summary.chassis_extracted is True
    assert lib.has_chassis()
    chassis = lib.load_chassis()
    assert chassis["data"]["tone"]["dsp0"]["blocks"] == {}


def test_ingest_does_not_re_extract_chassis(
    tmp_library, sample_serial_preset, tmp_path
):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    ingest_file(preset_path, lib)
    second = ingest_file(preset_path, lib)
    assert second.chassis_extracted is False


def test_ingest_single_block_does_not_extract_chassis(
    tmp_library, sample_amp_block, tmp_path
):
    block_path = tmp_path / "amp.json"
    block_path.write_text(json.dumps(sample_amp_block))
    lib = Library(tmp_library)

    summary = ingest_file(block_path, lib)
    assert summary.chassis_extracted is False
    assert not lib.has_chassis()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -v -k chassis`
Expected: FAIL — chassis_extracted always False currently.

- [ ] **Step 3: Implement**

In `src/helixgen/ingest.py`, modify `ingest_file` to extract chassis on first full-preset sighting. Replace the `if shape == Shape.PRESET:` branch with:

```python
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
```

(The `from helixgen.chassis import extract_chassis` is imported lazily here to avoid a top-level circular import: `chassis.py` imports from `ingest.py`.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ingest.py -v -k chassis`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ingest.py tests/test_ingest.py
git commit -m "feat: extract chassis on first full-preset ingest"
```

---

## Task 18: ingest.ingest_path — directory walk

**Files:**
- Modify: `src/helixgen/ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest.py`:
```python
from helixgen.ingest import ingest_path


def test_ingest_path_directory(tmp_library, sample_serial_preset, sample_amp_block, tmp_path):
    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    (presets_dir / "preset.hlx").write_text(json.dumps(sample_serial_preset))
    (presets_dir / "amp.json").write_text(json.dumps(sample_amp_block))
    (presets_dir / "junk.txt").write_text("ignore me")

    lib = Library(tmp_library)
    summary = ingest_path(presets_dir, lib)

    # 4 from preset + 1 from single-block = 5; some are duplicates so could match
    assert summary.new + summary.matched == 5
    assert summary.skipped == 0  # .txt is filtered by extension


def test_ingest_path_recurses(tmp_library, sample_serial_preset, tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.hlx").write_text(json.dumps(sample_serial_preset))

    lib = Library(tmp_library)
    summary = ingest_path(tmp_path, lib)

    assert summary.new == 4


def test_ingest_path_single_file_arg(tmp_library, sample_serial_preset, tmp_path):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    summary = ingest_path(preset_path, lib)
    assert summary.new == 4


def test_ingest_path_rebuilds_index(tmp_library, sample_serial_preset, tmp_path):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    ingest_path(preset_path, lib)
    assert (tmp_library / "index.json").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -v -k ingest_path`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/ingest.py`:
```python
INGEST_EXTENSIONS = {".hlx", ".json"}


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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ingest.py -v -k ingest_path`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ingest.py tests/test_ingest.py
git commit -m "feat: ingest.ingest_path walks directories and rebuilds index"
```

---

## Task 19: spec.parse_spec — top-level validation

**Files:**
- Create: `src/helixgen/spec.py`
- Create: `tests/test_spec.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_spec.py`:
```python
import json

import pytest

from helixgen.spec import Spec, SpecError, parse_spec


VALID = {
    "name": "Test Preset",
    "paths": [
        {
            "blocks": [
                {"block": "Brit 2204", "params": {"Drive": 0.6}},
            ],
        }
    ],
}


def test_parse_minimal_valid():
    spec = parse_spec(VALID, source="test.json")
    assert isinstance(spec, Spec)
    assert spec.name == "Test Preset"
    assert spec.author is None
    assert len(spec.paths) == 1
    assert spec.paths[0].blocks[0].block == "Brit 2204"
    assert spec.paths[0].blocks[0].params == {"Drive": 0.6}


def test_parse_with_author_and_io():
    data = {
        "name": "X",
        "author": "mike",
        "paths": [
            {"input": "Multi", "output": "Multi", "blocks": [{"block": "Y"}]}
        ],
    }
    spec = parse_spec(data, source="t.json")
    assert spec.author == "mike"
    assert spec.paths[0].input == "Multi"
    assert spec.paths[0].output == "Multi"


def test_missing_name_raises():
    bad = {k: v for k, v in VALID.items() if k != "name"}
    with pytest.raises(SpecError, match="name"):
        parse_spec(bad, source="t.json")


def test_paths_not_array_raises():
    bad = {"name": "X", "paths": {}}
    with pytest.raises(SpecError, match='"paths" must be an array'):
        parse_spec(bad, source="t.json")


def test_paths_too_long_raises():
    bad = {"name": "X", "paths": [{"blocks": []}, {"blocks": []}, {"blocks": []}]}
    with pytest.raises(SpecError, match="length 3 not supported"):
        parse_spec(bad, source="t.json")


def test_paths_empty_raises():
    bad = {"name": "X", "paths": []}
    with pytest.raises(SpecError, match="at least one"):
        parse_spec(bad, source="t.json")


def test_block_missing_block_field_raises():
    bad = {
        "name": "X",
        "paths": [{"blocks": [{"params": {}}]}],
    }
    with pytest.raises(SpecError, match='"block"'):
        parse_spec(bad, source="t.json")


def test_params_must_be_dict():
    bad = {
        "name": "X",
        "paths": [{"blocks": [{"block": "Y", "params": []}]}],
    }
    with pytest.raises(SpecError, match='"params" must be an object'):
        parse_spec(bad, source="t.json")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_spec.py -v`
Expected: FAIL — `helixgen.spec` missing.

- [ ] **Step 3: Implement**

In `src/helixgen/spec.py`:
```python
"""Spec: parse + validate the JSON tone description that `generate` consumes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SpecError(ValueError):
    """Raised when a spec is structurally invalid."""


@dataclass
class BlockEntry:
    block: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class PathEntry:
    blocks: list[BlockEntry]
    input: str | None = None
    output: str | None = None


@dataclass
class Spec:
    name: str
    paths: list[PathEntry]
    author: str | None = None


def _err(source: str, message: str) -> SpecError:
    return SpecError(f"Spec at {source}: {message}")


def parse_spec(data: Any, *, source: str = "<input>") -> Spec:
    """Parse and validate a spec dict. Raises SpecError on any structural problem."""
    if not isinstance(data, dict):
        raise _err(source, "top-level value must be an object.")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise _err(source, '"name" is required and must be a non-empty string.')

    author = data.get("author")
    if author is not None and not isinstance(author, str):
        raise _err(source, '"author" must be a string if provided.')

    paths_raw = data.get("paths")
    if not isinstance(paths_raw, list):
        raise _err(source, '"paths" must be an array.')
    if len(paths_raw) == 0:
        raise _err(source, '"paths" must contain at least one chain.')
    if len(paths_raw) > 2:
        raise _err(
            source,
            f'"paths" length {len(paths_raw)} not supported (max 2 — one per DSP).',
        )

    paths: list[PathEntry] = []
    for i, path_raw in enumerate(paths_raw):
        paths.append(_parse_path(path_raw, source=f"{source} paths[{i}]"))

    return Spec(name=name, paths=paths, author=author)


def _parse_path(data: Any, *, source: str) -> PathEntry:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")

    inp = data.get("input")
    if inp is not None and not isinstance(inp, str):
        raise _err(source, '"input" must be a string if provided.')
    out = data.get("output")
    if out is not None and not isinstance(out, str):
        raise _err(source, '"output" must be a string if provided.')

    blocks_raw = data.get("blocks")
    if not isinstance(blocks_raw, list):
        raise _err(source, '"blocks" must be an array.')

    blocks: list[BlockEntry] = []
    for i, b in enumerate(blocks_raw):
        blocks.append(_parse_block_entry(b, source=f"{source} blocks[{i}]"))

    return PathEntry(blocks=blocks, input=inp, output=out)


def _parse_block_entry(data: Any, *, source: str) -> BlockEntry:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")

    name = data.get("block")
    if not isinstance(name, str) or not name:
        raise _err(source, '"block" is required and must be a non-empty string.')

    params = data.get("params", {})
    if not isinstance(params, dict):
        raise _err(source, '"params" must be an object if provided.')

    return BlockEntry(block=name, params=dict(params))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_spec.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/spec.py tests/test_spec.py
git commit -m "feat: spec.parse_spec with top-level structural validation"
```

---

## Task 20: spec — reject `parallel` entries

**Files:**
- Modify: `src/helixgen/spec.py`
- Modify: `tests/test_spec.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_spec.py`:
```python
def test_parallel_entry_rejected():
    bad = {
        "name": "X",
        "paths": [
            {
                "blocks": [
                    {"parallel": [[{"block": "A"}], [{"block": "B"}]]},
                ]
            }
        ],
    }
    with pytest.raises(SpecError, match='"parallel" entries not supported in v1'):
        parse_spec(bad, source="t.json")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_spec.py::test_parallel_entry_rejected -v`
Expected: FAIL — currently raises a "block missing" error, not the parallel-specific one.

- [ ] **Step 3: Implement**

In `src/helixgen/spec.py`, modify `_parse_block_entry` to check for `parallel` first:
```python
def _parse_block_entry(data: Any, *, source: str) -> BlockEntry:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")

    if "parallel" in data:
        raise _err(
            source,
            '"parallel" entries not supported in v1. '
            "See docs/features/parallel-paths.md.",
        )

    name = data.get("block")
    if not isinstance(name, str) or not name:
        raise _err(source, '"block" is required and must be a non-empty string.')

    params = data.get("params", {})
    if not isinstance(params, dict):
        raise _err(source, '"params" must be an object if provided.')

    return BlockEntry(block=name, params=dict(params))
```

- [ ] **Step 4: Run test**

Run: `pytest tests/test_spec.py -v`
Expected: PASS for all spec tests.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/spec.py tests/test_spec.py
git commit -m "feat: spec rejects parallel entries with a forward-pointer to the docs"
```

---

## Task 21: generate — block resolution

**Files:**
- Create: `src/helixgen/generate.py`
- Create: `tests/test_generate.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_generate.py`:
```python
import json

import pytest

from helixgen.generate import resolve_blocks
from helixgen.library import Library
from helixgen.spec import parse_spec


def populate_library(lib: Library, sample_amp_block, sample_cab_block):
    """Helper: ingest the two sample fixture blocks into a library."""
    from helixgen.ingest import block_from_raw
    src = {"preset": "fixture", "firmware": "test", "date": "2026-05-01"}
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.save_block_with_dedup(block_from_raw(sample_cab_block, src))
    lib.rebuild_index()


def test_resolve_blocks_by_display_name(tmp_library, sample_amp_block, sample_cab_block):
    lib = Library(tmp_library)
    populate_library(lib, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "Test",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom", "params": {"Drive": 0.8}}]}],
    }, source="t.json")

    resolved = resolve_blocks(spec, lib)
    assert len(resolved) == 1  # one path
    assert len(resolved[0]) == 1  # one block in that path
    block, user_params = resolved[0][0]
    assert block.model_id == "HD2_AmpBrit2204Custom"
    assert user_params == {"Drive": 0.8}


def test_resolve_blocks_missing_block_raises(tmp_library, sample_amp_block, sample_cab_block):
    lib = Library(tmp_library)
    populate_library(lib, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "Test",
        "paths": [{"blocks": [{"block": "Nonexistent"}]}],
    }, source="t.json")

    with pytest.raises(KeyError, match="not found in library"):
        resolve_blocks(spec, lib)


def test_resolve_blocks_ambiguous_raises(tmp_library, sample_amp_block):
    lib = Library(tmp_library)
    from helixgen.ingest import block_from_raw
    src = {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    # Two blocks with the same display_name
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    other = dict(sample_amp_block)
    other["@model"] = "HD2_AmpBrit2204Variant"
    other["@name"] = "Brit 2204 Custom"  # collide on display name
    lib.save_block_with_dedup(block_from_raw(other, src))
    lib.rebuild_index()

    spec = parse_spec({
        "name": "T",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }, source="t.json")

    with pytest.raises(LookupError, match="multiple library entries"):
        resolve_blocks(spec, lib)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_generate.py -v`
Expected: FAIL — `helixgen.generate` missing.

- [ ] **Step 3: Implement**

In `src/helixgen/generate.py`:
```python
"""Generate: turn a parsed Spec + Library into a .hlx preset dict."""
from __future__ import annotations

from typing import Any

from helixgen.library import Block, Library
from helixgen.spec import Spec


ResolvedPath = list[tuple[Block, dict[str, Any]]]


def resolve_blocks(spec: Spec, library: Library) -> list[ResolvedPath]:
    """Look up every block in the spec against the library.

    Returns a list (one per path) of [(Block, user_params)] tuples, preserving order.
    Raises KeyError or LookupError on missing/ambiguous block names (re-raised
    from Library.find_block).
    """
    resolved: list[ResolvedPath] = []
    for path in spec.paths:
        chain: ResolvedPath = []
        for entry in path.blocks:
            block = library.find_block(entry.block)
            chain.append((block, entry.params))
        resolved.append(chain)
    return resolved
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_generate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate.py
git commit -m "feat: generate.resolve_blocks looks up spec blocks against library"
```

---

## Task 22: generate — param key validation

**Files:**
- Modify: `src/helixgen/generate.py`
- Modify: `tests/test_generate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_generate.py`:
```python
from helixgen.generate import validate_params, ParamValidationError


def test_validate_params_known_keys_pass(tmp_library, sample_amp_block, sample_cab_block):
    lib = Library(tmp_library)
    populate_library(lib, sample_amp_block, sample_cab_block)
    block = lib.find_block("Brit 2204 Custom")
    validate_params(block, {"Drive": 0.7, "Bass": 0.5})  # no raise


def test_validate_params_unknown_key_raises(tmp_library, sample_amp_block, sample_cab_block):
    lib = Library(tmp_library)
    populate_library(lib, sample_amp_block, sample_cab_block)
    block = lib.find_block("Brit 2204 Custom")
    with pytest.raises(ParamValidationError) as excinfo:
        validate_params(block, {"Drive2": 0.7})
    msg = str(excinfo.value)
    assert "Drive2" in msg
    assert "Brit 2204 Custom" in msg
    # known params listed in the message
    assert "Drive" in msg


def test_validate_params_lists_all_unknown_keys(tmp_library, sample_amp_block, sample_cab_block):
    lib = Library(tmp_library)
    populate_library(lib, sample_amp_block, sample_cab_block)
    block = lib.find_block("Brit 2204 Custom")
    with pytest.raises(ParamValidationError) as excinfo:
        validate_params(block, {"Drive2": 0, "BassX": 0})
    msg = str(excinfo.value)
    assert "Drive2" in msg
    assert "BassX" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_generate.py -v -k validate_params`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/generate.py`:
```python
class ParamValidationError(ValueError):
    """User specified parameters that don't exist on the resolved block."""


def validate_params(block: Block, user_params: dict[str, Any]) -> None:
    """Hard-fail if any user_params key isn't in the block's schema."""
    known = set(block.params.keys())
    unknown = sorted(set(user_params.keys()) - known)
    if not unknown:
        return
    raise ParamValidationError(
        f"Unknown param(s) {unknown} for block {block.display_name!r}. "
        f"Known params: {sorted(known)}."
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_generate.py -v -k validate_params`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate.py
git commit -m "feat: generate.validate_params catches unknown param keys"
```

---

## Task 23: generate.compose_preset — chassis copy + block placement + meta

This is the heart of generate. It takes the resolved spec and produces the final preset dict (in memory, not yet on disk).

**Files:**
- Modify: `src/helixgen/generate.py`
- Modify: `tests/test_generate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_generate.py`:
```python
from helixgen.generate import compose_preset
from helixgen.chassis import extract_chassis


def populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block):
    """Library has both blocks AND a chassis, like after first-run ingest."""
    from helixgen.ingest import block_from_raw
    src = {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.save_block_with_dedup(block_from_raw(sample_cab_block, src))
    lib.rebuild_index()
    lib.save_chassis(extract_chassis(sample_serial_preset))


def test_compose_preset_places_blocks_in_chassis_slots(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "Composed",
        "paths": [{"blocks": [
            {"block": "Brit 2204 Custom", "params": {"Drive": 0.99}},
            {"block": "4x12 Greenback25"},
        ]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")

    blocks = preset["data"]["tone"]["dsp0"]["blocks"]
    # Two blocks should land in dsp0_block_0 and dsp0_block_1
    assert "dsp0_block_0" in blocks
    assert "dsp0_block_1" in blocks
    assert blocks["dsp0_block_0"]["@model"] == "HD2_AmpBrit2204Custom"
    assert blocks["dsp0_block_0"]["Drive"] == 0.99  # user override
    assert blocks["dsp0_block_0"]["Bass"] == 0.5    # exemplar default kept
    assert blocks["dsp0_block_1"]["@model"] == "HD2_Cab4x12Greenback25"
    # Other slots are not present (chassis cleared them)
    assert "dsp0_block_2" not in blocks


def test_compose_preset_sets_meta_name(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "My Cool Preset",
        "author": "mike",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    assert preset["data"]["meta"]["name"] == "My Cool Preset"
    assert preset["data"]["meta"]["author"] == "mike"


def test_compose_preset_writes_provenance(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "X",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }, source="my-spec.json")

    preset = compose_preset(spec, lib, source="my-spec.json")
    prov = preset["data"]["meta"]["helixgen"]
    assert prov["spec_source"] == "my-spec.json"
    assert "version" in prov
    assert "generated_at" in prov


def test_compose_preset_strips_internal_helixgen_field(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    """The chassis stores _helixgen position_keys at the top level; output must not include it."""
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "X",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    assert "_helixgen" not in preset


def test_compose_preset_too_many_blocks_raises(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    # Chassis has 4 slots on dsp0; spec has 5 blocks
    spec = parse_spec({
        "name": "X",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}] * 5}],
    }, source="t.json")

    from helixgen.generate import GenerateError
    with pytest.raises(GenerateError, match="more blocks"):
        compose_preset(spec, lib, source="t.json")


def test_compose_preset_overlay_io(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec = parse_spec({
        "name": "X",
        "paths": [{"input": "USB 5/6", "output": "Multi", "blocks": [{"block": "Brit 2204 Custom"}]}],
    }, source="t.json")

    preset = compose_preset(spec, lib, source="t.json")
    assert preset["data"]["tone"]["dsp0"]["input"] == "USB 5/6"
    assert preset["data"]["tone"]["dsp0"]["output"] == "Multi"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_generate.py -v -k compose`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/generate.py`:
```python
import copy
import datetime

from helixgen import __version__
from helixgen.ingest import PRESET_DSP_KEYS


class GenerateError(ValueError):
    """Generation failed for a structural reason (chassis, slots, etc.)."""


def compose_preset(spec: Spec, library: Library, *, source: str) -> dict[str, Any]:
    """Build the final preset dict from a Spec + Library."""
    if not library.has_chassis():
        raise GenerateError(
            "Library has no chassis. Run `helixgen ingest <real-export.hlx>` first."
        )

    resolved = resolve_blocks(spec, library)
    for chain in resolved:
        for block, user_params in chain:
            validate_params(block, user_params)

    preset = copy.deepcopy(library.load_chassis())
    position_keys = preset.get("_helixgen", {}).get("position_keys", {"dsp0": [], "dsp1": []})

    for path_index, chain in enumerate(resolved):
        if path_index >= len(PRESET_DSP_KEYS):
            raise GenerateError(
                f"Spec has {len(resolved)} paths but only {len(PRESET_DSP_KEYS)} DSPs available."
            )
        dsp_key = PRESET_DSP_KEYS[path_index]
        slots = position_keys.get(dsp_key, [])
        if len(chain) > len(slots):
            raise GenerateError(
                f"Path {path_index} has more blocks ({len(chain)}) than chassis "
                f"slots on {dsp_key} ({len(slots)})."
            )

        spec_path = spec.paths[path_index]
        dsp = preset["data"]["tone"][dsp_key]
        if spec_path.input is not None:
            dsp["input"] = spec_path.input
        if spec_path.output is not None:
            dsp["output"] = spec_path.output

        dsp["blocks"] = {}
        for slot, (block, user_params) in zip(slots, chain):
            placed = copy.deepcopy(block.exemplar)
            for k, v in user_params.items():
                placed[k] = v
            dsp["blocks"][slot] = placed

    # Meta
    meta = preset["data"].setdefault("meta", {})
    meta["name"] = spec.name
    if spec.author is not None:
        meta["author"] = spec.author
    meta["helixgen"] = {
        "version": __version__,
        "spec_source": source,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    # Strip internal-only chassis metadata before returning
    preset.pop("_helixgen", None)
    return preset
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_generate.py -v`
Expected: PASS for all tests.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate.py
git commit -m "feat: generate.compose_preset places blocks into chassis slots"
```

---

## Task 24: generate — write to disk + top-level orchestrator

**Files:**
- Modify: `src/helixgen/generate.py`
- Modify: `tests/test_generate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_generate.py`:
```python
from helixgen.generate import generate_preset


def test_generate_preset_writes_file(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block, tmp_path
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "Disk Test",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }))

    out_path = tmp_path / "out.hlx"
    generate_preset(spec_path, out_path, lib)

    assert out_path.exists()
    content = json.loads(out_path.read_text())
    assert content["data"]["meta"]["name"] == "Disk Test"
    assert "_helixgen" not in content


def test_generate_preset_pretty_prints(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block, tmp_path
):
    lib = Library(tmp_library)
    populate_library_and_chassis(lib, sample_serial_preset, sample_amp_block, sample_cab_block)

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "X",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }))
    out_path = tmp_path / "out.hlx"
    generate_preset(spec_path, out_path, lib)

    text = out_path.read_text()
    assert "\n" in text  # multi-line, not minified
    assert text.startswith("{")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_generate.py -v -k generate_preset`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/generate.py`:
```python
import json
from pathlib import Path

from helixgen.spec import parse_spec


def generate_preset(spec_path: Path, output_path: Path, library: Library) -> Path:
    """Top-level: read spec from disk, compose, write output. Returns the output path."""
    spec_path = Path(spec_path)
    output_path = Path(output_path)

    raw = json.loads(spec_path.read_text())
    spec = parse_spec(raw, source=str(spec_path))
    preset = compose_preset(spec, library, source=str(spec_path))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(preset, indent=2))
    return output_path
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_generate.py -v`
Expected: PASS for all tests.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate.py
git commit -m "feat: generate.generate_preset top-level orchestrator writes to disk"
```

---

## Task 25: bootstrap — clone or pull phelix

**Files:**
- Create: `src/helixgen/bootstrap.py`
- Create: `tests/test_bootstrap.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_bootstrap.py`:
```python
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from helixgen.bootstrap import clone_or_pull_phelix


@patch("helixgen.bootstrap.subprocess.run")
def test_clone_when_cache_missing(mock_run, tmp_path):
    cache = tmp_path / "phelix"
    assert not cache.exists()

    clone_or_pull_phelix(cache, ref="main")

    # Should call git clone
    args = mock_run.call_args_list
    assert len(args) == 1
    cmd = args[0][0][0]
    assert cmd[0:2] == ["git", "clone"]
    assert "https://github.com/sensorium/phelix" in cmd
    assert str(cache) in cmd


@patch("helixgen.bootstrap.subprocess.run")
def test_pull_when_cache_exists(mock_run, tmp_path):
    cache = tmp_path / "phelix"
    cache.mkdir()
    (cache / ".git").mkdir()  # fake repo

    clone_or_pull_phelix(cache, ref="main")

    # Should call git fetch + checkout (no clone)
    cmds = [args[0][0] for args in mock_run.call_args_list]
    cmd_strs = [" ".join(c) for c in cmds]
    assert any("fetch" in c for c in cmd_strs)
    assert any("checkout" in c for c in cmd_strs)
    assert not any("clone" in c for c in cmd_strs)


@patch("helixgen.bootstrap.subprocess.run")
def test_uses_specified_ref(mock_run, tmp_path):
    cache = tmp_path / "phelix"
    cache.mkdir()
    (cache / ".git").mkdir()

    clone_or_pull_phelix(cache, ref="v1.2.3")

    cmds = [args[0][0] for args in mock_run.call_args_list]
    checkout = next(c for c in cmds if "checkout" in c)
    assert "v1.2.3" in checkout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bootstrap.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `src/helixgen/bootstrap.py`:
```python
"""Bootstrap: clone or pull sensorium/phelix and ingest its blocks/."""
from __future__ import annotations

import subprocess
from pathlib import Path

from helixgen.ingest import IngestSummary, ingest_path
from helixgen.library import Library, default_cache_path


PHELIX_REPO_URL = "https://github.com/sensorium/phelix"


def clone_or_pull_phelix(cache_dir: Path, *, ref: str = "main") -> Path:
    """Clone the phelix repo into cache_dir, or fetch+checkout if it already exists.

    Returns the path to the cloned repo. Raises CalledProcessError on git errors.
    """
    cache_dir = Path(cache_dir)
    if (cache_dir / ".git").exists():
        subprocess.run(["git", "-C", str(cache_dir), "fetch", "origin", ref], check=True)
        subprocess.run(["git", "-C", str(cache_dir), "checkout", ref], check=True)
    else:
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--branch", ref, PHELIX_REPO_URL, str(cache_dir)],
            check=True,
        )
    return cache_dir
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_bootstrap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/bootstrap.py tests/test_bootstrap.py
git commit -m "feat: bootstrap.clone_or_pull_phelix idempotent git ops"
```

---

## Task 26: bootstrap orchestrator

**Files:**
- Modify: `src/helixgen/bootstrap.py`
- Modify: `tests/test_bootstrap.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bootstrap.py`:
```python
import json

from helixgen.bootstrap import bootstrap


@patch("helixgen.bootstrap.clone_or_pull_phelix")
def test_bootstrap_clones_then_ingests_blocks(mock_clone, tmp_library, tmp_path, sample_amp_block):
    fake_phelix = tmp_path / "phelix"
    blocks_dir = fake_phelix / "blocks"
    blocks_dir.mkdir(parents=True)
    (blocks_dir / "amp.json").write_text(json.dumps(sample_amp_block))
    mock_clone.return_value = fake_phelix

    lib_obj = Library(tmp_library)
    summary = bootstrap(lib_obj, ref="main", cache_dir=fake_phelix)

    mock_clone.assert_called_once_with(fake_phelix, ref="main")
    assert summary.new == 1
    assert len(lib_obj.list_blocks()) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bootstrap.py::test_bootstrap_clones_then_ingests_blocks -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/bootstrap.py`:
```python
def bootstrap(
    library: Library, *, ref: str = "main", cache_dir: Path | None = None
) -> IngestSummary:
    """Clone (or pull) phelix and ingest its blocks/ folder into the library."""
    if cache_dir is None:
        cache_dir = default_cache_path() / "phelix"
    repo = clone_or_pull_phelix(cache_dir, ref=ref)
    blocks_dir = repo / "blocks"
    if not blocks_dir.exists():
        raise FileNotFoundError(
            f"phelix repo at {repo} does not contain a 'blocks/' directory."
        )
    return ingest_path(blocks_dir, library)
```

- [ ] **Step 4: Run test**

Run: `pytest tests/test_bootstrap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/bootstrap.py tests/test_bootstrap.py
git commit -m "feat: bootstrap orchestrator clones phelix and ingests blocks/"
```

---

## Task 27: CLI scaffold + ingest command

**Files:**
- Create: `src/helixgen/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`:
```python
import json

from click.testing import CliRunner

from helixgen.cli import cli


def test_cli_help_lists_subcommands():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ["ingest", "generate", "list-blocks", "show-block", "bootstrap"]:
        assert cmd in result.output


def test_cli_ingest_full_preset(tmp_library, sample_serial_preset, tmp_path):
    preset_path = tmp_path / "p.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))

    result = CliRunner().invoke(
        cli,
        ["ingest", str(preset_path), "--library", str(tmp_library)],
    )
    assert result.exit_code == 0
    assert "+4 new blocks" in result.output or "new blocks" in result.output
    # Library populated
    blocks_dir = tmp_library / "blocks"
    assert blocks_dir.exists()


def test_cli_ingest_uses_env_var(tmp_library, sample_amp_block, tmp_path, monkeypatch):
    block_path = tmp_path / "amp.json"
    block_path.write_text(json.dumps(sample_amp_block))

    monkeypatch.setenv("HELIXGEN_LIBRARY", str(tmp_library))
    result = CliRunner().invoke(cli, ["ingest", str(block_path)])
    assert result.exit_code == 0


def test_cli_ingest_missing_path_returns_user_error(tmp_library):
    result = CliRunner().invoke(
        cli,
        ["ingest", "/does/not/exist", "--library", str(tmp_library)],
    )
    assert result.exit_code == 1
    assert "not exist" in result.output.lower() or "no such" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL — `helixgen.cli` missing.

- [ ] **Step 3: Implement**

In `src/helixgen/cli.py`:
```python
"""CLI entry points for helixgen."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import click

from helixgen.ingest import IngestSummary, ingest_path
from helixgen.library import Library, default_library_path


def _library_option(f):
    return click.option(
        "--library",
        "library_path",
        envvar="HELIXGEN_LIBRARY",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Library directory. Defaults to ~/.helixgen/library/ or $HELIXGEN_LIBRARY.",
    )(f)


def _resolved_library(library_path: Path | None) -> Library:
    return Library(library_path or default_library_path())


def _format_summary(summary: IngestSummary, library: Library) -> str:
    lines: list[str] = []
    lines.append(f"+{summary.new} new blocks")
    if summary.matched:
        lines.append(f" {summary.matched} already in library")
    if summary.conflicted:
        lines.append(f" {summary.conflicted} conflicts (see *.v2.json files)")
    if summary.skipped:
        lines.append(f" {summary.skipped} files skipped")
    if summary.chassis_extracted:
        lines.append(" chassis extracted")

    # Per-category breakdown of new blocks
    if summary.new:
        cats = Counter(b.category for b in library.list_blocks())
        breakdown = ", ".join(f"{n} {c}" for c, n in sorted(cats.items()))
        lines.append(f"  Library now contains: {breakdown}")
    return "\n".join(lines)


@click.group()
@click.version_option()
def cli() -> None:
    """helixgen — generate Line 6 Helix .hlx presets from JSON tone specs."""


@cli.command(name="ingest")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@_library_option
def ingest_cmd(path: Path, library_path: Path | None) -> None:
    """Ingest a .hlx file or a directory of presets/blocks into the library."""
    library = _resolved_library(library_path)
    try:
        summary = ingest_path(path, library)
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e
    click.echo(_format_summary(summary, library))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/cli.py tests/test_cli.py
git commit -m "feat: cli scaffold + ingest command with --library and env var"
```

---

## Task 28: CLI generate command

**Files:**
- Modify: `src/helixgen/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:
```python
def test_cli_generate_writes_output(
    tmp_library, sample_serial_preset, sample_amp_block, sample_cab_block, tmp_path
):
    # Pre-populate library + chassis from fixture
    from helixgen.chassis import extract_chassis
    from helixgen.ingest import block_from_raw
    from helixgen.library import IngestStatus

    lib = Library(tmp_library)
    src = {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.save_block_with_dedup(block_from_raw(sample_cab_block, src))
    lib.rebuild_index()
    lib.save_chassis(extract_chassis(sample_serial_preset))

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "From CLI",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }))
    out_path = tmp_path / "out.hlx"

    result = CliRunner().invoke(
        cli,
        ["generate", str(spec_path), "-o", str(out_path), "--library", str(tmp_library)],
    )
    assert result.exit_code == 0
    assert out_path.exists()
    content = json.loads(out_path.read_text())
    assert content["data"]["meta"]["name"] == "From CLI"


def test_cli_generate_missing_block_user_error(tmp_library, sample_serial_preset, tmp_path):
    # Save chassis but no blocks
    from helixgen.chassis import extract_chassis
    lib = Library(tmp_library)
    lib.save_chassis(extract_chassis(sample_serial_preset))
    lib.rebuild_index()

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "X",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
    }))
    out_path = tmp_path / "out.hlx"

    result = CliRunner().invoke(
        cli,
        ["generate", str(spec_path), "-o", str(out_path), "--library", str(tmp_library)],
    )
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_cli_generate_invalid_spec_user_error(tmp_library, tmp_path):
    spec_path = tmp_path / "bad.json"
    spec_path.write_text(json.dumps({"paths": []}))  # no name
    out_path = tmp_path / "out.hlx"

    result = CliRunner().invoke(
        cli,
        ["generate", str(spec_path), "-o", str(out_path), "--library", str(tmp_library)],
    )
    assert result.exit_code == 1
    assert "name" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v -k generate`
Expected: FAIL — generate command missing.

- [ ] **Step 3: Implement**

Append to `src/helixgen/cli.py`:
```python
from helixgen.generate import GenerateError, ParamValidationError, generate_preset
from helixgen.spec import SpecError


@cli.command(name="generate")
@click.argument("spec_path", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", "output_path", type=click.Path(path_type=Path), required=True)
@_library_option
def generate_cmd(spec_path: Path, output_path: Path, library_path: Path | None) -> None:
    """Generate a .hlx preset from a JSON tone spec."""
    library = _resolved_library(library_path)
    try:
        generate_preset(spec_path, output_path, library)
    except (KeyError, LookupError, SpecError, ParamValidationError, GenerateError, FileNotFoundError) as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Wrote {output_path}")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: PASS for all.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/cli.py tests/test_cli.py
git commit -m "feat: cli generate command with structured error → exit 1"
```

---

## Task 29: CLI list-blocks command

**Files:**
- Modify: `src/helixgen/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:
```python
def test_cli_list_blocks_groups_by_category(
    tmp_library, sample_amp_block, sample_cab_block
):
    from helixgen.ingest import block_from_raw
    src = {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    lib = Library(tmp_library)
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.save_block_with_dedup(block_from_raw(sample_cab_block, src))
    lib.rebuild_index()

    result = CliRunner().invoke(
        cli, ["list-blocks", "--library", str(tmp_library)]
    )
    assert result.exit_code == 0
    assert "amp:" in result.output.lower()
    assert "cab:" in result.output.lower()
    assert "Brit 2204 Custom" in result.output
    assert "4x12 Greenback25" in result.output


def test_cli_list_blocks_filters_by_category(
    tmp_library, sample_amp_block, sample_cab_block
):
    from helixgen.ingest import block_from_raw
    src = {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    lib = Library(tmp_library)
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.save_block_with_dedup(block_from_raw(sample_cab_block, src))
    lib.rebuild_index()

    result = CliRunner().invoke(
        cli, ["list-blocks", "--category", "amp", "--library", str(tmp_library)]
    )
    assert result.exit_code == 0
    assert "Brit 2204 Custom" in result.output
    assert "4x12 Greenback25" not in result.output


def test_cli_list_blocks_empty_library(tmp_library):
    result = CliRunner().invoke(
        cli, ["list-blocks", "--library", str(tmp_library)]
    )
    assert result.exit_code == 0
    assert "no blocks" in result.output.lower() or result.output.strip() == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v -k list_blocks`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/cli.py`:
```python
@cli.command(name="list-blocks")
@click.option("--category", default=None, help="Filter to one category.")
@_library_option
def list_blocks_cmd(category: str | None, library_path: Path | None) -> None:
    """List blocks in the library, grouped by category."""
    library = _resolved_library(library_path)
    blocks = library.list_blocks(category=category)
    if not blocks:
        click.echo("(no blocks in library)")
        return
    by_category: dict[str, list] = {}
    for b in blocks:
        by_category.setdefault(b.category, []).append(b)
    for cat in sorted(by_category):
        click.echo(f"{cat}:")
        for b in sorted(by_category[cat], key=lambda x: x.display_name):
            click.echo(f"  {b.display_name}  [{b.model_id}]")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py -v -k list_blocks`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/cli.py tests/test_cli.py
git commit -m "feat: cli list-blocks groups by category, supports filter"
```

---

## Task 30: CLI show-block command

**Files:**
- Modify: `src/helixgen/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:
```python
def test_cli_show_block_prints_schema(tmp_library, sample_amp_block):
    from helixgen.ingest import block_from_raw
    lib = Library(tmp_library)
    lib.save_block_with_dedup(block_from_raw(
        sample_amp_block, {"preset": "f", "firmware": "t", "date": "2026-05-01"}
    ))
    lib.rebuild_index()

    result = CliRunner().invoke(
        cli, ["show-block", "Brit 2204 Custom", "--library", str(tmp_library)]
    )
    assert result.exit_code == 0
    assert "HD2_AmpBrit2204Custom" in result.output
    assert "Drive" in result.output
    assert "category: amp" in result.output.lower() or "amp" in result.output


def test_cli_show_block_missing_user_error(tmp_library):
    result = CliRunner().invoke(
        cli, ["show-block", "Nope", "--library", str(tmp_library)]
    )
    assert result.exit_code == 1
    assert "not found" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v -k show_block`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/cli.py`:
```python
@cli.command(name="show-block")
@click.argument("name_or_id")
@_library_option
def show_block_cmd(name_or_id: str, library_path: Path | None) -> None:
    """Print a block's schema (params, defaults, types) for spec authoring."""
    library = _resolved_library(library_path)
    try:
        block = library.find_block(name_or_id)
    except (KeyError, LookupError) as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"{block.display_name}  [{block.model_id}]")
    click.echo(f"category: {block.category}")
    if block.aliases:
        click.echo(f"aliases: {', '.join(block.aliases)}")
    click.echo("params:")
    for name, schema in block.params.items():
        meta_bits = [schema["type"], f"default={schema.get('default')!r}"]
        if "observed_range" in schema:
            meta_bits.append(f"observed={schema['observed_range']}")
        if "values" in schema:
            meta_bits.append(f"values={schema['values']}")
        click.echo(f"  {name}  ({', '.join(meta_bits)})")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py -v -k show_block`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/cli.py tests/test_cli.py
git commit -m "feat: cli show-block prints schema for spec authoring"
```

---

## Task 31: CLI bootstrap command

**Files:**
- Modify: `src/helixgen/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:
```python
from unittest.mock import patch


@patch("helixgen.cli.bootstrap")
def test_cli_bootstrap_invokes_bootstrap(mock_bootstrap, tmp_library):
    from helixgen.ingest import IngestSummary
    mock_bootstrap.return_value = IngestSummary(new=12)

    result = CliRunner().invoke(
        cli, ["bootstrap", "--library", str(tmp_library)]
    )
    assert result.exit_code == 0
    mock_bootstrap.assert_called_once()
    args, kwargs = mock_bootstrap.call_args
    assert kwargs.get("ref") == "main"
    assert "12 new blocks" in result.output or "+12" in result.output


@patch("helixgen.cli.bootstrap")
def test_cli_bootstrap_passes_ref(mock_bootstrap, tmp_library):
    from helixgen.ingest import IngestSummary
    mock_bootstrap.return_value = IngestSummary(new=0)

    CliRunner().invoke(
        cli, ["bootstrap", "--phelix-ref", "v2.0", "--library", str(tmp_library)]
    )
    _, kwargs = mock_bootstrap.call_args
    assert kwargs.get("ref") == "v2.0"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v -k bootstrap`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `src/helixgen/cli.py`:
```python
from helixgen.bootstrap import bootstrap


@cli.command(name="bootstrap")
@click.option("--phelix-ref", "ref", default="main", help="Git ref of sensorium/phelix to clone.")
@_library_option
def bootstrap_cmd(ref: str, library_path: Path | None) -> None:
    """Clone sensorium/phelix and ingest its blocks/ folder."""
    library = _resolved_library(library_path)
    try:
        summary = bootstrap(library, ref=ref)
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e
    click.echo(_format_summary(summary, library))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py -v -k bootstrap`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/cli.py tests/test_cli.py
git commit -m "feat: cli bootstrap command for phelix seed"
```

---

## Task 32: Round-trip integration test

Verify: ingest a fixture preset → derive a spec → generate from the spec → re-ingest → library state unchanged, and the regenerated preset places the same model_ids in the same slots.

**Files:**
- Create: `tests/test_roundtrip.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_roundtrip.py`:
```python
import json

from helixgen.ingest import ingest_path, block_from_raw
from helixgen.library import Library
from helixgen.generate import generate_preset
from helixgen.chassis import extract_chassis


def test_roundtrip_serial_preset(tmp_library, sample_serial_preset, tmp_path):
    """Ingest a fixture preset, generate from a derived spec, re-ingest the
    output, and verify the library is structurally unchanged."""

    # 1. Ingest fixture preset
    preset_path = tmp_path / "in.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)
    first_summary = ingest_path(preset_path, lib)
    assert first_summary.new == 4
    assert first_summary.chassis_extracted

    initial_blocks = sorted(b.model_id for b in lib.list_blocks())
    initial_chassis = lib.load_chassis()

    # 2. Derive a spec from the original preset (using each block's display name)
    spec_blocks = []
    for raw in sample_serial_preset["data"]["tone"]["dsp0"]["blocks"].values():
        block = block_from_raw(raw, {"preset": "_", "firmware": "_", "date": "2026-05-01"})
        # Pull just one param verbatim to verify overrides round-trip
        params = {}
        for k, v in raw.items():
            if k.startswith("@"):
                continue
            params[k] = v
            break
        spec_blocks.append({"block": block.display_name, "params": params})

    spec = {"name": "Roundtrip", "paths": [{"blocks": spec_blocks}]}
    spec_path = tmp_path / "rt.json"
    spec_path.write_text(json.dumps(spec))

    # 3. Generate
    out_path = tmp_path / "rt.hlx"
    generate_preset(spec_path, out_path, lib)
    assert out_path.exists()

    # 4. Re-ingest the generated file; library should be unchanged
    second_summary = ingest_path(out_path, lib)
    # All 4 blocks should match (same schema)
    assert second_summary.matched == 4
    assert second_summary.new == 0
    assert second_summary.conflicted == 0

    # Library block list unchanged
    assert sorted(b.model_id for b in lib.list_blocks()) == initial_blocks
    # Chassis unchanged
    assert lib.load_chassis() == initial_chassis

    # 5. Generated preset structurally equivalent: same model_ids in same dsp0 slot order
    out_data = json.loads(out_path.read_text())
    in_blocks = sample_serial_preset["data"]["tone"]["dsp0"]["blocks"]
    out_blocks = out_data["data"]["tone"]["dsp0"]["blocks"]
    in_models = [b["@model"] for b in in_blocks.values()]
    out_models = [out_blocks[k]["@model"] for k in sorted(out_blocks.keys())]
    assert in_models == out_models
```

- [ ] **Step 2: Run test to verify it passes (no implementation needed — exercises existing code)**

Run: `pytest tests/test_roundtrip.py -v`
Expected: PASS. If it fails, the failure is real — fix the relevant earlier task. Common failure modes: chassis isn't preserving routing, exemplar isn't being deep-copied, position-key zip doesn't preserve order.

- [ ] **Step 3: Commit**

```bash
git add tests/test_roundtrip.py
git commit -m "test: round-trip integration test for ingest → generate → re-ingest"
```

---

## Task 33: Goldfinger reference spec fixture

Lock in the canonical end-to-end test case: a JSON spec for the Goldfinger Superman rhythm tone, plus a test that generation succeeds against a populated library.

**Files:**
- Create: `tests/fixtures/specs/goldfinger.json`
- Modify: `tests/test_generate.py`

- [ ] **Step 1: Write `tests/fixtures/specs/goldfinger.json`**

```json
{
  "name": "Goldfinger Superman Rhythm",
  "author": "mike",
  "paths": [
    {
      "input": "Multi",
      "output": "Multi",
      "blocks": [
        { "block": "Noise Gate",        "params": { "Threshold": 0.40, "Decay": 0.30 } },
        { "block": "Scream 808",        "params": { "Drive": 0.10, "Tone": 0.50, "Level": 0.60 } },
        { "block": "Brit 2204 Custom",  "params": { "Drive": 0.60, "Bass": 0.50, "Mid": 0.75, "Treble": 0.55, "Presence": 0.55, "Master": 0.60, "Ch Vol": 0.50 } },
        { "block": "4x12 Greenback25",  "params": { "Mic": "57 Dynamic", "Distance": 0.10, "Axis": "12° off", "High Cut": 8000, "Low Cut": 80 } },
        { "block": "Plate Reverb",      "params": { "Mix": 0.10, "Decay": 1.2, "Pre-delay": 0.010 } }
      ]
    }
  ]
}
```

Note: this references blocks by the *humanized* display names of fixture model_ids: `Noise Gate` (HD2_DynamicsNoiseGate), `Scream 808` (HD2_DrvScream808), `Brit 2204 Custom` (HD2_AmpBrit2204Custom), `4x12 Greenback25` (HD2_Cab4x12Greenback25). `Plate Reverb` is not in the synthetic fixtures yet — the test will only exercise the blocks the library actually has, OR we add a Plate Reverb to the fixtures.

- [ ] **Step 2: Add Plate Reverb and Scream 808 to the synthetic preset fixture**

Modify `tests/fixtures/presets/sample_serial.json`. Add two more blocks to dsp0 (`dsp0_block_4` for EQ would be authentic, but we need Plate Reverb for the Goldfinger spec to resolve). Replace the dsp0 blocks dict with:

```json
"blocks": {
  "dsp0_block_0": {
    "@model": "HD2_DynamicsNoiseGate",
    "@enabled": true,
    "Threshold": 0.4,
    "Decay": 0.3
  },
  "dsp0_block_1": {
    "@model": "HD2_DrvScream808",
    "@enabled": true,
    "Drive": 0.1,
    "Tone": 0.5,
    "Level": 0.6
  },
  "dsp0_block_2": {
    "@model": "HD2_AmpBrit2204Custom",
    "@enabled": true,
    "Drive": 0.6,
    "Bass": 0.5,
    "Mid": 0.75,
    "Treble": 0.55,
    "Presence": 0.55,
    "Master": 0.6,
    "Ch Vol": 0.5
  },
  "dsp0_block_3": {
    "@model": "HD2_Cab4x12Greenback25",
    "@enabled": true,
    "Mic": "57 Dynamic",
    "Distance": 0.1,
    "Axis": "12° off",
    "High Cut": 8000,
    "Low Cut": 80
  },
  "dsp0_block_4": {
    "@model": "HD2_RvbPlate",
    "@enabled": true,
    "Mix": 0.1,
    "Decay": 1.2,
    "Pre-delay": 0.01
  }
}
```

(Block order matches the Goldfinger signal chain: Gate → Scream → Amp → Cab → Reverb. EQ is omitted from the synthetic preset because we have no fixture Parametric EQ block; the Goldfinger spec drops the EQ for the same reason.)

Also update `tests/fixtures/specs/goldfinger.json` to remove the Parametric EQ entry (it has no fixture block to resolve against).

Updated `goldfinger.json`:
```json
{
  "name": "Goldfinger Superman Rhythm",
  "author": "mike",
  "paths": [
    {
      "input": "Multi",
      "output": "Multi",
      "blocks": [
        { "block": "Noise Gate",        "params": { "Threshold": 0.40, "Decay": 0.30 } },
        { "block": "Scream 808",        "params": { "Drive": 0.10, "Tone": 0.50, "Level": 0.60 } },
        { "block": "Brit 2204 Custom",  "params": { "Drive": 0.60, "Bass": 0.50, "Mid": 0.75, "Treble": 0.55, "Presence": 0.55, "Master": 0.60, "Ch Vol": 0.50 } },
        { "block": "4x12 Greenback25",  "params": { "Mic": "57 Dynamic", "Distance": 0.10, "Axis": "12° off", "High Cut": 8000, "Low Cut": 80 } },
        { "block": "Plate Reverb",      "params": { "Mix": 0.10, "Decay": 1.2, "Pre-delay": 0.010 } }
      ]
    }
  ]
}
```

(Tests in earlier tasks that count "4 blocks in the fixture" now expect 5; update them as part of this task.)

- [ ] **Step 3: Update earlier counts**

In `tests/test_ingest.py`, find each `assert summary.new == 4` and `assert len(...) == 4` referring to the serial preset and change to `5`. Likewise in `test_chassis.py` for the position-key list. Run pytest after to confirm only the count changes were needed.

- [ ] **Step 4: Add the Goldfinger end-to-end test**

In `tests/test_generate.py`, append:
```python
def test_goldfinger_generates_successfully(
    tmp_library, sample_serial_preset, tmp_path
):
    """Acceptance test: generate the canonical Goldfinger preset from spec + library."""
    # Ingest fixture preset to populate library + chassis
    preset_path = tmp_path / "seed.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)
    from helixgen.ingest import ingest_path
    ingest_path(preset_path, lib)

    # Generate from goldfinger.json
    spec_path = Path("tests/fixtures/specs/goldfinger.json")
    out_path = tmp_path / "goldfinger.hlx"
    generate_preset(spec_path, out_path, lib)

    out = json.loads(out_path.read_text())
    assert out["data"]["meta"]["name"] == "Goldfinger Superman Rhythm"
    assert out["data"]["meta"]["author"] == "mike"
    blocks = out["data"]["tone"]["dsp0"]["blocks"]
    # 5 blocks placed
    assert len(blocks) == 5
    models = [blocks[k]["@model"] for k in sorted(blocks.keys())]
    assert models == [
        "HD2_DynamicsNoiseGate",
        "HD2_DrvScream808",
        "HD2_AmpBrit2204Custom",
        "HD2_Cab4x12Greenback25",
        "HD2_RvbPlate",
    ]
    # User overrides applied
    assert blocks[sorted(blocks.keys())[2]]["Mid"] == 0.75
    assert blocks[sorted(blocks.keys())[4]]["Mix"] == 0.10


from pathlib import Path
```
(Move the `from pathlib import Path` to the top with other imports.)

- [ ] **Step 5: Run all tests**

Run: `pytest -v`
Expected: PASS. If counts in earlier tests still say 4, update them.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/ tests/test_ingest.py tests/test_chassis.py tests/test_generate.py
git commit -m "test: Goldfinger reference spec end-to-end against synthetic fixtures"
```

---

## Task 34: Real-export validation pass (manual)

**This task requires the user.** It's the bridge between synthetic-fixture confidence and real-Helix correctness. The user provides a real `.hlx` export; we run the pipeline and reconcile any deviations.

- [ ] **Step 1: User exports a baseline preset from Helix/Helix Native.**

Ask the user to export one preset that contains, ideally:
- Brit 2204 (or similar) amp
- A 4x12 cab
- A Tube Screamer-style drive
- A Plate Reverb
- A Noise Gate

Save it to `tests/fixtures/presets/real/baseline.hlx`. Add `tests/fixtures/presets/real/` to `.gitignore` if the user prefers not to commit real exports.

- [ ] **Step 2: Inspect the real shape**

Run:
```bash
python -c "import json, sys; d=json.load(open('tests/fixtures/presets/real/baseline.hlx')); print(json.dumps(d, indent=2)[:2000])"
```

Compare to the assumptions encoded in `src/helixgen/ingest.py` (constants near the top of the file). Look specifically at:
- Where is `model_id` stored in a block? Top-level `@model`? Nested?
- What system keys exist on blocks? Anything besides `@model` and `@enabled`?
- What does `data.tone.dsp0.blocks` look like? Are keys `dsp0_block_N` or something else?
- Are there snapshot-related fields with their own block state?

- [ ] **Step 3: Reconcile assumptions**

For each deviation: update the constant at the top of `ingest.py` AND update the synthetic fixture in `tests/fixtures/` to match. Re-run `pytest`. If anything fails that was passing before, the deviation has logic implications — fix in code.

- [ ] **Step 4: Run end-to-end against the real export**

```bash
helixgen ingest tests/fixtures/presets/real/baseline.hlx
helixgen list-blocks
helixgen show-block "Brit 2204"
helixgen generate tests/fixtures/specs/goldfinger.json -o /tmp/gf.hlx
```

- [ ] **Step 5: Manual import test**

Open `/tmp/gf.hlx` in HX Edit / Helix Native and verify it imports without error and produces audio. This is the spec's acceptance criterion 3. Subjective tone fidelity is the user's call.

- [ ] **Step 6: Run bootstrap against real phelix**

```bash
helixgen bootstrap
helixgen list-blocks
```

If phelix's individual block files have a different shape than the synthetic single-block fixture, update `extract_block_from_single` and any related logic. Add a per-format adapter if needed; keep the adapter logic centralized in one function with a comment explaining the variant.

- [ ] **Step 7: Commit any reconciliation changes**

```bash
git add -p   # review carefully
git commit -m "fix: reconcile assumed wire format with real Helix export"
```

---

## Task 35: README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

```markdown
# helixgen

Generate Line 6 Helix `.hlx` preset files from a strict JSON tone spec, and build up a reusable library of block schemas by ingesting real exports.

## Install

```bash
git clone https://github.com/<you>/helixgen
cd helixgen
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Quickstart

```bash
# Seed the library from sensorium/phelix's pre-extracted blocks
helixgen bootstrap

# Or ingest your own exports
helixgen ingest ~/MyPresets/

# Browse the library
helixgen list-blocks
helixgen list-blocks --category amp
helixgen show-block "Brit 2204"

# Generate a preset
helixgen generate my-tone.json -o my-tone.hlx
```

## Spec format

A tone spec is a JSON document. Minimal example:

```json
{
  "name": "My Rhythm Tone",
  "paths": [
    {
      "blocks": [
        { "block": "Noise Gate", "params": { "Threshold": 0.4 } },
        { "block": "Brit 2204",  "params": { "Drive": 0.6, "Bass": 0.5 } },
        { "block": "4x12 Greenback25" }
      ]
    }
  ]
}
```

- `name` is the preset name shown in HX Edit.
- `paths` contains 1 or 2 chains (mapping to dsp0 / dsp1).
- Each block has a `block` (display name or model_id) and optional `params` (wire values: 0–1 floats for amp gain, integer Hz for cut frequencies, strings for enums like mic types).

Full design: `docs/superpowers/specs/2026-05-01-helix-preset-generator-design.md`.

## Library location

Default: `~/.helixgen/library/`. Override with `--library DIR` or `HELIXGEN_LIBRARY` env var.

## Limitations (v1)

- Single serial chain per DSP; no parallel A/B routing yet (see `docs/features/parallel-paths.md`).
- Wire values only — no display-value (0–10) translation.
- No snapshot variation.
- Output is not byte-identical to HX Edit's exports; it aims to load correctly.

## Tests

```bash
pytest
```
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README with quickstart and spec format"
```

---

## Self-review

The plan now has 35 tasks covering:

| Spec section | Covered by |
|---|---|
| Two-mode CLI architecture | Tasks 27–31 |
| Block library on-disk layout | Tasks 6, 7, 8, 9 |
| Block dataclass, dedup | Tasks 3, 10 |
| Display name + alias lookup | Task 7 |
| Category inference | Task 5 |
| Wire values policy | All schema tests use wire values; Task 12 documents it |
| Chassis extraction | Tasks 13, 17 |
| Spec format + parallel rejection | Tasks 19, 20 |
| Forward-compat for parallel | Task 20 (rejection); doc reference in Task 1 |
| Ingest: file, directory, idempotency, conflicts, chassis-on-first | Tasks 16, 17, 18 |
| Generate: resolve, validate, compose, write | Tasks 21, 22, 23, 24 |
| Bootstrap | Tasks 25, 26 |
| Error handling (loud, named) | Tasks 7, 19, 20, 22, 23, 28, 30 |
| Tests: unit + integration + round-trip | Throughout + Tasks 32, 33 |
| Real-export validation | Task 34 (manual) |
| README | Task 35 |
| `--library` + env var | Tasks 4, 27 |

**Open spec items I want to flag:**

- The plan does not implement a separate `meta.json` library file (mentioned in spec Section "Block library"). It's a low-value telemetry sink and not referenced by any other code. If the user wants it later, it's a one-task add: bump a counter inside `ingest_path` and append firmware versions seen.
- The plan accepts `.hlx` and `.json` for ingest. The spec's example summary shows ingesting a directory; the plan implements that and rebuilds the index.
- Per-block `observed_range` widening across multiple ingests is **not** implemented — the schema is captured from the first sighting only and never widened by subsequent matching sightings. The spec describes range as descriptive metadata; widening it is a small enhancement. I left it out of v1 because it adds complexity for no current consumer (Layer 2 will be the eventual consumer). Flag this so the user can request it if wanted.

**Placeholder scan:** No "TBD" / "TODO" / "implement later" text in any task. Every step has full code or a concrete shell command.

**Type / name consistency check:** `IngestStatus`, `IngestSummary`, `Block`, `Spec`, `BlockEntry`, `PathEntry`, `Shape` — all defined once and used consistently across tasks. CLI command callbacks are all named `*_cmd` to avoid collisions with imported helpers (e.g., `bootstrap` the function vs `bootstrap_cmd` the click command).

**Plan complete and saved to `docs/superpowers/plans/2026-05-01-helix-preset-generator.md`.**

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
