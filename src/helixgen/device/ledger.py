"""Local ledger of which authored tone helixgen put in which device slot.

When helixgen places a preset on the Helix (``device install`` / ``save`` /
``push`` / ``create``) it records the slot here, so you can list placements
offline, detect drift from the live device, and put a tone back in the same
spot. Pure stdlib; the offline read side needs no device. Distinct from
``device backup``'s ``manifest.json`` (a device *snapshot*) — this is a running
record of helixgen's own placements.

On-disk (default ``~/.helixgen/device-slots.json``, override
``$HELIXGEN_DEVICE_SLOTS``)::

    {"version": 1, "entries": [ {order, name, setlist, posi, slot_label, cid,
      source_kind, source_path, model, created_at, updated_at}, ... ]}

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
    """Where the slot ledger lives: ``~/.helixgen/device-slots.json``.

    Overridable wholesale with ``$HELIXGEN_DEVICE_SLOTS``.
    """
    override = os.environ.get("HELIXGEN_DEVICE_SLOTS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".helixgen" / "device-slots.json"


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
        path = Path(path) if path is not None else default_ledger_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls(path, [])
        if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
            return cls(path, [])
        entries = data.get("entries")
        if not isinstance(entries, list):
            return cls(path, [])
        return cls(path, [e for e in entries if isinstance(e, dict)])

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
        """Atomically write the ledger (temp file + ``os.replace``)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"version": LEDGER_VERSION, "entries": self.entries_in_order()}
        try:
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=False))
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
