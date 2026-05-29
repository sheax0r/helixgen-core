# IR Registration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a manual `irhash`→`wav` mapping (`register-irs`, `list-irs`), a block-level `ir` field in the spec, and teach the generator to emit `irhash` on IR slots — using only stdlib + click, following the existing TDD discipline.

**Architecture:** New `src/helixgen/ir.py` module owns the `IrMapping` dataclass (load/save/register/resolve) and a small helper to read `irhash` values out of an existing `.hsp` preset. `cli.py` exposes two thin subcommands that delegate to it. `spec.py` accepts a new optional block-level `ir` field. `ingest.py` records slot-level `irhash` on IR-block schemas. `generate.py` emits `irhash` on IR slots, sourced from either the spec's `ir` field (resolved via mapping) or the canonical ingested hash. Tone-skill update is a final separate edit.

**Tech Stack:** Python 3.10+ (stdlib only — `json`, `os`, `pathlib`, `dataclasses`, `hashlib` for tests), `click` for the CLI, `pytest` (via `CliRunner`) for tests. Tests live in `tests/`; design spec in `docs/superpowers/specs/2026-05-28-ir-registration-design.md`.

---

## File structure

| File                              | Status   | Responsibility                                                                  |
|-----------------------------------|----------|---------------------------------------------------------------------------------|
| `src/helixgen/ir.py`              | **new**  | `IrMapping` (load/save/register/resolve), `default_irs_path`, `extract_ir_hashes` |
| `src/helixgen/cli.py`             | modify   | Add `register-irs` and `list-irs` subcommands                                   |
| `src/helixgen/spec.py`            | modify   | Allow optional `ir` field on block entries; reject on non-IR blocks             |
| `src/helixgen/ingest.py`          | modify   | Capture slot-level `irhash` on IR-block schemas                                 |
| `src/helixgen/generate.py`        | modify   | Emit slot-level `irhash` from spec.ir or canonical; error when neither          |
| `CLAUDE.md`                       | modify   | Document `HELIXGEN_IRS`, the two new commands, and the `ir` field               |
| `.claude/skills/tone/SKILL.md`    | modify   | Make tone-skill IR-aware (memory-gated preference)                              |
| `tests/test_ir_mapping.py`        | **new**  | Unit tests for `IrMapping` (load/save/register/resolve)                         |
| `tests/test_ir_preset.py`         | **new**  | Unit tests for `extract_ir_hashes`                                              |
| `tests/test_ir_cli.py`            | **new**  | CLI tests for `register-irs` and `list-irs`                                     |
| `tests/test_ir_spec.py`           | **new**  | Spec-parsing tests for the `ir` field                                           |
| `tests/test_ir_ingest.py`         | **new**  | Ingest captures slot-level `irhash`                                             |
| `tests/test_ir_generate.py`       | **new**  | Generate emits `irhash` from spec-sugar or canonical fallback                   |

---

## Style conventions to follow

- pytest plain functions, no classes. Use `monkeypatch` for env vars.
- CLI tests use `click.testing.CliRunner`.
- Errors surfaced to the user wrap with `click.ClickException`.
- Library/IRs-dir env vars use the click `envvar=` pattern (see `_library_option` in `cli.py:16-24`).
- One responsibility per test. Use the existing `tmp_library` fixture from `tests/conftest.py` when you need a library; create a small `tmp_irs_dir` helper inline when you need an IRs dir.
- All file writes are atomic (tmp file + `os.replace`) — see `library.py` for the established pattern.

---

## Task 1: `IrMapping` skeleton — load/save/default_irs_path

**Files:**
- Create: `src/helixgen/ir.py`
- Create: `tests/test_ir_mapping.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ir_mapping.py
import json
import os
from pathlib import Path

import pytest

from helixgen.ir import IrMapping, default_irs_path


def test_default_irs_path_uses_home(monkeypatch):
    monkeypatch.delenv("HELIXGEN_IRS", raising=False)
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    assert default_irs_path() == Path("/tmp/fake-home/.helixgen/irs")


def test_default_irs_path_honors_env_var(monkeypatch):
    monkeypatch.setenv("HELIXGEN_IRS", "/custom/irs")
    assert default_irs_path() == Path("/custom/irs")


def test_load_returns_empty_when_no_file(tmp_path):
    mapping = IrMapping.load(tmp_path)
    assert mapping.irs_dir == tmp_path
    assert mapping.entries == {}


def test_save_then_reload_round_trips(tmp_path):
    m = IrMapping(irs_dir=tmp_path, entries={"abc": "foo.wav"})
    m.save()
    on_disk = json.loads((tmp_path / "mapping.json").read_text())
    assert on_disk == {"abc": "foo.wav"}
    reloaded = IrMapping.load(tmp_path)
    assert reloaded.entries == {"abc": "foo.wav"}


def test_save_is_atomic(tmp_path):
    """If save crashes mid-write, mapping.json must not be partial."""
    m = IrMapping(irs_dir=tmp_path, entries={"abc": "foo.wav"})
    m.save()
    # Verify no .tmp file is left behind on a successful save
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"stray tmp files: {leftovers}"


def test_save_creates_directory(tmp_path):
    target = tmp_path / "nested" / "irs"
    m = IrMapping(irs_dir=target, entries={"abc": "foo.wav"})
    m.save()
    assert (target / "mapping.json").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.config/superpowers/worktrees/helixgen/ir-registration && .venv/bin/pytest tests/test_ir_mapping.py -v`
Expected: ImportError / ModuleNotFoundError on `helixgen.ir`.

- [ ] **Step 3: Implement the module**

```python
# src/helixgen/ir.py
"""User-IR registration: maps Helix `irhash` slot values to local .wav paths."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def default_irs_path() -> Path:
    """Return the IRs directory path, honoring HELIXGEN_IRS env var."""
    env = os.environ.get("HELIXGEN_IRS")
    if env:
        return Path(env)
    return Path(os.environ["HOME"]) / ".helixgen" / "irs"


@dataclass
class IrMapping:
    """Hash→wav-path mapping for user IRs registered with helixgen."""

    irs_dir: Path
    entries: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, irs_dir: Path | None = None) -> "IrMapping":
        irs_dir = irs_dir if irs_dir is not None else default_irs_path()
        mapping_file = irs_dir / "mapping.json"
        if not mapping_file.exists():
            return cls(irs_dir=irs_dir, entries={})
        data = json.loads(mapping_file.read_text())
        return cls(irs_dir=irs_dir, entries=dict(data))

    def save(self) -> None:
        """Write mapping.json atomically. Creates irs_dir if needed."""
        self.irs_dir.mkdir(parents=True, exist_ok=True)
        target = self.irs_dir / "mapping.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.entries, indent=2, sort_keys=True))
        os.replace(tmp, target)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ir_mapping.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ir.py tests/test_ir_mapping.py
git commit -m "feat(ir): IrMapping load/save with atomic write"
```

---

## Task 2: `IrMapping.register` — happy path + canonicalize

**Files:**
- Modify: `src/helixgen/ir.py`
- Modify: `tests/test_ir_mapping.py`

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_ir_mapping.py
def test_register_records_new_hash_relative_when_inside_irs_dir(tmp_path):
    wav = tmp_path / "sub" / "foo.wav"
    wav.parent.mkdir()
    wav.write_bytes(b"riff")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc123", wav)
    assert m.entries == {"abc123": "sub/foo.wav"}


def test_register_records_absolute_when_outside_irs_dir(tmp_path):
    outside_wav = tmp_path.parent / "outside.wav"
    outside_wav.write_bytes(b"riff")
    irs = tmp_path / "irs"
    irs.mkdir()
    m = IrMapping(irs_dir=irs)
    m.register("abc123", outside_wav)
    assert m.entries == {"abc123": str(outside_wav.resolve())}


def test_register_same_hash_same_file_is_idempotent(tmp_path):
    wav = tmp_path / "foo.wav"
    wav.write_bytes(b"riff")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc123", wav)
    m.register("abc123", wav)  # no-op
    assert m.entries == {"abc123": "foo.wav"}


def test_register_validates_wav_exists(tmp_path):
    m = IrMapping(irs_dir=tmp_path)
    with pytest.raises(FileNotFoundError, match="missing.wav"):
        m.register("abc123", tmp_path / "missing.wav")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ir_mapping.py -v`
Expected: 4 failures with `AttributeError: 'IrMapping' object has no attribute 'register'`.

- [ ] **Step 3: Implement `register`**

Add to `IrMapping` class in `src/helixgen/ir.py`:

```python
    def register(self, hash_: str, wav_path: Path, *, force: bool = False) -> None:
        """Bind hash → wav_path. Idempotent for same (hash, file); see Task 3 for conflicts."""
        wav_path = Path(wav_path)
        if not wav_path.is_file():
            raise FileNotFoundError(f"wav file not found: {wav_path}")
        canonical = self._canonical(wav_path)
        existing = self.entries.get(hash_)
        if existing is not None and existing == canonical:
            return  # idempotent
        self.entries[hash_] = canonical

    def _canonical(self, wav_path: Path) -> str:
        """Return path relative to irs_dir if under it, else absolute."""
        wav_abs = wav_path.resolve()
        irs_abs = self.irs_dir.resolve()
        try:
            return str(wav_abs.relative_to(irs_abs))
        except ValueError:
            return str(wav_abs)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ir_mapping.py -v`
Expected: 9 passed (5 from Task 1 + 4 new).

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ir.py tests/test_ir_mapping.py
git commit -m "feat(ir): IrMapping.register with path canonicalization + idempotence"
```

---

## Task 3: `IrMapping.register` — conflict + `force`

**Files:**
- Modify: `src/helixgen/ir.py`
- Modify: `tests/test_ir_mapping.py`

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_ir_mapping.py
from helixgen.ir import IrMappingError


def test_register_conflict_raises(tmp_path):
    wav1 = tmp_path / "a.wav"
    wav2 = tmp_path / "b.wav"
    wav1.write_bytes(b"a")
    wav2.write_bytes(b"b")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc", wav1)
    with pytest.raises(IrMappingError, match="already mapped"):
        m.register("abc", wav2)


def test_register_force_overwrites(tmp_path):
    wav1 = tmp_path / "a.wav"
    wav2 = tmp_path / "b.wav"
    wav1.write_bytes(b"a")
    wav2.write_bytes(b"b")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc", wav1)
    m.register("abc", wav2, force=True)
    assert m.entries == {"abc": "b.wav"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ir_mapping.py -v`
Expected: ImportError on `IrMappingError` and/or test failures.

- [ ] **Step 3: Implement**

Add to `src/helixgen/ir.py` (near top, after imports):

```python
class IrMappingError(ValueError):
    """Raised when an IR mapping operation is rejected (conflict, ambiguity, etc.)."""
```

Modify `register` body — replace the `if existing is not None and existing == canonical` block with:

```python
        existing = self.entries.get(hash_)
        if existing is not None:
            if existing == canonical:
                return  # idempotent
            if not force:
                raise IrMappingError(
                    f"hash {hash_} is already mapped to {existing}; "
                    f"refusing to overwrite with {canonical} (use force=True)"
                )
        self.entries[hash_] = canonical
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ir_mapping.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ir.py tests/test_ir_mapping.py
git commit -m "feat(ir): IrMapping.register conflict detection + force flag"
```

---

## Task 4: `IrMapping.resolve_by_hash` + `resolve_by_basename`

**Files:**
- Modify: `src/helixgen/ir.py`
- Modify: `tests/test_ir_mapping.py`

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_ir_mapping.py
def test_resolve_by_hash_returns_absolute_path(tmp_path):
    wav = tmp_path / "foo.wav"
    wav.write_bytes(b"r")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc", wav)
    resolved = m.resolve_by_hash("abc")
    assert resolved == wav.resolve()


def test_resolve_by_hash_unknown_raises(tmp_path):
    m = IrMapping(irs_dir=tmp_path)
    with pytest.raises(IrMappingError, match="unknown IR hash"):
        m.resolve_by_hash("does-not-exist")


def test_resolve_by_basename_unique_returns_hash_and_path(tmp_path):
    sub = tmp_path / "packA"
    sub.mkdir()
    wav = sub / "foo.wav"
    wav.write_bytes(b"r")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc", wav)
    hash_, path = m.resolve_by_basename("foo.wav")
    assert hash_ == "abc"
    assert path == wav.resolve()


def test_resolve_by_basename_ambiguous_raises(tmp_path):
    a = tmp_path / "packA"
    b = tmp_path / "packB"
    a.mkdir()
    b.mkdir()
    wav_a = a / "foo.wav"
    wav_b = b / "foo.wav"
    wav_a.write_bytes(b"a")
    wav_b.write_bytes(b"b")
    m = IrMapping(irs_dir=tmp_path)
    m.register("h_a", wav_a)
    m.register("h_b", wav_b)
    with pytest.raises(IrMappingError, match="ambiguous"):
        m.resolve_by_basename("foo.wav")


def test_resolve_by_basename_missing_raises(tmp_path):
    m = IrMapping(irs_dir=tmp_path)
    with pytest.raises(IrMappingError, match="no registered IR matches"):
        m.resolve_by_basename("nope.wav")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ir_mapping.py -v`
Expected: 5 failures with `AttributeError: 'IrMapping' object has no attribute 'resolve_by_hash'`.

- [ ] **Step 3: Implement**

Add to `IrMapping` class in `src/helixgen/ir.py`:

```python
    def resolve_by_hash(self, hash_: str) -> Path:
        """Return absolute Path for hash. Raises IrMappingError on miss."""
        if hash_ not in self.entries:
            raise IrMappingError(f"unknown IR hash {hash_}")
        return self._absolute(self.entries[hash_])

    def resolve_by_basename(self, basename: str) -> tuple[str, Path]:
        """Return (hash, absolute_path) for unique basename match.

        Case-sensitive. Raises IrMappingError on ambiguous or missing.
        """
        matches = [
            (h, p) for h, p in self.entries.items() if os.path.basename(p) == basename
        ]
        if not matches:
            raise IrMappingError(f"no registered IR matches basename {basename!r}")
        if len(matches) > 1:
            paths = ", ".join(p for _, p in matches)
            raise IrMappingError(
                f"ambiguous IR basename {basename!r}; matches: {paths}"
            )
        h, p = matches[0]
        return h, self._absolute(p)

    def _absolute(self, stored: str) -> Path:
        p = Path(stored)
        if p.is_absolute():
            return p
        return (self.irs_dir / p).resolve()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ir_mapping.py -v`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ir.py tests/test_ir_mapping.py
git commit -m "feat(ir): IrMapping.resolve_by_hash / resolve_by_basename"
```

---

## Task 5: `extract_ir_hashes` from a preset file

**Files:**
- Modify: `src/helixgen/ir.py`
- Create: `tests/test_ir_preset.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ir_preset.py
"""Tests for extract_ir_hashes — reads slot-level irhash values from a .hsp body dict."""
from helixgen.ir import extract_ir_hashes


def make_ir_block(path: int, position: int, irhash: str) -> tuple[str, dict]:
    key = f"b{position:02d}_p{path}"
    return key, {
        "path": path,
        "position": position,
        "slot": [{"model": "HX2_ImpulseResponseWithPan", "irhash": irhash, "params": {}}],
    }


def make_input_block(key: str = "b00") -> tuple[str, dict]:
    return key, {
        "path": 0,
        "position": 0,
        "slot": [{"model": "P35_InputInst1", "params": {}}],
    }


def test_extract_ir_hashes_in_path_then_position_order():
    flow = {}
    flow.update([make_input_block("b00")])
    flow.update([make_ir_block(0, 2, "hash_p0_pos2")])
    flow.update([make_ir_block(0, 1, "hash_p0_pos1")])
    flow.update([make_ir_block(1, 1, "hash_p1_pos1")])

    preset = {"preset": {"flow": [flow]}}
    assert extract_ir_hashes(preset) == [
        "hash_p0_pos1",
        "hash_p0_pos2",
        "hash_p1_pos1",
    ]


def test_extract_ir_hashes_ignores_non_ir_blocks():
    flow = {}
    flow.update([make_input_block("b00")])
    flow.update([make_ir_block(0, 1, "ir_a")])
    flow["b02"] = {
        "path": 0,
        "position": 2,
        "slot": [{"model": "HD2_AmpBritPlexi", "params": {}}],
    }
    flow.update([make_ir_block(0, 3, "ir_b")])

    preset = {"preset": {"flow": [flow]}}
    assert extract_ir_hashes(preset) == ["ir_a", "ir_b"]


def test_extract_ir_hashes_empty_preset():
    assert extract_ir_hashes({"preset": {"flow": [{}]}}) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ir_preset.py -v`
Expected: ImportError on `extract_ir_hashes`.

- [ ] **Step 3: Implement**

Add to `src/helixgen/ir.py` (top-level, below the dataclass):

```python
IR_MODEL_PREFIX = "HX2_ImpulseResponse"


def extract_ir_hashes(preset_body: dict) -> list[str]:
    """Return slot-level irhash values from a .hsp body dict, in (path, position) order.

    Blocks whose `slot[0].model` does not start with HX2_ImpulseResponse are ignored.
    """
    hashes: list[tuple[int, int, str]] = []
    for path_obj in preset_body.get("preset", {}).get("flow", []):
        if not isinstance(path_obj, dict):
            continue
        for v in path_obj.values():
            if not isinstance(v, dict) or "slot" not in v:
                continue
            slot = v["slot"][0]
            if not str(slot.get("model", "")).startswith(IR_MODEL_PREFIX):
                continue
            if "irhash" not in slot:
                continue
            hashes.append((int(v.get("path", 0)), int(v.get("position", 0)), slot["irhash"]))
    hashes.sort()
    return [h for _, _, h in hashes]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ir_preset.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/ir.py tests/test_ir_preset.py
git commit -m "feat(ir): extract_ir_hashes preset reader"
```

---

## Task 6: `register-irs` CLI command

**Files:**
- Modify: `src/helixgen/cli.py`
- Create: `tests/test_ir_cli.py`

Per the design, the command reads a `.hsp` body (8-byte magic + JSON). `helixgen.hsp` exports `HSP_MAGIC` and `HSP_MAGIC_LEN`. Use those constants directly.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ir_cli.py
"""CLI tests for register-irs and list-irs."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen.cli import cli

HSP_MAGIC = b"rpshnosj"


def _write_hsp(path: Path, body: dict) -> None:
    path.write_bytes(HSP_MAGIC + json.dumps(body).encode())


def _ir_block(path: int, position: int, irhash: str) -> dict:
    return {
        "path": path,
        "position": position,
        "slot": [{"model": "HX2_ImpulseResponseWithPan", "irhash": irhash, "params": {}}],
    }


def _preset_with_irs(hashes: list[str]) -> dict:
    flow = {}
    for i, h in enumerate(hashes, start=1):
        flow[f"b{i:02d}"] = _ir_block(0, i, h)
    return {"meta": {"name": "t"}, "preset": {"flow": [flow]}}


def _write_wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF\0\0\0\0WAVE")
    return path


def test_register_irs_happy_path(tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    preset = tmp_path / "reg.hsp"
    _write_hsp(preset, _preset_with_irs(["hash1", "hash2"]))

    wav1 = _write_wav(irs_dir / "a.wav")
    wav2 = _write_wav(irs_dir / "b.wav")

    result = CliRunner().invoke(cli, ["register-irs", str(preset), str(wav1), str(wav2)])
    assert result.exit_code == 0, result.output
    mapping = json.loads((irs_dir / "mapping.json").read_text())
    assert mapping == {"hash1": "a.wav", "hash2": "b.wav"}


def test_register_irs_count_mismatch_errors(tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    preset = tmp_path / "reg.hsp"
    _write_hsp(preset, _preset_with_irs(["h1", "h2"]))
    wav1 = _write_wav(irs_dir / "a.wav")

    result = CliRunner().invoke(cli, ["register-irs", str(preset), str(wav1)])
    assert result.exit_code != 0
    assert "2 IR blocks" in result.output and "1 wav" in result.output


def test_register_irs_conflict_errors_without_force(tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    preset = tmp_path / "reg.hsp"
    _write_hsp(preset, _preset_with_irs(["hX"]))
    wav_old = _write_wav(irs_dir / "old.wav")
    wav_new = _write_wav(irs_dir / "new.wav")

    CliRunner().invoke(cli, ["register-irs", str(preset), str(wav_old)])
    result = CliRunner().invoke(cli, ["register-irs", str(preset), str(wav_new)])
    assert result.exit_code != 0
    assert "already mapped" in result.output


def test_register_irs_force_overwrites(tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    preset = tmp_path / "reg.hsp"
    _write_hsp(preset, _preset_with_irs(["hX"]))
    wav_old = _write_wav(irs_dir / "old.wav")
    wav_new = _write_wav(irs_dir / "new.wav")

    CliRunner().invoke(cli, ["register-irs", str(preset), str(wav_old)])
    result = CliRunner().invoke(
        cli, ["register-irs", "--force", str(preset), str(wav_new)]
    )
    assert result.exit_code == 0, result.output
    mapping = json.loads((irs_dir / "mapping.json").read_text())
    assert mapping == {"hX": "new.wav"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ir_cli.py -v`
Expected: `Error: No such command 'register-irs'`.

- [ ] **Step 3: Implement**

Add to `src/helixgen/cli.py`:

At the imports block (after the existing helixgen imports):

```python
from helixgen.ir import IrMapping, IrMappingError, default_irs_path, extract_ir_hashes
```

Add a shared option decorator (alongside `_library_option`):

```python
def _irs_option(f):
    return click.option(
        "--irs-dir",
        "irs_dir",
        envvar="HELIXGEN_IRS",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="IRs directory. Defaults to ~/.helixgen/irs/ or $HELIXGEN_IRS.",
    )(f)


def _resolved_irs(irs_dir: Path | None) -> IrMapping:
    return IrMapping.load(irs_dir if irs_dir is not None else default_irs_path())
```

Add the command:

```python
@cli.command(name="register-irs")
@click.argument("preset_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("wav_paths", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--force", is_flag=True, default=False, help="Overwrite existing hash mappings.")
@_irs_option
def register_irs_cmd(
    preset_path: Path,
    wav_paths: tuple[Path, ...],
    force: bool,
    irs_dir: Path | None,
) -> None:
    """Bind irhash values from a .hsp registration preset to local .wav files (in block order)."""
    import json as _json
    from helixgen.hsp import HSP_MAGIC, HSP_MAGIC_LEN

    raw = preset_path.read_bytes()
    if not raw.startswith(HSP_MAGIC):
        raise click.ClickException(f"{preset_path} is not a Stadium .hsp file")
    body = _json.loads(raw[HSP_MAGIC_LEN:])
    hashes = extract_ir_hashes(body)

    if len(hashes) != len(wav_paths):
        raise click.ClickException(
            f"preset has {len(hashes)} IR blocks, got {len(wav_paths)} wav arg(s)"
        )

    mapping = _resolved_irs(irs_dir)
    try:
        for h, wav in zip(hashes, wav_paths):
            mapping.register(h, wav, force=force)
    except IrMappingError as e:
        raise click.ClickException(str(e)) from e
    mapping.save()
    click.echo(f"Registered {len(hashes)} IR(s) to {mapping.irs_dir / 'mapping.json'}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ir_cli.py -v`
Expected: 4 passed (the register-irs ones).

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/cli.py tests/test_ir_cli.py
git commit -m "feat(cli): register-irs subcommand"
```

---

## Task 7: `list-irs` CLI command

**Files:**
- Modify: `src/helixgen/cli.py`
- Modify: `tests/test_ir_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_ir_cli.py
def test_list_irs_empty_prints_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_IRS", str(tmp_path))
    result = CliRunner().invoke(cli, ["list-irs"])
    assert result.exit_code == 0
    assert result.output == ""


def test_list_irs_prints_one_per_line_sorted(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_IRS", str(tmp_path))
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps({
        "bbb": "second.wav",
        "aaa": "first.wav",
    }))
    result = CliRunner().invoke(cli, ["list-irs"])
    assert result.exit_code == 0
    # sorted by hash
    assert result.output == "aaa  first.wav\nbbb  second.wav\n"


def test_cli_help_lists_new_commands():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "register-irs" in result.output
    assert "list-irs" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ir_cli.py::test_list_irs_empty_prints_nothing -v`
Expected: `Error: No such command 'list-irs'`.

- [ ] **Step 3: Implement**

Add to `src/helixgen/cli.py`:

```python
@cli.command(name="list-irs")
@_irs_option
def list_irs_cmd(irs_dir: Path | None) -> None:
    """List registered IR hashes and their wav paths."""
    mapping = _resolved_irs(irs_dir)
    for hash_ in sorted(mapping.entries):
        click.echo(f"{hash_}  {mapping.entries[hash_]}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ir_cli.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/cli.py tests/test_ir_cli.py
git commit -m "feat(cli): list-irs subcommand"
```

---

## Task 8: spec.py accepts `ir` field on blocks

**Files:**
- Modify: `src/helixgen/spec.py`
- Create: `tests/test_ir_spec.py`

Read the existing block-entry parser first:

```bash
grep -nE "block|ir" ~/.config/superpowers/worktrees/helixgen/ir-registration/src/helixgen/spec.py | head -30
```

Note the existing fields (`block`, `params`) and the dataclass/dict shape. The new `ir` field is optional, type `str | None`. It is carried through to generate-time without resolution (generate does the lookup).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ir_spec.py
"""Spec parser tests for the optional `ir` field on block entries."""
import json
from pathlib import Path

import pytest

from helixgen.spec import SpecError, load_spec


def _write_spec(path: Path, blocks: list[dict]) -> None:
    path.write_text(json.dumps({"name": "t", "paths": [{"blocks": blocks}]}))


def test_ir_field_basename_carried_through(tmp_path):
    p = tmp_path / "s.json"
    _write_spec(p, [{"block": "With Pan", "ir": "foo.wav"}])
    spec = load_spec(p)
    block = spec.paths[0].blocks[0]
    assert block.ir == "foo.wav"


def test_ir_field_hash_carried_through(tmp_path):
    p = tmp_path / "s.json"
    _write_spec(p, [{"block": "With Pan", "ir": "ad8182e1ebe9fd95dffde5dd54b6d89c"}])
    spec = load_spec(p)
    assert spec.paths[0].blocks[0].ir == "ad8182e1ebe9fd95dffde5dd54b6d89c"


def test_block_without_ir_field_has_none(tmp_path):
    p = tmp_path / "s.json"
    _write_spec(p, [{"block": "Brit Plexi Brt", "params": {"Drive": 0.7}}])
    spec = load_spec(p)
    assert spec.paths[0].blocks[0].ir is None


def test_ir_field_rejects_non_string(tmp_path):
    p = tmp_path / "s.json"
    _write_spec(p, [{"block": "With Pan", "ir": 42}])
    with pytest.raises(SpecError, match="ir.*str"):
        load_spec(p)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ir_spec.py -v`
Expected: failures on `AttributeError: ... has no attribute 'ir'` (or similar).

- [ ] **Step 3: Implement**

In `src/helixgen/spec.py`:

(a) Add `ir: str | None = None` to the `BlockEntry` dataclass at line 12-15:

```python
@dataclass
class BlockEntry:
    block: str
    params: dict[str, Any] = field(default_factory=dict)
    ir: str | None = None
```

(b) In `_parse_block_entry` (line 148-167), after the `params` validation and before the `return`, add:

```python
    ir = data.get("ir")
    if ir is not None and not isinstance(ir, str):
        raise _err(source, '"ir" must be a string if provided.')

    return BlockEntry(block=name, params=dict(params), ir=ir)
```

(replace the existing `return BlockEntry(...)` line.)

Note: rejection-on-non-IR-block happens at generate time (Task 10) — spec parsing can't know a block's category without consulting the library, so this stays purely structural here.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ir_spec.py -v`
Expected: 4 passed.

- [ ] **Step 5: Verify full suite still passes**

Run: `.venv/bin/pytest -q`
Expected: all green (no regressions in existing spec/generate tests).

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/spec.py tests/test_ir_spec.py
git commit -m "feat(spec): optional block-level ir field for IR slots"
```

---

## Task 9: ingest captures slot-level `irhash` on IR blocks

**Files:**
- Modify: `src/helixgen/ingest.py`
- Create: `tests/test_ir_ingest.py`

Re-read the ingest function that processes a single block (`block_from_raw` and `extract_blocks_from_preset` in `ingest.py`). The slot-level `irhash` lives on the slot dict alongside `model`, not in `params`. Today the ingester only reads `model` and `params` from the slot; this task adds `irhash` capture.

The captured hash lives on the canonical `Block` entry. The simplest place is a new optional field on `Block` (e.g. `default_irhash: str | None = None`), persisted alongside other block metadata in the library JSON.

Check the Block dataclass in `library.py`:

```bash
grep -nE "^class Block|^    [a-z_]+:" ~/.config/superpowers/worktrees/helixgen/ir-registration/src/helixgen/library.py | head -30
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ir_ingest.py
"""Ingest captures the slot-level irhash on IR-block schemas."""
import json
from pathlib import Path

from helixgen.ingest import block_from_raw


def test_block_from_raw_captures_irhash():
    raw_slot = {
        "model": "HX2_ImpulseResponseWithPan",
        "irhash": "ad8182e1ebe9fd95dffde5dd54b6d89c",
        "params": {"HighCut": {"value": 6500.0}},
    }
    src = {"preset": "x.hsp", "firmware": "t", "date": "2026-05-28"}
    block = block_from_raw(raw_slot, src)
    assert block.default_irhash == "ad8182e1ebe9fd95dffde5dd54b6d89c"


def test_block_from_raw_no_irhash_for_non_ir_block():
    raw_slot = {"model": "HD2_AmpBritPlexi", "params": {"Drive": {"value": 0.7}}}
    src = {"preset": "x.hsp", "firmware": "t", "date": "2026-05-28"}
    block = block_from_raw(raw_slot, src)
    assert block.default_irhash is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ir_ingest.py -v`
Expected: `AttributeError: 'Block' object has no attribute 'default_irhash'`.

- [ ] **Step 3: Implement**

(a) In `src/helixgen/library.py`, add to `Block` dataclass:

```python
    default_irhash: str | None = None
```

Place it after the existing optional fields. Ensure `to_dict` writes it only when not None (preserves backward-compat for existing library JSON) and `from_dict` reads it with `.get("default_irhash")`.

Concrete edit to `to_dict`:

```python
        if self.default_irhash is not None:
            out["default_irhash"] = self.default_irhash
```

Concrete edit to `from_dict`:

```python
            default_irhash=data.get("default_irhash"),
```

(b) In `src/helixgen/ingest.py`, in `block_from_raw`, after determining `model_id`, add:

```python
    default_irhash = raw_slot.get("irhash") if str(model_id).startswith("HX2_ImpulseResponse") else None
```

Pass `default_irhash=default_irhash` into the `Block(...)` constructor.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ir_ingest.py tests/test_library.py -v`
Expected: all green (new tests pass, existing library tests still pass).

- [ ] **Step 5: Verify full suite**

Run: `.venv/bin/pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/library.py src/helixgen/ingest.py tests/test_ir_ingest.py
git commit -m "feat(ingest): capture slot-level irhash as Block.default_irhash"
```

---

## Task 10: generate emits `irhash` (spec.ir OR canonical) on IR slots

**Files:**
- Modify: `src/helixgen/generate.py`
- Create: `tests/test_ir_generate.py`

This is the load-bearing integration task. The HSP emitter (`_compose_preset_hsp` and `_to_hsp_bnn` in `generate.py`) currently builds slot dicts from `{model, params, version}`. Add a slot-level `irhash` key for IR blocks, sourced from:

1. The spec's `block.ir` field if present (resolved via IrMapping — hash if 32-hex, else basename).
2. Otherwise `block.default_irhash` (the canonical ingested value).
3. Otherwise raise `GenerateError`.

The generator needs an `IrMapping` to resolve. Plumb it through: `generate_preset(spec_path, output_path, library, irs: IrMapping | None = None)` — load default if None.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ir_generate.py
"""Generator emits slot-level irhash on IR blocks (spec.ir or canonical fallback)."""
import json
from pathlib import Path

import pytest

from helixgen.chassis import extract_chassis
from helixgen.generate import GenerateError, generate_preset
from helixgen.ingest import block_from_raw
from helixgen.ir import IrMapping
from helixgen.library import Library

HSP_MAGIC = b"rpshnosj"


def _read_hsp_body(path: Path) -> dict:
    raw = path.read_bytes()
    return json.loads(raw[len(HSP_MAGIC):])


def _first_ir_slot(body: dict) -> dict:
    for path_obj in body["preset"]["flow"]:
        for v in path_obj.values():
            if isinstance(v, dict) and "slot" in v:
                slot = v["slot"][0]
                if str(slot.get("model", "")).startswith("HX2_ImpulseResponse"):
                    return slot
    raise AssertionError("no IR slot in preset")


@pytest.fixture
def stadium_library_with_ir(tmp_library, sample_serial_preset_hsp):
    """A library bootstrapped with a Stadium chassis + one IR block carrying a default hash."""
    lib = Library(tmp_library)
    lib.save_chassis(extract_chassis(sample_serial_preset_hsp))
    src = {"preset": "reg.hsp", "firmware": "t", "date": "2026-05-28"}
    raw = {
        "model": "HX2_ImpulseResponseWithPan",
        "irhash": "ad8182e1ebe9fd95dffde5dd54b6d89c",
        "params": {"HighCut": {"value": 20100.0}, "LowCut": {"value": 19.9},
                   "Mix": {"value": 1.0}, "Pan": {"value": 0.5},
                   "Level": {"value": -18.0}, "Delay": {"value": 0.0},
                   "IrData": {"value": 0}, "Polarity": {"value": False}},
    }
    lib.save_block_with_dedup(block_from_raw(raw, src))
    lib.rebuild_index()
    return lib


def test_generate_uses_canonical_irhash_when_spec_omits_ir(stadium_library_with_ir, tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "canon",
        "paths": [{"blocks": [{"block": "With Pan"}]}],
    }))
    out = tmp_path / "out.hsp"
    generate_preset(spec, out, stadium_library_with_ir)
    body = _read_hsp_body(out)
    assert _first_ir_slot(body)["irhash"] == "ad8182e1ebe9fd95dffde5dd54b6d89c"


def test_generate_uses_spec_ir_field_by_basename(stadium_library_with_ir, tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    wav = irs_dir / "Mix 05.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    m = IrMapping(irs_dir=irs_dir)
    m.register("da881f087ca8cf6be6266b564c8c7502", wav)
    m.save()

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "sugar",
        "paths": [{"blocks": [{"block": "With Pan", "ir": "Mix 05.wav"}]}],
    }))
    out = tmp_path / "out.hsp"
    generate_preset(spec, out, stadium_library_with_ir)
    body = _read_hsp_body(out)
    assert _first_ir_slot(body)["irhash"] == "da881f087ca8cf6be6266b564c8c7502"


def test_generate_uses_spec_ir_field_by_hash(stadium_library_with_ir, tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))
    m = IrMapping(irs_dir=irs_dir)
    wav = irs_dir / "x.wav"
    wav.write_bytes(b"RIFF")
    m.register("e93d155aedcf99109f7193f607707815", wav)
    m.save()

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "byhash",
        "paths": [{"blocks": [{"block": "With Pan",
                                "ir": "e93d155aedcf99109f7193f607707815"}]}],
    }))
    out = tmp_path / "out.hsp"
    generate_preset(spec, out, stadium_library_with_ir)
    body = _read_hsp_body(out)
    assert _first_ir_slot(body)["irhash"] == "e93d155aedcf99109f7193f607707815"


def test_generate_rejects_ir_field_on_non_ir_block(tmp_library, sample_serial_preset_hsp, sample_amp_block, tmp_path):
    """An `ir` field on a non-IR block must fail with a clear error."""
    lib = Library(tmp_library)
    lib.save_chassis(extract_chassis(sample_serial_preset_hsp))
    src = {"preset": "x.hsp", "firmware": "t", "date": "2026-05-28"}
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.rebuild_index()

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "wrong",
        "paths": [{"blocks": [{"block": "Brit 2204", "ir": "foo.wav"}]}],
    }))
    with pytest.raises(GenerateError, match="not an IR block"):
        generate_preset(spec, tmp_path / "out.hsp", lib)


def test_generate_errors_when_no_canonical_and_no_spec_ir(tmp_library, sample_serial_preset_hsp, tmp_path):
    """An IR block with no canonical default and no spec ir field MUST fail loudly."""
    lib = Library(tmp_library)
    lib.save_chassis(extract_chassis(sample_serial_preset_hsp))
    src = {"preset": "x.hsp", "firmware": "t", "date": "2026-05-28"}
    raw = {
        "model": "HX2_ImpulseResponseWithPan",
        # NB: no irhash
        "params": {"HighCut": {"value": 20100.0}, "LowCut": {"value": 19.9},
                   "Mix": {"value": 1.0}, "Pan": {"value": 0.5},
                   "Level": {"value": -18.0}, "Delay": {"value": 0.0},
                   "IrData": {"value": 0}, "Polarity": {"value": False}},
    }
    lib.save_block_with_dedup(block_from_raw(raw, src))
    lib.rebuild_index()

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "broken",
        "paths": [{"blocks": [{"block": "With Pan"}]}],
    }))
    out = tmp_path / "out.hsp"
    with pytest.raises(GenerateError, match="irhash"):
        generate_preset(spec, out, lib)
```

The fixture `sample_serial_preset_hsp` may not exist yet — check `tests/conftest.py`. If `sample_serial_preset` is `.hlx` shape only, add a sister fixture in conftest:

```python
@pytest.fixture
def sample_serial_preset_hsp() -> dict:
    """Minimal Stadium-chassis preset body for tests."""
    return {
        "meta": {"name": "t", "color": "auto", "device_id": 2490368,
                 "device_version": 318833973, "info": ""},
        "preset": {
            "clip": {"end": 0.0, "filename": "", "path": "", "start": 0.0},
            "cursor": {"flow": 0, "path": 0, "position": 0},
            "flow": [{}],
        },
    }
```

Reuse the `extract_chassis` output shape from this dict so the library knows the chassis is `hsp`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ir_generate.py -v`
Expected: failures — generate currently does not emit `irhash`.

- [ ] **Step 3: Implement**

(a) In `src/helixgen/generate.py`, update `_to_hsp_bnn` (the slot composer) to set `irhash` on IR slots. The function signature already takes the resolved block; it now also needs access to:
- the spec's `ir` field for the placed block (already on `BlockEntry.ir`)
- the IrMapping (passed through from `generate_preset`)

Add a helper near the top of `generate.py`:

```python
import re

_HASH_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def _resolve_irhash(block_default: str | None, spec_ir: str | None, irs: "IrMapping | None") -> str:
    """Decide which irhash to emit on an IR slot."""
    from helixgen.ir import IrMapping, IrMappingError  # local import to avoid cycle

    if spec_ir is not None:
        if irs is None:
            raise GenerateError(
                f"spec references IR {spec_ir!r} but no IRs are registered "
                f"(see `helixgen register-irs`)"
            )
        if _HASH_RE.fullmatch(spec_ir):
            try:
                irs.resolve_by_hash(spec_ir.lower())
            except IrMappingError as e:
                raise GenerateError(str(e)) from e
            return spec_ir.lower()
        try:
            h, _ = irs.resolve_by_basename(spec_ir)
            return h
        except IrMappingError as e:
            raise GenerateError(str(e)) from e
    if block_default is not None:
        return block_default
    raise GenerateError(
        "IR block requires an `ir` field (no canonical irhash available); "
        "see `helixgen list-irs`"
    )
```

(b) Plumb `irs` into `_compose_preset_hsp` and `generate_preset`. Default in `generate_preset`:

```python
def generate_preset(
    spec_path: Path,
    output_path: Path,
    library: Library,
    irs: "IrMapping | None" = None,
) -> Path:
    ...
    if irs is None:
        from helixgen.ir import IrMapping
        irs = IrMapping.load()  # default location, empty if no file
    ...
```

(c) In the IR-block slot emission inside `_to_hsp_bnn` (or wherever the slot dict is composed), once an IR block is detected (model startswith `HX2_ImpulseResponse`), add:

```python
            slot["irhash"] = _resolve_irhash(
                block_default=block.default_irhash,
                spec_ir=block_entry.ir,
                irs=irs,
            )
```

(c.i) Before slot emission, reject `ir` on non-IR blocks. In `_compose_preset_hsp`, after resolving spec blocks against the library, walk the resolved pairs and raise:

```python
    for path_entry in spec.paths:
        for block_entry, block in zip(path_entry.blocks, ...resolved blocks...):
            if block_entry.ir is not None and not block.model_id.startswith("HX2_ImpulseResponse"):
                raise GenerateError(
                    f"block {block.display_name!r} is not an IR block; "
                    f"remove the 'ir' field or change the block"
                )
```

(The exact iteration shape depends on the existing `resolve_blocks` return — match what's already there.)

(d) Update `cli.py`'s `generate_cmd` to load the IRs mapping and pass it:

```python
@cli.command(name="generate")
@click.argument("spec_path", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", "output_path", type=click.Path(path_type=Path), required=True)
@_library_option
@_irs_option
def generate_cmd(
    spec_path: Path,
    output_path: Path,
    library_path: Path | None,
    irs_dir: Path | None,
) -> None:
    """Generate a .hsp/.hlx preset from a JSON tone spec."""
    library = _resolved_library(library_path)
    irs = _resolved_irs(irs_dir)
    try:
        generate_preset(spec_path, output_path, library, irs=irs)
    except (KeyError, LookupError, SpecError, ParamValidationError, GenerateError, FileNotFoundError) as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Wrote {output_path}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ir_generate.py -v`
Expected: 5 passed.

- [ ] **Step 5: Verify full suite**

Run: `.venv/bin/pytest -q`
Expected: all green. Existing roundtrip tests should now produce IR blocks with their canonical `irhash` (previously the field was missing).

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/generate.py src/helixgen/cli.py tests/test_ir_generate.py tests/conftest.py
git commit -m "feat(generate): emit slot-level irhash from spec.ir or canonical default"
```

---

## Task 11: CLAUDE.md documentation

**Files:**
- Modify: `CLAUDE.md`

No tests for docs. Apply edits, verify by reading.

- [ ] **Step 1: Add a CLI bullet**

In the `## CLI` section, after the `bootstrap` bullet (or wherever the list ends):

```markdown
- `helixgen register-irs <preset.hsp> <wav1> <wav2> ...` — bind each unknown `irhash` in the preset (path-then-position order) to the corresponding wav arg. Use `--force` to overwrite existing mappings.
- `helixgen list-irs` — print `<hash>  <wav-path>` for every registered IR.
```

- [ ] **Step 2: Document `HELIXGEN_IRS`**

Update the opening paragraph (currently mentions `~/.helixgen/library/` + `HELIXGEN_LIBRARY`). After it, append:

```markdown
User IRs (impulse responses) registered with `helixgen register-irs` live at
`~/.helixgen/irs/` by default (override with `$HELIXGEN_IRS`). The mapping
file `mapping.json` records `irhash → wav-path`. See `helixgen list-irs`.
```

- [ ] **Step 3: Document the spec `ir` field**

Add a new section after "Optional: snapshots":

````markdown
### Optional: per-block IR reference

For IR blocks (`"block": "With Pan"` and other `HX2_ImpulseResponse*` variants),
add an optional `ir` field to load a registered user IR:

```json
{"block": "With Pan", "ir": "YA DXVB 112 Mix 01.wav",
 "params": {"HighCut": 6500.0, "LowCut": 90.0, "Mix": 1.0}}
```

- `ir` accepts a wav basename (looked up in `mapping.json` values) or a
  32-char hex hash (looked up in keys).
- If `ir` is omitted, the block uses the canonical `irhash` recorded during
  ingest of an IR-bearing preset.
- Register IRs first with `helixgen register-irs`; see `list-irs` for what's
  available.
````

- [ ] **Step 4: Update the project-layout module list**

In `## Project layout`, change `cli, ingest, hsp, chassis, library, spec, generate, bootstrap` to add `ir`:

```markdown
- `src/helixgen/` — `cli`, `ingest`, `hsp`, `chassis`, `library`, `spec`, `generate`, `bootstrap`, `ir`
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: register-irs/list-irs commands + spec ir field + HELIXGEN_IRS"
```

---

## Task 12: Tone-skill IR-awareness update

**Files:**
- Modify: `.claude/skills/tone/SKILL.md`

Behavior to encode: the tone skill checks `helixgen list-irs` early and, **only if** the user-preference memory says "prefer IRs over stock cabs when available", prefers IR blocks over stock cabs. Anti-fizz baseline still applies on the IR block.

- [ ] **Step 1: Find the right insertion point**

The existing skill has a "Pick blocks from the library" section (`### 3.`). Add a new sub-section after the cab-pick guidance, since the IR/stock-cab decision is upstream of which cab to pick.

Read context:

```bash
grep -n "^### 3\|^### 4\|^### 5\." ~/.config/superpowers/worktrees/helixgen/ir-registration/.claude/skills/tone/SKILL.md
```

- [ ] **Step 2: Insert the IR-awareness block**

After the existing `Cab pick matters a lot for "is this fizzy or musical":` bullet list at the end of step 3, append:

```markdown
**Check the user's IR library first** (memory-gated). Run `helixgen list-irs`. If the output is non-empty AND a feedback memory says the user prefers IRs over stock cabs when available, look for an IR that matches the chain's tonal target:

- Parse the wav filenames in the mapping — commercial IR packs encode cab + mic + position (e.g. `YA VX30 212 BLU Mix 01.wav` → Vox AC30-style 2x12 Blue, mix-position).
- If a match exists, use an IR block instead of a stock cab:
  ```json
  {"block": "With Pan", "ir": "YA VX30 212 BLU Mix 01.wav",
   "params": {"HighCut": 6500, "LowCut": 90, "Mix": 1.0}}
  ```
- Anti-fizz baseline (Hi Cut 6500–7000, Low Cut 80–100) still applies — set on the IR block itself.
- New users (no preference memory) get stock cabs by default. The preference flips on when the user explicitly says "from now on, prefer IRs when I have them" (and you save a feedback memory).
```

- [ ] **Step 3: Verify the edit reads well**

```bash
sed -n '70,110p' ~/.config/superpowers/worktrees/helixgen/ir-registration/.claude/skills/tone/SKILL.md
```

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/tone/SKILL.md
git commit -m "skill(tone): memory-gated IR preference over stock cabs"
```

---

## Self-review checklist

After implementing, verify against the spec:

| Spec requirement                                                      | Task |
|-----------------------------------------------------------------------|------|
| `~/.helixgen/irs/` default, `HELIXGEN_IRS` override                   | 1    |
| Mapping JSON: flat `{hash: wav-path}`                                 | 1    |
| `register-irs` positional, ordered                                    | 6    |
| `register-irs` count mismatch error                                   | 6    |
| `register-irs` conflict + `--force`                                   | 3, 6 |
| `register-irs` validates wav existence (click `exists=True`)          | 6    |
| `register-irs` auto-creates IRs dir + atomic write                    | 1, 6 |
| `list-irs` prints `hash  path` per line                               | 7    |
| Spec `ir` field accepts basename or hash                              | 8, 10|
| Ambiguous basename error                                              | 4, 10|
| Unknown hash error                                                    | 4, 10|
| Generator emits `irhash` from spec.ir OR canonical                    | 10   |
| `ir` field on non-IR block rejected at generate time                  | 10   |
| Ingest captures slot-level `irhash`                                   | 9    |
| CLAUDE.md updated                                                     | 11   |
| Tone-skill IR awareness (memory-gated)                                | 12   |

---

## Out of scope (deferred backlog)

- Cracking the Stadium `irhash` algorithm so users can drop a `.wav` in without round-tripping through a registration preset. Plan to be written after this lands.
- `show-ir` command.
- Mapping migration if users move their `irs/` directory.
- `.hlx` (legacy Helix) IR handling — uses a slot-number model, not a hash.
