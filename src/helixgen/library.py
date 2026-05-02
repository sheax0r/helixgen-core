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
