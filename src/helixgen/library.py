"""Helix block library: filesystem read/write, indexing, lookup."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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
