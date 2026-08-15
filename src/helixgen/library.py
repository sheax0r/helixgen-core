"""Helix block library: filesystem read/write, indexing, lookup."""
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from helixgen import home


class IngestStatus(Enum):
    NEW = "new"
    MATCH = "match"
    CONFLICT = "conflict"


# --- Path-traversal guard for ingested, attacker-controlled block fields ---
#
# `@model` and `@category` come straight out of ingested .hsp/.hlx/.json files
# and are used to build filesystem paths under the library root. Without
# validation a crafted `@model` like "../../../ESCAPED" (or a `@category` with
# a slash) escapes the root and writes an arbitrary *.json. Validate BOTH the
# model_id and the category BEFORE joining, then assert containment before any
# write (belt-and-suspenders against edge cases like symlinks).

# model_id: must start alphanumeric, then alnum / _ . -  (no "/", no "..").
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Allowed categories, derived from ingest._CATEGORY_PREFIXES' category values
# plus infer_category()'s "uncategorized" fallback. Kept as a literal set here
# (rather than importing) to avoid a circular import with helixgen.ingest,
# which imports Block/Library/IngestStatus from this module. Keep in sync if
# ingest._CATEGORY_PREFIXES gains a new category.
_ALLOWED_CATEGORIES = frozenset(
    {
        "amp",
        "cab",
        "drive",
        "reverb",
        "delay",
        "eq",
        "dynamics",
        "modulation",
        "pitch",
        "filter",
        "volume",
        "send",
        "looper",
        "uncategorized",
    }
)


def _validate_block_coords(model_id: str, category: str) -> None:
    """Reject model_id/category that could escape the library root.

    Raises ValueError on a non-conforming model_id or an unknown category.
    """
    if not isinstance(model_id, str) or not _MODEL_ID_RE.match(model_id):
        raise ValueError(
            f"invalid block model_id {model_id!r}: must match "
            f"{_MODEL_ID_RE.pattern} (no path separators or '..')"
        )
    if category not in _ALLOWED_CATEGORIES:
        raise ValueError(
            f"invalid block category {category!r}: not one of "
            f"{sorted(_ALLOWED_CATEGORIES)}"
        )


def default_library_path() -> Path:
    """Return the library path, honoring HELIXGEN_LIBRARY env var."""
    return home.library_dir()


@dataclass
class Block:
    """A single Helix block, as stored in the library."""

    model_id: str
    category: str
    display_name: str
    params: dict[str, dict[str, Any]]
    exemplar: dict[str, Any]
    first_seen: dict[str, str]
    aliases: list[str] = field(default_factory=list)
    default_irhash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if self.default_irhash is None:
            out.pop("default_irhash", None)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Block":
        # Resolve the display name at READ time rather than trusting what is
        # on disk: blocks ingested before hgc-3ll stored the DEVICE's own short
        # `@name`, which truncates ("ressor LAStudio Comp Mono") and collides
        # ("Stereo" named six models, one of them the most-used reverb in the
        # factory setlist). Overlaying here corrects an existing library on
        # upgrade with no migration — the block files are never rewritten. The
        # stored name is still the fallback for a model the editor's own table
        # has never heard of, so a curated name survives; and when it IS
        # superseded it becomes a legacy alias, so recipes written against the
        # old name keep resolving across the upgrade.
        from helixgen.ingest import (  # circular at import time
            legacy_alias,
            resolve_display_name,
        )

        model_id = data["model_id"]
        stored_name = data.get("display_name")
        aliases = list(data.get("aliases", []))
        legacy = legacy_alias(model_id, stored_name)
        if legacy is not None and legacy not in aliases:
            aliases.append(legacy)

        return cls(
            model_id=model_id,
            category=data["category"],
            display_name=resolve_display_name(model_id, stored_name),
            aliases=aliases,
            params=dict(data.get("params", {})),
            exemplar=dict(data.get("exemplar", {})),
            first_seen=dict(data.get("first_seen", {})),
            default_irhash=data.get("default_irhash"),
        )


class Library:
    """Filesystem-backed block library at a given root directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.blocks_dir = self.root / "blocks"

    def block_path(self, model_id: str, category: str) -> Path:
        _validate_block_coords(model_id, category)
        return self.blocks_dir / category / f"{model_id}.json"

    def _assert_contained(self, path: Path) -> Path:
        """Assert `path` resolves inside blocks_dir; raise ValueError if not."""
        resolved = path.resolve()
        if not resolved.is_relative_to(self.blocks_dir.resolve()):
            raise ValueError(
                f"refusing to write {resolved} outside library blocks dir "
                f"{self.blocks_dir.resolve()}"
            )
        return path

    def save_block(self, block: Block) -> Path:
        path = self.block_path(block.model_id, block.category)
        self._assert_contained(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(block.to_dict(), indent=2, sort_keys=False))
        return path

    def load_block(self, model_id: str) -> Block:
        # Search all category subdirs for the model_id
        if self.blocks_dir.exists():
            for category_dir in self.blocks_dir.iterdir():
                if not category_dir.is_dir():
                    continue
                candidate = category_dir / f"{model_id}.json"
                if candidate.exists():
                    return Block.from_dict(json.loads(candidate.read_text()))
        raise KeyError(f"No block with model_id {model_id!r} in library at {self.root}")

    def list_blocks(self, category: str | None = None) -> list[Block]:
        """Return all canonical blocks, optionally filtered to one category.

        Conflict variants (`<model_id>.vN.json`) are not returned — they are
        an audit trail of schema divergence, not separate library entries.
        """
        if not self.blocks_dir.exists():
            return []
        results: list[Block] = []
        category_dirs = (
            [self.blocks_dir / category] if category else list(self.blocks_dir.iterdir())
        )
        for cat_dir in category_dirs:
            if not cat_dir.is_dir():
                continue
            for entry in cat_dir.glob("*.json"):
                if _is_conflict_variant(entry.stem):
                    continue
                results.append(Block.from_dict(json.loads(entry.read_text())))
        return results

    def find_block(self, name_or_id: str) -> Block:
        """Resolve a display name, alias, or model_id to a single Block.

        Raises KeyError if no match. Raises LookupError if multiple matches.
        """
        all_blocks = self.list_blocks()
        # Exact model_id match wins over name match, since model_id is unique.
        for block in all_blocks:
            if block.model_id == name_or_id:
                return block
        # Then display names, which are unique per model; only if none match
        # do aliases get a say, so one block's alias can never shadow
        # another's real name (`Woody Blue` is the amp, even though it is also
        # the preamp's legacy alias -- hgc-3ll).
        matches = [b for b in all_blocks if b.display_name == name_or_id]
        if not matches:
            matches = [b for b in all_blocks if name_or_id in b.aliases]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(b.model_id for b in matches)
            raise LookupError(
                f"Block name {name_or_id!r} matches multiple library entries: {ids}. "
                f"Use the model_id explicitly."
            )
        raise KeyError(
            f"Block {name_or_id!r} not found in library at {self.root}. "
            f"Display names come from the editor's own model table and some "
            f"changed in hgc-3ll (e.g. 'Plate Stereo' is now 'Plate') -- run "
            f"`helixgen list-blocks` to see the current names, and prefer the "
            f"model_id, which never changes. An empty library instead needs "
            f"`helixgen ingest <export.hsp>`."
        )

    def rebuild_index(self) -> dict[str, Any]:
        """Re-derive index.json from the on-disk block files.

        Warns if two models resolve to the SAME display name. That cannot
        happen for models the vendored editor table names (it is unique by
        construction, hgc-3ll) — a warning here means a model the table has
        never heard of, e.g. one a firmware update added, and the fix is to
        regenerate the asset with `tools/build_param_meta.py`.
        """
        names: dict[str, list[str]] = {}
        categories: dict[str, str] = {}
        display: dict[str, list[str]] = {}

        for block in self.list_blocks():
            categories[block.model_id] = block.category
            if block.display_name:
                display.setdefault(block.display_name, []).append(block.model_id)
            for name in [block.display_name, *block.aliases]:
                if not name:
                    continue
                names.setdefault(name, []).append(block.model_id)

        for name, model_ids in sorted(display.items()):
            if len(model_ids) > 1:
                print(
                    f"warning: display name {name!r} is shared by "
                    f"{', '.join(sorted(model_ids))} — reference these by "
                    f"model_id, and regenerate the editor name table "
                    f"(`python tools/build_param_meta.py`).",
                    file=sys.stderr,
                )

        index = {"names": names, "categories": categories}
        index_path = self.root / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index, indent=2, sort_keys=True))
        return index

    @property
    def chassis_path(self) -> Path:
        return self.root / "chassis.json"

    def has_chassis(self) -> bool:
        return self.chassis_path.exists()

    def save_chassis(self, chassis: dict[str, Any]) -> Path:
        self.chassis_path.parent.mkdir(parents=True, exist_ok=True)
        self.chassis_path.write_text(json.dumps(chassis, indent=2))
        return self.chassis_path

    def load_chassis(self) -> dict[str, Any]:
        if not self.chassis_path.exists():
            raise FileNotFoundError(
                f"No chassis at {self.chassis_path}. Run `helixgen ingest <export.hlx>` first."
            )
        return json.loads(self.chassis_path.read_text())

    def save_block_with_dedup(self, block: Block) -> IngestStatus:
        """Save a block, deduplicating by model_id and detecting conflicts."""
        path = self.block_path(block.model_id, block.category)
        if not path.exists():
            self.save_block(block)
            return IngestStatus.NEW

        existing = Block.from_dict(json.loads(path.read_text()))
        if _schemas_match(existing.params, block.params):
            return IngestStatus.MATCH

        v = 2
        while True:
            conflict_path = path.with_name(f"{block.model_id}.v{v}.json")
            if not conflict_path.exists():
                self._assert_contained(conflict_path)
                conflict_path.write_text(
                    json.dumps(block.to_dict(), indent=2, sort_keys=False)
                )
                return IngestStatus.CONFLICT
            v += 1


def _schemas_match(a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]]) -> bool:
    """Two schemas match iff they have the same param keys and the same types per key."""
    if set(a) != set(b):
        return False
    for key in a:
        if a[key].get("type") != b[key].get("type"):
            return False
    return True


_CONFLICT_VARIANT_RE = re.compile(r"\.v\d+$")


def _is_conflict_variant(stem: str) -> bool:
    """True if `stem` (the file stem before `.json`) is a `<model>.vN` variant."""
    return bool(_CONFLICT_VARIANT_RE.search(stem))
