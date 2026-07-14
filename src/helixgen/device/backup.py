"""Local preset-backup library for the Helix Stadium.

Backs up a device setlist to plain files on disk so the user can browse and work
with their presets while the Helix is disconnected.  Pure stdlib — no device
dependency for the *offline* read side (``local_list`` / ``read_local``).

On-disk layout (all under one backup dir, default ``~/.helixgen/device-backups/``)::

    <backup-dir>/
        00-1A-clean-machine.sbe      # one file per preset: <NN-slot>-<safe-name>.<fmt>
        01-1B-lead-tone.sbe
        manifest.json                # index: {"version": ..., "entries": [ ... ]}

Each ``.sbe`` file is the raw content blob read from the device via the
non-activating ``get_content(cid)`` (``/GetContentData``) — the device's
**stored** content form (``\xff\xff\xff\xffpgsm``), which ``device push`` /
``device restore`` accept unchanged via ``content.to_content_data``.  ``fmt`` is
a first-class per-entry field so a future ``.hsp`` export can live in the same
manifest alongside ``.sbe`` blobs.

Manifest entry schema (one dict per backed-up preset)::

    {
        "name":       "Clean Machine",   # device preset display name
        "cid":        101,               # device content id (cid_)
        "posi":       0,                 # device slot position
        "slot_label": "1A",              # human bank/slot label
        "setlist":    "user",            # source setlist name
        "fmt":        "sbe",             # blob format
        "file":       "00-1A-clean-machine.sbe",   # basename, relative to backup dir
        "sha256":     "<hex>",           # sha256 of the file bytes
        "bytes":      42,                # byte length
        "saved_at":   "2026-07-11T…"     # from injected `now`; omitted if now is None
    }
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import (  # noqa: F401 - FACTORY/THROWAWAY re-exported for callers
    USER, FACTORY, THROWAWAY, _SETLIST_KEYWORDS, slot_label,
)

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
DEFAULT_FMT = "sbe"

# Inverse of the canonical keyword->container map (resolver pattern, #14) —
# derived, not cloned, so the two can never drift.
_SETLIST_NAMES = {v: k for k, v in _SETLIST_KEYWORDS.items()}

# Characters we allow verbatim in a filename slug; everything else is scrubbed.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def default_backup_dir() -> Path:
    """Where backups live: ``~/.helixgen/device-backups/``.

    Overridable wholesale with the ``$HELIXGEN_DEVICE_BACKUPS`` env var.
    """
    override = os.environ.get("HELIXGEN_DEVICE_BACKUPS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".helixgen" / "device-backups"


def _setlist_name(container: int) -> str:
    return _SETLIST_NAMES.get(container, str(container))


def sanitize_name(name: str) -> str:
    """Turn an arbitrary preset name into a safe filename slug.

    Slashes, spaces and other filesystem-hostile characters collapse to a single
    ``-``; leading/trailing separators and dots are stripped.  Never empty.
    """
    slug = _UNSAFE.sub("-", (name or "").strip())
    slug = slug.strip("-._")
    return slug or "untitled"


def _entry_filename(posi: Optional[int], name: str, fmt: str) -> str:
    idx = 0 if posi is None else posi
    return f"{idx:02d}-{slot_label(posi)}-{sanitize_name(name)}.{fmt}"


def backup_setlist(client, container: int = USER,
                   out_dir: Optional[Path] = None, *,
                   now: Optional[str] = None) -> List[Dict[str, Any]]:
    """Back up every preset in ``container`` to files under ``out_dir``.

    For each preset: read its ``.sbe`` content blob via the **non-activating**
    ``get_content(cid)`` (``/GetContentData`` — it never changes the device's
    active preset), write ``<NN-slot>-<safe-name>.sbe``, and record a manifest
    entry.  The manifest at ``out_dir/manifest.json`` is merged (entries with
    the same ``file`` are replaced) and rewritten.

    ``now`` is an injected ISO-timestamp string used verbatim as each entry's
    ``saved_at``; when ``None`` the field is omitted (this function never calls
    ``datetime`` itself, so callers control the clock / determinism).

    Because ``get_content`` is non-activating, backing up a setlist no longer
    disturbs the musician's live tone — there is no load/restore dance.

    Returns the list of entries written this run (in setlist order).
    """
    out_dir = Path(out_dir) if out_dir is not None else default_backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    presets = client.list_presets(container)

    setlist = _setlist_name(container)
    entries: List[Dict[str, Any]] = []
    for p in presets:
        cid = p.get("cid_")
        name = p.get("name", "") or ""
        posi = p.get("posi")

        blob = client.get_content(cid)

        fname = _entry_filename(posi, name, DEFAULT_FMT)
        (out_dir / fname).write_bytes(blob)

        entry: Dict[str, Any] = {
            "name": name,
            "cid": cid,
            "posi": posi,
            "slot_label": slot_label(posi),
            "setlist": setlist,
            "fmt": DEFAULT_FMT,
            "file": fname,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob),
        }
        if now is not None:
            entry["saved_at"] = now
        entries.append(entry)

    _write_manifest(out_dir, entries)
    return entries


def _read_manifest(out_dir: Path) -> Dict[str, Any]:
    path = Path(out_dir) / MANIFEST_NAME
    if not path.exists():
        return {"version": MANIFEST_VERSION, "entries": []}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"version": MANIFEST_VERSION, "entries": []}
    if not isinstance(data, dict):
        return {"version": MANIFEST_VERSION, "entries": []}
    data.setdefault("version", MANIFEST_VERSION)
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    return data


def _write_manifest(out_dir: Path, new_entries: List[Dict[str, Any]]) -> None:
    manifest = _read_manifest(out_dir)
    by_file = {e.get("file"): e for e in manifest["entries"]
               if isinstance(e, dict)}
    for e in new_entries:
        by_file[e["file"]] = e
    manifest["version"] = MANIFEST_VERSION
    manifest["entries"] = list(by_file.values())
    (Path(out_dir) / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=False))


def local_list(out_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return the manifest entries for a backup dir — offline, no device needed.

    Returns ``[]`` when no manifest exists.
    """
    out_dir = Path(out_dir) if out_dir is not None else default_backup_dir()
    return list(_read_manifest(out_dir)["entries"])


def read_local(path) -> bytes:
    """Read a backed-up ``.sbe`` (or other-fmt) blob's raw bytes off disk."""
    return Path(path).read_bytes()
