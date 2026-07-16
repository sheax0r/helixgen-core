"""Naming schema for library tones (spec §4).

Display name: ``"$Artist - $Song - $Guitar"`` with a descriptor fallback
``"$Descriptor - $Guitar"``; the guitar segment is omitted only for
guitar-agnostic tones. Filenames use the same schema, slugified
lowercase-with-dashes.

A **logical tone** (identified by artist+song, or by a descriptor) owns one
metadata JSON (``logical_slug``) and one or more **variants**, each targeting
a specific guitar (``variant_slug`` appends the guitar's slug).

Pure functions, no filesystem access.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = ["slugify", "display_name", "logical_slug", "variant_slug"]

# Characters normalized to a plain dash before punctuation-stripping.
_DASH_LIKE = re.compile(r"[\s_–—]+")  # whitespace, underscore, en-dash, em-dash
# Anything left over that isn't alphanumeric or a dash gets dropped.
_NON_SLUG_CHARS = re.compile(r"[^a-z0-9-]+")
# Collapse runs of dashes (including ones newly adjacent after stripping).
_REPEATED_DASHES = re.compile(r"-{2,}")


def slugify(text: str) -> str:
    """Normalize ``text`` into a lowercase, dash-separated slug.

    Spaces, underscores, em-dashes (``—``) and en-dashes (``–``) become a
    single ``-``; any other punctuation is stripped outright; repeated dashes
    collapse to one; leading/trailing dashes are stripped.
    """
    normalized = unicodedata.normalize("NFKC", text).lower()
    dashed = _DASH_LIKE.sub("-", normalized)
    stripped = _NON_SLUG_CHARS.sub("", dashed)
    collapsed = _REPEATED_DASHES.sub("-", stripped)
    return collapsed.strip("-")


def _validate_identity(*, artist, song, descriptor) -> None:
    has_artist_song = artist is not None or song is not None
    has_descriptor = descriptor is not None
    if bool(artist is not None) != bool(song is not None):
        raise ValueError("artist and song must be provided together")
    if has_artist_song and has_descriptor:
        raise ValueError("provide either artist+song or descriptor, not both")
    if not has_artist_song and not has_descriptor:
        raise ValueError("must provide either artist+song or a descriptor")


def display_name(
    *,
    artist: str | None = None,
    song: str | None = None,
    descriptor: str | None = None,
    guitar_short: str | None = None,
) -> str:
    """Build the display name ``"Artist - Song - Guitar"`` / ``"Descriptor - Guitar"``.

    The guitar segment is omitted when ``guitar_short`` is ``None``. Raises
    ``ValueError`` unless exactly one of (``artist``+``song``) or
    ``descriptor`` is supplied -- ``artist`` requires ``song`` and vice versa.
    """
    _validate_identity(artist=artist, song=song, descriptor=descriptor)
    base = f"{artist} - {song}" if artist is not None else descriptor
    if guitar_short is None:
        return base
    return f"{base} - {guitar_short}"


def logical_slug(
    *,
    artist: str | None = None,
    song: str | None = None,
    descriptor: str | None = None,
) -> str:
    """Slug identifying the logical tone (no guitar segment).

    This is the metadata JSON filename stem. Same identity rules as
    ``display_name`` apply (validated here independently of it).
    """
    _validate_identity(artist=artist, song=song, descriptor=descriptor)
    base = f"{artist} - {song}" if artist is not None else descriptor
    return slugify(base)


def variant_slug(logical: str, guitar_slug: str | None) -> str:
    """Per-``.hsp`` filename slug: ``logical`` with the guitar slug appended.

    ``guitar_slug=None`` leaves the logical slug unchanged (guitar-agnostic
    variant, the ``"generic"`` variant key).
    """
    if guitar_slug is None:
        return logical
    return f"{logical}-{guitar_slug}"
