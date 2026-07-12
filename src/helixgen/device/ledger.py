"""Local ledger of which authored tone helixgen put in which device slot.

When helixgen places a preset on the Helix (``device install`` / ``save`` /
``push`` / ``create``) it records the slot here, so you can list placements
offline, detect drift from the live device, and put a tone back in the same
spot. Pure stdlib; the offline read side needs no device. Distinct from
``device backup``'s ``manifest.json`` (a device *snapshot*) — this is a running
record of helixgen's own placements.

**One file.** The ledger's placements are *folded into* the setlist manifest
(design §3): they live as an ``entries`` section of the single
``~/.helixgen/setlists.json`` document (override ``$HELIXGEN_SETLISTS``), right
alongside the manifest's own ``tones`` / ``setlists`` / ``observed`` sections.
``SlotLedger.load()`` / ``save()`` read and write *only* that ``entries``
section, preserving the manifest's sections untouched, so the two views never
diverge across two files. The legacy ``~/.helixgen/device-slots.json``
(``$HELIXGEN_DEVICE_SLOTS``) is retired as a writer — it is read once, for a
one-time migration, and never written again.

On-disk (the ``entries`` section of ``setlists.json``)::

    {"version": 1, ..., "entries": [ {order, name, setlist, posi, slot_label,
      cid, source_kind, source_path, model, created_at, updated_at}, ... ]}

An entry is keyed by ``(setlist, posi)`` — one preset per device slot.
``source_kind`` (``hsp`` | ``sbe`` | ``edit-buffer`` | ``copy``) records how the
tone got there, so ``restore`` knows whether a local source exists to re-push.
Timestamps are injected by the caller (``now``), never produced here — keeps the
module deterministic, matching ``device/backup.py``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import slot_label

LEDGER_VERSION = 1


def default_ledger_path() -> Path:
    """The **legacy** slot-ledger path: ``~/.helixgen/device-slots.json``.

    Overridable wholesale with ``$HELIXGEN_DEVICE_SLOTS``. Since the fold this
    file is only ever *read*, once, to migrate old placements into the manifest
    (see :meth:`SlotLedger._migrate_from_legacy`); the ledger no longer writes
    it. New placements live in the manifest file (:func:`_storage_path`).
    """
    override = os.environ.get("HELIXGEN_DEVICE_SLOTS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".helixgen" / "device-slots.json"


def _storage_path() -> Path:
    """Where the ledger's ``entries`` actually live now: the single setlist
    manifest file (``~/.helixgen/setlists.json`` / ``$HELIXGEN_SETLISTS``).

    Lazy import of the manifest's path resolver avoids a circular import
    (``manifest`` imports :func:`default_ledger_path` from this module)."""
    from .manifest import default_setlists_path

    return default_setlists_path()


def _read_doc(path: Path) -> Optional[Dict[str, Any]]:
    """Return the on-disk JSON document if it is a valid, current-version dict,
    else ``None`` (missing / corrupt / unknown version). The manifest and the
    ledger share ``version == 1``, so a manifest document reads back fine here —
    the ledger simply looks at its ``entries`` section."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        return None
    return data


def _dev_cid(preset: Dict[str, Any]) -> Optional[int]:
    """Device presets carry the content id as ``cid_`` (or ``cid``)."""
    return preset.get("cid", preset.get("cid_"))


class SlotLedger:
    """Ordered record of helixgen's device-slot placements."""

    def __init__(self, path: Path, entries: Optional[List[Dict[str, Any]]] = None):
        self.path = Path(path)
        self.entries: List[Dict[str, Any]] = entries if entries is not None else []

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SlotLedger":
        """Load the ledger's ``entries`` from the single manifest file.

        With no ``path`` the folded storage location (:func:`_storage_path`) is
        used. If that file is missing/corrupt and this is the default location,
        a one-time migration folds a legacy ``device-slots.json`` in. An explicit
        ``path`` (tests, callers targeting a specific file) never migrates.
        """
        use_default = path is None
        path = Path(path) if path is not None else _storage_path()
        data = _read_doc(path)
        entries = data.get("entries") if data is not None else None
        if isinstance(entries, list):
            return cls(path, [e for e in entries if isinstance(e, dict)])
        # No ledger section yet (file absent, or a manifest written before any
        # placement). On the default file, fold a legacy device-slots.json in.
        ledger = cls(path, [])
        if use_default:
            ledger._migrate_from_legacy()
        return ledger

    def _migrate_from_legacy(self) -> None:
        """One-time fold of a legacy ``device-slots.json`` into the ledger view.

        Reads (never writes) the old file at :func:`default_ledger_path`; its
        ``entries`` become this ledger's entries. A subsequent :meth:`save`
        persists them into the manifest file's ``entries`` section. No-op if the
        legacy file is absent/corrupt or aliases the new storage path."""
        legacy = default_ledger_path()
        if legacy == self.path:
            return
        try:
            data = json.loads(legacy.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            return
        self.entries = [e for e in data["entries"] if isinstance(e, dict)]

    # -- lookups --------------------------------------------------------------

    def _match(self, *, setlist: Optional[str] = None, posi: Optional[int] = None,
               cid: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Match by (setlist, posi) when both given, else by cid."""
        for e in self.entries:
            if setlist is not None and posi is not None:
                if e.get("setlist") == setlist and e.get("posi") == posi:
                    return e
            elif cid is not None and e.get("cid") == cid:
                return e
        return None

    def entries_in_order(self) -> List[Dict[str, Any]]:
        return sorted(self.entries, key=lambda e: e.get("order", 0))

    def find(self, *, name: Optional[str] = None, setlist: Optional[str] = None,
             posi: Optional[int] = None) -> Optional[Dict[str, Any]]:
        for e in self.entries_in_order():
            if name is not None and e.get("name") != name:
                continue
            if setlist is not None and e.get("setlist") != setlist:
                continue
            if posi is not None and e.get("posi") != posi:
                continue
            return e
        return None

    # -- mutations ------------------------------------------------------------

    def record(self, *, setlist: str, posi: int, name: str, cid: Optional[int],
               source_kind: str, source_path: Optional[str] = None,
               model: Optional[str] = None, now: Optional[str] = None) -> Dict[str, Any]:
        """Upsert the entry for ``(setlist, posi)``.

        New slot → appended with the next ``order`` and ``created_at``.
        Existing slot → fields refreshed, ``order`` preserved, ``updated_at`` set.
        """
        existing = self._match(setlist=setlist, posi=posi)
        if existing is not None:
            existing.update({
                "name": name,
                "cid": cid,
                "slot_label": slot_label(posi),
                "source_kind": source_kind,
                "source_path": source_path,
                "model": model,
            })
            if now is not None:
                existing["updated_at"] = now
            return existing

        order = max((e.get("order", -1) for e in self.entries), default=-1) + 1
        entry: Dict[str, Any] = {
            "order": order,
            "name": name,
            "setlist": setlist,
            "posi": posi,
            "slot_label": slot_label(posi),
            "cid": cid,
            "source_kind": source_kind,
            "source_path": source_path,
            "model": model,
        }
        if now is not None:
            entry["created_at"] = now
            entry["updated_at"] = now
        self.entries.append(entry)
        return entry

    def rename(self, *, setlist: Optional[str] = None, posi: Optional[int] = None,
               cid: Optional[int] = None, new_name: str,
               now: Optional[str] = None) -> bool:
        e = self._match(setlist=setlist, posi=posi, cid=cid)
        if e is None:
            return False
        e["name"] = new_name
        if now is not None:
            e["updated_at"] = now
        return True

    def remove(self, *, setlist: Optional[str] = None, posi: Optional[int] = None,
               cid: Optional[int] = None) -> bool:
        e = self._match(setlist=setlist, posi=posi, cid=cid)
        if e is None:
            return False
        self.entries.remove(e)
        self._densify_order()
        return True

    def _densify_order(self) -> None:
        for i, e in enumerate(self.entries_in_order()):
            e["order"] = i

    def reorder(self, *, name: Optional[str] = None, cid: Optional[int] = None,
                to_index: int) -> bool:
        """Move an entry to ``to_index`` within its own setlist's sequence.

        Local-only (no device write). Redistributes the setlist's existing
        ``order`` values over the new sequence, so interleaving with other
        setlists' entries is preserved. Returns False if no entry matched.
        """
        entry = self.find(name=name) if name is not None else self._match(cid=cid)
        if entry is None:
            return False
        setlist = entry.get("setlist")
        siblings = [e for e in self.entries_in_order() if e.get("setlist") == setlist]
        order_values = sorted(e.get("order", 0) for e in siblings)
        siblings.remove(entry)
        to_index = max(0, min(to_index, len(siblings)))
        siblings.insert(to_index, entry)
        for e, order in zip(siblings, order_values):
            e["order"] = order
        return True

    def sync_plan(self, device_presets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Compute the slot moves that would make the device match the ledger's
        desired order — pure, no client, no writes.

        Per setlist, the tracked presets that are actually on the device are
        rearranged **among the slots they already occupy**: the i-th entry (in
        ledger order) goes to the i-th smallest occupied slot. This never
        disturbs untracked presets and never needs a slot outside the tracked
        set. Missing (not-on-device) and untracked presets are excluded.

        Returns a list of ``{setlist, cid, name, from, to}`` for slots that
        change (in ledger order), skipping no-ops.
        """
        dev_posi: Dict[Any, int] = {}
        for p in device_presets:
            dev_posi[(p.get("setlist"), _dev_cid(p))] = p.get("posi")

        by_setlist: Dict[str, List[Dict[str, Any]]] = {}
        for e in self.entries_in_order():
            by_setlist.setdefault(e.get("setlist"), []).append(e)

        moves: List[Dict[str, Any]] = []
        for setlist, entries in by_setlist.items():
            present = [e for e in entries if (setlist, e.get("cid")) in dev_posi]
            occupied = sorted(dev_posi[(setlist, e.get("cid"))] for e in present)
            for e, target in zip(present, occupied):
                current = dev_posi[(setlist, e.get("cid"))]
                if current != target:
                    moves.append({
                        "setlist": setlist,
                        "cid": e.get("cid"),
                        "name": e.get("name"),
                        "from": current,
                        "to": target,
                    })
        return moves

    # -- verify ---------------------------------------------------------------

    def verify(self, device_presets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Cross-check the ledger against the device's current presets.

        ``device_presets`` is a flat list annotated with ``setlist`` + ``posi``
        (+ ``name``/``cid_``). Returns one record per ledger entry with a
        ``status`` (``ok`` / ``changed`` / ``missing`` / ``moved``), followed by
        one ``untracked`` record per device preset in a slot the ledger doesn't
        cover. Pure — no client, so it is unit-testable offline.
        """
        by_slot: Dict[Any, Dict[str, Any]] = {}
        by_cid: Dict[int, Dict[str, Any]] = {}
        for p in device_presets:
            by_slot[(p.get("setlist"), p.get("posi"))] = p
            cid = _dev_cid(p)
            if cid is not None:
                by_cid[cid] = p

        results: List[Dict[str, Any]] = []
        tracked_slots = set()
        for e in self.entries_in_order():
            key = (e.get("setlist"), e.get("posi"))
            tracked_slots.add(key)
            dev = by_slot.get(key)
            if dev is None:
                if e.get("cid") is not None and e["cid"] in by_cid:
                    status = "moved"
                else:
                    status = "missing"
            else:
                dev_cid = _dev_cid(dev)
                if dev.get("name") == e.get("name") or (
                        e.get("cid") is not None and dev_cid == e.get("cid")):
                    status = "ok"
                else:
                    status = "changed"
            results.append({**e, "status": status})

        for p in device_presets:
            if (p.get("setlist"), p.get("posi")) not in tracked_slots:
                results.append({
                    "status": "untracked",
                    "setlist": p.get("setlist"),
                    "posi": p.get("posi"),
                    "slot_label": slot_label(p.get("posi")),
                    "name": p.get("name"),
                    "cid": _dev_cid(p),
                })
        return results

    # -- persistence ----------------------------------------------------------

    def save(self) -> None:
        """Atomically update *only* the ``entries`` section of the single
        manifest file (temp file + ``os.replace``).

        Any manifest sections already on disk (``tones`` / ``setlists`` /
        ``observed``) are read back and preserved verbatim, so writing the
        ledger never clobbers the manifest's half of the shared document."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = _read_doc(self.path) or {}
        doc["version"] = LEDGER_VERSION
        doc["entries"] = self.entries_in_order()
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
