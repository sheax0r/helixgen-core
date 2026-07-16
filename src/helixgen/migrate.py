"""One-shot migration of a pre-library (v2-era) ``~/.helixgen`` into the tone
library layout, plus the shared per-tone placement used by ``library import``.

Design: ``docs/superpowers/specs/2026-07-15-library-metadata-design.md`` §2/§4/§8.

Two public entry points, kept CLI-free (they raise / return data; the
``cli_library`` verbs translate to click output):

- :func:`plan_migration` inspects the manifest + preferences + IR mapping and
  emits an **editable plan** dict (name inference only; never guesses beyond
  the documented rules).
- :func:`run_migration` executes a plan **idempotently and data-safely**:
  moving each tone's ``.hsp`` into ``tones_dir()`` under its new slug, rewriting
  ``meta.name``, folding a sibling ``.md`` into ``description_md``, building the
  ToneMeta JSON, re-keying the manifest, and **copying** each mapped IR WAV into
  ``library_irs_dir()/<pack>/`` (copy, never move — paid packs stay in place)
  with a scaffolded sidecar and a rewritten ``mapping.json``.

Design guarantees:

- **Idempotence.** Re-running on an already-migrated home is all skips: a tone
  whose plan path already resolves to its destination is skipped; an IR WAV
  already living under ``library_irs_dir()`` is skipped. No duplicate files, no
  manifest/mapping churn (the manifest and mapping are re-saved only when this
  run actually changed them).
- **Data safety.** A tone move is copy → byte-verify → remove-source (never a
  lossy move); a per-tone or per-IR failure is recorded in the summary and the
  run CONTINUES (it never aborts into an unreconcilable half-migrated state).
- **Slug collision.** Two distinct tones mapping to the same destination slug
  are recorded as a collision with a rename suggestion and NEITHER is moved —
  one ``.hsp`` never silently overwrites another.

Instruments are RECORDED in the plan and seeded into guitar profiles by
:func:`migrate_instruments` (Task 11): each entry becomes a
``library/guitars/<slug>.json`` and the retired ``instruments`` /
``preset_output_dir`` keys are stripped from ``preferences.json``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from helixgen import gitops, home, ir_meta, libinit, naming, tone_meta
from helixgen.hsp import read_hsp, write_hsp
from helixgen.ir import IrMapping
from helixgen.preferences import load_preferences
from helixgen.device.manifest import SetlistManifest

# " - " / " – " (en) / " — " (em), with surrounding whitespace.
_NAME_SEP = re.compile(r"\s+[-–—]\s+")


# ---------------------------------------------------------------------------
# identity inference (pure)
# ---------------------------------------------------------------------------


def _instrument_labels() -> Dict[str, str]:
    """Map each instrument's matchable label (name, and ``short_name`` if a
    later schema adds one) -> the canonical display short form, lowercased for
    case-insensitive lookup. Preference-load failures degrade to ``{}`` (no
    guitar inference) rather than aborting the plan."""
    try:
        prefs = load_preferences()
    except Exception:  # noqa: BLE001 - inference must never crash the plan
        return {}
    labels: Dict[str, str] = {}
    for inst in prefs.instruments:
        for cand in (inst.name, getattr(inst, "short_name", None)):
            if cand:
                labels[cand.strip().lower()] = inst.name
    return labels


def infer_identity(old_name: str, instrument_labels: Dict[str, str]) -> Dict[str, Any]:
    """Infer ``(artist, song, descriptor, guitar)`` from a legacy tone name.

    Rules (design §4, never guesses further):

    - Split on `` - `` / `` – `` / `` — ``. If the **trailing** segment
      case-insensitively matches an instrument label, it is the ``guitar`` and
      the remaining leading segments are the identity: exactly two -> artist +
      song; otherwise the whole remainder (re-joined with `` - ``) is the
      descriptor.
    - If the trailing segment is NOT a known instrument, the **whole** original
      name is the descriptor (never guess artist/song from a single unknown
      trailing token).
    """
    segments = [s for s in _NAME_SEP.split(old_name.strip()) if s != ""]
    guitar: Optional[str] = None
    artist = song = descriptor = None

    if len(segments) >= 2 and segments[-1].strip().lower() in instrument_labels:
        guitar = instrument_labels[segments[-1].strip().lower()]
        remainder = segments[:-1]
        if len(remainder) == 2:
            artist, song = remainder[0].strip(), remainder[1].strip()
        else:
            descriptor = " - ".join(remainder)
    else:
        descriptor = old_name

    return {"artist": artist, "song": song, "descriptor": descriptor, "guitar": guitar}


def _variant_names(identity: Dict[str, Any]) -> Dict[str, Any]:
    """Compute ``logical`` / ``new_slug`` / ``new_name`` for an identity.

    Returns those three plus the derived ``guitar_slug`` / ``guitar_short``.
    Raises ``ValueError`` (via ``naming``) on a self-contradictory identity.
    """
    artist, song, descriptor = identity["artist"], identity["song"], identity["descriptor"]
    guitar_short = identity["guitar"]
    guitar_slug = naming.slugify(guitar_short) if guitar_short else None
    if guitar_short and not guitar_slug:
        guitar_slug = None
        guitar_short = None
    logical = naming.logical_slug(artist=artist, song=song, descriptor=descriptor)
    new_slug = naming.variant_slug(logical, guitar_slug)
    new_name = naming.display_name(
        artist=artist, song=song, descriptor=descriptor, guitar_short=guitar_short)
    return {"logical": logical, "new_slug": new_slug, "new_name": new_name,
            "guitar_slug": guitar_slug, "guitar_short": guitar_short}


# ---------------------------------------------------------------------------
# plan_migration
# ---------------------------------------------------------------------------


def plan_migration() -> Dict[str, Any]:
    """Inspect the manifest + preferences + IR mapping and emit the editable plan.

    Shape::

        {"tones": [{"name": <old>, "path": <abs .hsp>, "artist", "song",
                    "descriptor", "guitar", "logical", "new_name", "new_slug"}],
         "instruments": [<prefs instrument dicts, recorded only>],
         "irs": [{"hash": <irhash>, "wav": <abs wav path>}]}

    Only tones with a backing ``.hsp`` path are planned (pathless device-origin
    tones have nothing to move). Inference is via :func:`infer_identity`; a name
    whose identity is self-contradictory is skipped from the tone list (it
    cannot be re-keyed) — such data is vanishingly rare and surfaces at run time
    only if edited back in.
    """
    labels = _instrument_labels()
    manifest = SetlistManifest.load()

    tones: List[Dict[str, Any]] = []
    for name, rec in manifest.tones.items():
        path = rec.get("path")
        if not path:
            continue
        identity = infer_identity(name, labels)
        try:
            derived = _variant_names(identity)
        except ValueError:
            continue
        tones.append({
            "name": name,
            "path": str(Path(path)),
            "artist": identity["artist"],
            "song": identity["song"],
            "descriptor": identity["descriptor"],
            "guitar": identity["guitar"],
            "logical": derived["logical"],
            "new_name": derived["new_name"],
            "new_slug": derived["new_slug"],
        })

    try:
        prefs = load_preferences()
        instruments = [i.to_dict() for i in prefs.instruments]
    except Exception:  # noqa: BLE001
        instruments = []

    irs: List[Dict[str, Any]] = []
    try:
        mapping = IrMapping.load()
        for h in mapping.entries:
            irs.append({"hash": h, "wav": str(mapping.resolve_by_hash(h))})
    except Exception:  # noqa: BLE001
        pass

    return {"tones": tones, "instruments": instruments, "irs": irs}


# ---------------------------------------------------------------------------
# shared per-tone placement (used by run_migration AND library import)
# ---------------------------------------------------------------------------


class ToneCollision(Exception):
    """The destination ``.hsp`` for a tone already exists (would overwrite)."""


def _is_under(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def _data_safe_place(src: Path, dest: Path, *, move: bool) -> None:
    """Copy ``src`` to ``dest``, byte-verify, then (if ``move``) remove ``src``.

    Never a lossy move: the source is only unlinked after the destination is
    confirmed byte-identical. ``dest`` must not already exist (callers check)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    if dest.read_bytes() != src.read_bytes():
        try:
            dest.unlink()
        except OSError:
            pass
        raise OSError(f"copy verification failed for {src} -> {dest}")
    if move:
        src.unlink()


def place_tone(
    src: Path,
    *,
    artist: Optional[str],
    song: Optional[str],
    descriptor: Optional[str],
    guitar_slug: Optional[str],
    guitar_short: Optional[str],
    new_name: str,
    logical: str,
    new_slug: str,
    move: bool,
    description_md: Optional[str],
    tags: Optional[List[str]] = None,
) -> Path:
    """Place one ``.hsp`` into the tone library and record its metadata.

    Moves (or copies) ``src`` to ``tones_dir()/<new_slug>.hsp``, rewrites its
    ``meta.name`` to ``new_name``, upserts the ToneMeta variant (folding
    ``description_md`` when given), and returns the destination path. Raises
    :class:`ToneCollision` if the destination already exists (never overwrites).
    The caller owns manifest registration + committing. Does not verify a
    logical-identity mismatch — callers that need generate's identity-equality
    guard (``library import``) enforce it before calling."""
    libinit.ensure_initialized()
    tones = home.tones_dir()
    tones.mkdir(parents=True, exist_ok=True)
    # Intentionally NOT ``.resolve()``d: the tone-metadata JSON stores the hsp
    # path relative to ``home.library_dir()`` (via ``upsert_variant`` ->
    # ``_to_library_relative``), which only relativizes cleanly when this path
    # shares the un-symlink-resolved library base (matches ``generate``).
    dest = tones / f"{new_slug}.hsp"
    if dest.exists():
        raise ToneCollision(str(dest))

    _data_safe_place(src, dest, move=move)

    body = read_hsp(dest)
    body.setdefault("meta", {})["name"] = new_name
    write_hsp(dest, body)

    existing = (tone_meta.load_tone_meta(logical)
                if tone_meta.meta_path(logical).exists() else None)
    meta = tone_meta.upsert_variant(
        existing, artist=artist, song=song, descriptor=descriptor,
        guitar_slug=guitar_slug, guitar_short=guitar_short, hsp_path=dest,
        tags=tags,
    )
    if description_md is not None:
        meta.description_md = description_md
    tone_meta.save_tone_meta(meta)
    return dest


def _rekey_manifest_tone(m: SetlistManifest, old_name: str, new_name: str,
                         dest: Path) -> None:
    """Re-key ``old_name`` -> ``new_name`` at ``dest`` in the manifest.

    Preserves ``slot`` + ``source`` (+ the ``auto_marked`` provenance flag),
    recomputes ``content_hash`` off the moved file, replaces any setlist
    membership referencing the old name, and drops the dangling old key."""
    old_rec = m.tones.get(old_name) or {}
    slot = old_rec.get("slot")
    source = old_rec.get("source") or "authored"
    auto_marked = old_rec.get("auto_marked")

    # Drop the stale entry (the tone was named the new style already OR the key
    # is changing) so ``register_tone`` -- which refuses to re-point an existing
    # name at a different path -- writes a fresh record at the moved location.
    m.tones.pop(old_name, None)
    if old_name != new_name:
        m.tones.pop(new_name, None)
        for rec in m.setlists_map.values():
            rec["tones"] = [new_name if t == old_name else t for t in rec.get("tones", [])]

    m.register_tone(dest, source=source)  # keys by dest's meta.name == new_name
    m.tones[new_name]["slot"] = slot
    if auto_marked:
        m.tones[new_name]["auto_marked"] = True


# ---------------------------------------------------------------------------
# run_migration
# ---------------------------------------------------------------------------


def _empty_summary(dry_run: bool) -> Dict[str, Any]:
    return {
        "dry_run": dry_run,
        "tones": {"moved": [], "skipped": [], "errors": [], "collisions": []},
        "irs": {"copied": [], "skipped": [], "errors": []},
        "instruments": None,
    }


# Keys `run_migration`/`_migrate_tones`/`_migrate_one_tone` read with `e[...]`
# (not `.get`) on every planned tone -- a hand-edited `--plan` missing any of
# them must fail with a clear error, not an uncaught KeyError deep in the run.
REQUIRED_TONE_KEYS = ("name", "path", "logical", "new_name", "new_slug")


class PlanError(ValueError):
    """A supplied (hand-edited) migration plan is malformed."""


def validate_plan(plan: Dict[str, Any]) -> None:
    """Validate a plan's per-tone entries before :func:`run_migration` touches
    anything, raising :class:`PlanError` naming the offending entry.

    Only the tone list is validated (IRs/instruments are read defensively with
    ``.get``); the required keys mirror exactly what ``run_migration`` indexes
    with ``e[...]``."""
    tones = plan.get("tones", [])
    if not isinstance(tones, list):
        raise PlanError("plan 'tones' must be a list")
    for i, e in enumerate(tones):
        if not isinstance(e, dict):
            raise PlanError(f"plan tone #{i} is not a JSON object: {e!r}")
        missing = [k for k in REQUIRED_TONE_KEYS if k not in e]
        if missing:
            label = e.get("name") or e.get("new_slug") or f"#{i}"
            raise PlanError(
                f"plan tone {label!r} is missing required key(s): "
                f"{', '.join(missing)}")


def run_migration(plan: Dict[str, Any], *, dry_run: bool = False) -> Dict[str, Any]:
    """Execute ``plan`` idempotently + data-safely; return a summary dict.

    See the module docstring for the guarantees. With ``dry_run=True`` nothing
    is written — the summary records what WOULD happen. The manifest and IR
    mapping are re-saved (and the home advisory-committed) only when this run
    actually changed something, so a re-run on a migrated home is inert."""
    summary = _empty_summary(dry_run)
    summary["instruments"] = migrate_instruments(plan, dry_run=dry_run)

    if not dry_run:
        libinit.ensure_initialized()

    manifest = SetlistManifest.load()
    manifest_dirty = _migrate_tones(plan.get("tones", []), manifest, summary, dry_run)

    mapping = IrMapping.load()
    mapping_dirty = _migrate_irs(plan.get("irs", []), mapping, summary, dry_run)

    if not dry_run:
        if manifest_dirty:
            manifest.save()
        if mapping_dirty:
            mapping.save()
        if manifest_dirty or mapping_dirty:
            gitops.auto_commit(home.helixgen_home(), "helixgen: library migration")

    return summary


def _migrate_tones(entries: List[Dict[str, Any]], manifest: SetlistManifest,
                   summary: Dict[str, Any], dry_run: bool) -> bool:
    tones_dir = home.tones_dir()

    # collision detection: two DISTINCT tones -> the same destination slug.
    by_slug: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        by_slug.setdefault(e["new_slug"], []).append(e)
    colliding: Dict[str, List[str]] = {}
    for slug, group in by_slug.items():
        distinct = sorted({e["name"] for e in group})
        if len(distinct) > 1:
            colliding[slug] = distinct
            summary["tones"]["collisions"].append({
                "new_slug": slug,
                "names": distinct,
                "suggestion": ("rename one of these tones (distinct "
                               "--descriptor/--artist/--song/--guitar) so they "
                               "no longer share the slug " + repr(slug)),
            })

    dirty = False
    for e in entries:
        if e["new_slug"] in colliding:
            continue
        try:
            changed = _migrate_one_tone(e, manifest, summary, dry_run, tones_dir)
            dirty = dirty or changed
        except Exception as exc:  # noqa: BLE001 - record + continue (data safety)
            summary["tones"]["errors"].append({"name": e.get("name"), "error": str(exc)})
    return dirty


def _migrate_one_tone(e: Dict[str, Any], manifest: SetlistManifest,
                      summary: Dict[str, Any], dry_run: bool,
                      tones_dir: Path) -> bool:
    old_name = e["name"]
    src = Path(e["path"]).resolve()
    dest = (tones_dir / f"{e['new_slug']}.hsp").resolve()

    if src == dest and dest.exists():
        summary["tones"]["skipped"].append({"name": old_name, "reason": "already in place"})
        return False
    if dest.exists():
        summary["tones"]["errors"].append(
            {"name": old_name, "error": f"destination already exists: {dest}"})
        return False
    if not src.exists():
        summary["tones"]["errors"].append(
            {"name": old_name, "error": f"source .hsp missing: {src}"})
        return False
    if dry_run:
        summary["tones"]["moved"].append(
            {"old": old_name, "new_name": e["new_name"], "to": str(dest)})
        return True

    guitar_short = e.get("guitar")
    guitar_slug = naming.slugify(guitar_short) if guitar_short else None
    md_path = src.with_suffix(".md")
    description_md = md_path.read_text() if md_path.exists() else None

    placed = place_tone(
        src,
        artist=e.get("artist"), song=e.get("song"), descriptor=e.get("descriptor"),
        guitar_slug=guitar_slug, guitar_short=guitar_short,
        new_name=e["new_name"], logical=e["logical"], new_slug=e["new_slug"],
        move=True, description_md=description_md,
    )
    _rekey_manifest_tone(manifest, old_name, e["new_name"], placed)
    summary["tones"]["moved"].append(
        {"old": old_name, "new_name": e["new_name"], "to": str(placed)})
    return True


def _migrate_irs(entries: List[Dict[str, Any]], mapping: IrMapping,
                 summary: Dict[str, Any], dry_run: bool) -> bool:
    lib_irs = home.library_irs_dir()
    dirty = False
    for ir in entries:
        try:
            dirty = _migrate_one_ir(ir, mapping, summary, dry_run, lib_irs) or dirty
        except Exception as exc:  # noqa: BLE001 - record + continue
            summary["irs"]["errors"].append({"hash": ir.get("hash"), "error": str(exc)})
    return dirty


def _ir_content_matches(existing: Path, src: Path) -> bool:
    """True when an already-present library copy is byte-identical to ``src``.

    Guards the "adopt the existing dest" path: adopting a byte-DIFFERENT file
    would silently alias a distinct IR onto this hash's mapping."""
    try:
        return existing.read_bytes() == src.read_bytes()
    except OSError:
        return False


def _choose_ir_dest(lib_irs: Path, pack: str, src: Path, h: str) -> tuple[Path, Path]:
    """Pick the library ``(wav_dest, stub)`` for ``src``.

    Normally ``<pack>/<basename>`` (+ ``<stem>.json``). But two source packs
    that slugify to the SAME ``<pack>`` dir can share a WAV basename while
    holding DIFFERENT content; if the natural dest already exists and is NOT
    byte-identical to ``src``, disambiguate by prefixing the irhash so two
    distinct IRs never collapse onto one file (silent wrong-content mapping)."""
    natural = lib_irs / pack / src.name
    if not natural.exists() or _ir_content_matches(natural, src):
        return natural, lib_irs / pack / (src.stem + ".json")
    prefix = (h or "")[:8] or "ir"
    dis = lib_irs / pack / f"{src.stem}-{prefix}{src.suffix}"
    return dis, lib_irs / pack / f"{dis.stem}.json"


def _migrate_one_ir(ir: Dict[str, Any], mapping: IrMapping,
                    summary: Dict[str, Any], dry_run: bool, lib_irs: Path) -> bool:
    h = ir["hash"]
    src = Path(ir["wav"]).resolve()

    if _is_under(src, lib_irs):
        summary["irs"]["skipped"].append({"hash": h, "reason": "already in library"})
        return False
    if not src.exists():
        summary["irs"]["errors"].append({"hash": h, "error": f"wav missing: {src}"})
        return False

    pack = ir_meta.derive_pack(src)
    # `dest` is chosen so it either does not exist yet, or is byte-identical to
    # `src` (safe to adopt). A different-content basename collision from another
    # pack is routed to a hash-disambiguated `dest` instead of aliasing.
    dest, stub = _choose_ir_dest(lib_irs, pack, src, h)

    if dest.exists() and stub.exists():
        # already copied (byte-identical); point the mapping at it if it doesn't yet.
        if str(mapping.entries.get(h)) != str(dest):
            if not dry_run:
                mapping.entries[h] = str(dest)
            summary["irs"]["copied"].append({"hash": h, "to": str(dest)})
            return True
        summary["irs"]["skipped"].append({"hash": h, "reason": "already copied"})
        return False

    if dry_run:
        summary["irs"]["copied"].append({"hash": h, "to": str(dest)})
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)  # COPY, never move — paid packs stay in place
    ir_meta._write_meta_atomic(
        ir_meta.scaffold(dest, h, imported_from=str(src)), stub)
    mapping.entries[h] = str(dest)
    summary["irs"]["copied"].append({"hash": h, "to": str(dest)})
    return True


# ---------------------------------------------------------------------------
# migrate_instruments — seed guitar profiles + retire prefs keys (Task 11)
# ---------------------------------------------------------------------------


def _strip_deprecated_prefs_keys(*, dry_run: bool) -> List[str]:
    """Drop the retired ``instruments`` / ``preset_output_dir`` keys from the
    preferences FILE on disk (design §6), atomically. Returns the list of keys
    actually removed (empty when neither is present, so a re-run is inert).

    Null/absent/parse-failure safe: a missing file, non-dict JSON, or unreadable
    file removes nothing. With ``dry_run`` the file is left untouched but the
    keys that WOULD be removed are still reported."""
    from helixgen.preferences import default_prefs_path

    path = default_prefs_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    removed = [k for k in ("instruments", "preset_output_dir") if k in data]
    if not removed or dry_run:
        return removed

    for k in removed:
        data.pop(k, None)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    return removed


def migrate_instruments(plan: Dict[str, Any], *, dry_run: bool = False) -> Dict[str, Any]:
    """Seed a guitar profile from each ``plan["instruments"]`` entry, then retire
    the deprecated preferences keys.

    - For each instrument dict (optionally carrying a plan-added ``short_name``),
      build ``guitars.profile_from_instrument`` and ``guitars.save_profile`` it.
      IDEMPOTENT: an entry whose profile file already exists is skipped, so a
      re-run is all skips. A malformed entry (missing ``name``) is recorded under
      ``skipped`` with a reason rather than aborting the run.
    - After seeding, strip ``instruments`` + ``preset_output_dir`` from the
      preferences file (see :func:`_strip_deprecated_prefs_keys`).
    - With ``dry_run`` nothing is written (no profiles, no prefs rewrite); the
      summary still reports what WOULD happen.

    Returns ``{"status": "migrated", "profiles_created": [...slugs],
    "skipped": [...], "prefs_keys_removed": [...]}``."""
    from helixgen import guitars

    created: List[str] = []
    skipped: List[Dict[str, Any]] = []
    built: List["guitars.GuitarProfile"] = []
    for d in plan.get("instruments", []) or []:
        try:
            profile = guitars.profile_from_instrument(d)
        except (KeyError, TypeError) as exc:
            skipped.append({"instrument": (d or {}).get("name") if isinstance(d, dict) else None,
                            "reason": f"malformed instrument entry: {exc}"})
            continue
        built.append(profile)  # tracks the post-migration profile set (dry-run too)
        if guitars.profile_path(profile.slug).exists():
            skipped.append({"slug": profile.slug, "reason": "profile already exists"})
            continue
        if not dry_run:
            guitars.save_profile(profile)
        created.append(profile.slug)

    prefs_keys_removed = _strip_deprecated_prefs_keys(dry_run=dry_run)
    default_guitar_unresolved = _reconcile_default_guitar(built)

    return {
        "status": "migrated",
        "profiles_created": created,
        "skipped": skipped,
        "prefs_keys_removed": prefs_keys_removed,
        "default_guitar_unresolved": default_guitar_unresolved,
    }


def _read_default_guitar() -> Optional[str]:
    """Read the persisted ``default_guitar`` straight from the preferences FILE.

    Deliberately NOT via ``load_preferences`` -- that would (a) re-emit the
    deprecation warnings for the ``instruments``/``preset_output_dir`` keys this
    same run is retiring, and (b) apply the ``HELIXGEN_DEFAULT_GUITAR`` env
    override. Reconciliation is about the value written on disk. Null/absent/
    parse-failure safe: returns ``None`` for a missing/non-dict/unreadable file."""
    from helixgen.preferences import default_prefs_path

    path = default_prefs_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("default_guitar")
    return value if isinstance(value, str) and value.strip() else None


def _reconcile_default_guitar(seeded: List["Any"]) -> Optional[str]:
    """After seeding, check the persisted ``default_guitar`` still resolves.

    ``seeded`` is every profile this run built (created OR already-present),
    UNIONed with whatever is already on disk -- so the check reflects the
    post-migration profile set even under dry-run (where ``seeded`` is not yet
    written). If a non-null ``default_guitar`` no longer resolves, emit a
    one-line STDERR warning and return the value (recorded in the summary);
    otherwise return ``None``. Never crashes: an *ambiguous* match is treated as
    resolving (it does name known profiles), not as unresolved."""
    from helixgen import guitars

    default_guitar = _read_default_guitar()
    if not default_guitar:
        return None

    post = list(guitars.load_all_profiles()) + list(seeded)
    try:
        resolved = guitars.find_profile_in(default_guitar, post) is not None
    except guitars.AmbiguousGuitarError:
        resolved = True
    if resolved:
        return None

    print(
        f"helixgen: preferences default_guitar {default_guitar!r} no longer "
        "names a known guitar profile after migration -- update it to an "
        "existing profile's slug/name (see `helixgen library list`).",
        file=sys.stderr,
    )
    return default_guitar
