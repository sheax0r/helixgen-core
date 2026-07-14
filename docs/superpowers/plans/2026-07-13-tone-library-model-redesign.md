# Tone-library model redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the tone (not the `.hsp`, not a ledger row) the first-class managed entity in a single tone-centric manifest, retire `SlotLedger`, and rework `device sync` into a managed-set mirror that installs/updates/reorders/removes only helixgen-managed tones.

**Architecture:** Evolve the shipped `SetlistManifest` to schema v2 (each `tones` record carries desired `slot` + observed `device`; `setlists` become `{tones, synced}`). Delete `ledger.py` and rewire its `cli.py` callers onto the manifest. Extend `setlist_sync.py` with slot auto-assignment, delete-on-unsync, and untracked-safe reconciliation. Auto-register every authored tone.

**Tech Stack:** Python 3 stdlib + `click` (CLI) + `mcp` SDK (MCP). Tests: `pytest` via `PYTHONPATH=$PWD/src python -m pytest`. Spec: `docs/superpowers/specs/2026-07-13-tone-library-model-redesign.md`.

## Global Constraints

- Pure stdlib + `click` runtime only; no new runtime deps.
- TDD: failing test first, minimal impl, then pass. Frequent commits.
- Run tests with `PYTHONPATH=$PWD/src python -m pytest`.
- The `.hsp` stays the audio source of truth; the manifest is the management source of truth. Never write placement/membership into a `.hsp`.
- Manifest file: `~/.helixgen/setlists.json`, override `$HELIXGEN_SETLISTS`. Atomic writes (temp + `os.replace`).
- Tone identity = its name (the `tones` key), unique in the manifest; also the device preset key.
- "On the device" ⟺ `slot != null`. Slot values: `"5A".."8D"`, `"auto"` (unresolved), or `null`.
- Device-import (`register --from-device`) is OUT OF SCOPE (separate fast-follow spec).
- Work on branch `tone-library-model-redesign` (already created). Ship = version bump in `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` (must match) + `pyproject.toml` + `src/helixgen/__init__.py`, PR, merge to `main`; CI cuts the release.

## File Structure

- `src/helixgen/device/manifest.py` — schema v2, migration, tone-record + setlist accessors (heaviest change).
- `src/helixgen/device/ledger.py` — **deleted**.
- `src/helixgen/device/setlist_sync.py` — managed-set mirror sync (slot auto-assign, delete-on-unsync).
- `src/helixgen/cli.py` — retire `_ledger_*`, re-home `device slots`, add `register` / `device add` / `device unsync` / `setlist sync-on|sync-off` / `library list`; auto-register in `generate`.
- `src/helixgen/recipe.py` or `generate.py` — hook auto-registration (called by both CLI + MCP).
- `mcp_server/tools.py` — mirror new verbs; auto-register in `generate_preset_handler`.
- `tests/test_manifest*.py`, `tests/test_setlist_sync*.py`, `tests/test_cli_*` — tests.

---

## Task 1: Manifest schema v2 — record shape, migration, round-trip

**Files:**
- Modify: `src/helixgen/device/manifest.py`
- Test: `tests/test_manifest_v2.py` (create)

**Interfaces:**
- Consumes: existing `SetlistManifest(path, *, tones, setlists, observed)`, `_hash_file`, `default_setlists_path`.
- Produces: `MANIFEST_VERSION == 2`; `tones[name]` records with keys `path, content_hash, doc, source, slot, device`; `setlists_map[name]` is `{"tones": List[str], "synced": bool}`; `SetlistManifest.load()` migrates a v1 doc (with `entries` + list-valued setlists) to v2 in memory; `to_dict()` / `save()` emit v2 and no longer preserve `entries`.

- [ ] **Step 1: Write the failing test** (`tests/test_manifest_v2.py`)

```python
import json
from pathlib import Path
from helixgen.device.manifest import SetlistManifest, MANIFEST_VERSION


def _write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc))


def test_version_is_2():
    assert MANIFEST_VERSION == 2


def test_loads_native_v2(tmp_path):
    p = tmp_path / "setlists.json"
    _write(p, {
        "version": 2,
        "tones": {"A": {"path": "/x/a.hsp", "content_hash": "sha256:aa",
                        "doc": None, "source": "authored", "slot": "5A",
                        "device": {"cid": 10, "posi": 17}}},
        "setlists": {"helixgen": {"tones": ["A"], "synced": True}},
    })
    m = SetlistManifest.load(p)
    assert m.tones["A"]["slot"] == "5A"
    assert m.setlists_map["helixgen"] == {"tones": ["A"], "synced": True}


def test_migrates_v1_entries_and_list_setlists(tmp_path):
    p = tmp_path / "setlists.json"
    _write(p, {
        "version": 1,
        "tones": {"A": {"path": "/x/a.hsp", "content_hash": "sha256:aa", "source": "hsp"}},
        "setlists": {"user": ["A"], "helixgen": ["A"]},
        "observed": {"pool": {"A": {"cid": 10, "posi": 3}},
                     "setlists": {"helixgen": {"cid": 42, "refs": {"A": {"ref_cid": 99, "posi": 0}}}}},
        "entries": [{"setlist": "user", "posi": 3, "name": "A", "cid": 10,
                     "source_kind": "hsp", "source_path": "/x/a.hsp", "slot_label": "1D"}],
    })
    m = SetlistManifest.load(p)
    # slot lifted from the ledger entry's slot_label; device from observed pool
    assert m.tones["A"]["slot"] == "1D"
    assert m.tones["A"]["device"] == {"cid": 10, "posi": 3}
    # setlists became {tones, synced}; helixgen was observed on device => synced True
    assert m.setlists_map["helixgen"] == {"tones": ["A"], "synced": True}
    assert m.setlists_map["user"]["synced"] is True  # user always device-backed
    # entries dropped on save
    m.save()
    on_disk = json.loads(p.read_text())
    assert on_disk["version"] == 2
    assert "entries" not in on_disk


def test_save_roundtrips_v2(tmp_path):
    p = tmp_path / "setlists.json"
    m = SetlistManifest(p)
    m.tones["A"] = {"path": None, "content_hash": None, "doc": None,
                    "source": "create", "slot": None, "device": None}
    m.setlists_map["draft"] = {"tones": ["A"], "synced": False}
    m.save()
    m2 = SetlistManifest.load(p)
    assert m2.tones["A"]["source"] == "create"
    assert m2.setlists_map["draft"] == {"tones": ["A"], "synced": False}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_manifest_v2.py -v`
Expected: FAIL (`MANIFEST_VERSION == 1`; v1 doc not migrated; setlists still lists).

- [ ] **Step 3: Implement v2 in `manifest.py`**

Change the version constant:

```python
MANIFEST_VERSION = 2
```

Add a normalizer + slot helper near the top (after `_hash_file`):

```python
_SLOT_LABELS = tuple(f"{b}{c}" for b in range(1, 9) for c in "ABCD")  # "1A".."8D"


def _tone_record(rec: dict) -> dict:
    """Coerce any partial/legacy tone dict to the full v2 record shape."""
    return {
        "path": rec.get("path"),
        "content_hash": rec.get("content_hash"),
        "doc": rec.get("doc"),
        "source": rec.get("source") or "authored",
        "slot": rec.get("slot"),
        "device": rec.get("device"),
    }


def _setlist_record(v) -> dict:
    """Coerce a setlist value (v1 list OR v2 {tones,synced}) to v2 shape."""
    if isinstance(v, dict):
        return {"tones": list(v.get("tones") or []), "synced": bool(v.get("synced"))}
    return {"tones": list(v or []), "synced": False}
```

Rewrite `load()` to branch on version and migrate:

```python
@classmethod
def load(cls, path: Optional[Path] = None) -> "SetlistManifest":
    path = Path(path) if path is not None else default_setlists_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = None

    if isinstance(data, dict) and data.get("version") == MANIFEST_VERSION:
        return cls(
            path,
            tones={k: _tone_record(v) for k, v in (data.get("tones") or {}).items()},
            setlists={k: _setlist_record(v) for k, v in (data.get("setlists") or {}).items()},
            observed=cls._coerce_observed(data.get("observed")),
        )
    if isinstance(data, dict) and data.get("version") == 1:
        return cls._migrate_v1(path, data)
    # No usable manifest — try one-time migration from the old standalone ledger.
    manifest = cls(path)
    manifest._migrate_from_ledger()  # keep: still folds a legacy device-slots.json
    manifest._promote_ledger_entries()
    return manifest
```

Add the v1→v2 migration (lift ledger `slot_label` + observed into records, setlists→{tones,synced}):

```python
@classmethod
def _migrate_v1(cls, path: Path, data: dict) -> "SetlistManifest":
    observed = cls._coerce_observed(data.get("observed"))
    entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    slot_by_name = {}
    for e in entries:
        if isinstance(e, dict) and e.get("name") and e.get("slot_label"):
            slot_by_name[e["name"]] = e["slot_label"]

    tones = {}
    for name, rec in (data.get("tones") or {}).items():
        r = _tone_record(rec)
        r["slot"] = slot_by_name.get(name)
        dev = observed.get("pool", {}).get(name)
        r["device"] = dev if isinstance(dev, dict) else None
        tones[name] = r

    setlists = {}
    for name, v in (data.get("setlists") or {}).items():
        rec = _setlist_record(v)
        # user is always device-backed; others synced iff observed on device.
        rec["synced"] = name == "user" or name in (observed.get("setlists") or {})
        setlists[name] = rec

    return cls(path, tones=tones, setlists=setlists, observed=observed)
```

Add `_promote_ledger_entries()` (covers the no-manifest legacy path — after `_migrate_from_ledger` populated v1-style state, lift into v2 records):

```python
def _promote_ledger_entries(self) -> None:
    """After a legacy device-slots.json fold, lift slot + synced into v2 shape."""
    for name, rec in self.tones.items():
        self.tones[name] = _tone_record(rec)
        dev = self.observed.get("pool", {}).get(name)
        if isinstance(dev, dict):
            self.tones[name]["device"] = dev
            self.tones[name]["slot"] = self.tones[name]["slot"] or _posi_to_slot(dev.get("posi"))
    self.setlists_map = {k: _setlist_record(v) for k, v in self.setlists_map.items()}
    for name, rec in self.setlists_map.items():
        rec["synced"] = name == "user" or name in (self.observed.get("setlists") or {})
```

Add a `posi → slot label` helper (used by promotion + sync later):

```python
def _posi_to_slot(posi) -> Optional[str]:
    if not isinstance(posi, int) or not (0 <= posi < len(_SLOT_LABELS)):
        return None
    return _SLOT_LABELS[posi]
```

Update `to_dict()` (unchanged keys, but now v2 values) — it already emits `tones`/`setlists`/`observed`; ensure it does NOT special-case entries. Update `save()` to **remove** the `entries`-preservation block entirely:

```python
def save(self) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    doc = self.to_dict()
    tmp = self.path.with_name(self.path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    os.replace(tmp, self.path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_manifest_v2.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/device/manifest.py tests/test_manifest_v2.py
git commit -m "feat(manifest): schema v2 — tone slot+device records, setlists {tones,synced}, v1 migration"
```

---

## Task 2: Manifest tone/setlist mutators (register, on-device, unsync-cascade, synced flag)

**Files:**
- Modify: `src/helixgen/device/manifest.py`
- Test: `tests/test_manifest_ops.py` (create)

**Interfaces:**
- Consumes: v2 record shapes from Task 1; `read_hsp` (already imported in manifest.py), `_hash_file`.
- Produces:
  - `register_tone(hsp_path, *, source="authored", doc=None) -> str` — add to library, no setlist, `slot=None`.
  - `register_pathless(name, *, source) -> None` — library entry with `path/content_hash=None`.
  - `mark_on_device(name, slot="auto") -> None` — set `tones[name].slot`.
  - `unsync(name) -> List[str]` — set `slot=None`; remove `name` from every **synced** setlist; return the setlist names it was pulled from.
  - `set_setlist_synced(name, synced) -> None` — flip flag; turning on marks all members on-device (`mark_on_device(m, "auto")` where slot is None).
  - `add_to_setlist(setlist, name, *, pos=None)` / `remove_from_setlist(setlist, name)`.
  - `tones_in(setlist) -> List[str]`, `is_synced(setlist) -> bool`, `library() -> List[dict]`.

- [ ] **Step 1: Write the failing test** (`tests/test_manifest_ops.py`)

```python
import json
from pathlib import Path
import pytest
from helixgen.device.manifest import SetlistManifest, ManifestError


def _hsp(tmp_path, name):
    from helixgen.hsp import write_hsp  # existing writer
    p = tmp_path / f"{name}.hsp"
    write_hsp(p, {"meta": {"name": name}})
    return p


def test_register_tone_adds_offdevice(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    n = m.register_tone(_hsp(tmp_path, "Alpha"), source="authored")
    assert n == "Alpha"
    assert m.tones["Alpha"]["slot"] is None
    assert m.tones["Alpha"]["source"] == "authored"


def test_mark_on_device_and_unsync_cascade(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    m.register_tone(_hsp(tmp_path, "Alpha"))
    m.add_to_setlist("helixgen", "Alpha")
    m.set_setlist_synced("helixgen", True)
    assert m.tones["Alpha"]["slot"] == "auto"       # synced-on marked it on-device
    pulled = m.unsync("Alpha")
    assert m.tones["Alpha"]["slot"] is None
    assert "helixgen" in pulled                       # cascaded out of the synced setlist
    assert "Alpha" not in m.tones_in("helixgen")


def test_unsync_keeps_membership_in_unsynced_setlist(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    m.register_tone(_hsp(tmp_path, "Alpha"))
    m.mark_on_device("Alpha")
    m.add_to_setlist("draft", "Alpha")                # draft stays synced=False
    m.unsync("Alpha")
    assert "Alpha" in m.tones_in("draft")             # unsynced setlist untouched


def test_register_duplicate_name_different_path_rejected(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    m.register_tone(_hsp(tmp_path, "Alpha"))
    other = tmp_path / "sub"; other.mkdir()
    from helixgen.hsp import write_hsp
    p2 = other / "Alpha.hsp"; write_hsp(p2, {"meta": {"name": "Alpha"}})
    with pytest.raises(ManifestError):
        m.register_tone(p2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_manifest_ops.py -v`
Expected: FAIL (`register_tone` / `mark_on_device` / `set_setlist_synced` / `unsync` undefined).

- [ ] **Step 3: Implement the mutators in `manifest.py`**

```python
def register_tone(self, hsp_path, *, source: str = "authored", doc=None) -> str:
    p = Path(hsp_path).resolve()
    body = read_hsp(p)
    name = (body.get("meta") or {}).get("name") or p.stem
    abs_path = str(p)
    existing = self.tones.get(name)
    if existing is not None and existing.get("path") not in (None, abs_path):
        raise ManifestError(
            f"tone name {name!r} already registered to a different file "
            f"({existing.get('path')!r}); rename one before registering")
    self.tones[name] = _tone_record({
        "path": abs_path, "content_hash": _hash_file(p),
        "doc": str(doc) if doc else None, "source": source,
        "slot": existing.get("slot") if existing else None,
        "device": existing.get("device") if existing else None,
    })
    return name

def register_pathless(self, name: str, *, source: str) -> None:
    if source not in ("save", "create"):
        raise ManifestError(f"pathless source must be save|create, got {source!r}")
    self.tones[name] = _tone_record({"path": None, "content_hash": None,
                                     "source": source})

def mark_on_device(self, name: str, slot: str = "auto") -> None:
    if name not in self.tones:
        raise ManifestError(f"unknown tone {name!r}")
    if slot != "auto" and slot not in _SLOT_LABELS:
        raise ManifestError(f"invalid slot {slot!r}")
    self.tones[name]["slot"] = slot

def unsync(self, name: str) -> List[str]:
    if name not in self.tones:
        raise ManifestError(f"unknown tone {name!r}")
    self.tones[name]["slot"] = None
    pulled = []
    for sl, rec in self.setlists_map.items():
        if rec.get("synced") and name in rec["tones"]:
            rec["tones"].remove(name)
            pulled.append(sl)
    return pulled

def set_setlist_synced(self, setlist: str, synced: bool) -> None:
    rec = self.setlists_map.setdefault(setlist, {"tones": [], "synced": False})
    rec["synced"] = bool(synced)
    if synced:
        for name in rec["tones"]:
            if self.tones.get(name, {}).get("slot") is None:
                self.mark_on_device(name, "auto")

def add_to_setlist(self, setlist: str, name: str, *, pos=None) -> None:
    if name not in self.tones:
        raise ManifestError(f"unknown tone {name!r}")
    rec = self.setlists_map.setdefault(setlist, {"tones": [], "synced": False})
    if name in rec["tones"]:
        return
    if pos is None:
        rec["tones"].append(name)
    else:
        rec["tones"].insert(pos, name)
    if rec["synced"] and self.tones[name]["slot"] is None:
        self.mark_on_device(name, "auto")

def remove_from_setlist(self, setlist: str, name: str) -> bool:
    rec = self.setlists_map.get(setlist)
    if not rec or name not in rec["tones"]:
        return False
    rec["tones"].remove(name)
    return True

def tones_in(self, setlist: str) -> List[str]:
    return list(self.setlists_map.get(setlist, {}).get("tones", []))

def is_synced(self, setlist: str) -> bool:
    return bool(self.setlists_map.get(setlist, {}).get("synced"))

def library(self) -> List[dict]:
    out = []
    for name, rec in self.tones.items():
        out.append({
            "name": name, "slot": rec.get("slot"),
            "on_device": rec.get("slot") is not None, "source": rec.get("source"),
            "setlists": [sl for sl, r in self.setlists_map.items() if name in r["tones"]],
        })
    return out
```

Note: the existing `add_tone`, `tones_in`, `union_tones`, `remove_tone`, `record_observed_pool` reference `setlists_map[x]` as a list. Update those to use the `{tones, synced}` shape (e.g. `self.setlists_map[setlist]["tones"]`). Search the file for `setlists_map[` and `.setlists_map.get` and fix each call site; keep `add_tone` working (it can delegate to `register_tone` + `add_to_setlist`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_manifest_ops.py tests/test_manifest_v2.py -v`
Expected: PASS. Then run the full manifest test module to catch call-site regressions:
Run: `PYTHONPATH=$PWD/src python -m pytest tests/ -k manifest -v`
Expected: PASS (fix any list-vs-dict call sites surfaced).

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/device/manifest.py tests/test_manifest_ops.py
git commit -m "feat(manifest): tone mutators — register, mark_on_device, unsync-cascade, synced flag"
```

---

## Task 3: Retire `SlotLedger`; re-home `cli.py` callers onto the manifest

**Files:**
- Delete: `src/helixgen/device/ledger.py`
- Modify: `src/helixgen/cli.py` (`_ledger_record`/`_ledger_rename`/`_ledger_remove` ~639-685; `device slots` group 1311-1500), `src/helixgen/device/manifest.py` (drop `_migrate_from_ledger`/`_promote_ledger_entries` legacy calls if the ledger module is gone — keep a self-contained legacy `device-slots.json` reader inline).
- Test: `tests/test_cli_slots.py` (create/modify), plus delete `tests/test_ledger*.py`.

**Interfaces:**
- Consumes: manifest ops from Task 2.
- Produces: `device slots list [--verify]` reads the manifest library view; `_ledger_record/rename/remove` become manifest-backed helpers `_manifest_record_placement(...)` / `_manifest_rename(...)` / `_manifest_remove(...)`.

- [ ] **Step 1: Write the failing test** (`tests/test_cli_slots.py`)

```python
from click.testing import CliRunner
from helixgen.cli import cli


def test_slots_list_reads_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "s.json"))
    from helixgen.device.manifest import SetlistManifest
    from helixgen.hsp import write_hsp
    hp = tmp_path / "Alpha.hsp"; write_hsp(hp, {"meta": {"name": "Alpha"}})
    m = SetlistManifest.load()
    m.register_tone(hp); m.mark_on_device("Alpha", "5A"); m.save()

    r = CliRunner().invoke(cli, ["device", "slots", "list"])
    assert r.exit_code == 0
    assert "Alpha" in r.output
    assert "5A" in r.output


def test_no_ledger_module():
    import importlib
    try:
        importlib.import_module("helixgen.device.ledger")
        assert False, "ledger module should be deleted"
    except ModuleNotFoundError:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_cli_slots.py -v`
Expected: FAIL (`ledger` still importable; `slots list` still reads the ledger).

- [ ] **Step 3: Delete `ledger.py` and rewire callers**

```bash
git rm src/helixgen/device/ledger.py
git rm -f tests/test_ledger.py 2>/dev/null || true
```

In `manifest.py`, replace `_migrate_from_ledger` (which imported `default_ledger_path` from the deleted module) with a self-contained inline legacy reader:

```python
def _legacy_ledger_path() -> Path:
    override = os.environ.get("HELIXGEN_DEVICE_SLOTS")
    return Path(override).expanduser() if override else Path.home() / ".helixgen" / "device-slots.json"
```

and point `_migrate_from_ledger` at `_legacy_ledger_path()` (body otherwise unchanged from Task-1 state). Remove any `from .ledger import ...`.

In `cli.py`, replace the three `_ledger_*` helpers with manifest-backed versions:

```python
def _manifest_record_placement(name, *, slot, source, path=None, cid=None, posi=None):
    from helixgen.device.manifest import SetlistManifest
    m = SetlistManifest.load()
    if name not in m.tones:
        if path:
            m.register_tone(path, source=source)
        else:
            m.register_pathless(name, source=source)
    m.mark_on_device(name, slot or "auto")
    if cid is not None:
        m.tones[name]["device"] = {"cid": cid, "posi": posi}
    m.save()

def _manifest_rename(old, new):
    from helixgen.device.manifest import SetlistManifest
    m = SetlistManifest.load()
    if old in m.tones:
        m.tones[new] = m.tones.pop(old)
        for rec in m.setlists_map.values():
            rec["tones"] = [new if t == old else t for t in rec["tones"]]
        m.save()

def _manifest_remove(name):
    from helixgen.device.manifest import SetlistManifest
    m = SetlistManifest.load()
    if name in m.tones:
        m.tones.pop(name)
        for rec in m.setlists_map.values():
            if name in rec["tones"]:
                rec["tones"].remove(name)
        m.save()
```

Update the call sites (`device save/push/create/rename/delete`) to call these by tone **name** (they currently pass cid/posi). For rename/delete, resolve the tone name from the manifest via the cid recorded in `tones[*].device`. Rewrite the `device slots list` command body to print `m.library()` rows (`slot_label  name  on/off  setlists`); keep `--verify` cross-checking against a live `client.list_presets(...)` and flagging drift; `restore`/`reorder`/`sync` subcommands: make `restore` call `mark_on_device` + a single-tone sync (Task 4), `reorder` edit `setlists_map[sl]["tones"]` order + save, and fold `slots sync` to delegate to `device sync` (Task 4).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_cli_slots.py -v`
Expected: PASS. Then: `PYTHONPATH=$PWD/src python -m pytest tests/ -k "slots or manifest or ledger" -v` — expect PASS with the ledger tests gone.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: delete SlotLedger; re-home device slots + placement recording onto the manifest"
```

---

## Task 4: Managed-set mirror sync (auto-slot, delete-on-unsync, untracked-safe)

**Files:**
- Modify: `src/helixgen/device/setlist_sync.py`
- Test: `tests/test_setlist_sync_mirror.py` (create)

**Interfaces:**
- Consumes: manifest v2 (`tones[*].slot/device`, `is_synced`, `tones_in`), the fake-client pattern already used in existing sync tests, `plan_pool`/`plan_references`.
- Produces:
  - `assign_slots(manifest, occupied: set) -> dict` — resolve every `slot == "auto"` tone to the first free label not in `occupied` and not held by an untracked preset; returns `{name: label}` and mutates the manifest records.
  - `plan_mirror(manifest, device_presets, managed_names) -> dict` — pure planner returning `{install: [...], repush: [...], delete: [...], skip: [...]}` where `delete` = managed presets on device whose manifest `slot is None`, and untracked presets (name ∉ manifest) are excluded from all buckets.
  - `sync_setlists(...)` extended to: (1) call `plan_mirror`, (2) assign auto slots around untracked-occupied slots, (3) execute install/repush/delete, (4) rebuild synced-setlist references in manifest order.

- [ ] **Step 1: Write the failing test** (`tests/test_setlist_sync_mirror.py`)

```python
from helixgen.device.manifest import SetlistManifest
from helixgen.device.setlist_sync import plan_mirror, assign_slots


def _mk(tmp_path, tones):
    m = SetlistManifest(tmp_path / "s.json")
    for name, slot in tones.items():
        m.tones[name] = {"path": f"/x/{name}.hsp", "content_hash": f"sha256:{name}",
                         "doc": None, "source": "authored", "slot": slot,
                         "device": None}
    return m


def test_plan_mirror_installs_updates_deletes_and_ignores_untracked(tmp_path):
    m = _mk(tmp_path, {"A": "5A", "B": "auto", "C": None})
    # device currently holds A (managed, unchanged hash), C (managed, slot cleared),
    # and X (untracked).
    device = [
        {"name": "A", "posi": 4, "cid": 10, "content_hash": "sha256:A"},
        {"name": "C", "posi": 6, "cid": 12, "content_hash": "sha256:C"},
        {"name": "X", "posi": 7, "cid": 99, "content_hash": "sha256:X"},
    ]
    managed = set(m.tones)
    plan = plan_mirror(m, device, managed)
    assert "B" in [p["name"] for p in plan["install"]]     # auto slot, not yet on device
    assert "C" in [p["name"] for p in plan["delete"]]       # slot cleared => delete
    assert "A" in [p["name"] for p in plan["skip"]]         # unchanged hash
    assert all(p["name"] != "X" for b in plan.values() for p in b)  # untracked untouched


def test_assign_slots_avoids_untracked_and_occupied(tmp_path):
    m = _mk(tmp_path, {"B": "auto"})
    occupied = {"1A", "1B"}          # untracked/occupied labels
    assigned = assign_slots(m, occupied)
    assert assigned["B"] == "1C"
    assert m.tones["B"]["slot"] == "1C"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_setlist_sync_mirror.py -v`
Expected: FAIL (`plan_mirror` / `assign_slots` undefined).

- [ ] **Step 3: Implement in `setlist_sync.py`**

```python
from .manifest import _SLOT_LABELS  # "1A".."8D"


def assign_slots(manifest, occupied):
    assigned = {}
    used = set(occupied)
    # also reserve concretely-slotted managed tones
    for rec in manifest.tones.values():
        s = rec.get("slot")
        if s and s != "auto":
            used.add(s)
    free = (lbl for lbl in _SLOT_LABELS if lbl not in used)
    for name, rec in manifest.tones.items():
        if rec.get("slot") == "auto":
            lbl = next(free)
            rec["slot"] = lbl
            assigned[name] = lbl
            used.add(lbl)
    return assigned


def plan_mirror(manifest, device_presets, managed_names):
    by_name = {p["name"]: p for p in device_presets}
    install, repush, delete, skip = [], [], [], []
    for name, rec in manifest.tones.items():
        slot = rec.get("slot")
        dev = by_name.get(name)
        if slot is None:
            if dev is not None:            # managed, on device, slot cleared => delete
                delete.append({"name": name, "cid": dev.get("cid")})
            continue
        if dev is None:
            install.append({"name": name, "slot": slot})
        elif dev.get("content_hash") != rec.get("content_hash"):
            repush.append({"name": name, "slot": slot, "cid": dev.get("cid")})
        else:
            skip.append({"name": name})
    # untracked device presets (name not managed) are excluded entirely.
    return {"install": install, "repush": repush, "delete": delete, "skip": skip}
```

Then extend `sync_setlists(...)`: before reference rebuild, compute `managed = set(manifest.tones)`, `occupied = {p["posi"]→label for untracked p}`, call `assign_slots`, `plan_mirror`, and execute install (transcode + push), repush (restore content), delete (`client` remove from pool). Record `tones[name]["device"]` from the fresh listing. Keep the existing never-orphan reference rebuild for `synced` setlists only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_setlist_sync_mirror.py tests/ -k sync -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/device/setlist_sync.py tests/test_setlist_sync_mirror.py
git commit -m "feat(sync): managed-set mirror — auto-slot assign, delete-on-unsync, untracked-safe"
```

---

## Task 5: CLI surfaces + auto-register on authoring

**Files:**
- Modify: `src/helixgen/cli.py` (add `register`, `device add`, `device unsync`, `setlist sync-on|sync-off`, `library list`; hook auto-register in `generate_cmd` ~99-104), `src/helixgen/recipe.py` (`generate_from_recipe` ~200 — return the written path so callers register).
- Test: `tests/test_cli_library.py` (create)

**Interfaces:**
- Consumes: Task 2 mutators, Task 4 sync.
- Produces: CLI verbs; `generate` writes the `.hsp` then calls `SetlistManifest.load().register_tone(out, source="authored"); .save()`.

- [ ] **Step 1: Write the failing test** (`tests/test_cli_library.py`)

```python
from click.testing import CliRunner
from helixgen.cli import cli


def test_generate_auto_registers(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "s.json"))
    recipe = tmp_path / "r.json"
    recipe.write_text('{"name": "Auto Reg Test", "paths": [{"blocks": []}]}')
    out = tmp_path / "out.hsp"
    r = CliRunner().invoke(cli, ["generate", str(recipe), "-o", str(out)])
    assert r.exit_code == 0, r.output
    from helixgen.device.manifest import SetlistManifest
    m = SetlistManifest.load()
    assert "Auto Reg Test" in m.tones
    assert m.tones["Auto Reg Test"]["slot"] is None   # off-device by default


def test_device_add_and_unsync(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "s.json"))
    from helixgen.device.manifest import SetlistManifest
    from helixgen.hsp import write_hsp
    hp = tmp_path / "Alpha.hsp"; write_hsp(hp, {"meta": {"name": "Alpha"}})
    m = SetlistManifest.load(); m.register_tone(hp); m.save()
    assert CliRunner().invoke(cli, ["device", "add", "Alpha"]).exit_code == 0
    assert SetlistManifest.load().tones["Alpha"]["slot"] == "auto"
    assert CliRunner().invoke(cli, ["device", "unsync", "Alpha"]).exit_code == 0
    assert SetlistManifest.load().tones["Alpha"]["slot"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_cli_library.py -v`
Expected: FAIL (auto-register + verbs missing).

- [ ] **Step 3: Implement**

In `generate_cmd`, after the `.hsp` is written to `out`, add (guard so a generate failure never blocks output):

```python
    try:
        from helixgen.device.manifest import SetlistManifest
        m = SetlistManifest.load()
        m.register_tone(out, source="authored")
        m.save()
    except Exception as e:  # noqa: BLE001 — registration is advisory
        click.echo(f"warning: could not register tone in library: {e}", err=True)
```

Add the new commands (register under top-level `cli`, add/unsync under `device`, sync-on/off under a `setlist` group, `library list`):

```python
@cli.command(name="register")
@click.argument("hsp_path", type=click.Path(exists=True))
@click.option("--doc", type=click.Path(exists=True), default=None)
def register_cmd(hsp_path, doc):
    """Register an existing local .hsp into the tone library."""
    from helixgen.device.manifest import SetlistManifest
    m = SetlistManifest.load()
    name = m.register_tone(hsp_path, source="import-local", doc=doc)
    m.save()
    click.echo(f"registered {name!r}")

@device.command(name="add")
@click.argument("tone")
@click.option("--slot", default="auto")
def device_add_cmd(tone, slot):
    """Mark a library tone for the device (default slot: auto)."""
    from helixgen.device.manifest import SetlistManifest
    m = SetlistManifest.load(); m.mark_on_device(tone, slot); m.save()
    click.echo(f"{tone!r} -> device slot {slot}")

@device.command(name="unsync")
@click.argument("tone")
def device_unsync_cmd(tone):
    """Take a tone off the device on next sync (keeps it in the library)."""
    from helixgen.device.manifest import SetlistManifest
    m = SetlistManifest.load(); pulled = m.unsync(tone); m.save()
    msg = f"{tone!r} unsynced"
    if pulled:
        msg += f"; removed from synced setlists: {', '.join(pulled)}"
    click.echo(msg)

@device.command(name="library")
def device_library_cmd():
    """List every tone: slot, on/off device, setlist memberships."""
    from helixgen.device.manifest import SetlistManifest
    for row in SetlistManifest.load().library():
        flag = row["slot"] or "-"
        click.echo(f"{flag:<5} {row['name']}  [{', '.join(row['setlists'])}]")
```

Add `setlist sync-on` / `sync-off` to the existing setlist group (calls `set_setlist_synced`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_cli_library.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/cli.py src/helixgen/recipe.py tests/test_cli_library.py
git commit -m "feat(cli): register / device add|unsync / setlist sync-on|off / library; auto-register on generate"
```

---

## Task 6: MCP parity + auto-register in `generate_preset_handler`

**Files:**
- Modify: `mcp_server/tools.py` (`generate_preset_handler` ~99; add `register_tone`, `device_add`, `device_unsync` tools; `device_install_preset_handler` ~594 to record the manifest).
- Test: `tests/test_mcp_library.py` (create)

**Interfaces:**
- Consumes: Task 2 mutators.
- Produces: `generate_preset_handler` auto-registers; new MCP tools `register_tone_handler(model, hsp_path, doc=None)`, `device_add_handler(model, tone, slot="auto")`, `device_unsync_handler(model, tone)`; `device_install_preset_handler` records placement + observed device after a successful push.

- [ ] **Step 1: Write the failing test** (`tests/test_mcp_library.py`)

```python
def test_generate_handler_auto_registers(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "s.json"))
    from mcp_server.tools import generate_preset_handler
    out = tmp_path / "o.hsp"
    generate_preset_handler("Stadium XL",
        recipe={"name": "MCP Reg", "paths": [{"blocks": []}]}, out_path=str(out))
    from helixgen.device.manifest import SetlistManifest
    assert "MCP Reg" in SetlistManifest.load().tones
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_mcp_library.py -v`
Expected: FAIL (handler does not register).

- [ ] **Step 3: Implement**

In `generate_preset_handler`, after writing `out_path`, add the same advisory-register block as Task 5 (load manifest, `register_tone(out_path, source="authored")`, save; swallow errors into the returned `warnings`). Add the three new handler functions mirroring the CLI verbs and register them in the tool table (follow the existing `device_*` registration pattern in the file). In `device_install_preset_handler`, after a successful `push_to_slot`, call `SetlistManifest.load()`, `register_tone`/`mark_on_device` for the slot label from `pos`, set `tones[name]["device"] = {"cid": cid, "posi": pos}`, and save.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_mcp_library.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server/tools.py tests/test_mcp_library.py
git commit -m "feat(mcp): auto-register on generate; register/device add/unsync tools; install records manifest"
```

---

## Task 7: Full suite green, docs, version bump, ship

**Files:**
- Modify: `CLAUDE.md` (device-slots + new verbs section), `docs/BACKLOG.md` (mark #6/#7 resolved, note device-import fast-follow), `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `pyproject.toml`, `src/helixgen/__init__.py`, `.claude/skills/device/SKILL.md` (tone-library model + new verbs).

- [ ] **Step 1: Run the entire suite**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS (all green). Fix any acceptance/golden fallout from the manifest change.

- [ ] **Step 2: Update docs**

Update `CLAUDE.md` `device slots` section to describe the tone-library manifest (tone = content+identity+management state; `slot` = on-device flag; `register` / `device add|unsync` / `setlist sync-on|off` / `library`). Update `.claude/skills/device/SKILL.md` to drive the new verbs. In `docs/BACKLOG.md`, mark #6 (single-tone parity) and #7 (reordering) resolved/reframed and note device-import as the fast-follow spec.

- [ ] **Step 3: Bump version**

Determine the next version (fetch tags first, pick the next minor):

```bash
git fetch github --tags
git tag --list 'helixgen--v*' | sort -V | tail -3
```

Set the SAME new version in `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` (they must match or CI fails), and bump the lib line in `pyproject.toml` + `src/helixgen/__init__.py`.

- [ ] **Step 4: Commit + PR + merge**

```bash
git add -A
git commit -m "release X.Y.Z: tone-library model — retire ledger, managed-set mirror sync"
git push -u github tone-library-model-redesign
gh pr create --title "Tone-library model redesign — retire ledger, managed-set mirror sync" \
  --body "Implements docs/superpowers/specs/2026-07-13-tone-library-model-redesign.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Merge to `main` after review; the release workflow auto-tags and fast-forwards `stable`. Do NOT move `stable`/tags by hand.

- [ ] **Step 5: Verify release fired**

```bash
git fetch github --tags && git tag --list 'helixgen--v*' | sort -V | tail -1
```

Expected: the new `helixgen--vX.Y.Z` tag exists.

---

## Self-Review notes

- **Spec coverage:** §3 model → Task 1–2; §4 sync → Task 4; §5 surfaces → Task 5–6; §6 migration → Task 1; §7 retire ledger → Task 3; §8 testing → every task + Task 7; §9 device-import → explicitly deferred. Covered.
- **Type consistency:** `setlists_map[x]` is `{"tones": List[str], "synced": bool}` everywhere after Task 1; `slot ∈ {label, "auto", None}`; `plan_mirror` buckets `install/repush/delete/skip`. Consistent across tasks.
- **Hardware validation** (device writes) is gated by the auto-mode classifier; a real device sync must run via user `!` or a granted Bash rule — flag to the user before claiming end-to-end device validation.
