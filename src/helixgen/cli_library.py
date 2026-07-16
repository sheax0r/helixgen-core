"""CLI entry points for `helixgen library` -- read/manage the tone metadata
library (``~/.helixgen/library/tones/*.json``), plus the top-level
``describe`` verb for printing one tone's full write-up.

Pattern: a pure extraction like ``cli_device.py`` -- the ``library`` click
group and the standalone ``describe`` command are imported back into
``cli.py`` (``cli.add_command(library)``, ``cli.add_command(describe)``) so
``helixgen.cli:cli`` stays the single entry point.

**Name resolution** (shared by ``library show``, ``library doc``, and
``describe``): a NAME is tried, in order, as (1) the logical tone slug
(also accepting a trailing ``.json``, i.e. the metadata filename), then (2)
any variant's ``preset_name`` across the whole library. Two tones whose
variants share a ``preset_name`` collide as ambiguous; no match is a
``ClickException`` (exit 1).

PR 2 has no guitar profiles or per-IR metadata yet -- ``library list``'s
``guitars``/``irs`` sections and ``library validate``'s ``guitar_slugs`` are
always empty; the flags/shape exist now so they stay stable when a later PR
fills them in.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import click

from helixgen import home, tone_meta
from helixgen.device.manifest import SetlistManifest


# ---------------------------------------------------------------------------
# name resolution
# ---------------------------------------------------------------------------


def _resolve_slug(name: str) -> str:
    """Resolve ``name`` to a logical tone slug (see module docstring).

    Raises ``click.ClickException`` (exit 1) when nothing matches, or when
    more than one tone's variant ``preset_name`` matches (ambiguous).
    """
    candidate = name[:-5] if name.endswith(".json") else name
    if tone_meta.meta_path(candidate).exists():
        return candidate

    matches = {
        meta.logical_slug
        for meta in tone_meta.load_all_tone_metas()
        if any(v.preset_name == name for v in meta.variants.values())
    }
    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        raise click.ClickException(
            f"{name!r} matches more than one tone's preset_name -- ambiguous "
            f"(logical slugs: {sorted(matches)})"
        )
    raise click.ClickException(
        f"no tone found matching {name!r} -- tried it as a logical slug "
        "(library show/list) and as a variant's preset_name"
    )


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
              help="List only guitar profiles. Always empty in this release "
                   "(no guitar-profile library yet) -- reserved for a later PR.")
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
    guitar-targeted variants). Guitar profiles and per-IR metadata are
    later-PR features -- their sections are always empty lists today; the
    --guitars/--irs flags and the --json shape exist now so both stay stable
    once that library grows. --tones/--guitars/--irs narrow the human
    listing to one section; with none given, everything is shown grouped.
    """
    tones = [_tone_summary(m) for m in tone_meta.load_all_tone_metas()]
    guitars: List[Dict[str, Any]] = []
    irs: List[Dict[str, Any]] = []

    if as_json:
        click.echo(json.dumps({"tones": tones, "guitars": guitars, "irs": irs}, indent=2))
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
        click.echo(f"Guitars ({len(guitars)}): (none yet -- guitar profiles are a later PR)")
    if show_all or only_irs:
        click.echo(f"IRs ({len(irs)}): (none yet -- per-IR metadata is a later PR)")


@library.command(name="show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Dump the exact on-disk metadata JSON instead of a human summary.")
def show_cmd(name: str, as_json: bool) -> None:
    """Show one tone's metadata.

    NAME resolves, in order, as the logical slug, the metadata filename
    (``<slug>.json``), or any variant's ``preset_name``; an unknown or
    ambiguous NAME exits 1. Human output is a compact summary (identity,
    tags, description presence, each variant's key/preset_name/hsp path);
    --json dumps the exact bytes stored on disk. See also the top-level
    `describe` command for a longer, human-oriented write-up.
    """
    slug = _resolve_slug(name)
    if as_json:
        click.echo(tone_meta.meta_path(slug).read_text())
        return

    meta = tone_meta.load_tone_meta(slug)
    click.echo(f"{slug}  -- {meta.display_base}")
    click.echo(f"Tags: {', '.join(meta.tags) if meta.tags else '(none)'}")
    click.echo(f"Description: {'set' if meta.description_md else '(none)'}")
    click.echo(f"Variants ({len(meta.variants)}):")
    for key, variant in meta.variants.items():
        click.echo(f"  {key}: {variant.preset_name}  [{variant.hsp}]")


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
    meta = tone_meta.load_tone_meta(slug)

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
    """Validate every tone's metadata: shape + cross-link checks.

    Runs ``validate_tone_meta`` over every ``library/tones/*.json`` against
    the setlist manifest (each variant's ``preset_name`` must be registered)
    and the known guitar-profile slugs -- in this release there are no
    guitar profiles yet, so only the special ``"generic"`` variant key
    validates; any other key is flagged. Each problem line is prefixed with
    its tone's logical slug. Exits 1 if any problems are found across the
    whole library, 0 if it's fully clean. --json emits
    {"problems": [...]} (empty list when clean) with the same exit-code rule.
    """
    manifest = SetlistManifest.load()
    guitar_slugs: set[str] = set()  # no guitar-profile library yet (later PR)
    tones_dir = home.tones_dir()

    problems: List[str] = []
    for meta in tone_meta.load_all_tone_metas():
        for p in tone_meta.validate_tone_meta(
            meta, tones_dir=tones_dir, manifest=manifest, guitar_slugs=guitar_slugs
        ):
            problems.append(f"{meta.logical_slug}: {p}")

    if as_json:
        click.echo(json.dumps({"problems": problems}, indent=2))
    elif not problems:
        click.echo("OK -- no problems found.")
    else:
        click.echo(f"{len(problems)} problem(s) found:")
        for p in problems:
            click.echo(f"  {p}")

    if problems:
        ctx.exit(1)


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
    meta = tone_meta.load_tone_meta(slug)

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
