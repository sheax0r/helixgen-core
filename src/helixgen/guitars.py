"""Guitar profiles: ``library/guitars/<slug>.json`` (design §5.2).

A **guitar profile** is the single source of truth for one guitar the user
owns -- replacing the old ``preferences.instruments`` array. It carries the
guitar's identity (``name`` / ``short_name``), its tonal ``character_md`` (what
the guitar is *for* -- read by the tone skill to adapt params), and a
**control inventory** (named knobs/switches with a ``kind``) that
``guitar_settings`` keys on a tone variant validate against::

    {"schema": 1,
     "name": "Gibson Les Paul Junior", "short_name": "Les Paul Jr",
     "type": "guitar", "active": false,
     "pickups": "one bridge P-90 (soapbar single-coil)",
     "construction": null,
     "character_md": "P-90 grind; raw rock rhythm; brighter than a humbucker LP...",
     "genres": ["punk", "garage", "raw rock", "blues"],
     "controls": [
         {"name": "volume", "kind": "knob"},
         {"name": "tone", "kind": "knob", "notes": "no coil split"}]}

Profiles live at ``home.guitars_dir()/<slug>.json`` (== ``library/guitars/``);
``slug`` is ``naming.slugify(name)``. ``short_name`` is what appears in preset
display names / filename slugs.

Every write goes through :func:`save_profile`, which -- like
``tone_meta.save_tone_meta`` -- calls ``libinit.ensure_initialized()`` first,
writes atomically (temp file + ``os.replace``), and then advisory-commits via
``gitops.auto_commit`` (never raises; gated by ``git_commit_tones``) -- but only
when the written file resolves under ``home.helixgen_home()``.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from helixgen import gitops, home, libinit, naming

_CONTROL_KINDS = ("knob", "switch", "push-pull", "other")

# --- pickup classification (read by `library fork`'s adaptation checklist) ---
# Free-text `pickups` prose plus the first-class `active` flag, reduced to the
# only distinction the tone skill actually adapts to: output level (active vs
# passive) and coil type (humbucker vs single-coil).
_ACTIVE_WORDS = ("active", "emg", "blackout", "fluence", "battery", "preamp")
_HUMBUCKER_WORDS = ("humbucker", "hum-bucker", "bucker", "paf", "emg",
                    "blackout", "mini-hum")
_SINGLE_WORDS = ("single", "p-90", "p90", "soapbar", "lipstick", "strat",
                 "tele", "jazzmaster", "gold foil")
# A pickup CONFIG code -- "HH", "HSS", "SSS", "HSH" -- as a standalone token.
_CONFIG_CODE = re.compile(r"\b([hs]{2,3})\b")

# " - " / " -- " / " – " / " — " with surrounding whitespace: the separator the
# display-name schema owns (`naming.display_name`). A descriptor carrying one
# is an identity the author meant to put in --artist/--song/--guitar.
_SMUGGLED_SEPARATOR = re.compile(r"\s+[-–—]{1,2}\s+")


@dataclass
class Control:
    """One named control on a guitar (a knob, switch, push-pull, ...)."""

    name: str
    kind: str  # one of _CONTROL_KINDS
    positions: Optional[List[str]] = None
    notes: Optional[str] = None


@dataclass
class GuitarProfile:
    """A guitar's identity, tonal character, and control inventory (design §5.2)."""

    name: str
    short_name: str
    type: str
    active: Optional[bool]
    pickups: Optional[str]
    construction: Optional[str]
    character_md: Optional[str]
    genres: List[str] = field(default_factory=list)
    controls: List[Control] = field(default_factory=list)
    schema: int = 1

    @property
    def slug(self) -> str:
        """The profile filename stem: ``naming.slugify(self.name)``."""
        return naming.slugify(self.name)


# ---------------------------------------------------------------------------
# (de)serialization
# ---------------------------------------------------------------------------


def _control_to_dict(c: Control) -> Dict[str, Any]:
    return {
        "name": c.name,
        "kind": c.kind,
        "positions": list(c.positions) if c.positions is not None else None,
        "notes": c.notes,
    }


def _control_from_dict(d: Dict[str, Any]) -> Control:
    positions = d.get("positions")
    return Control(
        name=d["name"],
        kind=d.get("kind") or "other",
        positions=list(positions) if positions is not None else None,
        notes=d.get("notes"),
    )


def _profile_to_dict(p: GuitarProfile) -> Dict[str, Any]:
    return {
        "schema": p.schema,
        "name": p.name,
        "short_name": p.short_name,
        "type": p.type,
        "active": p.active,
        "pickups": p.pickups,
        "construction": p.construction,
        "character_md": p.character_md,
        "genres": list(p.genres),
        "controls": [_control_to_dict(c) for c in p.controls],
    }


def _profile_from_dict(d: Dict[str, Any]) -> GuitarProfile:
    name = d["name"]
    return GuitarProfile(
        name=name,
        short_name=d.get("short_name") or name,
        type=d.get("type") or "guitar",
        active=d.get("active"),
        pickups=d.get("pickups"),
        construction=d.get("construction"),
        character_md=d.get("character_md"),
        genres=list(d.get("genres") or []),
        controls=[_control_from_dict(c) for c in (d.get("controls") or [])],
        schema=d.get("schema", 1),
    )


# ---------------------------------------------------------------------------
# paths + load/save
# ---------------------------------------------------------------------------


def profile_path(slug: str) -> Path:
    """Where a guitar profile lives: ``guitars_dir()/f"{slug}.json"``."""
    return home.guitars_dir() / f"{slug}.json"


def load_profile(slug: str) -> GuitarProfile:
    """Load the profile for guitar ``slug``. Raises if it doesn't exist."""
    data = json.loads(profile_path(slug).read_text())
    return _profile_from_dict(data)


def load_all_profiles() -> List[GuitarProfile]:
    """Every guitar profile under ``guitars_dir()``.

    Empty list if the directory doesn't exist yet, or is empty. Files that
    fail to parse are skipped (tolerated, not fatal), matching
    ``tone_meta.load_all_tone_metas``.
    """
    d = home.guitars_dir()
    if not d.is_dir():
        return []
    profiles: List[GuitarProfile] = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        try:
            profiles.append(_profile_from_dict(data))
        except (KeyError, TypeError):
            continue
    return profiles


class AmbiguousGuitarError(ValueError):
    """A guitar label matched two or more DISTINCT profiles.

    Mirrors how tone resolution (``cli_library._resolve_slug``) refuses to
    silently pick one of several matches -- resolving an ambiguous guitar
    silently would bake the wrong guitar into a preset's slug/display name.
    Carries the colliding profiles' (unique) ``slugs`` so the caller can tell
    the user exactly what to disambiguate with.
    """

    def __init__(self, label: str, slugs: List[str]) -> None:
        self.label = label
        self.slugs = list(slugs)
        super().__init__(
            f"guitar {label!r} is ambiguous: it matches {len(self.slugs)} "
            f"distinct guitar profiles ({', '.join(self.slugs)}) -- "
            "disambiguate by using the exact guitar slug"
        )


def find_profile(label: str) -> Optional[GuitarProfile]:
    """Resolve ``label`` to a profile by ``slug`` / ``name`` / ``short_name``
    (case-insensitive), most-specific tier first. Returns ``None`` when nothing
    matches; raises :class:`AmbiguousGuitarError` when the label matches 2+
    DISTINCT profiles within a tier.

    Resolution tiers (a match in an earlier tier wins outright -- "most specific
    wins" -- so an exact ``slug``, which is unique by construction (it is the
    filename stem), always resolves deterministically even when short_names
    collide):

    1. exact ``slug`` -- unique, so at most one profile can match;
    2. exact ``name`` (stripped + lowercased);
    3. exact ``short_name`` (stripped + lowercased).

    In tiers 2/3, 2+ DISTINCT profiles (deduped by their unique slug) matching
    the label is ambiguous and raises. A blank/``None`` label never matches.
    """
    return find_profile_in(label, load_all_profiles())


def find_profile_in(
    label: str, profiles: List[GuitarProfile]
) -> Optional[GuitarProfile]:
    """Resolve ``label`` against an EXPLICIT ``profiles`` list (see
    :func:`find_profile` for the tiering + ambiguity contract).

    Split out so callers that need to resolve against a *hypothetical* profile
    set -- e.g. migration reconciling ``default_guitar`` against the profiles it
    is ABOUT to seed (dry-run: not yet on disk) -- reuse the exact same
    most-specific-wins logic without duplicating it.
    """
    if not label or not label.strip():
        return None
    target = label.strip().lower()

    # Tier 1: exact slug (already lowercase; unique by construction).
    for p in profiles:
        if p.slug == target:
            return p

    # Tiers 2/3: exact name, then short_name. Dedupe matches by slug so a single
    # profile matching a label two ways is never mistaken for two profiles.
    for attr in ("name", "short_name"):
        matches: Dict[str, GuitarProfile] = {
            p.slug: p
            for p in profiles
            if getattr(p, attr).strip().lower() == target
        }
        if len(matches) == 1:
            return next(iter(matches.values()))
        if len(matches) > 1:
            raise AmbiguousGuitarError(target, sorted(matches))
    return None


def save_profile(p: GuitarProfile) -> GuitarProfile:
    """Persist ``p`` atomically under ``guitars_dir()``, then advisory-commit.

    - ``libinit.ensure_initialized()`` first (mkdir + git-init the home).
    - Written via a per-process-unique temp file + ``os.replace`` (atomic;
      any failure before the replace removes the temp file -- same pattern
      as ``tone_meta.save_tone_meta``).
    - ``gitops.auto_commit`` afterward -- but ONLY when the written path
      resolves under ``home.helixgen_home()`` (mirrors
      ``tone_meta.save_tone_meta``); advisory, never raises.

    Returns ``p`` unchanged.
    """
    libinit.ensure_initialized()

    path = profile_path(p.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(_profile_to_dict(p), indent=2))
        os.replace(tmp, path)
    except BaseException:  # KeyboardInterrupt/SIGTERM must also clean up
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    home_dir = home.helixgen_home()
    try:
        under_home = path.resolve().is_relative_to(home_dir.resolve())
    except OSError:
        under_home = False
    if under_home:
        gitops.auto_commit(home_dir, f"helixgen: update guitar profile ({p.slug})")
    return p


# ---------------------------------------------------------------------------
# profile_from_instrument (migration seed)
# ---------------------------------------------------------------------------


def profile_from_instrument(d: Dict[str, Any]) -> GuitarProfile:
    """Seed a :class:`GuitarProfile` from a preferences ``instruments`` entry.

    ``d`` is the editable-plan instrument dict (the same shape
    ``preferences.Instrument.to_dict`` emits), which MAY carry an added
    ``short_name`` the migration plan's author set; absent -> ``short_name =
    name`` (YAGNI -- no last-two-words heuristic).

    Field mapping (prefs ``Instrument`` -> profile):

    - ``name`` -> ``name``
    - ``short_name`` (plan-added, optional) -> ``short_name`` (default: ``name``)
    - ``type`` -> ``type`` (default ``"guitar"``)
    - ``active`` -> ``active``
    - ``pickups`` -> ``pickups``
    - ``genres`` -> ``genres``
    - ``notes`` -> ``character_md`` (best-fit: freeform tonal prose)
    - ``selector`` -> synthesized ``Control(name="pickup selector",
      kind="switch", notes=<selector>)`` -- the selector has no first-class
      profile field, and the control inventory is exactly where a
      pickup-selector switch belongs (the user can rename/refine it later).
    - ``construction`` has no ``Instrument`` source -> ``None``.
    """
    name = d["name"]
    short_name = d.get("short_name") or name

    controls: List[Control] = []
    selector = d.get("selector")
    if selector:
        controls.append(
            Control(name="pickup selector", kind="switch", notes=str(selector))
        )

    return GuitarProfile(
        name=name,
        short_name=short_name,
        type=d.get("type") or "guitar",
        active=d.get("active"),
        pickups=d.get("pickups"),
        construction=None,
        character_md=d.get("notes"),
        genres=list(d.get("genres") or []),
        controls=controls,
    )


# ---------------------------------------------------------------------------
# pickup_class  (the data behind `library fork`'s adaptation checklist)
# ---------------------------------------------------------------------------


def pickup_class(p: Optional[GuitarProfile]) -> tuple[Optional[str], frozenset]:
    """``(output_level, coil_types)`` for one guitar -- the coarse pickup class
    the tone skill adapts a chain to.

    - ``output_level`` is ``"active"`` / ``"passive"`` from the profile's
      first-class ``active`` flag, falling back to active-pickup words in the
      free-text ``pickups`` prose (``"active EMG HH"``, Fishman Fluence, ...).
      ``None`` when neither says.
    - ``coil_types`` is a subset of ``{"humbucker", "single-coil"}``, read from
      the prose two ways: keywords (``humbucker``, ``P-90``, ``soapbar``, ...)
      and the standalone CONFIG CODE convention (``HH`` / ``HSS`` / ``HSH`` /
      ``SSS``), where each letter names one pickup. Empty when the prose says
      nothing recognizable.

    A profile with neither signal classifies as ``(None, frozenset())``, which
    is deliberately DIFFERENT from every populated class -- an unknown target
    must earn the adaptation checklist, not silently pass as a match.

    Heuristic by construction: ``pickups`` is free prose and always will be.
    Used only to decide whether to PRINT advice; nothing is ever re-voiced off
    it, so a miss costs a spurious (or missing) reminder, never a wrong param.
    """
    if p is None:
        return (None, frozenset())
    text = (p.pickups or "").lower()

    level: Optional[str]
    if p.active is True:
        level = "active"
    elif p.active is False:
        level = "passive"
    elif any(w in text for w in _ACTIVE_WORDS):
        level = "active"
    else:
        level = None

    coils = set()
    if any(w in text for w in _HUMBUCKER_WORDS):
        coils.add("humbucker")
    if any(w in text for w in _SINGLE_WORDS):
        coils.add("single-coil")
    for code in _CONFIG_CODE.findall(text):
        if "h" in code:
            coils.add("humbucker")
        if "s" in code:
            coils.add("single-coil")
    return (level, frozenset(coils))


def describe_pickups(p: Optional[GuitarProfile]) -> str:
    """One human phrase for a guitar's pickup class + its own prose, e.g.
    ``"active humbucker (active EMG HH)"`` -- the checklist's header line."""
    level, coils = pickup_class(p)
    parts = [x for x in (level, "/".join(sorted(coils)) or None) if x]
    label = " ".join(parts) or "unknown pickups"
    prose = (p.pickups or "").strip() if p is not None else ""
    return f"{label} ({prose})" if prose else label


# ---------------------------------------------------------------------------
# descriptor identity guard (shared by generate / library import / library fork)
# ---------------------------------------------------------------------------


def descriptor_identity_problem(descriptor: Optional[str]) -> Optional[str]:
    """A human error message when an EXPLICIT ``--descriptor`` has an identity
    smuggled into it, or ``None`` when it is a clean descriptor.

    The display-name schema is ``"$Descriptor - $Guitar"``, so the ``" - "``
    separator and the trailing guitar segment are the schema's, not the
    descriptor's. A descriptor that carries either produces a display name
    with a doubled separator, a guitar string that is not the profile's
    ``short_name``, and a logical slug that will NOT group with the tone it
    was forked from (which is exactly how six near-identical tones appear).

    Two checks:

    1. the descriptor contains a schema separator (``" - "`` / ``" -- "`` /
       en/em dash), or
    2. its slug ends in a known guitar profile's slug or ``short_name`` slug.

    Only ever applied to an EXPLICIT ``--descriptor`` flag. The implicit
    fallbacks -- a recipe's own ``name`` in ``generate``, an ``.hsp``'s
    ``meta.name`` in ``library import`` -- are NOT guarded: those are existing
    artifacts whose names legitimately contain dashes, and rejecting them
    would break authoring for a naming choice the caller did not just make.
    """
    if descriptor is None or not descriptor.strip():
        return None
    text = descriptor.strip()
    flags_hint = ("Put the song identity in --artist/--song and the guitar in "
                  "--guitar; --descriptor is the tone name ALONE.")
    if _SMUGGLED_SEPARATOR.search(text):
        return (f"--descriptor {descriptor!r} contains a ' - ' separator, which "
                "is the display-name schema's own (\"$Descriptor - $Guitar\"). "
                + flags_hint)

    slug = naming.slugify(text)
    try:
        profiles = load_all_profiles()
    except Exception:  # noqa: BLE001 -- a broken profile never blocks authoring
        profiles = []
    for prof in profiles:
        for tail in {prof.slug, naming.slugify(prof.short_name)}:
            if tail and slug.endswith(f"-{tail}"):
                return (f"--descriptor {descriptor!r} ends in the guitar "
                        f"{prof.short_name!r}, which the display name appends "
                        "itself. " + flags_hint)
    return None
