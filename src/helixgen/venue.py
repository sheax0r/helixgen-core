"""Venue vocabulary: what a tone variant is played through."""
from __future__ import annotations

VENUES: tuple[str, ...] = ("headphones", "direct", "amp-in")


def validate_venue(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in VENUES:
        raise ValueError(f"venue must be one of {', '.join(VENUES)}; got {value!r}")
    return value
