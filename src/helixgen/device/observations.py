"""Per-device *observed* placement state — ``~/.helixgen/devices/<serial>.json``.

Split out of the manifest (design ``2026-07-15-library-metadata-design`` §3):
the manifest (``setlists/manifest.json``) holds only committed **intent**; the
``cid``/``posi`` a *specific* Helix reports is **observation**, rebuilt
wholesale by every sync and keyed by the device serial reported by
``device info`` (``/ProductInfoGet``). A second or replacement Helix simply
gets its own file. Losing a devices file costs nothing — the next sync
rebuilds it — so this state is **not** committed to the ``~/.helixgen`` git repo.

On-disk shape (``version`` 1)::

    {"version": 1, "serial": "<serial>",
     "tones":    {"<name>": {"cid": int, "posi": int}},
     "pool":     {"<name>": {"cid": int, "posi": int, "synced_hash"?: "sha256:…"}},
     "setlists": {"<name>": {"cid": int, "refs": {"<name>": {...}}}}}

``tones`` is the per-tone observed placement (the old per-tone ``device``
field — read by ``slots restore``'s fallback and rename/delete-by-cid).
``pool`` mirrors it with the last-synced content hash (the sync skip/update
decision). ``setlists`` records observed reference cids/posi for next run's
diffing. This module is PURE local-file logic — no device, no network.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from helixgen import home

OBSERVATIONS_VERSION = 1

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_serial(serial: str) -> str:
    """Filesystem-safe basename for a serial (``ip-192.168.4.84``, ``legacy``,
    a real alphanumeric serial). Never allows path separators to escape
    ``devices/``."""
    s = _UNSAFE.sub("_", str(serial)).strip("._") or "unknown"
    return s


def _path_for(serial: str) -> Path:
    return home.devices_dir() / f"{_safe_serial(serial)}.json"


def _coerce_map(raw: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}


@dataclass
class DeviceObservations:
    """One device's observed placement state (``devices/<serial>.json``)."""

    serial: str
    tones: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    pool: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    setlists: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": OBSERVATIONS_VERSION,
            "serial": self.serial,
            "tones": self.tones,
            "pool": self.pool,
            "setlists": self.setlists,
        }

    # -- sync bookkeeping helpers (mirror the old manifest.observed_* API) ----

    def record_pool(self, name: str, cid: int, posi: int,
                    *, synced_hash: Optional[str] = None) -> None:
        """Record a pool preset's observed placement + last-synced hash, and
        mirror the placement into ``tones`` (the per-tone observed field)."""
        entry: Dict[str, Any] = {"cid": cid, "posi": posi}
        if synced_hash is not None:
            entry["synced_hash"] = synced_hash
        self.pool[name] = entry
        self.tones[name] = {"cid": cid, "posi": posi}

    def clear_pool(self, name: str) -> None:
        """Forget a tone's observed pool placement (deleted from the device)."""
        self.pool.pop(name, None)
        self.tones.pop(name, None)

    def pool_hash(self, name: str) -> Optional[str]:
        entry = self.pool.get(name)
        return entry.get("synced_hash") if isinstance(entry, dict) else None

    def record_setlist(self, name: str, cid: int, refs: Dict[str, Any]) -> None:
        self.setlists[name] = {"cid": cid, "refs": dict(refs)}

    def tone_placement(self, name: str) -> Optional[Dict[str, Any]]:
        entry = self.tones.get(name)
        return entry if isinstance(entry, dict) else None


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def load_observations(serial: str) -> DeviceObservations:
    """Load one device's observations (missing/corrupt file → empty)."""
    path = _path_for(serial)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return DeviceObservations(serial=serial)
    return DeviceObservations(
        serial=str(data.get("serial") or serial),
        tones=_coerce_map(data.get("tones")),
        pool=_coerce_map(data.get("pool")),
        setlists=_coerce_map(data.get("setlists")),
    )


def save_observations(obs: DeviceObservations) -> None:
    """Atomically write ``obs`` to ``devices/<serial>.json`` (temp + replace)."""
    path = _path_for(obs.serial)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obs.to_dict(), indent=2))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# cross-device lookups (replace the old per-tone ``device`` reads)
# ---------------------------------------------------------------------------

def _device_files() -> List[Path]:
    """Every ``devices/*.json`` file, newest mtime first."""
    d = home.devices_dir()
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return []
    files.sort(key=lambda p: _mtime(p), reverse=True)
    return files


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lookup_tone(name: str) -> Optional[Dict[str, Any]]:
    """Observed ``{cid, posi}`` for tone ``name`` across every device file,
    **newest-modified first** (a live device's file is the freshest). Returns
    the first hit or ``None``. Replaces the old per-tone ``device`` reads
    (e.g. the #25 ``slots restore`` posi fallback)."""
    for path in _device_files():
        data = _read(path)
        if data is None:
            continue
        tones = data.get("tones")
        if isinstance(tones, dict):
            entry = tones.get(name)
            if isinstance(entry, dict):
                return entry
    return None


def lookup_name_by_cid(cid: int) -> Optional[str]:
    """First tone name (newest device file first) whose observed ``cid``
    matches — used to map a device preset cid back to a library tone
    (``device rename`` / ``device delete``)."""
    for path in _device_files():
        data = _read(path)
        if data is None:
            continue
        tones = data.get("tones")
        if isinstance(tones, dict):
            for nm, entry in tones.items():
                if isinstance(entry, dict) and entry.get("cid") == cid:
                    return nm
    return None
