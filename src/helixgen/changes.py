"""Declarative registry of tone-affecting engine changes.

Each :class:`ChangeEntry` pairs a human description with a content detector
and an idempotent in-place migration. ``library upgrade`` drives the registry
to find and repair stale tones; ``helixgen changes`` renders it as a
changelog. Entries are defined in :mod:`helixgen.changes_entries` and appended
to :data:`REGISTRY` (imported for its side effect at CLI load).

A detector/migration receives an ``UpgradeCtx`` (defined in
:mod:`helixgen.cli_library`) — a variant-scoped view exposing the loaded
``.hsp`` body and the variant/meta/library paths — and returns a
:class:`Finding`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

import click


@dataclass(frozen=True)
class Finding:
    """One entry's verdict for one variant."""
    affected: bool
    blocks: List[str]          # affected block ids; [] when block-level N/A
    status: str                # e.g. "would-clear" | "cleared" | "unchanged" | "missing" | "skipped"
    detail: str = ""


@dataclass
class UpgradeCtx:
    """A variant-scoped view handed to an entry's detect/apply.

    ``body`` is the loaded ``.hsp`` (mutated in place by ``apply``); ``variant``
    is the live tone-meta Variant (an entry may set e.g. ``venue_detail`` on it).
    ``resources`` is a per-run cache the driver fills once with entry-specific
    inputs (donor-info set, block library, IRs, pref values) so entries stay
    stateless and the library is loaded once, not once per variant.
    """
    hsp_path: Any
    body: dict
    meta: Any
    variant_key: str
    variant: Any
    library_dir: Any
    resources: dict


@dataclass(frozen=True)
class ChangeEntry:
    id: str
    landed_in: str             # engine version the fix shipped in
    kind: str                  # "bugfix" | "protocol" | "cosmetic"
    summary: str
    research: str
    detect: Callable[[Any], Finding]
    apply: Callable[[Any], Finding]


# Populated by helixgen.changes_entries (append-registered), kept ordered by
# landed_in ascending so migrations apply oldest-fix-first.
REGISTRY: List[ChangeEntry] = []


def _vtuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return (0,)


def register(entry: ChangeEntry) -> ChangeEntry:
    """Append an entry, keeping REGISTRY sorted by landed_in ascending."""
    REGISTRY.append(entry)
    REGISTRY.sort(key=lambda e: _vtuple(e.landed_in))
    return entry


def entries_since(version: Optional[str]) -> List[ChangeEntry]:
    """Entries whose fix landed strictly after ``version`` (all if None)."""
    if not version:
        return list(REGISTRY)
    floor = _vtuple(version)
    return [e for e in REGISTRY if _vtuple(e.landed_in) > floor]


def _entry_dict(e: ChangeEntry) -> dict:
    return {"id": e.id, "landed_in": e.landed_in, "kind": e.kind,
            "summary": e.summary, "research": e.research}


def render_changes(since: Optional[str], as_json: bool) -> str:
    entries = entries_since(since)
    if as_json:
        return json.dumps([_entry_dict(e) for e in entries])
    if not entries:
        return "No tone-affecting changes recorded."
    return "\n".join(
        f"{e.landed_in}  {e.id}  [{e.kind}]  {e.summary}" for e in entries)


@click.command(name="changes")
@click.option("--since", default=None, metavar="VERSION",
              help="Only changes that landed after this engine version.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the changelog as JSON.")
def changes_cmd(since: Optional[str], as_json: bool) -> None:
    """List tone-affecting engine changes (the migration registry).

    Each row is a fix that may have made older tones stale; `library upgrade`
    detects and repairs the affected ones in place.
    """
    click.echo(render_changes(since, as_json))
