"""Library-first IR folders — a disk sidecar mapping IR hash -> folder label.

The device's IR store (container -11) is flat and hash-addressed; folders are an
app-side organization layer the device never learns about (design spec
``2026-11-22-library-ir-librarian-ui-design.md`` §A.6). So helixgen owns IR
folders *on disk*, as the library's own structure, and the device never sees
them: copy/push/pull ignore folders entirely.

The sidecar is keyed by the IR *hash* (not the display name), so a ``rename-ir``
— which changes only the name — preserves folder membership. A missing, empty or
corrupt sidecar degrades to today's flat view (no migration).

Folders are implicit: a folder "exists" while at least one IR is assigned to it.
(Empty folders are not kept in v1 — the spec's empty-folder affordance can layer
on later without changing this schema.)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from helixgen import home

SIDECAR_NAME = "ir-folders.json"


def _path(library_dir: Optional[Path] = None) -> Path:
    base = Path(library_dir) if library_dir else home.library_dir()
    return base / SIDECAR_NAME


def load(library_dir: Optional[Path] = None) -> Dict[str, str]:
    """``hash -> folder label`` for every assigned IR. A missing or unreadable
    sidecar, or any shape that isn't ``{"folders": {hash: label}}`` with
    non-empty string labels, degrades to ``{}`` (the flat view)."""
    try:
        doc = json.loads(_path(library_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    folders = doc.get("folders") if isinstance(doc, dict) else None
    if not isinstance(folders, dict):
        return {}
    return {str(k): v.strip() for k, v in folders.items()
            if isinstance(v, str) and v.strip()}


def set_folder(hash_: str, folder: Optional[str],
               *, library_dir: Optional[Path] = None) -> Dict[str, str]:
    """Assign IR ``hash_`` to ``folder`` (an empty/blank/``None`` folder unfiles
    it — removes the entry). Writes the sidecar atomically and returns the new
    ``hash -> folder`` map. Raises ``ValueError`` for a blank hash."""
    hash_ = (hash_ or "").strip()
    if not hash_:
        raise ValueError("an IR hash is required")
    folders = load(library_dir)
    label = (folder or "").strip()
    if label:
        folders[hash_] = label
    else:
        folders.pop(hash_, None)
    p = _path(library_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    home.atomic_write_text(
        p, json.dumps({"version": 1, "folders": folders}, indent=2) + "\n")
    return folders
