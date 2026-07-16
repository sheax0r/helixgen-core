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
     "ip"?: "192.168.x.x", "ip_updated_at"?: float,
     "model"?: "stadium", "firmware"?: "1.3.2",
     "tones":    {"<name>": {"cid": int, "posi": int}},
     "pool":     {"<name>": {"cid": int, "posi": int, "synced_hash"?: "sha256:…"}},
     "setlists": {"<name>": {"cid": int, "refs": {"<name>": {...}}}}}

The optional ``ip``/``ip_updated_at``/``model``/``firmware`` fields are the
device's **discovered address record** (workspace #74, written by
``helixgen device discover``); they round-trip through every load/save so a
sync rebuild never drops them. Losing a devices file now costs one
re-``discover`` (plus the free sync rebuild).

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
    """Filesystem-safe basename for a serial (``ip-192.168.0.10``, ``legacy``,
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
    # discovered address record (workspace #74) — optional, round-tripped.
    ip: Optional[str] = None
    ip_updated_at: Optional[float] = None
    model: Optional[str] = None
    firmware: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "version": OBSERVATIONS_VERSION,
            "serial": self.serial,
            "tones": self.tones,
            "pool": self.pool,
            "setlists": self.setlists,
        }
        for k in ("ip", "ip_updated_at", "model", "firmware"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

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
        **_ip_fields(data),
    )


def _ip_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """The optional discovered-address fields of a raw devices dict, coerced
    (used by every path that reconstructs a :class:`DeviceObservations` from
    file data, so no rewrite ever drops them)."""
    out: Dict[str, Any] = {}
    if data.get("ip") is not None:
        out["ip"] = str(data["ip"])
    if isinstance(data.get("ip_updated_at"), (int, float)):
        out["ip_updated_at"] = float(data["ip_updated_at"])
    if data.get("model") is not None:
        out["model"] = str(data["model"])
    if data.get("firmware") is not None:
        out["firmware"] = str(data["firmware"])
    return out


def save_observations(obs: DeviceObservations) -> None:
    """Atomically write ``obs`` to ``devices/<serial>.json`` (temp + replace)."""
    path = _path_for(obs.serial)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obs.to_dict(), indent=2))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# discovered address records (workspace #74)
# ---------------------------------------------------------------------------

def record_device_ip(serial: str, ip: str, *,
                     model: Optional[str] = None,
                     firmware: Optional[str] = None,
                     updated_at: Optional[float] = None) -> Path:
    """Persist a discovered device address into ``devices/<serial>.json``,
    preserving any existing observed placement state. Returns the path."""
    import time as _time

    obs = load_observations(serial)
    obs.ip = str(ip)
    obs.ip_updated_at = float(updated_at if updated_at is not None
                              else _time.time())
    if model is not None:
        obs.model = str(model)
    if firmware is not None:
        obs.firmware = str(firmware)
    save_observations(obs)
    return _path_for(serial)


def devices_with_ips() -> List[Dict[str, Any]]:
    """Every persisted device record carrying a discovered ``ip``, sorted
    most-recently-discovered first (``ip_updated_at`` desc, serial desc as
    the deterministic tie-break). Each row:
    ``{serial, ip, ip_updated_at, model?, firmware?}``."""
    rows: List[Dict[str, Any]] = []
    for path in _device_files():
        data = _read(path)
        if data is None or not data.get("ip"):
            continue
        fields = _ip_fields(data)
        rows.append({
            "serial": str(data.get("serial") or path.stem),
            "ip": fields.get("ip"),
            "ip_updated_at": fields.get("ip_updated_at", 0.0),
            "model": fields.get("model"),
            "firmware": fields.get("firmware"),
        })
    rows.sort(key=lambda r: (r.get("ip_updated_at") or 0.0, r["serial"]),
              reverse=True)
    return rows


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


def _rewrite_tones_key(path: Path, data: Dict[str, Any],
                       transform) -> None:
    """Apply ``transform(tones_dict)`` in place and, if it changed anything,
    write the file back via :func:`save_observations`. Used by
    :func:`rename_tone` / :func:`remove_tone`."""
    tones = data.get("tones")
    if not isinstance(tones, dict):
        return
    before = dict(tones)
    transform(tones)
    if tones == before:
        return
    obs = DeviceObservations(
        serial=str(data.get("serial") or path.stem),
        tones=_coerce_map(tones),
        pool=_coerce_map(data.get("pool")),
        setlists=_coerce_map(data.get("setlists")),
        **_ip_fields(data),
    )
    save_observations(obs)


def rename_tone(old_name: str, new_name: str) -> None:
    """Best-effort: rename ``old_name`` -> ``new_name`` in the ``tones`` map
    of every ``devices/*.json`` file that has it (mirrors a ``device rename``
    reflected in the manifest — see ``cli_device._ledger_rename``, Minor 5).
    A missing/corrupt file, or the name simply not being present anywhere, is
    a silent no-op."""
    for path in _device_files():
        data = _read(path)
        if data is None:
            continue

        def _do_rename(tones: Dict[str, Any], _old=old_name, _new=new_name) -> None:
            if _old in tones:
                tones[_new] = tones.pop(_old)

        _rewrite_tones_key(path, data, _do_rename)


def remove_tone(name: str) -> None:
    """Best-effort: drop ``name`` from the ``tones`` map of every
    ``devices/*.json`` file that has it (mirrors a ``device delete`` reflected
    in the manifest — see ``cli_device._ledger_remove``, Minor 5). A
    missing/corrupt file, or the name simply not being present anywhere, is a
    silent no-op."""
    for path in _device_files():
        data = _read(path)
        if data is None:
            continue

        def _do_remove(tones: Dict[str, Any], _name=name) -> None:
            tones.pop(_name, None)

        _rewrite_tones_key(path, data, _do_remove)
