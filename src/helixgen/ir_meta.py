"""Per-IR JSON metadata: ``library/irs/<pack>/<name>.json`` (design §5.3).

Each user IR copied into the library owns a sidecar JSON next to its WAV,
carrying provenance mined from the pack manual / ``_catalog`` (filled by the
skill after import) plus a few things helixgen can guess cheaply at import
time (the ``Mix NN`` number from the filename). The WAV bytes stay gitignored
(``library/irs/**/*.wav``); the sidecar ``.json`` and ``mapping.json`` ARE
committed.

``measured`` STAYS ``None`` in core — the optional FFT 5-band pass is a skill
step (stdlib-only rule, no numpy here).

Shape (``docs/…/2026-07-15-library-metadata-design.md`` §5.3)::

    { "schema": 1,
      "irhash": "553b0d…", "wav": "irs/york-audio-bogn/YA BOGN Mix 01.wav",
      "imported_from": "/Users/…/irs/YA BOGN/Mixes/….wav",
      "pack": {"name": "York Audio BOGN", "manual": "…pdf"},
      "cab": "Bogner 4x12", "speaker": "V30", "mics": ["57", "121"],
      "mix": "Mix 01", "tags": ["tight", "mid-forward", "modern"],
      "measured": null, "notes_md": null }

``wav`` is stored **relative to** ``home.library_dir()`` (matching how
``tone_meta`` stores hsp paths); an IR whose copy lives outside the library
root (``$HELIXGEN_IRS`` pointed elsewhere) stores the absolute path instead.

Every persisting write goes through :func:`save_ir_meta` (atomic temp-file +
``os.replace``, then advisory ``gitops.auto_commit`` under the same under-home
guard as ``tone_meta.save_tone_meta``). :func:`import_wav` (the register /
scaffold path) writes the sidecar atomically WITHOUT committing so its bulk
callers (``register-irs`` / ``ir-scan`` / ``ir-backfill`` / ``library
migrate``) can commit once at the end.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from helixgen import gitops, home, libinit, naming
from helixgen.ir import IrMapping, IrMappingError


# ---------------------------------------------------------------------------
# controlled tag vocabulary (the `_catalog` README's, folded in verbatim)
# ---------------------------------------------------------------------------

CONTROLLED_TAGS: frozenset[str] = frozenset({
    # tone
    "bright", "dark", "warm", "neutral", "scooped", "mid-forward", "beefy",
    "tight", "boomy", "boxy", "fizzy", "smooth", "articulate", "aggressive",
    "airy", "full", "chime",
    # gain
    "clean", "edge-of-breakup", "crunch", "high-gain",
    # era
    "vintage", "modern",
    # use
    "classic-rock", "blues", "metal", "thrash", "garage", "fuzz", "indie",
    "lead", "rhythm", "stereo", "room",
})

# Casefolded lookup set: tag validation is case-insensitive (a ``"Bright"`` tag
# is as valid as ``"bright"``), matching the case-insensitive guitar_settings
# check elsewhere in this PR.
_CONTROLLED_TAGS_CF: frozenset[str] = frozenset(t.casefold() for t in CONTROLLED_TAGS)


# ---------------------------------------------------------------------------
# dataclass
# ---------------------------------------------------------------------------


@dataclass
class IrMeta:
    """A user IR's provenance + character metadata (design §5.3)."""

    irhash: str
    wav: str
    imported_from: Optional[str] = None
    pack: Optional[Dict[str, Any]] = None
    cab: Optional[str] = None
    speaker: Optional[str] = None
    mics: List[str] = field(default_factory=list)
    mix: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    measured: Optional[Dict[str, Any]] = None
    notes_md: Optional[str] = None
    schema: int = 1


def _meta_to_dict(m: IrMeta) -> Dict[str, Any]:
    return {
        "schema": m.schema,
        "irhash": m.irhash,
        "wav": m.wav,
        "imported_from": m.imported_from,
        "pack": m.pack,
        "cab": m.cab,
        "speaker": m.speaker,
        "mics": list(m.mics),
        "mix": m.mix,
        "tags": list(m.tags),
        "measured": m.measured,
        "notes_md": m.notes_md,
    }


def _meta_from_dict(d: Dict[str, Any]) -> IrMeta:
    return IrMeta(
        irhash=d.get("irhash"),
        wav=d.get("wav"),
        imported_from=d.get("imported_from"),
        pack=d.get("pack"),
        cab=d.get("cab"),
        speaker=d.get("speaker"),
        mics=list(d.get("mics") or []),
        mix=d.get("mix"),
        tags=list(d.get("tags") or []),
        measured=d.get("measured"),
        notes_md=d.get("notes_md"),
        schema=d.get("schema", 1),
    )


# ---------------------------------------------------------------------------
# paths + helpers
# ---------------------------------------------------------------------------


def meta_path_for(wav_in_library: Path) -> Path:
    """The sidecar path for a library WAV: same path, ``.json`` suffix."""
    return Path(wav_in_library).with_suffix(".json")


def _is_under(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def _to_library_relative(wav_path: Path | str) -> str:
    """Normalize ``wav_path`` to a library-root-relative POSIX string.

    An absolute path under ``home.library_dir()`` is relativized; anything
    else (already relative, or absolute-but-elsewhere — e.g. ``$HELIXGEN_IRS``
    outside the library) is stored as given (mirrors ``tone_meta``)."""
    p = Path(wav_path)
    if p.is_absolute():
        try:
            p = p.relative_to(home.library_dir())
        except ValueError:
            return str(p)
    return str(p).replace(os.sep, "/")


_MIX_RE = re.compile(r"(?i)\bmix[\s_-]*(\d+)")


def _guess_mix(name: str) -> Optional[str]:
    """Guess a ``"Mix NN"`` label from a filename (case-insensitive, digits
    preserved verbatim so ``"YA BOGN Mix 01.wav"`` -> ``"Mix 01"``). ``None``
    when no ``Mix <digits>`` token is present."""
    match = _MIX_RE.search(name)
    if not match:
        return None
    return f"Mix {match.group(1)}"


# Generic container dirs that a commercial pack nests its WAVs under
# (``<PackName>/Mixes/*.wav``); the pack IDENTITY is the grandparent, not this.
_GENERIC_PACK_DIRS: frozenset[str] = frozenset({
    "mixes", "mix", "wavs", "wav", "irs", "ir",
})


def derive_pack(src: Path) -> str:
    """Derive the library pack-subdir slug for a source WAV.

    Normally ``slugify(src.parent.name)``. But commercial packs are laid out
    ``<PackName>/Mixes/*.wav``, so when the immediate parent is a generic
    container (``mixes``/``wavs``/``irs``/... -- see :data:`_GENERIC_PACK_DIRS`)
    AND a grandparent exists, the grandparent (the real pack dir) names the
    subdir instead. This keeps every pack's WAVs grouped under their own dir
    rather than colliding in a shared ``mixes/`` (design §5.3). Falls back to
    ``"unknown"`` when the chosen name slugifies to empty."""
    src = Path(src)
    parent = src.parent
    name = parent.name
    if name.lower() in _GENERIC_PACK_DIRS and parent.parent.name:
        name = parent.parent.name
    return naming.slugify(name) or "unknown"


# ---------------------------------------------------------------------------
# scaffold + (de)serialization I/O
# ---------------------------------------------------------------------------


def scaffold(wav_in_library: Path, irhash: str, *,
             imported_from: Optional[str] = None) -> IrMeta:
    """Build a fresh :class:`IrMeta` for a WAV already placed in the library.

    Only the cheap, filename-derivable fields are populated: ``wav``
    (library-relative), ``irhash``, ``imported_from``, and ``mix`` (guessed
    from a ``Mix NN`` filename token). Everything else is ``None``/empty for a
    skill to enrich (provenance from the pack manual, character ``tags``, and
    the optional ``measured`` FFT pass — none of which core computes)."""
    wav_in_library = Path(wav_in_library)
    return IrMeta(
        irhash=irhash,
        wav=_to_library_relative(wav_in_library),
        imported_from=imported_from,
        mix=_guess_mix(wav_in_library.name),
    )


def _write_meta_atomic(m: IrMeta, path: Path) -> None:
    """Write ``m`` to ``path`` atomically (temp file + ``os.replace``). No
    git commit — bulk callers commit once at the end."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(_meta_to_dict(m), indent=2))
    os.replace(tmp, path)


def save_ir_meta(m: IrMeta, path: Path) -> IrMeta:
    """Persist ``m`` to ``path`` atomically, then advisory-commit the home.

    - ``libinit.ensure_initialized()`` first (mkdir + git-init the home).
    - Atomic temp-file + ``os.replace`` write.
    - ``gitops.auto_commit`` afterward — but ONLY when ``path`` resolves under
      ``home.helixgen_home()`` (mirrors ``tone_meta.save_tone_meta`` /
      ``guitars.save_profile``; advisory, never raises). When
      ``$HELIXGEN_IRS`` points outside the home the commit is skipped so an
      unrelated repo is never swept up."""
    libinit.ensure_initialized()
    path = Path(path)
    _write_meta_atomic(m, path)
    home_dir = home.helixgen_home()
    if _is_under(path, home_dir):
        gitops.auto_commit(home_dir, f"helixgen: update IR metadata ({m.irhash})")
    return m


def load_ir_meta(path: Path) -> IrMeta:
    """Load one IR sidecar JSON. Raises on a missing/unreadable/invalid file."""
    return _meta_from_dict(json.loads(Path(path).read_text()))


def load_all_ir_metas() -> List[IrMeta]:
    """Every IR sidecar under ``library_irs_dir()`` (recursively), sorted by
    path. ``mapping.json`` and any ``*.tmp`` are excluded; files that fail to
    parse are skipped (tolerated, not fatal — same as ``tone_meta``)."""
    root = home.library_irs_dir()
    if not root.is_dir():
        return []
    metas: List[IrMeta] = []
    for p in sorted(root.rglob("*.json")):
        if p.name == "mapping.json":
            continue
        try:
            metas.append(load_ir_meta(p))
        except (OSError, ValueError):
            continue
    return metas


# ---------------------------------------------------------------------------
# import_wav — copy a source WAV into the library + scaffold its sidecar
# ---------------------------------------------------------------------------


def _content_matches(existing: Path, src: Path) -> bool:
    try:
        return existing.read_bytes() == src.read_bytes()
    except OSError:
        return False


def _choose_dest(lib: Path, pack_dir: str, src: Path, irhash: str) -> Path:
    """Pick the library WAV destination for ``src`` under ``lib/pack_dir``.

    Normally ``<pack_dir>/<basename>``. If that natural dest already exists and
    is NOT byte-identical to ``src`` (two packs slugging to the same dir with a
    shared basename but different content), disambiguate with an 8-hex irhash
    prefix so two distinct IRs never collapse onto one file (mirrors
    ``migrate._choose_ir_dest``). Two distinct IRs sharing a basename AND the
    8-hex prefix is a ~2^-32 accident (backlog #79f); if even the prefixed
    dest exists with different content, fall back to the FULL irhash in the
    filename -- unique per content by construction -- so, given a real
    (non-empty) ``irhash`` as every register path supplies, the chosen dest
    is always either absent or byte-identical to ``src``, never silently
    aliased. (A falsy ``irhash`` degrades to the shared ``-ir`` suffix and
    keeps only the natural-dest guarantee.)"""
    natural = lib / pack_dir / src.name
    if not natural.exists() or _content_matches(natural, src):
        return natural
    prefix = (irhash or "")[:8] or "ir"
    dis = lib / pack_dir / f"{src.stem}-{prefix}{src.suffix}"
    if not dis.exists() or _content_matches(dis, src):
        return dis
    return lib / pack_dir / f"{src.stem}-{irhash or 'ir'}{src.suffix}"


def import_wav(src: Path, irhash: str, *,
               pack: Optional[str] = None) -> Tuple[Path, Path]:
    """Copy ``src`` into the library and scaffold its sidecar; return
    ``(wav_path, meta_path)``.

    - Destination is ``library_irs_dir()/<pack or derive_pack(src)>/
      <src.name>`` (basename collisions across packs disambiguated by irhash
      prefix — see :func:`_choose_dest`; see :func:`derive_pack` for the
      ``<Pack>/Mixes/`` grandparent rule). An identical file already at the
      destination is NOT re-copied.
    - When ``src`` is ALREADY under ``library_irs_dir()`` it is left in place
      (no copy, ``imported_from`` recorded as ``None`` — it originated here).
    - The sidecar is (re)scaffolded only when absent, written atomically
      WITHOUT a git commit (the caller commits once). ``irhash`` is recorded
      as given — ``import_wav`` never (re)hashes.
    """
    src = Path(src)
    lib = home.library_irs_dir()

    if _is_under(src, lib):
        dest = src
        imported_from = None
    else:
        pack_dir = pack or derive_pack(src)
        dest = _choose_dest(lib, pack_dir, src, irhash)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)  # COPY, never move — paid packs stay in place
        imported_from = str(src.resolve())

    meta_path = meta_path_for(dest)
    if not meta_path.exists():
        _write_meta_atomic(scaffold(dest, irhash, imported_from=imported_from),
                           meta_path)
    return dest, meta_path


# ---------------------------------------------------------------------------
# ir-backfill — scaffold metadata for mapped IRs lacking a library copy/sidecar
# ---------------------------------------------------------------------------


def backfill(mapping: IrMapping) -> Dict[str, List[Any]]:
    """For every mapping entry whose WAV is outside ``library_irs_dir()`` or
    lacks a sidecar: copy it in (``import_wav``) and rewrite the mapping value
    to the library copy. Idempotent — an entry already in-library WITH a
    sidecar is skipped, so a re-run is all skips and never re-copies.

    Mutates ``mapping.entries`` in place; the caller saves + commits. Returns a
    ``{"backfilled": [...hashes], "skipped": [...], "errors": [...]}`` summary.
    """
    lib = home.library_irs_dir()
    result: Dict[str, List[Any]] = {"backfilled": [], "skipped": [], "errors": []}
    for h in list(mapping.entries):
        try:
            wav_abs = mapping.resolve_by_hash(h)
        except IrMappingError as exc:
            result["errors"].append({"hash": h, "error": str(exc)})
            continue
        in_lib = _is_under(wav_abs, lib)
        if in_lib and meta_path_for(wav_abs).exists():
            result["skipped"].append(h)
            continue
        if not wav_abs.exists():
            result["errors"].append({"hash": h, "error": f"wav missing: {wav_abs}"})
            continue
        try:
            dest, _ = import_wav(wav_abs, h)
        except OSError as exc:
            result["errors"].append({"hash": h, "error": str(exc)})
            continue
        mapping.entries[h] = mapping._canonical(dest)
        result["backfilled"].append(h)
    return result


# ---------------------------------------------------------------------------
# validation (for `library validate`)
# ---------------------------------------------------------------------------


def validate_ir_metas(metas: List[IrMeta],
                      mapping: IrMapping) -> Tuple[List[str], List[str]]:
    """Cross-check every IR sidecar; return ``(problems, warnings)``.

    Problems (exit-1 in ``library validate``): the sidecar's ``irhash`` is not
    registered in ``mapping.json``; the sidecar's ``wav`` file does not exist
    under ``library_dir()``. Warnings (never fail): any ``tag`` outside
    :data:`CONTROLLED_TAGS`."""
    problems: List[str] = []
    warnings: List[str] = []
    lib = home.library_dir()
    for m in metas:
        label = (m.irhash or "?")[:8]
        if not m.irhash or m.irhash not in mapping.entries:
            problems.append(f"IR {label}: irhash not registered in mapping.json")
        if not m.wav or not (lib / m.wav).exists():
            problems.append(f"IR {label}: wav file not found: {m.wav}")
        for t in m.tags:
            if t.casefold() not in _CONTROLLED_TAGS_CF:
                warnings.append(
                    f"IR {label}: tag {t!r} is not in the controlled vocabulary")
    return problems, warnings
