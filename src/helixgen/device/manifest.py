"""Local setlist manifest — desired setlist membership + observed device state.

One file, ``~/.helixgen/setlists.json`` (override ``$HELIXGEN_SETLISTS``), folds
the older ``device-slots.json`` slot ledger into a richer, multi-setlist model so
a single authored tone can live in many setlists. It is PURE local-file logic:
no device, no network, no msgpack/zmq — importable without the ``device`` extra.

Two clearly separated halves (per design §3):

* **desired** — a ``tones`` registry (``name → {path, content_hash, source}``,
  a tone appears once) plus ``setlists`` (``name → [tone-name, …]``, ordered
  membership == slot order).
* **observed** — ``pool`` + ``setlists`` placement rebuilt from a fresh device
  listing on every sync; never trusted as delete input, always cross-checked.

On-disk shape::

    {"version": 1,
     "tones": {"<name>": {"path": <abs .hsp | null>,
                          "content_hash": "sha256:…" | null,
                          "source": "hsp"|"save"|"create"|"push"}},
     "setlists": {"<setlist>": ["<tone name>", …]},
     "observed": {"pool": {"<name>": {"cid": int, "posi": int}},
                  "setlists": {"<setlist>": {"cid": int,
                      "refs": {"<name>": {"ref_cid": int, "posi": int}}}}}}

The manifest is **never hand-edited** — it is written by the authoring/sync
surfaces (CLI ``device setlist …`` / MCP). On first load, if the manifest file
is absent but the old slot ledger exists, the ledger's entries are migrated in
and the old file is left in place (read-once, never mutated).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from helixgen.hsp import read_hsp
from .ledger import default_ledger_path

MANIFEST_VERSION = 1

_VALID_SOURCES = ("hsp", "save", "create", "push")


class ManifestError(Exception):
    """A manifest operation was rejected (e.g. a unique-name collision)."""


def default_setlists_path() -> Path:
    """Where the setlist manifest lives: ``~/.helixgen/setlists.json``.

    Overridable wholesale with ``$HELIXGEN_SETLISTS`` — mirrors the ledger's
    ``$HELIXGEN_DEVICE_SLOTS`` and preferences' ``$HELIXGEN_PREFS`` conventions.
    """
    override = os.environ.get("HELIXGEN_SETLISTS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".helixgen" / "setlists.json"


def _hash_file(path: Path) -> str:
    """Return the ``sha256:<hex>`` content hash of a file's raw bytes."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _empty_observed() -> Dict[str, Any]:
    return {"pool": {}, "setlists": {}}


class SetlistManifest:
    """Desired setlist membership + observed device placement (one JSON file)."""

    def __init__(
        self,
        path: Path,
        *,
        tones: Optional[Dict[str, Any]] = None,
        setlists: Optional[Dict[str, List[str]]] = None,
        observed: Optional[Dict[str, Any]] = None,
    ):
        self.path = Path(path)
        self.tones: Dict[str, Any] = tones if tones is not None else {}
        self.setlists_map: Dict[str, List[str]] = setlists if setlists is not None else {}
        self.observed: Dict[str, Any] = observed if observed is not None else _empty_observed()

    # -- construction ---------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SetlistManifest":
        """Load the manifest, or start empty.

        If the manifest file is missing/corrupt/unknown-version and the old slot
        ledger (``~/.helixgen/device-slots.json`` or ``$HELIXGEN_DEVICE_SLOTS``)
        exists, the ledger's entries are migrated in. The old ledger file is read
        once and never mutated.
        """
        path = Path(path) if path is not None else default_setlists_path()
        data = cls._read_json(path)
        if data is not None:
            return cls(
                path,
                tones=data.get("tones") or {},
                setlists={k: list(v) for k, v in (data.get("setlists") or {}).items()},
                observed=cls._coerce_observed(data.get("observed")),
            )
        # No usable manifest — try a one-time migration from the slot ledger.
        manifest = cls(path)
        manifest._migrate_from_ledger()
        return manifest

    @staticmethod
    def _read_json(path: Path) -> Optional[Dict[str, Any]]:
        """Return the manifest dict if valid, else ``None`` (missing/corrupt/old)."""
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

    def _migrate_from_ledger(self) -> None:
        """Fold a legacy ``device-slots.json`` into this (empty) manifest.

        Each ledger entry ``{setlist, posi, name, cid, source_kind, source_path}``
        becomes a ``tones`` registry entry, a membership entry (appended in posi
        order), and an ``observed`` pool + setlist-reference placement. The old
        file is only read; a later real sync overwrites ``observed``.
        """
        ledger_path = default_ledger_path()
        try:
            data = json.loads(ledger_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            return

        entries = [e for e in data["entries"] if isinstance(e, dict)]
        # Membership follows posi order within each setlist.
        entries.sort(key=lambda e: (e.get("setlist") or "", e.get("posi") or 0))

        for e in entries:
            name = e.get("name")
            setlist = e.get("setlist")
            if not name or not setlist:
                continue
            source_kind = e.get("source_kind") or "hsp"
            source_path = e.get("source_path")
            posi = e.get("posi")
            cid = e.get("cid")

            content_hash: Optional[str] = None
            if source_kind == "hsp" and source_path:
                p = Path(source_path)
                if p.exists():
                    content_hash = _hash_file(p)

            self.tones[name] = {
                "path": source_path,
                "content_hash": content_hash,
                "source": source_kind,
            }
            self.setlists_map.setdefault(setlist, [])
            if name not in self.setlists_map[setlist]:
                self.setlists_map[setlist].append(name)

            self.observed["pool"][name] = {"cid": cid, "posi": posi}
            sl = self.observed["setlists"].setdefault(setlist, {"cid": None, "refs": {}})
            sl["refs"][name] = {"ref_cid": None, "posi": posi}

    # -- desired: tones + membership -----------------------------------------

    def add_tone(self, setlist: str, hsp_path: Path | str, *, pos: Optional[int] = None) -> str:
        """Register a ``.hsp`` tone and add it to ``setlist``'s membership.

        Reads the file's ``meta.name`` (falling back to the filename stem) as the
        tone name, records ``{path, content_hash, source:"hsp"}`` in the registry,
        and inserts the name into ``setlists[setlist]`` at ``pos`` (append if
        ``None``; never duplicated). Raises :class:`ManifestError` if the name is
        already registered to a *different* path (unique-name enforcement).
        Returns the resolved tone name.
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

        self.tones[name] = {
            "path": abs_path,
            "content_hash": _hash_file(p),
            "source": "hsp",
        }
        self._add_membership(setlist, name, pos)
        return name

    def pathless_add(
        self,
        name: str,
        source: str,
        *,
        setlist: Optional[str] = None,
        pos: Optional[int] = None,
    ) -> str:
        """Register a source-less tone (live edit buffer ``save`` / on-device
        ``create``) — ``path`` and ``content_hash`` are ``None`` (nothing to
        hash or re-push), but it still appears in ``device slots`` and may be
        referenced into a setlist. If ``setlist`` is given, appends membership at
        ``pos``. Returns the name.
        """
        if source not in _VALID_SOURCES:
            raise ManifestError(f"source must be one of {_VALID_SOURCES}, got {source!r}")
        existing = self.tones.get(name)
        if existing is not None and existing.get("path") is not None:
            raise ManifestError(
                f"tone name {name!r} is already registered with a path; "
                f"names must be unique in the manifest"
            )
        self.tones[name] = {"path": None, "content_hash": None, "source": source}
        if setlist is not None:
            self._add_membership(setlist, name, pos)
        return name

    def _add_membership(self, setlist: str, name: str, pos: Optional[int]) -> None:
        members = self.setlists_map.setdefault(setlist, [])
        if name in members:
            return  # no duplicates within a setlist
        if pos is None:
            members.append(name)
        else:
            members.insert(pos, name)

    def remove_tone(self, setlist: str, name: str) -> bool:
        """Drop ``name`` from ``setlist``'s membership.

        If no setlist references the tone any more, its registry entry is also
        garbage-collected. Returns whether the tone was in that setlist.
        """
        members = self.setlists_map.get(setlist)
        if not members or name not in members:
            return False
        members.remove(name)
        if not self._is_referenced(name):
            self.tones.pop(name, None)
        return True

    def _is_referenced(self, name: str) -> bool:
        return any(name in members for members in self.setlists_map.values())

    def create_setlist(self, name: str) -> None:
        """Create an empty setlist in the manifest (idempotent — never wipes an
        existing setlist's membership)."""
        self.setlists_map.setdefault(name, [])

    # -- desired: read accessors ---------------------------------------------

    def setlists(self) -> List[str]:
        """The manifest's setlist names, in insertion order."""
        return list(self.setlists_map.keys())

    def tones_in(self, setlist: str) -> List[str]:
        """Ordered tone names in ``setlist`` (empty list if unknown)."""
        return list(self.setlists_map.get(setlist, []))

    def tone_path(self, name: str) -> Optional[str]:
        """The registered ``.hsp`` path for ``name`` (``None`` if pathless/absent)."""
        entry = self.tones.get(name)
        return entry.get("path") if entry else None

    def content_hash(self, name: str) -> Optional[str]:
        """The registered content hash for ``name`` (``None`` if pathless/absent)."""
        entry = self.tones.get(name)
        return entry.get("content_hash") if entry else None

    def union_tones(self, setlists: List[str]) -> List[str]:
        """Ordered, de-duplicated union of tone names across ``setlists`` — the
        set of pool presets those setlists collectively need (for pool reconcile).
        First-seen order is preserved."""
        seen: Dict[str, None] = {}
        for setlist in setlists:
            for name in self.setlists_map.get(setlist, []):
                if name not in seen:
                    seen[name] = None
        return list(seen.keys())

    # -- observed (rebuilt each sync) ----------------------------------------

    def record_observed_pool(self, name: str, cid: int, posi: int,
                             *, synced_hash: Optional[str] = None) -> None:
        """Record a pool preset's observed device placement (name → cid + posi).

        ``synced_hash`` (when given) is the content hash of the ``.hsp`` body most
        recently pushed to this pool preset, so a later sync can skip a tone whose
        registered content hash still matches (idempotent re-sync). Read it back
        with :meth:`observed_pool_hash`.
        """
        entry: Dict[str, Any] = {"cid": cid, "posi": posi}
        if synced_hash is not None:
            entry["synced_hash"] = synced_hash
        self.observed["pool"][name] = entry

    def observed_pool_hash(self, name: str) -> Optional[str]:
        """The last-synced content hash recorded for pool preset ``name`` (the
        ``synced_hash`` stored by :meth:`record_observed_pool`), or ``None`` if
        the preset is unknown or was recorded without one."""
        entry = self.observed.get("pool", {}).get(name)
        return entry.get("synced_hash") if isinstance(entry, dict) else None

    def record_observed_setlist(self, setlist: str, cid: int, refs: Dict[str, Any]) -> None:
        """Record a setlist's observed device cid + its reference placements
        (``{tone-name: {ref_cid, posi}}``)."""
        self.observed["setlists"][setlist] = {"cid": cid, "refs": dict(refs)}

    def clear_observed(self) -> None:
        """Reset the observed half — the sync engine rebuilds it each run."""
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
        """Atomically write the manifest as pretty JSON (temp file + ``os.replace``).

        The slot ledger is folded into this same file as an ``entries`` section
        (see :mod:`helixgen.device.ledger`). It is not part of the manifest's own
        model, so any ``entries`` already on disk are read back and preserved
        verbatim, so saving the manifest never clobbers the ledger's placements.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = self.to_dict()
        existing = self._read_json(self.path)
        if existing is not None and isinstance(existing.get("entries"), list):
            doc["entries"] = existing["entries"]
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(doc, indent=2, sort_keys=False))
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
