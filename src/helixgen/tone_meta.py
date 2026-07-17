"""Per-tone JSON metadata: ``library/tones/<logical-slug>.json`` (design §5.1).

A **logical tone** (identified by artist+song, or by a descriptor -- see
``naming.py``) owns exactly one metadata JSON, keyed by its ``logical_slug``.
That JSON folds in what used to be a companion ``.md`` sidecar
(``description_md`` -- no more sidecar file, no sidecar path) and carries one
or more **variants**, each targeting a specific guitar (or the special key
``"generic"`` for a guitar-agnostic tone)::

    {"schema": 1,
     "artist": "Foo Fighters", "song": "White Limo", "descriptor": null,
     "tags": ["hard rock", "lead"],
     "description_md": "…the full companion markdown, folded in…",
     "variants": {
         "gibson-les-paul-junior": {
             "hsp": "tones/foo-fighters-white-limo-les-paul-jr.hsp",
             "preset_name": "Foo Fighters - White Limo - Les Paul Jr",
             "guitar_settings": {"pickup": "bridge", "tone": "7"},
             "notes_md": null}},
     "created": "2026-07-15", "updated": "2026-07-15"}

**hsp-path convention:** ``Variant.hsp`` is stored as a string **relative to
the library root** (``home.library_dir()``), matching the design spec's own
example (``"tones/foo-fighters-....hsp"`` -- note the ``tones/`` prefix is
already part of the stored string, it is NOT relative to ``tones_dir()``
itself). ``validate_tone_meta`` takes a ``tones_dir`` argument (per the task
interface) but resolves each variant against ``tones_dir.parent`` (==
``home.library_dir()``), so the two conventions agree: ``resolved =
library_dir() / variant.hsp``.

Every write goes through :func:`save_tone_meta`, which -- like
``device/manifest.py``'s ``SetlistManifest.save`` -- calls
``libinit.ensure_initialized()`` first, writes atomically (temp file +
``os.replace``), and then advisory-commits via ``gitops.auto_commit`` (never
raises; gated by the ``git_commit_tones`` preference) -- but only when the
written file resolves under ``home.helixgen_home()``, same guard shape as
``SetlistManifest.save``.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from helixgen import gitops, home, libinit, naming

if TYPE_CHECKING:  # avoid a runtime import cycle; only for type hints
    from helixgen.guitars import GuitarProfile


@dataclass
class Variant:
    """One guitar-targeted realization of a logical tone.

    ``normalized`` is an OPTIONAL record written by ``device normalize
    --yes`` when this variant's ``.hsp`` is the file it wrote trims into --
    proof the tone has been level-matched, and the FULL per-target
    measurement telemetry of the run::

        {"at": "2026-07-16T12:00:00-07:00",   # ISO timestamp of the run
         "scope": "snapshots",                 # or "setlist"
         "target_total_db": 27.96,             # the run's loudness target
         "tolerance_db": 1.0,                  # the run's dead band
         "seconds": 20.0,                      # per-target window (--seconds)
         "helixgen_version": "0.26.0",
         "targets": [                          # one per measured target,
             {"snapshot": 0, "name": "Rhythm", # exactly as normalize --json
              "ok": True, "reason": None,      # reports them ("tone"/"path"
              "gain_db": 27.96,                # keys in setlist scope)
              "output_db": -6.0,               # chain-out dBFS: > 0 means
              "playing_seconds": 5.2,          #   in-chain CLIPPING
              "output_level_db": 0.0,          # output level in force
              "total_db": 27.96,               # gain + level (what's matched)
              "trim_db": 0.0,
              "applied": False}]}

    The telemetry is the valuable part, not just the trims -- ``output_db``
    (chain-out dBFS) flags in-chain clipping, which agents use to drive
    gain-staging fixes. ``targets`` entries are OPEN dicts: the serializers
    deep-copy the whole record verbatim, so unknown per-target keys (future
    per-node stats) round-trip without a schema change.

    Latest run wins (overwrite, never append); in-band zero trims still
    count -- they confirm the tone measures level-matched. The field is a
    plain optional dict and the schema stays 1: an older reader's
    ``_variant_from_dict`` simply drops the unknown key (and would drop it
    on a re-save -- acceptable, the record is re-creatable by re-running
    normalize), so no version fence is needed for a purely additive,
    advisory field.
    """

    hsp: str
    preset_name: str
    guitar_settings: Dict[str, str] = field(default_factory=dict)
    notes_md: Optional[str] = None
    normalized: Optional[Dict[str, Any]] = None


@dataclass
class ToneMeta:
    """A logical tone's metadata: identity, tags, description, and variants."""

    artist: Optional[str]
    song: Optional[str]
    descriptor: Optional[str]
    tags: List[str]
    description_md: Optional[str]
    variants: Dict[str, Variant]
    created: str
    updated: str
    schema: int = 1

    @property
    def logical_slug(self) -> str:
        """The metadata JSON filename stem (no guitar segment)."""
        return naming.logical_slug(artist=self.artist, song=self.song,
                                    descriptor=self.descriptor)

    @property
    def display_base(self) -> str:
        """``"Artist - Song"`` or the descriptor -- no guitar segment."""
        return naming.display_name(artist=self.artist, song=self.song,
                                    descriptor=self.descriptor, guitar_short=None)


# ---------------------------------------------------------------------------
# (de)serialization
# ---------------------------------------------------------------------------


def _variant_to_dict(v: Variant) -> Dict[str, Any]:
    return {
        "hsp": v.hsp,
        "preset_name": v.preset_name,
        "guitar_settings": dict(v.guitar_settings),
        "notes_md": v.notes_md,
        # deep copy: the record's nested target entries are OPEN dicts and
        # must serialize verbatim (unknown per-target keys included)
        "normalized": copy.deepcopy(v.normalized) if v.normalized else None,
    }


def _variant_from_dict(d: Dict[str, Any]) -> Variant:
    normalized = d.get("normalized")
    return Variant(
        hsp=d["hsp"],
        preset_name=d["preset_name"],
        guitar_settings=dict(d.get("guitar_settings") or {}),
        notes_md=d.get("notes_md"),
        normalized=(copy.deepcopy(normalized)
                    if isinstance(normalized, dict) else None),
    )


def _meta_to_dict(meta: ToneMeta) -> Dict[str, Any]:
    return {
        "schema": meta.schema,
        "artist": meta.artist,
        "song": meta.song,
        "descriptor": meta.descriptor,
        "tags": list(meta.tags),
        "description_md": meta.description_md,
        "variants": {k: _variant_to_dict(v) for k, v in meta.variants.items()},
        "created": meta.created,
        "updated": meta.updated,
    }


def _meta_from_dict(d: Dict[str, Any]) -> ToneMeta:
    return ToneMeta(
        artist=d.get("artist"),
        song=d.get("song"),
        descriptor=d.get("descriptor"),
        tags=list(d.get("tags") or []),
        description_md=d.get("description_md"),
        variants={k: _variant_from_dict(v) for k, v in (d.get("variants") or {}).items()},
        created=d.get("created"),
        updated=d.get("updated"),
        schema=d.get("schema", 1),
    )


def _to_library_relative(hsp_path: Path | str) -> str:
    """Normalize ``hsp_path`` to a library-relative POSIX-style string.

    An absolute path under ``home.library_dir()`` is relativized; anything
    else (already relative, or absolute-but-elsewhere) is stored as given.
    """
    p = Path(hsp_path)
    if p.is_absolute():
        try:
            p = p.relative_to(home.library_dir())
        except ValueError:
            return str(p)
    return str(p).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# paths + load/save
# ---------------------------------------------------------------------------


def meta_path(slug: str) -> Path:
    """Where a logical tone's metadata JSON lives: ``tones_dir()/f"{slug}.json"``."""
    return home.tones_dir() / f"{slug}.json"


def load_tone_meta(slug: str) -> ToneMeta:
    """Load the metadata for logical tone ``slug``. Raises if it doesn't exist."""
    data = json.loads(meta_path(slug).read_text())
    return _meta_from_dict(data)


def load_all_tone_metas() -> List[ToneMeta]:
    """Every tone metadata JSON under ``tones_dir()``. Empty list if the
    directory doesn't exist yet, or is empty. Files that fail to parse are
    skipped (tolerated, not fatal)."""
    d = home.tones_dir()
    if not d.is_dir():
        return []
    metas: List[ToneMeta] = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        metas.append(_meta_from_dict(data))
    return metas


def find_variant_by_hsp(hsp_path: Path | str) -> Optional[tuple[ToneMeta, str]]:
    """The ``(meta, variant_key)`` whose variant ``.hsp`` is ``hsp_path``,
    or ``None`` when the path is not a registered library variant.

    Each stored ``Variant.hsp`` is resolved per the module's hsp-path
    convention -- a relative string against ``home.library_dir()``, an
    absolute string verbatim -- and compared to ``hsp_path`` with both sides
    fully resolved (symlinks/tmp-dir aliases included). First match wins
    (a ``.hsp`` belongs to at most one variant in a well-formed library).
    """
    try:
        target = Path(hsp_path).resolve()
    except OSError:
        return None
    library_root = home.library_dir()
    for meta in load_all_tone_metas():
        for key, variant in meta.variants.items():
            p = Path(variant.hsp)
            if not p.is_absolute():
                p = library_root / p
            try:
                if p.resolve() == target:
                    return meta, key
            except OSError:
                continue
    return None


def save_tone_meta(meta: ToneMeta) -> ToneMeta:
    """Persist ``meta`` atomically under ``tones_dir()``, then advisory-commit.

    - ``libinit.ensure_initialized()`` first (mkdir + git-init the home).
    - ``created`` is set once: if a file already exists at this slug's path,
      its on-disk ``created`` wins (preserved across re-saves) regardless of
      what the in-memory object carries; otherwise ``meta.created`` (or
      today, if falsy) is used.
    - ``updated`` is always bumped to today.
    - Written via temp file + ``os.replace`` (atomic).
    - ``gitops.auto_commit`` afterward -- but ONLY when the written path
      resolves under ``home.helixgen_home()`` (mirrors
      ``device/manifest.py``'s ``SetlistManifest.save`` guard); when
      ``$HELIXGEN_LIBRARY`` points somewhere else entirely, the commit is
      skipped so an unrelated home repo never gets swept up. Advisory; never
      raises.

    Mutates and returns ``meta`` (its ``created``/``updated`` reflect what
    was actually written).
    """
    libinit.ensure_initialized()

    path = meta_path(meta.logical_slug)
    today = date.today().isoformat()

    created = meta.created or today
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            created = existing.get("created") or created
        except (OSError, ValueError):
            pass

    meta.created = created
    meta.updated = today

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(_meta_to_dict(meta), indent=2))
    os.replace(tmp, path)

    home_dir = home.helixgen_home()
    try:
        under_home = path.resolve().is_relative_to(home_dir.resolve())
    except OSError:
        under_home = False
    if under_home:
        gitops.auto_commit(home_dir,
                            f"helixgen: update tone metadata ({meta.logical_slug})")
    return meta


# ---------------------------------------------------------------------------
# upsert_variant
# ---------------------------------------------------------------------------


def upsert_variant(
    meta: Optional[ToneMeta],
    *,
    artist: Optional[str] = None,
    song: Optional[str] = None,
    descriptor: Optional[str] = None,
    guitar_slug: Optional[str],
    guitar_short: Optional[str],
    hsp_path: Path | str,
    tags: Optional[Iterable[str]] = None,
) -> ToneMeta:
    """Create ``meta`` if absent, then add/replace one variant in place.

    - ``guitar_slug`` and ``guitar_short`` must travel together: both
      ``None``/blank (generic) or both set. Exactly one set raises
      ``ValueError`` -- a variant key without a matching display short name
      (or vice versa) is self-contradictory metadata.
    - Variant key is ``guitar_slug`` or ``"generic"`` when ``guitar_slug`` is
      ``None`` (guitar-agnostic tone; ``preset_name`` then omits the guitar
      segment, since ``guitar_short`` is also ``None`` in that case).
    - ``preset_name`` is computed via ``naming.display_name`` (raises
      ``ValueError`` unless exactly one of (``artist``+``song``) or
      ``descriptor`` is given -- same identity rule as the rest of the
      naming schema).
    - A new ``ToneMeta`` is created with ``created``/``updated`` = today and
      ``tags`` = the given tags; an existing ``meta`` has any new tags
      merged in (order-preserving, de-duplicated) and is otherwise left
      alone (mutated in place and returned).
    """
    has_slug = not naming._is_blank(guitar_slug)
    has_short = not naming._is_blank(guitar_short)
    if has_slug != has_short:
        raise ValueError(
            "guitar_slug and guitar_short must be provided together "
            "(both set or both absent)"
        )

    today = date.today().isoformat()
    key = guitar_slug if guitar_slug is not None else "generic"
    preset_name = naming.display_name(
        artist=artist, song=song, descriptor=descriptor, guitar_short=guitar_short
    )
    variant = Variant(hsp=_to_library_relative(hsp_path), preset_name=preset_name)

    tag_list = list(tags or [])

    if meta is None:
        meta = ToneMeta(
            artist=artist, song=song, descriptor=descriptor,
            tags=tag_list, description_md=None,
            variants={}, created=today, updated=today, schema=1,
        )
    else:
        for t in tag_list:
            if t not in meta.tags:
                meta.tags.append(t)

    meta.variants[key] = variant
    return meta


# ---------------------------------------------------------------------------
# validate_tone_meta
# ---------------------------------------------------------------------------


def _identity_problems(meta: ToneMeta) -> List[str]:
    has_artist = not naming._is_blank(meta.artist)
    has_song = not naming._is_blank(meta.song)
    has_descriptor = not naming._is_blank(meta.descriptor)
    has_song_side = has_artist or has_song
    problems: List[str] = []
    if has_song_side and has_descriptor:
        problems.append("both song and descriptor are set; exactly one tone identity is allowed")
    if not has_song_side and not has_descriptor:
        problems.append("neither song nor descriptor is set; exactly one tone identity is required")
    if has_artist != has_song:
        problems.append("artist and song must both be set or both be empty")
    return problems


def validate_tone_meta(
    meta: ToneMeta,
    *,
    tones_dir: Path,
    manifest: Any,
    guitar_slugs: Iterable[str],
) -> List[str]:
    """Shape/cross-link checks on ``meta``; returns a list of problem strings
    (empty when fully valid).

    - Exactly one of song/descriptor identity (see ``_identity_problems``);
      a blank/whitespace-only string counts as absent, matching ``naming``'s
      blank rule.
    - ``meta.schema`` must be ``1`` (the only supported schema version).
    - Each variant's ``hsp`` must exist on disk, resolved as
      ``tones_dir.parent / variant.hsp`` (== ``library_dir() / variant.hsp``
      -- see the module docstring's hsp-path convention).
    - Each variant key must be in ``guitar_slugs`` or the special
      ``"generic"`` key.
    - Each variant's ``preset_name`` must be registered in ``manifest.tones``
      (a ``SetlistManifest``).

    Unknown-control (``guitar_settings``) warnings are Task 12's job (guitar
    profiles don't exist yet in this PR) -- not checked here.
    """
    problems = _identity_problems(meta)
    if meta.schema != 1:
        problems.append(f"schema {meta.schema!r} is not supported; expected 1")
    allowed_keys = set(guitar_slugs) | {"generic"}
    library_root = Path(tones_dir).parent

    for key, variant in meta.variants.items():
        if key not in allowed_keys:
            problems.append(
                f"variant {key!r}: not a known guitar slug (or 'generic')"
            )
        resolved = library_root / variant.hsp
        if not resolved.exists():
            problems.append(f"variant {key!r}: hsp file not found: {resolved}")
        if variant.preset_name not in manifest.tones:
            problems.append(
                f"variant {key!r}: preset_name {variant.preset_name!r} "
                "not registered in the manifest"
            )
    return problems


# ---------------------------------------------------------------------------
# guitar_settings_warnings (a SEPARATE, non-fatal channel from validate_tone_meta)
# ---------------------------------------------------------------------------


def guitar_settings_warnings(
    meta: ToneMeta,
    *,
    guitar_profiles: "Dict[str, GuitarProfile] | None" = None,
) -> List[str]:
    """Warnings (NOT errors) for ``guitar_settings`` keys that aren't controls
    on the variant's target guitar profile (design §5.1/§8).

    - ``guitar_profiles`` maps a guitar slug -> its ``GuitarProfile``. When a
      variant's key has NO profile in the map (the profile may lag the tone --
      design §8), that variant is skipped silently: no profile, no warning.
    - For a variant whose key DOES have a profile, each ``guitar_settings`` key
      not matching one of the profile's control ``name``s yields one warning.
    - This is deliberately a sibling of :func:`validate_tone_meta` (which stays
      ``List[str]`` errors): warnings never fail ``library validate``.
    """
    warnings: List[str] = []
    if not guitar_profiles:
        return warnings
    for key, variant in meta.variants.items():
        profile = guitar_profiles.get(key)
        if profile is None:
            continue
        # Case-insensitive: the rest of the guitar surface matches labels
        # case-folded (see guitars.find_profile), so a "Volume" setting key
        # against a "volume" control must NOT warn.
        control_names = {c.name.casefold() for c in profile.controls}
        for setting_key in variant.guitar_settings:
            if setting_key.casefold() not in control_names:
                warnings.append(
                    f"variant {key!r}: guitar_settings key {setting_key!r} is not "
                    f"a control on the {profile.short_name!r} guitar profile"
                )
    return warnings
