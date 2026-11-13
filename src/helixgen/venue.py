"""Venue vocabulary: what a tone variant is played through."""
from __future__ import annotations

VENUES: tuple[str, ...] = ("headphones", "direct", "amp-in")


def validate_venue(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in VENUES:
        raise ValueError(f"venue must be one of {', '.join(VENUES)}; got {value!r}")
    return value


# --- room IR selection / blend level ---------------------------------------

import ctypes
import math
from pathlib import Path

from helixgen import ir as _ir
from helixgen.ir import IrMapping


class VenueError(ValueError):
    pass


def ir_energy_db(wav: Path) -> float:
    """``10*log10(sum(x**2))`` over channel 0 of ``wav``."""
    wav = Path(wav)
    if not wav.is_file():
        raise VenueError(f"IR file not found: {wav}")
    sf = _ir._load_libsndfile()
    info = _ir._SF_INFO()
    h = sf.sf_open(str(wav).encode(), _ir._SFM_READ, ctypes.byref(info))
    if not h:
        raise VenueError(f"could not read IR file: {wav}")
    try:
        ch = max(info.channels, 1)
        buf = (ctypes.c_float * (info.frames * ch))()
        n = sf.sf_readf_float(h, buf, info.frames)
    finally:
        sf.sf_close(h)
    total = sum(buf[i * ch] ** 2 for i in range(n))
    if total <= 0:
        raise VenueError(f"IR file is silent: {wav}")
    return 10 * math.log10(total)


def _norm(path: str) -> str:
    return path.lower().replace("-", " ").replace("_", " ")


def find_room_ir(
    close_wav: Path, irs: IrMapping, override: str | None = None
) -> tuple[str, Path] | None:
    """The room IR to blend under ``close_wav``: ``(hash, path)`` or None.

    Candidates: registered IRs whose basename has `` ROOM`` and whose text
    before it (plus a space) prefixes the close IR's basename. Prefer
    ``natural-phase`` paths, then highest energy, then lowest basename.
    ``override`` is a hash or basename and skips selection.
    """
    if override is not None:
        if override in irs.entries:
            return override, irs.resolve_by_hash(override)
        hits = [h for h, p in irs.entries.items() if Path(p).name == override]
        if not hits:
            raise VenueError(f"no registered IR with hash or basename {override!r}")
        if len(hits) > 1:
            raise VenueError(
                f"basename {override!r} is ambiguous ({', '.join(sorted(hits))}); "
                "pass the IR hash instead"
            )
        return hits[0], irs.resolve_by_hash(hits[0])

    close_name = Path(close_wav).name
    cands = []
    for h, stored in irs.entries.items():
        name = Path(stored).name
        stem, sep, _ = name.partition(" ROOM")
        if sep and close_name.startswith(stem + " "):
            cands.append((h, irs.resolve_by_hash(h)))
    if not cands:
        return None
    natural = [c for c in cands if "natural phase" in _norm(str(c[1]))]
    cands = natural or cands
    return min(cands, key=lambda c: (-ir_energy_db(c[1]), c[1].name))


def room_level_db(
    close_level: float, close_wav: Path, room_wav: Path, offset_db: float = 4.0
) -> float:
    """Room IR Level that sits ``offset_db`` under the close IR, energy-matched."""
    diff = ir_energy_db(room_wav) - ir_energy_db(close_wav)
    return round(close_level - diff - offset_db, 1)
