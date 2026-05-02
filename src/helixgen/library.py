"""Helix block library: filesystem read/write, indexing, lookup."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def default_library_path() -> Path:
    """Return the library path, honoring HELIXGEN_LIBRARY env var."""
    env = os.environ.get("HELIXGEN_LIBRARY")
    if env:
        return Path(env)
    return Path(os.environ["HOME"]) / ".helixgen" / "library"


def default_cache_path() -> Path:
    """Return the cache path used for cloned upstream repos."""
    return Path(os.environ["HOME"]) / ".helixgen" / ".cache"


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Block":
        return cls(
            model_id=data["model_id"],
            category=data["category"],
            display_name=data["display_name"],
            aliases=list(data.get("aliases", [])),
            params=dict(data.get("params", {})),
            exemplar=dict(data.get("exemplar", {})),
            first_seen=dict(data.get("first_seen", {})),
        )


class Library:
    """Filesystem-backed block library at a given root directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.blocks_dir = self.root / "blocks"

    def block_path(self, model_id: str, category: str) -> Path:
        return self.blocks_dir / category / f"{model_id}.json"

    def save_block(self, block: Block) -> Path:
        path = self.block_path(block.model_id, block.category)
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
        """Return all blocks, optionally filtered to one category."""
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
        # Then collect all blocks whose display_name or aliases match.
        matches = [
            b for b in all_blocks
            if b.display_name == name_or_id or name_or_id in b.aliases
        ]
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
            f"Try `helixgen ingest <export.hlx>` or `helixgen bootstrap`."
        )

    def rebuild_index(self) -> dict[str, Any]:
        """Re-derive index.json from the on-disk block files."""
        names: dict[str, list[str]] = {}
        categories: dict[str, str] = {}

        for block in self.list_blocks():
            categories[block.model_id] = block.category
            for name in [block.display_name, *block.aliases]:
                if not name:
                    continue
                names.setdefault(name, []).append(block.model_id)

        index = {"names": names, "categories": categories}
        index_path = self.root / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index, indent=2, sort_keys=True))
        return index
