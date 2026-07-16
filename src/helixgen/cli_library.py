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

``library list`` now lists guitar profiles (``library/guitars/*.json``)
alongside tones; per-IR metadata is still a later-PR feature so the ``irs``
section stays empty. ``library show`` resolves a NAME as a tone first, then
as a guitar profile. ``library validate`` checks each variant key against the
real profile slugs (falling back to the on-disk variant keys only when NO
profiles exist yet) and adds a separate ``guitar_settings`` warnings channel.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import click

from helixgen import gitops, guitars, home, migrate, naming, tone_meta
from helixgen.device.manifest import ManifestError, SetlistManifest
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


def _guitar_summary(p: "guitars.GuitarProfile") -> Dict[str, Any]:
    """The JSON shape for one guitar in ``library list --json``: slug, full
    name, display short_name, and type."""
    return {
        "slug": p.slug,
        "name": p.name,
        "short_name": p.short_name,
        "type": p.type,
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


# ---------------------------------------------------------------------------
# library group
# ---------------------------------------------------------------------------


@click.group(name="library")
def library() -> None:
    """Manage the tone metadata library (``~/.helixgen/library/``).

    Subcommands:

    \b
      list      enumerate tones (+ guitars/IRs -- empty until a later PR)
      show      one tone's metadata, human summary or raw --json
      doc       set a tone's description_md, or one variant's notes_md
      validate  shape + cross-link checks across every tone's metadata

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
              help="List only per-IR metadata. Always empty in this release "
                   "(no per-IR metadata yet) -- reserved for a later PR.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help='Emit {"tones": [...], "guitars": [...], "irs": [...]} JSON '
                   "instead of a human-readable listing.")
def list_cmd(only_tones: bool, only_guitars: bool, only_irs: bool, as_json: bool) -> None:
    """List the library's metadata: tones, guitar profiles, and IRs.

    Tones are read from every ``~/.helixgen/library/tones/*.json`` (one
    logical tone per file: artist+song or a descriptor, with one or more
    guitar-targeted variants); guitar profiles from
    ``~/.helixgen/library/guitars/*.json`` (slug, name, short_name, type).
    Per-IR metadata is a later-PR feature -- its section is always an empty
    list today; the --irs flag and the --json shape exist now so both stay
    stable once that library grows. --tones/--guitars/--irs narrow the human
    listing to one section; with none given, everything is shown grouped.
    """
    tones = [_tone_summary(m) for m in tone_meta.load_all_tone_metas()]
    guitar_rows = [_guitar_summary(g) for g in guitars.load_all_profiles()]
    irs: List[Dict[str, Any]] = []

    if as_json:
        click.echo(json.dumps(
            {"tones": tones, "guitars": guitar_rows, "irs": irs}, indent=2))
        return

    show_all = not (only_tones or only_guitars or only_irs)

    if show_all or only_tones:
        click.echo(f"Tones ({len(tones)}):")
        if not tones:
            click.echo("  (none)")
        for t in tones:
            click.echo(f"  {t['slug']}  -- {t['display_base']}")
            for key, preset_name in t["variants"].items():
                click.echo(f"    {key}: {preset_name}")
    if show_all or only_guitars:
        click.echo(f"Guitars ({len(guitar_rows)}):")
        if not guitar_rows:
            click.echo("  (none)")
        for g in guitar_rows:
            click.echo(
                f"  {g['slug']}  -- {g['name']} ({g['short_name']}) [{g['type']}]")
    if show_all or only_irs:
        click.echo(f"IRs ({len(irs)}): (none yet -- per-IR metadata is a later PR)")


@library.command(name="show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Dump the exact on-disk metadata JSON instead of a human summary.")
def show_cmd(name: str, as_json: bool) -> None:
    """Show one tone's -- or one guitar profile's -- metadata.

    NAME is resolved as a TONE first (logical slug, the metadata filename
    ``<slug>.json``, or any variant's ``preset_name``); if no tone matches it
    is then tried as a GUITAR profile (slug / name / short_name). An unknown
    or ambiguous NAME exits 1. Human output is a compact summary; --json dumps
    the exact bytes stored on disk (the tone or guitar JSON). See also the
    top-level `describe` command for a longer, human-oriented write-up.
    """
    tone_slug = None
    tone_error: click.ClickException | None = None
    try:
        tone_slug = _resolve_slug(name)
    except click.ClickException as err:
        tone_error = err

    if tone_slug is not None:
        if as_json:
            click.echo(tone_meta.meta_path(tone_slug).read_text())
            return
        meta = _load_meta_for(tone_slug)
        click.echo(f"{tone_slug}  -- {meta.display_base}")
        click.echo(f"Tags: {', '.join(meta.tags) if meta.tags else '(none)'}")
        click.echo(f"Description: {'set' if meta.description_md else '(none)'}")
        click.echo(f"Variants ({len(meta.variants)}):")
        for key, variant in meta.variants.items():
            click.echo(f"  {key}: {variant.preset_name}  [{variant.hsp}]")
        return

    # Not a tone -- try a guitar profile (slug / name / short_name).
    profile = guitars.find_profile(name)
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

    SEPARATELY, a non-fatal WARNINGS channel flags ``guitar_settings`` keys
    that aren't controls on the target guitar's profile (skipped when that
    guitar has no profile -- it may lag). Warnings never change the exit
    code: the command exits 1 only when a PROBLEM (error) is found across the
    library, 0 when clean. --json emits {"problems": [...], "warnings": [...]}
    (both empty when clean) with that same exit-code rule.

    This is the safety net for hand/skill-edited JSON: unlike ``library
    list`` (built on ``load_all_tone_metas()``, which silently skips any
    ``tones/*.json`` that fails to parse -- the right behavior for a
    listing), ``validate`` ALSO independently re-globs ``tones_dir()`` and
    attempts to parse every ``*.json`` file itself; one that isn't valid
    JSON is reported as a problem (prefixed with its filename, since a
    broken file has no parseable identity to key a logical slug by) and
    forces a nonzero exit, instead of silently vanishing from the report.
    """
    manifest = SetlistManifest.load()
    tones_dir = home.tones_dir()
    metas = tone_meta.load_all_tone_metas()

    malformed: List[str] = []
    if tones_dir.is_dir():
        for p in sorted(tones_dir.glob("*.json")):
            try:
                json.loads(p.read_text())
            except (OSError, ValueError) as err:
                malformed.append(f"{p.name}: not valid JSON ({err})")

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
    a per-tone/IR error is recorded and the run CONTINUES. A slug collision (two
    tones -> one destination) is recorded with a rename suggestion and NEITHER
    is moved. Instrument -> guitar-profile seeding is DEFERRED to a later PR
    (the plan records instruments; nothing is written).

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
    requires the other where paired), plus an optional --guitar. With no
    identity flag the .hsp's own meta.name becomes the descriptor. A target
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
    the command exits nonzero when any file failed.
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
    except ManifestError as err:
        raise click.ClickException(str(err)) from err

    click.echo(f"Imported {r['src'].name} -> {dest}")
    click.echo(f"Preset name: {r['preset_name']}")



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
    variant's guitar key, preset_name, and guitar_settings; the tone's full
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

    if meta.description_md:
        click.echo("")
        click.echo(meta.description_md)
