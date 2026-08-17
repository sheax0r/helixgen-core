"""CLI entry points for `helixgen library` -- read/manage the tone metadata
library (``~/.helixgen/library/tones/*.json``), plus the top-level
``describe`` verb for printing one tone's full write-up.

Pattern: a pure extraction like ``cli_device.py`` -- the ``library`` click
group and the standalone ``describe`` command are imported back into
``cli.py`` (``cli.add_command(library)``, ``cli.add_command(describe)``) so
``helixgen.cli:cli`` stays the single entry point.

**Name resolution** (shared by ``library show``, ``library doc``, and
``describe``): a NAME is tried as BOTH (1) the logical tone slug (also
accepting a trailing ``.json``, i.e. the metadata filename) AND (2) any
variant's ``preset_name`` across the whole library. Two tones whose variants
share a ``preset_name`` collide as ambiguous; if NAME matches a metadata file
AND separately matches a *different* tone's variant ``preset_name``, that is
also ambiguous (a silent pick would hide the collision). No match, an
ambiguous match, malformed on-disk metadata, or a metadata file whose content
computes a different identity slug than its own filename are all a
``ClickException`` (exit 1) -- see ``_resolve_slug`` and ``_load_meta_for``.

``library list`` now lists guitar profiles (``library/guitars/*.json``) AND
per-IR metadata (``library/irs/**/*.json`` sidecars) alongside tones (narrow
with ``--tones``/``--guitars``/``--irs``). ``library show`` resolves a NAME as
a tone first, then as a guitar profile. ``library validate`` checks each variant key against the
real profile slugs (falling back to the on-disk variant keys only when NO
profiles exist yet) and adds a separate ``guitar_settings`` warnings channel.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

import click

from helixgen import gitops, guitars, home, ir_meta, migrate, naming, tone_meta
from helixgen.device.manifest import SetlistManifest
from helixgen.ir import IrMapping
from helixgen.hsp import read_hsp


# ---------------------------------------------------------------------------
# name resolution
# ---------------------------------------------------------------------------


def _is_safe_slug_candidate(candidate: str) -> bool:
    """True when treating ``candidate`` as a metadata filename stem resolves
    to a path that stays inside ``tones_dir()``.

    This is the ONLY path-traversal guard in name resolution: it gates
    whether NAME is even considered for the file-match branch. A candidate
    with a separator or a ``..`` segment (e.g. ``"../etc/passwd"``, or
    ``"foo/../bar"`` if it happened to escape) simply fails this check and
    is never treated as a file -- it falls through to (safe, in-memory)
    ``preset_name`` matching instead of raising. A legitimate preset_name
    that happens to contain ``/`` or ``..`` (e.g. artist "AC/DC", or a title
    with an ellipsis) is therefore never blocked, because preset_name
    matching never touches the filesystem. A candidate with an embedded
    null byte makes ``Path.resolve()`` raise ``ValueError`` (not
    ``OSError``) -- that is caught here too, so it is likewise treated as
    "not a safe file candidate" rather than propagating as an uncaught
    traceback.
    """
    if not candidate:
        return False
    tones = home.tones_dir()
    try:
        cand = (tones / f"{candidate}.json").resolve()
        return cand.parent == tones.resolve()
    except (OSError, ValueError):
        return False


def _resolve_slug(name: str) -> str:
    """Resolve ``name`` to a logical tone slug (see module docstring).

    Checks BOTH resolution mechanisms (not one-then-fallback): does NAME
    name an existing metadata file (guarded by ``_is_safe_slug_candidate``
    so this branch can never read outside ``tones_dir()``), AND does NAME
    separately match some variant's ``preset_name`` (an in-memory lookup
    over already-loaded metadata -- inherently safe, so it is never guarded
    against ``/``/``..``; a real preset_name legitimately containing either
    must still resolve). Raises ``click.ClickException`` (exit 1) when
    nothing matches; when more than one tone's variant ``preset_name``
    matches; or when NAME resolves to a metadata file AND ALSO matches a
    *different* tone's variant ``preset_name`` -- picking the file silently
    in that case would hide a real naming collision.
    """
    candidate = name[:-5] if name.endswith(".json") else name
    file_match = (
        candidate
        if _is_safe_slug_candidate(candidate) and tone_meta.meta_path(candidate).exists()
        else None
    )

    preset_matches = {
        meta.logical_slug
        for meta in tone_meta.load_all_tone_metas()
        if any(v.preset_name == name for v in meta.variants.values())
    }

    if file_match is not None:
        other_matches = preset_matches - {file_match}
        if other_matches:
            raise click.ClickException(
                f"{name!r} is ambiguous: it names the metadata file "
                f"{file_match!r} AND ALSO matches a different tone's "
                f"variant preset_name (logical slugs: {sorted(other_matches)}) "
                "-- disambiguate by using the exact logical slug or the "
                "exact preset_name"
            )
        return file_match

    if len(preset_matches) == 1:
        return next(iter(preset_matches))
    if len(preset_matches) > 1:
        raise click.ClickException(
            f"{name!r} matches more than one tone's preset_name -- ambiguous "
            f"(logical slugs: {sorted(preset_matches)})"
        )
    raise click.ClickException(
        f"no tone found matching {name!r} -- tried it as a logical slug "
        "(library show/list) and as a variant's preset_name"
    )


def _load_meta_for(slug: str) -> tone_meta.ToneMeta:
    """Load the metadata for ``slug``, guarding both failure modes a caller
    resolved via ``_resolve_slug`` can hit:

    - malformed/unreadable on-disk JSON becomes a clean ``ClickException``
      instead of a raw ``json.JSONDecodeError``/``OSError`` traceback (I-1);
    - a metadata file whose own content computes a DIFFERENT identity slug
      than the filename it was loaded from (a hand-rename/edit divergence)
      is refused rather than silently proceeding to act on it -- a caller
      that goes on to write via ``save_tone_meta`` would otherwise write to
      ``meta_path(meta.logical_slug)``, a different path, orphaning/
      duplicating the tone (I-4).
    """
    try:
        meta = tone_meta.load_tone_meta(slug)
    except (OSError, ValueError) as err:
        raise click.ClickException(f"could not read metadata for {slug!r}: {err}")
    if meta.logical_slug != slug:
        raise click.ClickException(
            f"metadata file {slug}.json does not match its own identity "
            f"slug {meta.logical_slug!r} -- rename the file to "
            f"{meta.logical_slug}.json or re-migrate it"
        )
    return meta


def _tone_summary(meta: tone_meta.ToneMeta) -> Dict[str, Any]:
    """The JSON shape for one tone in ``library list --json``: logical slug,
    display base name, tags, and a map of variant key -> preset_name (also
    giving the variant count via ``len()``)."""
    return {
        "slug": meta.logical_slug,
        "display_base": meta.display_base,
        "tags": list(meta.tags),
        "variants": {k: v.preset_name for k, v in meta.variants.items()},
    }


def _normalized_brief(rec: Dict[str, Any], *, verbose: bool = False) -> str:
    """One-line summary of a variant's ``normalized`` record (written by
    ``device normalize --yes``). The record stores the run's FULL per-target
    telemetry (open dicts -- see ``tone_meta.Variant``); the human views
    deliberately summarize and leave the telemetry to ``library show
    --json``: date, non-zero trim count (or "in band" when every trim was
    zero -- still a level-match confirmation), scope; ``verbose`` (the
    `describe` view) adds the target count and the hottest chain-out dBFS
    (over 0 = in-chain clipping)."""
    at = str(rec.get("at") or "")[:10]
    targets = [t for t in (rec.get("targets") or []) if isinstance(t, dict)]
    n_trims = sum(1 for t in targets if t.get("trim_db"))
    detail = (f"{n_trims} trim{'s' if n_trims != 1 else ''}"
              if n_trims else "in band")
    scope = rec.get("scope")
    head = f"normalized {at}" if at else "normalized"
    if not verbose:
        suffix = f", {scope}" if scope else ""
        return f"{head} ({detail}{suffix})"
    parts = [f"{len(targets)} target{'s' if len(targets) != 1 else ''}",
             detail]
    outs = [t["output_db"] for t in targets
            if isinstance(t.get("output_db"), (int, float))
            and not isinstance(t.get("output_db"), bool)]
    if outs:
        parts.append(f"max chain-out {max(outs):+.1f} dBFS")
    suffix = f" ({scope})" if scope else ""
    return f"{head}, " + ", ".join(parts) + suffix


def _forked_from_brief(rec: Dict[str, Any]) -> str:
    """One-line summary of a variant's ``forked_from`` record (written by
    `library fork`): which variant of which tone it was copied off, and when.
    The full record -- source .hsp path, helixgen version -- is in `library
    show --json`."""
    variant = rec.get("variant") or "?"
    tone = rec.get("tone")
    at = str(rec.get("at") or "")[:10]
    where = f"{variant} ({tone})" if tone else f"{rec.get('hsp') or variant}"
    return f"forked from {where}" + (f", {at}" if at else "")


def _guitar_summary(p: "guitars.GuitarProfile") -> Dict[str, Any]:
    """The JSON shape for one guitar in ``library list --json``: slug, full
    name, display short_name, and type."""
    return {
        "slug": p.slug,
        "name": p.name,
        "short_name": p.short_name,
        "type": p.type,
    }


def _ir_summary(m: "ir_meta.IrMeta") -> Dict[str, Any]:
    """The JSON shape for one IR in ``library list --irs``: hash, library-
    relative wav, pack name (if known), mix, and character tags."""
    return {
        "irhash": m.irhash,
        "wav": m.wav,
        "pack": (m.pack or {}).get("name") if isinstance(m.pack, dict) else None,
        "mix": m.mix,
        "tags": list(m.tags),
    }


def _echo_guitar_profile(p: "guitars.GuitarProfile") -> None:
    """Human-readable one-guitar summary for ``library show <guitar>``."""
    click.echo(f"{p.slug}  -- {p.name} ({p.short_name})")
    click.echo(f"Type: {p.type}")
    click.echo(f"Active: {p.active}")
    click.echo(f"Pickups: {p.pickups or '(none)'}")
    click.echo(f"Construction: {p.construction or '(none)'}")
    click.echo(f"Genres: {', '.join(p.genres) if p.genres else '(none)'}")
    click.echo(f"Character: {'set' if p.character_md else '(none)'}")
    click.echo(f"Controls ({len(p.controls)}):")
    for c in p.controls:
        extras = []
        if c.positions:
            extras.append("positions: " + "/".join(c.positions))
        if c.notes:
            extras.append(c.notes)
        suffix = f"  ({'; '.join(extras)})" if extras else ""
        click.echo(f"  {c.name} [{c.kind}]{suffix}")


def _note_shadowed_guitar(name: str) -> None:
    """Stderr note when a NAME that resolved as a TONE also matches a guitar
    profile (backlog #79h): ``library show`` resolves tone-first, which would
    otherwise silently mask the guitar. Advisory only -- never raises, never
    changes what is shown."""
    try:
        profile = guitars.find_profile(name)
    except guitars.AmbiguousGuitarError as exc:
        click.echo(
            f"note: {name!r} also matches {len(exc.slugs)} guitar profiles "
            f"({', '.join(exc.slugs)}) -- showing the TONE (tones win). Use a "
            "guitar's exact slug to see its profile.", err=True)
        return
    except Exception:  # noqa: BLE001 -- advisory; a broken profile never blocks show
        return
    if profile is None:
        return
    labels = sorted({profile.slug, profile.name, profile.short_name} - {name})
    hint = (f" (try {' / '.join(repr(l) for l in labels)})" if labels else "")
    click.echo(
        f"note: {name!r} also matches the guitar profile {profile.slug!r} -- "
        "showing the TONE (tones win). Address the guitar by a label only it "
        f"matches to see the profile{hint}.", err=True)


# ---------------------------------------------------------------------------
# library group
# ---------------------------------------------------------------------------


@click.group(name="library")
def library() -> None:
    """Manage the tone metadata library (``~/.helixgen/library/``).

    Subcommands:

    \b
      list        enumerate tones, guitar profiles, and IRs (all populated)
      show        one tone's metadata, human summary or raw --json
      doc         set a tone's description_md, or one variant's notes_md
      fork        copy a variant's .hsp onto another guitar (or another song)
      validate    shape + cross-link checks across every tone's metadata
      add-guitar  scaffold + commit a new guitar profile JSON

    Every tone lives at ``library/tones/<logical-slug>.json`` and can be
    addressed by its logical slug, any variant's ``preset_name``, or the
    metadata filename -- see each subcommand's --help for the exact
    resolution order. For a human-readable write-up of one tone (identity,
    tags, variants, full description) use the top-level ``describe`` command
    instead of ``library show``.
    """


@library.command(name="list")
@click.option("--tones", "only_tones", is_flag=True, default=False,
              help="List only tones (default: list everything, grouped).")
@click.option("--guitars", "only_guitars", is_flag=True, default=False,
              help="List only guitar profiles (from library/guitars/*.json).")
@click.option("--irs", "only_irs", is_flag=True, default=False,
              help="List only per-IR metadata (from library/irs/**/*.json "
                   "sidecars: hash, wav, pack, mix, tags).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help='Emit {"tones": [...], "guitars": [...], "irs": [...]} JSON '
                   "instead of a human-readable listing (narrowed to only the "
                   "requested key(s) when a section flag is given).")
def list_cmd(only_tones: bool, only_guitars: bool, only_irs: bool, as_json: bool) -> None:
    """List the library's metadata: tones, guitar profiles, and IRs.

    Tones are read from every ``~/.helixgen/library/tones/*.json`` (one
    logical tone per file: artist+song or a descriptor, with one or more
    guitar-targeted variants); guitar profiles from
    ``~/.helixgen/library/guitars/*.json`` (slug, name, short_name, type);
    per-IR metadata from the ``~/.helixgen/library/irs/**/*.json`` sidecars
    (irhash, library-relative wav, pack, mix, character tags).
    --tones/--guitars/--irs narrow the listing to one section -- the human
    view AND the --json shape, which then carries only the requested key(s);
    with none given, everything is shown grouped (--json emits all three
    keys).
    """
    show_all = not (only_tones or only_guitars or only_irs)
    want_tones = show_all or only_tones
    want_guitars = show_all or only_guitars
    want_irs = show_all or only_irs

    tones = ([_tone_summary(m) for m in tone_meta.load_all_tone_metas()]
             if want_tones else [])
    guitar_rows = ([_guitar_summary(g) for g in guitars.load_all_profiles()]
                   if want_guitars else [])
    irs = ([_ir_summary(m) for m in ir_meta.load_all_ir_metas()]
           if want_irs else [])

    if as_json:
        payload: Dict[str, Any] = {}
        if want_tones:
            payload["tones"] = tones
        if want_guitars:
            payload["guitars"] = guitar_rows
        if want_irs:
            payload["irs"] = irs
        click.echo(json.dumps(payload, indent=2))
        return

    if want_tones:
        click.echo(f"Tones ({len(tones)}):")
        if not tones:
            click.echo("  (none)")
        for t in tones:
            click.echo(f"  {t['slug']}  -- {t['display_base']}")
            for key, preset_name in t["variants"].items():
                click.echo(f"    {key}: {preset_name}")
    if want_guitars:
        click.echo(f"Guitars ({len(guitar_rows)}):")
        if not guitar_rows:
            click.echo("  (none)")
        for g in guitar_rows:
            click.echo(
                f"  {g['slug']}  -- {g['name']} ({g['short_name']}) [{g['type']}]")
    if want_irs:
        click.echo(f"IRs ({len(irs)}):")
        if not irs:
            click.echo("  (none)")
        for r in irs:
            label = (r["irhash"] or "?")[:8]
            tags = f"  [{', '.join(r['tags'])}]" if r["tags"] else ""
            click.echo(f"  {label}  {r['wav']}{tags}")


@library.command(name="show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Dump the exact on-disk metadata JSON instead of a human summary.")
def show_cmd(name: str, as_json: bool) -> None:
    """Show one tone's -- or one guitar profile's -- metadata.

    NAME is resolved as a TONE first (logical slug, the metadata filename
    ``<slug>.json``, or any variant's ``preset_name``); if no tone matches it
    is then tried as a GUITAR profile (slug / name / short_name). An unknown
    or ambiguous NAME exits 1. When NAME resolves as a tone AND ALSO matches
    a guitar profile, the tone is shown (tone-first order) with a stderr
    note naming the shadowed profile, so the collision is never silent --
    address the guitar by a label that only it matches (its slug, full
    name, or short name) to see the profile. Human output is a compact summary -- each
    variant line notes its `forked_from` provenance (written by `library
    fork`: source variant + date) and its `normalized` record (written by `device normalize
    --yes`: date, trim count or "in band", scope) when one exists; the
    record's full per-target measurement telemetry is in the --json dump,
    which emits the exact bytes stored on disk (the tone or guitar JSON).
    See also the top-level `describe` command for a longer write-up.
    """
    tone_slug = None
    tone_error: click.ClickException | None = None
    try:
        tone_slug = _resolve_slug(name)
    except click.ClickException as err:
        tone_error = err

    if tone_slug is not None:
        _note_shadowed_guitar(name)
        if as_json:
            click.echo(tone_meta.meta_path(tone_slug).read_text())
            return
        meta = _load_meta_for(tone_slug)
        click.echo(f"{tone_slug}  -- {meta.display_base}")
        click.echo(f"Tags: {', '.join(meta.tags) if meta.tags else '(none)'}")
        click.echo(f"Description: {'set' if meta.description_md else '(none)'}")
        click.echo(f"Variants ({len(meta.variants)}):")
        for key, variant in meta.variants.items():
            norm = (f"  -- {_normalized_brief(variant.normalized)}"
                    if variant.normalized else "")
            fork = (f"  -- {_forked_from_brief(variant.forked_from)}"
                    if variant.forked_from else "")
            click.echo(f"  {key}: {variant.preset_name}  [{variant.hsp}]{norm}{fork}")
        return

    # Not a tone -- try a guitar profile (slug / name / short_name).
    try:
        profile = guitars.find_profile(name)
    except guitars.AmbiguousGuitarError as exc:
        raise click.ClickException(str(exc)) from exc
    if profile is None:
        raise tone_error  # surface the original "no tone found ..." message
    if as_json:
        click.echo(guitars.profile_path(profile.slug).read_text())
        return
    _echo_guitar_profile(profile)


@library.command(name="doc")
@click.argument("name")
@click.argument("source", required=False, default=None, metavar="-")
@click.option("--variant", "variant_key", default=None, metavar="GUITAR_SLUG",
              help='Guitar slug (or "generic") whose notes_md to set, instead '
                   "of the tone's own description_md.")
@click.option("--from-file", "from_file", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Read the markdown from this file (mutually exclusive with "
                   "the literal - stdin argument).")
def doc_cmd(name: str, source: str | None, variant_key: str | None,
            from_file: Path | None) -> None:
    """Set a tone's (or one variant's) markdown write-up.

    Content comes from exactly one of --from-file PATH or a literal ``-``
    SOURCE argument (reads stdin); neither or both given is an error.
    Without --variant, sets the logical tone's ``description_md`` -- the
    full write-up the top-level `describe` command prints verbatim. With
    --variant GUITAR_SLUG, sets that variant's ``notes_md`` instead (exits 1
    if the tone has no such variant). Bumps the tone's `updated` date and
    advisory-commits the home repo (see `save_tone_meta`).
    """
    if from_file is not None and source is not None:
        raise click.ClickException(
            "pass either --from-file PATH or the literal - argument (stdin), not both"
        )
    if from_file is not None:
        text = from_file.read_text()
    elif source == "-":
        text = sys.stdin.read()
    elif source is not None:
        raise click.ClickException(
            f"SOURCE must be the literal - (stdin); got {source!r} -- use "
            "--from-file PATH to read from a file"
        )
    else:
        raise click.ClickException(
            "no markdown source given -- pass --from-file PATH or - (stdin)"
        )

    slug = _resolve_slug(name)
    meta = _load_meta_for(slug)

    if variant_key is None:
        meta.description_md = text
        target = "description_md"
    else:
        if variant_key not in meta.variants:
            raise click.ClickException(
                f"no variant {variant_key!r} on tone {slug!r} "
                f"(have: {sorted(meta.variants)})"
            )
        meta.variants[variant_key].notes_md = text
        target = f"variant {variant_key!r} notes_md"

    tone_meta.save_tone_meta(meta)
    click.echo(f"Updated {target} for {slug!r} ({len(text)} chars).")


@library.command(name="validate")
@click.option("--json", "as_json", is_flag=True, default=False,
              help='Emit {"problems": [...]} JSON instead of a human report.')
@click.pass_context
def validate_cmd(ctx: click.Context, as_json: bool) -> None:
    """Validate every tone's metadata: shape + cross-link checks, plus a
    separate guitar_settings warnings channel.

    Runs ``validate_tone_meta`` over every ``library/tones/*.json`` against
    the setlist manifest (each variant's ``preset_name`` must be registered)
    and the known guitar-profile slugs (``library/guitars/*.json``); each
    variant key must be a known guitar slug or the special ``generic`` key.
    When NO guitar profiles exist yet, the guitar-key check falls back to the
    variant keys already present across the library (so guitar-targeted tones
    made via ``generate --guitar`` aren't falsely flagged); once ANY profile
    exists the check is exact. Each problem line is prefixed with its tone's
    logical slug.

    IR sidecars (``library/irs/**/*.json``) are cross-checked too: each
    ``irhash`` must be registered in ``mapping.json`` and its ``wav`` must
    exist (problems); each character tag must be in the controlled vocabulary
    (warnings).

    SEPARATELY, a non-fatal WARNINGS channel flags ``guitar_settings`` keys
    that aren't controls on the target guitar's profile (skipped when that
    guitar has no profile -- it may lag) and off-vocabulary IR tags. Warnings
    never change the exit code: the command exits 1 only when a PROBLEM (error)
    is found across the library, 0 when clean. --json emits {"problems": [...],
    "warnings": [...]} (both empty when clean) with that same exit-code rule.

    This is the safety net for hand/skill-edited JSON: unlike ``library
    list`` (built on ``load_all_tone_metas()``, which skips any
    ``tones/*.json`` that fails to parse or deserialize -- the right
    behavior for a listing), ``validate`` ALSO independently re-globs
    ``tones_dir()`` and checks every ``*.json`` file itself; one that isn't
    valid JSON -- or that parses but is SHAPE-INVALID (the exact
    deserialization check the loaders warn-and-skip on: a non-dict top
    level, a variant that isn't a dict or is missing ``hsp``/
    ``preset_name``, ...) -- is reported as a problem (prefixed with its
    filename, since a broken file has no reliable identity to key a
    logical slug by) and forces a nonzero exit, instead of silently
    vanishing from the report.
    """
    manifest = SetlistManifest.load()
    tones_dir = home.tones_dir()
    metas = tone_meta.load_all_tone_metas()

    malformed: List[str] = []
    if tones_dir.is_dir():
        for p in sorted(tones_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text())
            except (OSError, ValueError) as err:
                malformed.append(f"{p.name}: not valid JSON ({err})")
                continue
            try:
                tone_meta.parse_tone_meta(data)
            except Exception as err:  # noqa: BLE001 -- any shape failure counts
                malformed.append(
                    f"{p.name}: shape-invalid tone metadata ({err!r})")

    profiles = guitars.load_all_profiles()
    guitar_profiles = {p.slug: p for p in profiles}
    guitar_slugs: set[str] = set(guitar_profiles)
    if not guitar_slugs:
        # No guitar profiles on disk yet (pre-migration): don't falsely flag
        # variant keys authored via `generate --guitar` as unknown. Fall back
        # to every variant key already present across the library (an inert
        # allow-list) ONLY in that genuinely-empty case; once ANY profile
        # exists the guitar-key check is exact.
        guitar_slugs = {key for meta in metas for key in meta.variants}

    problems: List[str] = list(malformed)
    warnings: List[str] = []
    for meta in metas:
        for p in tone_meta.validate_tone_meta(
            meta, tones_dir=tones_dir, manifest=manifest, guitar_slugs=guitar_slugs
        ):
            problems.append(f"{meta.logical_slug}: {p}")
        for w in tone_meta.guitar_settings_warnings(
            meta, guitar_profiles=guitar_profiles
        ):
            warnings.append(f"{meta.logical_slug}: {w}")

    # IR sidecar checks: each irhash present in mapping.json + its wav exists
    # (problems); each tag in the controlled vocabulary (warnings).
    ir_problems, ir_warnings = ir_meta.validate_ir_metas(
        ir_meta.load_all_ir_metas(), IrMapping.load())
    problems.extend(ir_problems)
    warnings.extend(ir_warnings)

    if as_json:
        click.echo(json.dumps(
            {"problems": problems, "warnings": warnings}, indent=2))
    else:
        if not problems:
            click.echo("OK -- no problems found.")
        else:
            click.echo(f"{len(problems)} problem(s) found:")
            for p in problems:
                click.echo(f"  {p}")
        if warnings:
            click.echo(f"{len(warnings)} warning(s):")
            for w in warnings:
                click.echo(f"  {w}")

    if problems:
        ctx.exit(1)


# ---------------------------------------------------------------------------
# migrate + import (Task 9 -- data movement into the tone library)
# ---------------------------------------------------------------------------


@library.command(name="migrate")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Print the migration PLAN as JSON and mutate NOTHING. Edit "
                   "it and feed it back with --plan to execute an adjusted run.")
@click.option("--plan", "plan_file", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Execute a (previously --dry-run, agent-edited) plan JSON "
                   "instead of re-inferring one. Mutually exclusive with "
                   "--dry-run.")
def migrate_cmd(dry_run: bool, plan_file: Path | None) -> None:
    """One-shot migration of a pre-library ~/.helixgen into the tone library.

    Inspects the manifest + preferences + IR mapping and, for every tone with a
    backing .hsp, MOVES it into ~/.helixgen/library/tones/<slug>.hsp under the
    new naming schema, rewrites its meta.name, folds a sibling .md into
    description_md, writes the per-tone metadata JSON, and re-keys the manifest
    (slot + source preserved, content_hash recomputed). Each mapped IR WAV is
    COPIED (never moved -- paid packs stay in place) into
    library/irs/<pack>/ with a scaffolded sidecar, and mapping.json is
    rewritten to the library copy.

    IDEMPOTENT + data-safe: re-running is all skips (no duplicate files, no
    manifest/mapping churn); a tone move is copy -> byte-verify -> remove-source;
    a per-tone/IR error is recorded and the run CONTINUES. A tone whose .hsp
    already sits at its destination but whose metadata/manifest bookkeeping
    is incomplete (a prior run died mid-tone) is SELF-HEALED on re-run --
    file untouched, bookkeeping recreated. A slug collision (two
    tones -> one destination) is recorded with a rename suggestion and NEITHER
    is moved. Each prefs ``instruments`` entry is SEEDED into a guitar profile
    (library/guitars/<slug>.json); the retired ``instruments`` and
    ``preset_output_dir`` keys are then removed from preferences.json, and
    ``default_guitar`` is reconciled against the seeded profiles -- a
    still-unresolved value is WARNED (stderr) and recorded, never rewritten.

    \b
      --dry-run        print the plan JSON, mutate nothing
      --plan FILE      execute an edited plan
      (no flag)        plan + run in one go

    Output is a JSON summary of moves / skips / errors / collisions.
    """
    if dry_run and plan_file is not None:
        raise click.ClickException("--dry-run and --plan are mutually exclusive")

    if dry_run:
        click.echo(json.dumps(migrate.plan_migration(), indent=2))
        return

    if plan_file is not None:
        try:
            plan = json.loads(Path(plan_file).read_text())
        except (OSError, ValueError) as err:
            raise click.ClickException(f"could not read plan {plan_file}: {err}")
        if not isinstance(plan, dict):
            raise click.ClickException(f"plan {plan_file} must be a JSON object")
    else:
        plan = migrate.plan_migration()

    try:
        migrate.validate_plan(plan)
    except migrate.PlanError as err:
        raise click.ClickException(str(err)) from err

    summary = migrate.run_migration(plan)
    click.echo(json.dumps(summary, indent=2))


@library.command(name="import")
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.option("--artist", default=None, help="Song identity: artist (needs --song).")
@click.option("--song", default=None, help="Song identity: song title (needs --artist).")
@click.option("--descriptor", default=None,
              help="Descriptor identity (mutually exclusive with --artist/--song). "
                   "Defaults to the .hsp's meta.name when no identity flag is given.")
@click.option("--guitar", "guitar", default=None,
              help="Target guitar label; slugified and appended to the display "
                   "name + filename.")
@click.option("--keep-source", "keep_source", is_flag=True, default=False,
              help="COPY the source .hsp into the library instead of MOVING it "
                   "(default moves it in).")
def import_cmd(source: Path, artist: str | None, song: str | None,
               descriptor: str | None, guitar: str | None,
               keep_source: bool) -> None:
    """Import an external .hsp (or a directory of them) into the tone library.

    By DEFAULT the source .hsp is MOVED into ~/.helixgen/library/tones/ under
    the resolved naming schema; --keep-source COPIES it instead (leaving the
    original in place). A sibling .md (same stem) is folded into the tone's
    description_md; a MISSING .md leaves description_md null and prints a
    warning. meta.name is rewritten to the resolved display name, the per-tone
    metadata JSON is written, the tone is registered in the manifest, and the
    home is advisory-committed.

    Naming flags drive identity with the SAME validation + collision rules as
    `generate`: exactly one of (--artist + --song) OR --descriptor (each
    requires the other where paired), plus an optional --guitar. An explicit
    --descriptor carrying a smuggled identity -- a " - " separator, or a
    trailing guitar name -- is REFUSED (exit 1) naming the flags to use
    instead. With no identity flag the .hsp's own meta.name becomes the
    descriptor (unguarded: an existing artifact's name is not a naming choice
    you just made). A target
    slug that already exists is refused (exit 1) -- the existing .hsp is never
    overwritten. When SOURCE is a directory, every *.hsp under it is imported
    self-named from its meta.name (per-tone identity flags aren't allowed for a
    directory; --guitar / --keep-source still apply to all).

    A directory import is ATOMIC on naming collisions: the whole batch is
    pre-validated (identities + target slugs) and refused -- moving NOTHING --
    if any two files collide or clash with the existing library. During the
    move pass, an unexpected per-file error is recorded and the run CONTINUES,
    and the manifest is always saved, so tones that already succeeded are
    registered on disk (never left in an unreconcilable half-imported state);
    the command exits nonzero when any file failed. A failure AFTER a file
    was placed (manifest registration) names the exact recovery command
    (`helixgen register <placed .hsp>`).
    """
    source = Path(source)
    if source.is_dir():
        if artist or song or descriptor:
            raise click.ClickException(
                "--artist/--song/--descriptor apply to a single .hsp; importing "
                "a directory self-names each file from its meta.name")
        _import_directory(source, guitar, keep_source)
    else:
        _import_single(source, artist, song, descriptor, guitar, keep_source)


def _import_single(source: Path, artist: str | None, song: str | None,
                   descriptor: str | None, guitar: str | None,
                   keep_source: bool) -> None:
    """Import one .hsp. Collisions are pre-checked BEFORE any move so a
    manifest/slug clash can't strand a moved file; the manifest is saved in a
    ``finally`` so a post-move register failure never silently loses progress."""
    manifest = SetlistManifest.load()
    r = _resolve_import(source, artist, song, descriptor, guitar)
    reason = _import_collision_reason(r, manifest, tones_dir=home.tones_dir())
    if reason:
        raise click.ClickException(reason)  # nothing moved yet
    try:
        _place_and_register(r, keep_source, manifest)
    finally:
        manifest.save()
    gitops.auto_commit(home.helixgen_home(), "helixgen: import tone(s) into library")


def _import_directory(source: Path, guitar: str | None, keep_source: bool) -> None:
    """Import every ``*.hsp`` under ``source`` atomically-on-collision and
    record-and-continue on unexpected per-file errors (see ``import`` --help)."""
    hsps = sorted(source.rglob("*.hsp"))
    if not hsps:
        raise click.ClickException(f"no .hsp files found under {source}")
    manifest = SetlistManifest.load()
    tones_dir = home.tones_dir()

    # PHASE A -- pre-validate the WHOLE batch before moving anything. Resolve
    # each file's identity + target slug, then refuse atomically on any
    # intra-batch OR existing-library naming collision (MOVE NOTHING).
    resolved = [_resolve_import(h, None, None, None, guitar) for h in hsps]
    collisions = _detect_batch_collisions(resolved, manifest, tones_dir)
    if collisions:
        raise click.ClickException(
            "refusing to import -- naming collisions (nothing moved):\n  "
            + "\n  ".join(collisions))

    # PHASE B -- move pass: record-and-continue, and ALWAYS save (finally) so
    # tones already moved+registered are persisted even if a later file fails.
    failures: List[tuple[Path, Exception]] = []
    try:
        for r in resolved:
            try:
                _place_and_register(r, keep_source, manifest)
            except Exception as exc:  # noqa: BLE001 - record + continue (C1)
                failures.append((r["src"], exc))
                click.echo(f"error: failed to import {r['src'].name}: {exc}", err=True)
    finally:
        manifest.save()
    gitops.auto_commit(home.helixgen_home(), "helixgen: import tone(s) into library")

    if failures:
        raise click.ClickException(
            f"{len(failures)} of {len(resolved)} file(s) failed to import "
            "(the rest were saved): "
            + ", ".join(f"{src.name} ({exc})" for src, exc in failures))


def _resolve_import(src: Path, artist: str | None, song: str | None,
                    descriptor: str | None, guitar: str | None) -> Dict[str, Any]:
    """Resolve one .hsp's identity the SAME way `generate` does, WITHOUT moving
    anything (reads only ``src``'s meta.name and a sibling ``.md``).

    Raises ``ClickException`` on a bad identity combo / empty slug / an
    identity-equality mismatch against an existing logical JSON. Returns a dict
    with everything ``_place_and_register`` needs."""
    src = Path(src)
    if artist or song or descriptor:
        # Guard the EXPLICIT flag only -- the meta.name fallback below is an
        # existing artifact's name, not a naming choice the caller just made.
        problem = guitars.descriptor_identity_problem(descriptor)
        if problem:
            raise click.ClickException(problem)
        r_artist, r_song, r_descriptor = artist, song, descriptor
    else:
        try:
            r_descriptor = (read_hsp(src).get("meta") or {}).get("name") or src.stem
        except (OSError, ValueError):
            r_descriptor = src.stem
        r_artist = r_song = None

    # Route --guitar through the SAME profile resolution as `generate` so both
    # surfaces agree (unknown guitar with profiles present -> error listing
    # them; no profiles yet -> literal slugify + one-line notice). Lazy import
    # avoids a cli <-> cli_library import cycle.
    if guitar:
        from helixgen.cli import _resolve_guitar
        guitar_slug, guitar_short = _resolve_guitar(guitar)
    else:
        guitar_slug = guitar_short = None
    if guitar and not guitar_slug:
        raise click.ClickException(
            f"--guitar {guitar!r} has no slug-able characters (needs letters or "
            "digits) -- pick a different guitar label.")

    try:
        preset_name = naming.display_name(
            artist=r_artist, song=r_song, descriptor=r_descriptor,
            guitar_short=guitar_short)
        logical = naming.logical_slug(
            artist=r_artist, song=r_song, descriptor=r_descriptor)
    except ValueError as err:
        raise click.ClickException(str(err)) from err
    if not logical:
        raise click.ClickException(
            "the tone identity has no slug-able characters (letters or digits) "
            "-- give a --descriptor/--artist/--song with real text.")
    new_slug = naming.variant_slug(logical, guitar_slug)

    # Identity-equality guard (mirrors generate): appending a variant to a
    # logical JSON that already belongs to a DIFFERENT identity is refused.
    existing = (tone_meta.load_tone_meta(logical)
                if tone_meta.meta_path(logical).exists() else None)
    if existing is not None:
        def _norm(v: str | None) -> str | None:
            return v.strip() if v and v.strip() else None
        if (_norm(r_artist), _norm(r_song), _norm(r_descriptor)) != (
                _norm(existing.artist), _norm(existing.song), _norm(existing.descriptor)):
            raise click.ClickException(
                f"logical slug {logical!r} already belongs to a different tone "
                f"identity ({existing.display_base!r}); rename this tone "
                "(--artist/--song/--descriptor) to disambiguate.")

    md_path = src.with_suffix(".md")
    md_missing = not md_path.exists()
    description_md = None if md_missing else md_path.read_text()

    return {
        "src": src, "artist": r_artist, "song": r_song, "descriptor": r_descriptor,
        "guitar_slug": guitar_slug, "guitar_short": guitar_short,
        "preset_name": preset_name, "logical": logical, "new_slug": new_slug,
        "description_md": description_md, "md_missing": md_missing,
    }


def _import_collision_reason(r: Dict[str, Any], manifest: SetlistManifest, *,
                             tones_dir: Path) -> str | None:
    """A human message when placing ``r`` would overwrite an existing tone .hsp
    or clash with a differently-pathed manifest name; else ``None``. Checked
    BEFORE any move so a collision never strands a moved source."""
    dest = tones_dir / f"{r['new_slug']}.hsp"
    if dest.exists():
        return (f"a tone already exists at {dest} -- refusing to overwrite. "
                "Rename this one (change --descriptor/--artist/--song or --guitar).")
    existing = manifest.tones.get(r["preset_name"])
    if existing is not None and existing.get("path") not in (None, str(dest.resolve())):
        return (f"the manifest already registers a tone named {r['preset_name']!r} "
                f"at a different path ({existing.get('path')}) -- refusing to "
                "overwrite. Rename this one (--descriptor/--artist/--song/--guitar).")
    return None


def _detect_batch_collisions(resolved: List[Dict[str, Any]],
                             manifest: SetlistManifest,
                             tones_dir: Path) -> List[str]:
    """Every naming collision in a directory batch, as human strings (empty ==
    safe): two files sharing a target slug (intra-batch), or a file clashing
    with the existing library/manifest."""
    problems: List[str] = []
    by_slug: Dict[str, List[Dict[str, Any]]] = {}
    for r in resolved:
        by_slug.setdefault(r["new_slug"], []).append(r)
    for slug, group in sorted(by_slug.items()):
        if len(group) > 1:
            names = ", ".join(sorted(x["src"].name for x in group))
            problems.append(
                f"{len(group)} files map to the same target slug {slug!r}: {names}")
    for r in resolved:
        if len(by_slug[r["new_slug"]]) > 1:
            continue  # already reported as an intra-batch slug collision
        reason = _import_collision_reason(r, manifest, tones_dir=tones_dir)
        if reason:
            problems.append(f"{r['src'].name}: {reason}")
    return problems


def _place_and_register(r: Dict[str, Any], keep_source: bool,
                        manifest: SetlistManifest) -> None:
    """Place one resolved tone into the library and register it into ``manifest``
    (in memory -- the caller saves). Warns on a missing sibling .md; converts
    ``place_tone``/``register_tone`` failure modes to ``ClickException``."""
    if r["md_missing"]:
        click.echo(
            f"warning: no sibling .md for {r['src'].name}; description_md left null",
            err=True)
    try:
        dest = migrate.place_tone(
            r["src"], artist=r["artist"], song=r["song"], descriptor=r["descriptor"],
            guitar_slug=r["guitar_slug"], guitar_short=r["guitar_short"],
            new_name=r["preset_name"], logical=r["logical"], new_slug=r["new_slug"],
            move=not keep_source, description_md=r["description_md"])
    except migrate.ToneCollision:
        raise click.ClickException(
            f"a tone already exists at {home.tones_dir() / (r['new_slug'] + '.hsp')} "
            "-- refusing to overwrite. Rename this one (change "
            "--descriptor/--artist/--song or --guitar).")

    try:
        manifest.register_tone(dest, source="import-local")
    except Exception as err:  # noqa: BLE001 -- the file is already placed
        # The .hsp + metadata are already placed; only the manifest
        # registration failed. A plain re-import would refuse (the
        # destination now exists), so name the exact recovery command
        # (backlog #79e).
        raise click.ClickException(
            f"{err}\nThe tone was already placed at {dest} (metadata "
            "written) but could NOT be registered in the manifest. After "
            "fixing the conflict, run `helixgen register "
            f"{dest}` to complete the import."
        ) from err

    click.echo(f"Imported {r['src'].name} -> {dest}")
    click.echo(f"Preset name: {r['preset_name']}")


# ---------------------------------------------------------------------------
# fork (hgc-2ja)
# ---------------------------------------------------------------------------


# The chain params the tone skill reconsiders when the target guitar's pickups
# differ in class. Keyed by the block's `type` in the .hsp flow so a
# compressor's makeup `Gain` isn't reported as drive into the amp, and a cab's
# `Level` isn't reported as the amp's. ADVISORY ONLY -- fork never touches a
# param; this table only decides what to REMIND the author about.
_ADAPT_GROUPS: tuple[tuple[str, Dict[str, tuple[str, ...]]], ...] = (
    ("drive into the amp", {"amp": ("Drive",), "fx": ("Gain",)}),
    ("brightness / treble pull",
     {"amp": ("Treble", "Presence", "Bright"), "cab": ("HighCut",)}),
    ("input stage", {"input": ("Pad", "Trim")}),
    ("level", {"amp": ("Master", "MasterVol", "ChVol", "Level"),
               "output": ("gain",)}),
)


def _chain_params(hsp_path: Path) -> List[tuple[str, str, str]]:
    """``(block_type, block_label, param_name)`` for every param in the .hsp's
    chain, read straight off the raw body -- no Library, no block-library
    lookup, so it works on any .hsp. The label is the editor's own display
    name (vendored table) plus the grid coordinate, e.g.
    ``"Brit 2203MV [0:b05]"``."""
    from helixgen.device.paraminfo import model_display_name

    def _slot(bnn: Any) -> Dict[str, Any] | None:
        """A bNN entry's model dict, or None when it isn't a block at all --
        a path dict also carries non-block keys (``@enabled``)."""
        if not isinstance(bnn, dict):
            return None
        slot = bnn.get("slot")
        if not isinstance(slot, list) or not slot or not isinstance(slot[0], dict):
            return None
        return slot[0]

    body = read_hsp(hsp_path)
    rows: List[tuple[str, str, str]] = []
    for pi, path_dict in enumerate((body.get("preset") or {}).get("flow") or []):
        if not isinstance(path_dict, dict):
            continue
        # An EMPTY path (endpoints only) has nothing to adapt -- reporting its
        # input Trim / output gain is noise, and every Stadium preset using
        # one DSP carries such a path.
        if not any(_slot(b) is not None and b.get("type") not in ("input", "output")
                   for b in path_dict.values()):
            continue
        for key in sorted(path_dict):
            entry = _slot(path_dict[key])
            if entry is None:
                continue
            model = entry.get("model")
            label = (model_display_name(model) if model else None) or model or key
            for param in (entry.get("params") or {}):
                rows.append((str(path_dict[key].get("type") or ""),
                             f"{label} [{pi}:{key}]", str(param)))
    return rows


def _adaptation_checklist(hsp_path: Path) -> List[str]:
    """The advisory "reconsider these" lines for a fork whose target guitar's
    pickups differ in class -- one line per group in ``_ADAPT_GROUPS`` that
    this particular chain actually has params for. A chain with no matching
    params yields no lines (and an unreadable .hsp yields none either: the
    checklist is advice, never a reason to fail a fork)."""
    try:
        rows = _chain_params(hsp_path)
    except (OSError, ValueError, KeyError):
        return []
    lines: List[str] = []
    for title, wanted in _ADAPT_GROUPS:
        hits = [f"{label} `{param}`" for btype, label, param in rows
                if param in wanted.get(btype, ())]
        if hits:
            lines.append(f"{title}: " + ", ".join(dict.fromkeys(hits)))
    return lines


def _inherited_note(source_label: str, target_label: str, when: str,
                    inherited: str | None) -> str:
    """The forked variant's ``notes_md``: an unmissable inherited-from banner,
    then the source's own notes verbatim (when it had any).

    Written on EVERY fork, not only when there is text to carry: a same-tone
    fork shares the logical tone's ``description_md``, which was written for
    the SOURCE variant -- and an agent reading that write-up as if it
    described the fork is exactly the failure this verb exists to stop."""
    banner = (
        f"> **Inherited from the `{source_label}` variant "
        f"(`helixgen library fork`, {when}).** This write-up -- and the tone's "
        f"`description_md` -- was written for the SOURCE. Update it for "
        f"{target_label} before treating it as a description of this variant."
    )
    return f"{banner}\n\n{inherited}" if inherited else banner + "\n"


def _resolve_fork_source(source: str) -> tuple[tone_meta.ToneMeta | None, str | None, Path]:
    """Resolve SOURCE to ``(meta, variant_key, hsp_path)``.

    An existing ``.hsp`` PATH is taken as-is (and matched back to a library
    variant when it is one, so a path and a slug fork identically); anything
    else resolves through the shared ``library show`` name resolution."""
    p = Path(source)
    if p.suffix.lower() == ".hsp" and p.exists():
        found = tone_meta.find_variant_by_hsp(p)
        return (found[0], found[1], p) if found else (None, None, p)

    slug = _resolve_slug(source)
    meta = _load_meta_for(slug)
    keys = [k for k, v in meta.variants.items() if v.preset_name == source]
    if not keys:
        keys = sorted(meta.variants)
    if not keys:
        raise click.ClickException(f"tone {slug!r} has no variants to fork")
    if len(keys) > 1:
        raise click.ClickException(
            f"{source!r} names the tone {slug!r}, which has {len(keys)} "
            f"variants ({', '.join(keys)}) -- name the exact variant to fork by "
            "its preset_name: "
            + ", ".join(repr(meta.variants[k].preset_name) for k in keys))
    key = keys[0]
    # Same stored-path convention every other library surface uses.
    hsp = tone_meta._resolve_variant_hsp(meta.variants[key].hsp, home.library_dir())
    if hsp is None or not hsp.is_file():
        raise click.ClickException(
            f"variant {key!r} of {slug!r} points at a missing .hsp "
            f"({meta.variants[key].hsp!r}) -- run `helixgen library validate`")
    return meta, key, hsp


@library.command(name="fork")
@click.argument("source")
@click.option("--guitar", "guitar", default=None,
              help="Target guitar label (slug / name / short_name). Defaults to "
                   "the SOURCE variant's own guitar, which then only makes sense "
                   "with a new identity.")
@click.option("--artist", default=None, help="New identity: artist (needs --song).")
@click.option("--song", default=None, help="New identity: song title (needs --artist).")
@click.option("--descriptor", default=None,
              help="New descriptor identity (mutually exclusive with "
                   "--artist/--song). Must be the tone name ALONE -- no ' - ' "
                   "separator, no trailing guitar name.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Print exactly what would be written and write NOTHING.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the fork record as JSON instead of a human summary.")
def fork_cmd(source: str, guitar: str | None, artist: str | None,
             song: str | None, descriptor: str | None, dry_run: bool,
             as_json: bool) -> None:
    """Fork a tone onto another guitar (or another song) from its .hsp.

    THE .hsp IS THE SOURCE. The forked preset is a byte-for-byte copy of the
    source variant's .hsp with only its `meta.name` rewritten, so every block,
    param, snapshot, footswitch, expression assignment and IR reference comes
    across VERBATIM -- including anything helixgen does not model. A tone's
    `description_md` / `notes_md` is NEVER read for values: re-authoring a
    fork from the write-up is how stale, pre-rewrite settings come back.

    SOURCE resolves the same way `library show` does -- a logical slug, the
    metadata filename, or a variant's exact `preset_name` -- and may also be a
    path to an .hsp (so something not yet in the library can be forked). A
    logical slug with more than one variant is ambiguous and exits 1, naming
    each variant's preset_name.

    \b
    Identity:
      * --guitar ALONE keeps the source's artist/song (or descriptor) and adds
        a SECOND VARIANT of the SAME LOGICAL TONE -- one metadata JSON, two
        entries under `variants`.
      * --artist/--song or --descriptor makes it a NEW LOGICAL TONE (the "same
        rig, different song" case); --guitar then defaults to the source's.
      * SOURCE given as an .hsp path that is not a registered variant has no
        identity to inherit, so an identity flag is REQUIRED.

    An existing target variant is an ERROR naming the existing .hsp -- nothing
    is overwritten and nothing is written. Pick a different --guitar, or edit
    the existing .hsp in place with the surgical verbs.

    PROVENANCE is recorded on the new variant (`forked_from`: source tone,
    source variant, source .hsp, helixgen version, date) and shown by
    `describe` / `library show --json`. The source's write-up is carried over
    into the new variant's `notes_md` behind an INHERITED banner -- a starting
    point, never a description of the fork.

    ADAPTATION IS ADVISORY. helixgen does NOT re-voice anything: pickups are a
    judgement call the corpus cannot make for you. When the two guitars'
    pickup classes differ (active vs passive, humbucker vs single-coil), a
    checklist prints on STDERR naming the params THIS chain actually has --
    drive into the amp, treble pull, input pad, level. Matching pickups print
    nothing. `guitar_settings` are NOT copied: they describe the source
    guitar's own controls.
    """
    src_meta, src_key, src_hsp = _resolve_fork_source(source)

    # --- target identity -------------------------------------------------
    if artist or song or descriptor:
        problem = guitars.descriptor_identity_problem(descriptor)
        if problem:
            raise click.ClickException(problem)
        t_artist, t_song, t_descriptor = artist, song, descriptor
    elif src_meta is not None:
        t_artist, t_song, t_descriptor = (
            src_meta.artist, src_meta.song, src_meta.descriptor)
    else:
        raise click.ClickException(
            f"{source} is not a registered library variant, so there is no "
            "identity to inherit -- give --artist/--song or --descriptor "
            "(and --guitar) for the fork.")

    # --- target guitar ---------------------------------------------------
    from helixgen.cli import _resolve_guitar  # lazy: avoids a cli import cycle

    if guitar:
        guitar_slug, guitar_short = _resolve_guitar(guitar)
        if not guitar_slug:
            raise click.ClickException(
                f"--guitar {guitar!r} has no slug-able characters (needs letters "
                "or digits) -- pick a different guitar label.")
    elif src_key and src_key != "generic":
        guitar_slug, guitar_short = _resolve_guitar(src_key)
    else:
        guitar_slug = guitar_short = None

    try:
        preset_name = naming.display_name(
            artist=t_artist, song=t_song, descriptor=t_descriptor,
            guitar_short=guitar_short)
        logical = naming.logical_slug(
            artist=t_artist, song=t_song, descriptor=t_descriptor)
    except ValueError as err:
        raise click.ClickException(str(err)) from err
    if not logical:
        raise click.ClickException(
            "the tone identity has no slug-able characters (letters or digits) "
            "-- give a --descriptor/--artist/--song with real text.")
    new_slug = naming.variant_slug(logical, guitar_slug)

    # --- collision + identity guards (BEFORE anything is written) --------
    existing = (tone_meta.load_tone_meta(logical)
                if tone_meta.meta_path(logical).exists() else None)
    if existing is not None:
        def _norm(v: str | None) -> str | None:
            return v.strip() if v and v.strip() else None
        if (_norm(t_artist), _norm(t_song), _norm(t_descriptor)) != (
                _norm(existing.artist), _norm(existing.song),
                _norm(existing.descriptor)):
            raise click.ClickException(
                f"logical slug {logical!r} already belongs to a different tone "
                f"identity ({existing.display_base!r}); rename this fork "
                "(--artist/--song/--descriptor) to disambiguate.")

    manifest = SetlistManifest.load()
    r = {"new_slug": new_slug, "preset_name": preset_name}
    reason = _import_collision_reason(r, manifest, tones_dir=home.tones_dir())
    if reason:
        raise click.ClickException(
            reason + " (Or edit the existing .hsp in place -- `helixgen "
            "set-param` / `patch` -- instead of forking over it.)")
    dest = home.tones_dir() / f"{new_slug}.hsp"

    # --- provenance + inherited write-ups --------------------------------
    from helixgen import __version__ as helixgen_version

    when = date.today().isoformat()
    source_label = src_key or Path(source).name
    forked_from = {
        "tone": src_meta.logical_slug if src_meta is not None else None,
        "variant": src_key,
        "hsp": tone_meta._to_library_relative(src_hsp),
        "helixgen_version": helixgen_version,
        "at": when,
    }
    src_variant = (src_meta.variants[src_key]
                   if src_meta is not None and src_key else None)
    notes_md = _inherited_note(
        source_label, guitar_short or "this variant", when,
        src_variant.notes_md if src_variant else None)
    # description_md belongs to the LOGICAL tone. A same-tone fork shares the
    # one already on disk (copying it onto itself would be a no-op at best and
    # a clobber at worst); only a NEW logical tone gets an inherited copy.
    new_logical = existing is None
    description_md = None
    if new_logical and src_meta is not None and src_meta.description_md:
        description_md = _inherited_note(
            source_label, guitar_short or "this tone", when,
            src_meta.description_md)

    # --- adaptation checklist (advisory) ---------------------------------
    src_profile = (guitars.find_profile(src_key) if src_key else None)
    dst_profile = (guitars.find_profile(guitar_slug) if guitar_slug else None)
    pickups_differ = (guitars.pickup_class(src_profile)
                      != guitars.pickup_class(dst_profile))
    checklist = _adaptation_checklist(src_hsp) if pickups_differ else []

    record = {
        "dry_run": dry_run,
        "source": {"tone": forked_from["tone"], "variant": src_key,
                   "hsp": str(src_hsp)},
        "target": {"tone": logical, "variant": guitar_slug or "generic",
                   "preset_name": preset_name, "hsp": str(dest),
                   "new_logical_tone": new_logical},
        "forked_from": forked_from,
        "adaptation": {
            "pickups_differ": pickups_differ,
            "from": guitars.describe_pickups(src_profile),
            "to": guitars.describe_pickups(dst_profile),
            "checklist": checklist,
        },
    }

    if not dry_run:
        try:
            written = migrate.place_tone(
                src_hsp, artist=t_artist, song=t_song, descriptor=t_descriptor,
                guitar_slug=guitar_slug, guitar_short=guitar_short,
                new_name=preset_name, logical=logical, new_slug=new_slug,
                move=False, description_md=description_md,
                variant_fields={"notes_md": notes_md,
                                "forked_from": forked_from})
        except migrate.ToneCollision:
            raise click.ClickException(
                f"a tone already exists at {dest} -- refusing to overwrite. "
                "Pick a different --guitar, or edit the existing .hsp in place.")
        try:
            manifest.register_tone(written, source="authored")
        except Exception as err:  # noqa: BLE001 -- the .hsp is already placed
            raise click.ClickException(
                f"{err}\nThe fork was written to {written} (metadata recorded) "
                "but could NOT be registered in the manifest. After fixing the "
                f"conflict, run `helixgen register {written}`.") from err
        manifest.save()
        gitops.auto_commit(home.helixgen_home(), f"helixgen: fork tone {new_slug}")

    _report_fork(record, as_json=as_json)


def _report_fork(record: Dict[str, Any], *, as_json: bool) -> None:
    """Human summary on stdout (or the JSON record), adaptation checklist on
    stderr -- so a `--json` consumer's stdout stays parseable either way."""
    adapt = record["adaptation"]
    if adapt["pickups_differ"]:
        click.echo(
            f"Pickups differ -- {adapt['from']} -> {adapt['to']}. The .hsp was "
            "copied VERBATIM; helixgen re-voices nothing. Reconsider:", err=True)
        for line in adapt["checklist"]:
            click.echo(f"  - {line}", err=True)
        if not adapt["checklist"]:
            click.echo("  - (this chain has none of the usual "
                       "drive/treble/pad/level params)", err=True)

    if as_json:
        click.echo(json.dumps(record, indent=2))
        return

    tgt, prov = record["target"], record["forked_from"]
    prefix = "Would fork" if record["dry_run"] else "Forked"
    click.echo(f"{prefix} {record['source']['hsp']} -> {tgt['hsp']}")
    click.echo(f"Preset name: {tgt['preset_name']}")
    kind = ("NEW logical tone" if tgt["new_logical_tone"]
            else "second variant of the same logical tone")
    click.echo(f"Logical tone: {tgt['tone']}  ({kind}, variant {tgt['variant']})")
    click.echo(f"Provenance: forked from {prov['variant']} "
               f"({prov['tone']}), {prov['at']}")
    if record["dry_run"]:
        click.echo("Dry run -- nothing was written.")


# ---------------------------------------------------------------------------
# ir-backfill (Task 12 -- scaffold metadata for library IRs lacking it)
# ---------------------------------------------------------------------------


@library.command(name="ir-backfill")
@click.option("--json", "as_json", is_flag=True, default=False,
              help='Emit {"backfilled": [...], "skipped": [...], '
                   '"errors": [...]} JSON instead of a human summary.')
def ir_backfill_cmd(as_json: bool) -> None:
    """Scaffold library metadata for every registered IR that lacks it.

    For each ``mapping.json`` entry whose WAV lives OUTSIDE
    ``library/irs/`` or has no sidecar JSON: the WAV is COPIED into
    ``library/irs/<pack>/`` (never moved -- paid packs stay in place), a
    metadata sidecar is scaffolded next to it (mix guessed from the filename;
    provenance/tags left blank for the skill to enrich), and mapping.json is
    rewritten to the library copy. An entry already in-library WITH a sidecar
    is skipped, so this is IDEMPOTENT -- a re-run is all skips and never
    re-copies. The home is advisory-committed when the mapping lives under it.

    WAV bytes stay gitignored; only the sidecar + mapping.json are committed.
    Use this once after adopting the library layout (or the `setup` skill runs
    it) so already-registered IRs get metadata; the skill then fills each
    sidecar's provenance and character tags.
    """
    mapping = IrMapping.load()
    result = ir_meta.backfill(mapping)
    if result["backfilled"]:
        mapping.save()
        home_dir = home.helixgen_home()
        try:
            under = mapping.irs_dir.resolve().is_relative_to(home_dir.resolve())
        except (OSError, ValueError):
            under = False
        if under:
            gitops.auto_commit(home_dir, "helixgen: ir-backfill IR metadata")

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    click.echo(
        f"Backfilled {len(result['backfilled'])} IR(s), "
        f"{len(result['skipped'])} already had metadata, "
        f"{len(result['errors'])} error(s).")
    for e in result["errors"]:
        click.echo(f"  error {e.get('hash')}: {e.get('error')}", err=True)


# ---------------------------------------------------------------------------
# add-guitar (#79j -- a core write path for new guitar profiles)
# ---------------------------------------------------------------------------


@library.command(name="add-guitar")
@click.argument("name")
@click.option("--short-name", "short_name", default=None, metavar="SHORT",
              help="Display short name used in preset names/slugs "
                   "(default: NAME).")
@click.option("--type", "type_", type=click.Choice(["guitar", "bass"]),
              default="guitar", show_default=True,
              help="Instrument type.")
def add_guitar_cmd(name: str, short_name: str | None, type_: str) -> None:
    """Scaffold a new guitar profile at ``library/guitars/<slug>.json``.

    Writes a minimal schema-1 profile (name, short_name, type; every other
    field null/empty for the `setup` skill -- or a hand edit -- to enrich:
    pickups, construction, character_md, genres, and the control inventory)
    and auto-commits the home repo like every other library write. This is
    the core write path for new profiles: a profile JSON written directly by
    a skill would otherwise only get committed on core's NEXT library write.

    The slug is ``slugify(NAME)``; a profile already at that slug is refused
    (exit 1) -- edit the existing JSON instead, `library validate` checks it.
    """
    slug = naming.slugify(name)
    if not slug:
        raise click.ClickException(
            f"guitar name {name!r} has no slug-able characters (needs "
            "letters or digits) -- pick a different name.")
    path = guitars.profile_path(slug)
    if path.exists():
        raise click.ClickException(
            f"a guitar profile already exists at {path} -- edit that JSON "
            "instead (or pick a different name); `library validate` checks "
            "the result.")

    profile = guitars.GuitarProfile(
        name=name, short_name=(short_name or name), type=type_,
        active=None, pickups=None, construction=None, character_md=None,
        genres=[], controls=[],
    )
    guitars.save_profile(profile)  # atomic write + advisory auto-commit
    click.echo(f"Wrote {path}")
    click.echo(f"Slug: {slug}  (short name: {profile.short_name})")
    click.echo(
        "Enrich it (pickups, character_md, genres, controls) by editing the "
        "JSON or via the setup skill; `helixgen library validate` checks it.")


# ---------------------------------------------------------------------------
# describe (top-level)
# ---------------------------------------------------------------------------


@click.command(name="describe")
@click.argument("tone")
def describe(tone: str) -> None:
    """Print one tone's full write-up: identity, tags, variants, description.

    TONE resolves the same way as `library show` (logical slug, metadata
    filename, or any variant's preset_name; unknown/ambiguous exits 1). The
    header is "Artist - Song" or the descriptor; a variants table lists each
    variant's guitar key, preset_name, guitar_settings, its `forked_from`
    provenance when `library fork` made it (source variant + date), whether it
    carries its own `notes_md`, and (when `device
    normalize --yes` has recorded one) a brief `normalized` summary -- date,
    target count, trim count or "in band", the hottest chain-out dBFS (over
    0 = in-chain clipping), scope; the full per-target telemetry lives in
    `library show --json`. The tone's full
    `description_md` (if set) follows verbatim below a blank line -- this is
    the human-oriented counterpart to `library show`'s compact summary /
    raw --json dump.
    """
    slug = _resolve_slug(tone)
    meta = _load_meta_for(slug)

    click.echo(meta.display_base)
    click.echo(f"Tags: {', '.join(meta.tags) if meta.tags else '(none)'}")
    click.echo("")
    click.echo("Variants:")
    for key, variant in meta.variants.items():
        settings = ", ".join(f"{k}={v}" for k, v in variant.guitar_settings.items())
        click.echo(f"  {key}")
        click.echo(f"    preset_name: {variant.preset_name}")
        click.echo(f"    guitar_settings: {settings if settings else '(none)'}")
        if variant.forked_from:
            click.echo(f"    {_forked_from_brief(variant.forked_from)}")
        if variant.normalized:
            click.echo(
                f"    {_normalized_brief(variant.normalized, verbose=True)}")
        if variant.notes_md:
            click.echo(f"    notes_md: set ({len(variant.notes_md)} chars)")

    if meta.description_md:
        click.echo("")
        click.echo(meta.description_md)
