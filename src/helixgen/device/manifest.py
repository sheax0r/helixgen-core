"""Tone-library manifest — the tone as a first-class managed entity.

One file, ``~/.helixgen/setlists.json`` (override ``$HELIXGEN_SETLISTS``). It is
PURE local-file logic: no device, no network, no msgpack/zmq — importable
without the ``device`` extra.

A **tone** is content + identity + management state (design
``2026-07-13-tone-library-model-redesign.md``):

* **content** — its ``.hsp`` (``path`` + ``content_hash``) and optional ``doc``;
  ``path``/``content_hash`` are ``null`` for a pathless (device-origin) tone.
* **identity** — the ``tones`` key (name), unique in the manifest, also the
  device preset key.
* **management state** — ``source`` (provenance), ``slot`` (desired on-device
  address; ``null`` = off device, ``"auto"`` = wants device, address TBD, or a
  concrete ``"1A".."128D"``), and ``device`` (observed cid/posi, rebuilt on sync).

Setlists are ordered membership plus a ``synced`` flag (mirrored to the device
or a local-only draft). ``"On the device"`` ⟺ ``slot != null``.

On-disk shape (version 2)::

    {"version": 2,
     "tones": {"<name>": {"path": <abs .hsp | null>,
                          "content_hash": "sha256:…" | null,
                          "doc": <abs .md | null>,
                          "source": "authored"|"import-local"|"import-device"|"save"|"create",
                          "slot": "1A".."128D" | "auto" | null,
                          "device": {"cid": int, "posi": int} | null}},
     "setlists": {"<setlist>": {"tones": ["<tone name>", …], "synced": bool}}}

The manifest is **never hand-edited** — it is written by the authoring/sync
surfaces (CLI / MCP). A version-1 document (list-valued setlists + a folded
``entries`` slot-ledger section) is migrated to version 2 on load.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from helixgen.hsp import read_hsp

MANIFEST_VERSION = 2

# provenance tags accepted on a tone record
_VALID_SOURCES = ("authored", "import-local", "import-device", "save", "create",
                  "hsp", "push")  # last two are legacy synonyms kept for migration
_PATHLESS_SOURCES = ("save", "create")

# Every user-setlist slot label, in device posi order: "1A".."128D".
# The device labels a posi as ``f"{posi//4 + 1}{'ABCD'[posi%4]}"`` (uncapped —
# see ``client.slot_label``). A Helix setlist holds up to 128 banks of 4 (the
# Stadium XL's full user bank goes to 128D = 512 slots); base models simply fill
# fewer. Sizing to the max keeps slot validation + auto-assign from imposing an
# artificial "device full" ceiling — the hardware is the real capacity check.
_SLOT_BANKS = 128
_SLOT_LABELS = tuple(f"{b}{c}" for b in range(1, _SLOT_BANKS + 1) for c in "ABCD")


class ManifestError(Exception):
    """A manifest operation was rejected (e.g. a unique-name collision)."""


def default_setlists_path() -> Path:
    """Where the tone-library manifest lives: ``~/.helixgen/setlists.json``.

    Overridable wholesale with ``$HELIXGEN_SETLISTS``.
    """
    override = os.environ.get("HELIXGEN_SETLISTS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".helixgen" / "setlists.json"


def _legacy_ledger_path() -> Path:
    """The retired standalone slot-ledger file (read once for migration only)."""
    override = os.environ.get("HELIXGEN_DEVICE_SLOTS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".helixgen" / "device-slots.json"


def _hash_file(path: Path) -> str:
    """Return the ``sha256:<hex>`` content hash of a file's raw bytes."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _empty_observed() -> Dict[str, Any]:
    return {"pool": {}, "setlists": {}}


def _posi_to_slot(posi: Any) -> Optional[str]:
    """Map a device 0-based posi to its user-setlist slot label ("1A".."128D")."""
    if not isinstance(posi, int) or not (0 <= posi < len(_SLOT_LABELS)):
        return None
    return _SLOT_LABELS[posi]


def _tone_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce any partial/legacy tone dict to the full v2 record shape."""
    return {
        "path": rec.get("path"),
        "content_hash": rec.get("content_hash"),
        "doc": rec.get("doc"),
        "source": rec.get("source") or "authored",
        "slot": rec.get("slot"),
        "device": rec.get("device"),
    }


def _setlist_record(v: Any) -> Dict[str, Any]:
    """Coerce a setlist value (v1 list OR v2 {tones,synced}) to v2 shape."""
    if isinstance(v, dict):
        return {"tones": list(v.get("tones") or []), "synced": bool(v.get("synced"))}
    return {"tones": list(v or []), "synced": False}


class SetlistManifest:
    """The tone library: tone registry (with placement) + ordered setlists."""

    def __init__(
        self,
        path: Path,
        *,
        tones: Optional[Dict[str, Any]] = None,
        setlists: Optional[Dict[str, Any]] = None,
        observed: Optional[Dict[str, Any]] = None,
    ):
        self.path = Path(path)
        self.tones: Dict[str, Any] = tones if tones is not None else {}
        self.setlists_map: Dict[str, Dict[str, Any]] = setlists if setlists is not None else {}
        self.observed: Dict[str, Any] = observed if observed is not None else _empty_observed()

    # -- construction ---------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SetlistManifest":
        """Load the manifest, migrating a v1 document (or a legacy ledger) to v2."""
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

        # No usable manifest — try a one-time migration from the standalone ledger.
        manifest = cls(path)
        manifest._migrate_from_ledger()
        return manifest

    @staticmethod
    def _read_json(path: Path) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
            return None
        return data

    @staticmethod
    def _coerce_observed(raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return _empty_observed()
        return {
            "pool": raw.get("pool") if isinstance(raw.get("pool"), dict) else {},
            "setlists": raw.get("setlists") if isinstance(raw.get("setlists"), dict) else {},
        }

    @classmethod
    def _migrate_v1(cls, path: Path, data: Dict[str, Any]) -> "SetlistManifest":
        """Upgrade a version-1 document (list setlists + folded ledger) to v2.

        Each ledger ``entries`` row contributes a tone's ``slot`` (its
        ``slot_label``); ``observed.pool`` contributes ``device``; list-valued
        setlists become ``{tones, synced}`` (``user`` and any observed-on-device
        setlist are ``synced``).
        """
        observed = cls._coerce_observed(data.get("observed"))
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        slot_by_name: Dict[str, str] = {}
        for e in entries:
            if isinstance(e, dict) and e.get("name"):
                lbl = e.get("slot_label") or _posi_to_slot(e.get("posi"))
                if lbl:
                    slot_by_name[e["name"]] = lbl

        tones: Dict[str, Any] = {}
        for name, rec in (data.get("tones") or {}).items():
            r = _tone_record(rec)
            r["slot"] = slot_by_name.get(name) or r["slot"]
            dev = observed.get("pool", {}).get(name)
            r["device"] = dev if isinstance(dev, dict) else None
            if r["slot"] is None and isinstance(dev, dict):
                r["slot"] = _posi_to_slot(dev.get("posi"))
            tones[name] = r

        setlists: Dict[str, Any] = {}
        for name, v in (data.get("setlists") or {}).items():
            rec = _setlist_record(v)
            rec["synced"] = name == "user" or name in (observed.get("setlists") or {})
            setlists[name] = rec

        return cls(path, tones=tones, setlists=setlists, observed=observed)

    def _migrate_from_ledger(self) -> None:
        """Fold a legacy ``device-slots.json`` into this (empty) v2 manifest."""
        try:
            data = json.loads(_legacy_ledger_path().read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            return
        entries = [e for e in data["entries"] if isinstance(e, dict)]
        entries.sort(key=lambda e: (e.get("setlist") or "", e.get("posi") or 0))
        for e in entries:
            name = e.get("name")
            setlist = e.get("setlist")
            if not name or not setlist:
                continue
            source = e.get("source_kind") or "authored"
            source_path = e.get("source_path")
            posi = e.get("posi")
            cid = e.get("cid")
            content_hash: Optional[str] = None
            if source_path and Path(source_path).exists():
                content_hash = _hash_file(Path(source_path))
            self.tones[name] = _tone_record({
                "path": source_path,
                "content_hash": content_hash,
                "source": source,
                "slot": e.get("slot_label") or _posi_to_slot(posi),
                "device": {"cid": cid, "posi": posi},
            })
            rec = self.setlists_map.setdefault(setlist, {"tones": [], "synced": True})
            if name not in rec["tones"]:
                rec["tones"].append(name)
            self.observed["pool"][name] = {"cid": cid, "posi": posi}
            sl = self.observed["setlists"].setdefault(setlist, {"cid": None, "refs": {}})
            sl["refs"][name] = {"ref_cid": None, "posi": posi}

    # -- library: register tones ---------------------------------------------

    def register_tone(self, hsp_path: Path | str, *, source: str = "authored",
                      doc: Optional[Path | str] = None) -> str:
        """Register a local ``.hsp`` into the library (no setlist, off-device).

        Reads ``meta.name`` (falling back to the filename stem) as the tone name.
        Preserves an existing tone's ``slot``/``device`` if it was already known.
        Raises :class:`ManifestError` on a name collision with a different path.
        """
        p = Path(hsp_path).resolve()
        body = read_hsp(p)
        name = (body.get("meta") or {}).get("name") or p.stem
        abs_path = str(p)
        existing = self.tones.get(name)
        if existing is not None and existing.get("path") not in (None, abs_path):
            raise ManifestError(
                f"tone name {name!r} is already registered to a different path "
                f"({existing.get('path')!r}); names must be unique in the manifest"
            )
        self.tones[name] = _tone_record({
            "path": abs_path,
            "content_hash": _hash_file(p),
            "doc": str(Path(doc).resolve()) if doc else (existing or {}).get("doc"),
            "source": source,
            "slot": (existing or {}).get("slot"),
            "device": (existing or {}).get("device"),
        })
        return name

    def register_pathless(self, name: str, *, source: str) -> str:
        """Register a source-less tone (device edit-buffer ``save`` / ``create``)."""
        if source not in _PATHLESS_SOURCES:
            raise ManifestError(
                f"pathless source must be one of {_PATHLESS_SOURCES}, got {source!r}")
        existing = self.tones.get(name)
        if existing is not None and existing.get("path") is not None:
            raise ManifestError(
                f"tone name {name!r} is already registered with a path; "
                f"names must be unique in the manifest")
        self.tones[name] = _tone_record({
            "path": None, "content_hash": None, "source": source,
            "slot": (existing or {}).get("slot"),
            "device": (existing or {}).get("device"),
        })
        return name

    def add_tone(self, setlist: str, hsp_path: Path | str, *,
                 pos: Optional[int] = None) -> str:
        """Register a ``.hsp`` and add it to ``setlist``'s membership (legacy API)."""
        name = self.register_tone(hsp_path, source="import-local")
        self.add_to_setlist(setlist, name, pos=pos)
        return name

    def pathless_add(self, name: str, source: str, *,
                     setlist: Optional[str] = None, pos: Optional[int] = None) -> str:
        """Register a pathless tone and optionally add it to a setlist (legacy API)."""
        self.register_pathless(name, source=source)
        if setlist is not None:
            self.add_to_setlist(setlist, name, pos=pos)
        return name

    # -- desired placement (slots + setlists) --------------------------------

    def mark_on_device(self, name: str, slot: str = "auto") -> None:
        """Mark a library tone for the device (``slot='auto'`` = address TBD)."""
        if name not in self.tones:
            raise ManifestError(f"unknown tone {name!r}")
        if slot != "auto" and slot not in _SLOT_LABELS:
            raise ManifestError(f"invalid slot {slot!r} (expected '1A'..'128D' or 'auto')")
        self.tones[name]["slot"] = slot

    def unsync(self, name: str) -> List[str]:
        """Take ``name`` off the device (``slot=None``) and cascade it out of every
        **synced** setlist. Keeps the tone in the library. Returns the synced
        setlists it was pulled from."""
        if name not in self.tones:
            raise ManifestError(f"unknown tone {name!r}")
        self.tones[name]["slot"] = None
        pulled: List[str] = []
        for sl, rec in self.setlists_map.items():
            if rec.get("synced") and name in rec["tones"]:
                rec["tones"].remove(name)
                pulled.append(sl)
        return pulled

    def set_setlist_synced(self, setlist: str, synced: bool) -> None:
        """Flip a setlist's ``synced`` flag. Turning it on marks every member for
        the device (``slot='auto'`` where a member has no slot yet)."""
        rec = self.setlists_map.setdefault(setlist, {"tones": [], "synced": False})
        rec["synced"] = bool(synced)
        if synced:
            for name in rec["tones"]:
                if self.tones.get(name, {}).get("slot") is None:
                    self.mark_on_device(name, "auto")

    def add_to_setlist(self, setlist: str, name: str, *, pos: Optional[int] = None) -> None:
        """Add ``name`` to ``setlist`` at ``pos`` (append if None; no duplicates).
        If the setlist is synced, the tone is marked on-device."""
        if name not in self.tones:
            raise ManifestError(f"unknown tone {name!r}")
        rec = self.setlists_map.setdefault(setlist, {"tones": [], "synced": False})
        if name not in rec["tones"]:
            if pos is None:
                rec["tones"].append(name)
            else:
                rec["tones"].insert(pos, name)
        if rec["synced"] and self.tones[name].get("slot") is None:
            self.mark_on_device(name, "auto")

    def remove_from_setlist(self, setlist: str, name: str) -> bool:
        """Drop ``name`` from ``setlist``'s membership. Returns whether it was there."""
        rec = self.setlists_map.get(setlist)
        if not rec or name not in rec["tones"]:
            return False
        rec["tones"].remove(name)
        return True

    def remove_tone(self, setlist: str, name: str) -> bool:
        """Drop ``name`` from ``setlist``; GC the registry entry if now unreferenced
        (legacy API — membership removal, not a device delete)."""
        if not self.remove_from_setlist(setlist, name):
            return False
        if not self._is_referenced(name):
            self.tones.pop(name, None)
        return True

    def delete_tone(self, name: str) -> None:
        """Remove a tone from the library entirely (registry + all memberships)."""
        self.tones.pop(name, None)
        for rec in self.setlists_map.values():
            if name in rec["tones"]:
                rec["tones"].remove(name)

    def _is_referenced(self, name: str) -> bool:
        return any(name in rec["tones"] for rec in self.setlists_map.values())

    def create_setlist(self, name: str) -> None:
        """Create an empty setlist (idempotent — never wipes existing membership)."""
        self.setlists_map.setdefault(name, {"tones": [], "synced": False})

    # -- desired: read accessors ---------------------------------------------

    def setlists(self) -> List[str]:
        """The manifest's setlist names, in insertion order."""
        return list(self.setlists_map.keys())

    def tones_in(self, setlist: str) -> List[str]:
        """Ordered tone names in ``setlist`` (empty list if unknown)."""
        rec = self.setlists_map.get(setlist)
        return list(rec["tones"]) if rec else []

    def is_synced(self, setlist: str) -> bool:
        """Whether ``setlist`` is mirrored to the device."""
        rec = self.setlists_map.get(setlist)
        return bool(rec and rec.get("synced"))

    def tone_path(self, name: str) -> Optional[str]:
        entry = self.tones.get(name)
        return entry.get("path") if entry else None

    def content_hash(self, name: str) -> Optional[str]:
        entry = self.tones.get(name)
        return entry.get("content_hash") if entry else None

    def union_tones(self, setlists: List[str]) -> List[str]:
        """Ordered, de-duplicated union of tone names across ``setlists``."""
        seen: Dict[str, None] = {}
        for setlist in setlists:
            for name in self.tones_in(setlist):
                if name not in seen:
                    seen[name] = None
        return list(seen.keys())

    def library(self) -> List[Dict[str, Any]]:
        """A display view: every tone with slot, on/off-device, source, setlists."""
        out: List[Dict[str, Any]] = []
        for name, rec in self.tones.items():
            out.append({
                "name": name,
                "slot": rec.get("slot"),
                "on_device": rec.get("slot") is not None,
                "source": rec.get("source"),
                "setlists": [sl for sl, r in self.setlists_map.items()
                             if name in r["tones"]],
            })
        return out

    # -- observed (rebuilt each sync) ----------------------------------------

    def record_observed_pool(self, name: str, cid: int, posi: int,
                             *, synced_hash: Optional[str] = None) -> None:
        """Record a pool preset's observed device placement (name → cid + posi).

        Also mirrors the placement into the tone's ``device`` field (and, when the
        tone still wants the device, resolves an ``"auto"`` slot to the concrete
        label)."""
        entry: Dict[str, Any] = {"cid": cid, "posi": posi}
        if synced_hash is not None:
            entry["synced_hash"] = synced_hash
        self.observed["pool"][name] = entry
        tone = self.tones.get(name)
        if tone is not None:
            tone["device"] = {"cid": cid, "posi": posi}
            if tone.get("slot") == "auto":
                lbl = _posi_to_slot(posi)
                if lbl:
                    tone["slot"] = lbl

    def observed_pool_hash(self, name: str) -> Optional[str]:
        entry = self.observed.get("pool", {}).get(name)
        return entry.get("synced_hash") if isinstance(entry, dict) else None

    def record_observed_setlist(self, setlist: str, cid: int, refs: Dict[str, Any]) -> None:
        self.observed["setlists"][setlist] = {"cid": cid, "refs": dict(refs)}

    def clear_observed(self) -> None:
        self.observed = _empty_observed()

    # -- persistence ----------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """The full on-disk document (desired + observed)."""
        return {
            "version": MANIFEST_VERSION,
            "tones": self.tones,
            "setlists": self.setlists_map,
            "observed": self.observed,
        }

    def save(self) -> None:
        """Atomically write the manifest as pretty JSON (temp file + ``os.replace``)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2))
        os.replace(tmp, self.path)
