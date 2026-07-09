"""CLI entry points for helixgen."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import click

from helixgen import mutate
from helixgen.bootstrap import bootstrap
from helixgen.chassis import CHASSIS_SHAPE_KEY
from helixgen.generate import GenerateError, ParamValidationError, generate_preset
from helixgen.hsp import HSP_MAGIC, HSP_MAGIC_LEN, read_hsp, write_hsp
from helixgen.ingest import IngestSummary, ingest_path
from helixgen.ir import (
    IrMapping,
    IrMappingError,
    compute_stadium_irhash,
    default_irs_path,
    extract_ir_hashes,
)
from helixgen.library import Library, default_library_path
from helixgen.mutate import MutateError
from helixgen.recipe import generate_from_recipe
from helixgen.spec import SpecError, parse_spec
from helixgen.view import view as view_projection


def _library_option(f):
    return click.option(
        "--library",
        "library_path",
        envvar="HELIXGEN_LIBRARY",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Library directory. Defaults to ~/.helixgen/library/ or $HELIXGEN_LIBRARY.",
    )(f)


def _resolved_library(library_path: Path | None) -> Library:
    return Library(library_path or default_library_path())


def _irs_option(f):
    return click.option(
        "--irs-dir",
        "irs_dir",
        envvar="HELIXGEN_IRS",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="IRs directory. Defaults to ~/.helixgen/irs/ or $HELIXGEN_IRS.",
    )(f)


def _resolved_irs(irs_dir: Path | None) -> IrMapping:
    return IrMapping.load(irs_dir if irs_dir is not None else default_irs_path())


def _format_summary(summary: IngestSummary, library: Library) -> str:
    lines: list[str] = []
    lines.append(f"+{summary.new} new blocks")
    if summary.matched:
        lines.append(f" {summary.matched} already in library")
    if summary.conflicted:
        lines.append(f" {summary.conflicted} conflicts (see *.v2.json files)")
    if summary.skipped:
        lines.append(f" {summary.skipped} files skipped")
    if summary.chassis_extracted:
        lines.append(" chassis extracted")

    if summary.new:
        cats = Counter(b.category for b in library.list_blocks())
        breakdown = ", ".join(f"{n} {c}" for c, n in sorted(cats.items()))
        lines.append(f"  Library now contains: {breakdown}")
    return "\n".join(lines)


@click.group()
@click.version_option()
def cli() -> None:
    """helixgen — generate Line 6 Helix .hlx presets from JSON tone specs."""


@cli.command(name="ingest")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@_library_option
def ingest_cmd(path: Path, library_path: Path | None) -> None:
    """Ingest a .hlx file or a directory of presets/blocks into the library."""
    library = _resolved_library(library_path)
    try:
        summary = ingest_path(path, library)
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e
    click.echo(_format_summary(summary, library))


@cli.command(name="generate")
@click.argument("spec_path", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", "output_path", type=click.Path(path_type=Path), required=True)
@_library_option
@_irs_option
def generate_cmd(
    spec_path: Path,
    output_path: Path,
    library_path: Path | None,
    irs_dir: Path | None,
) -> None:
    """Generate a .hsp preset from a JSON recipe (no sidecar is written).

    For a legacy .hlx chassis, delegates to the original spec-compile path.
    """
    library = _resolved_library(library_path)
    irs = _resolved_irs(irs_dir)
    output_path = Path(output_path)
    try:
        # Parse+validate the recipe before touching the chassis, matching the
        # legacy error-ordering tests rely on (a malformed recipe reports its
        # own error rather than being masked by a missing-chassis error).
        raw = json.loads(spec_path.read_text())
        spec = parse_spec(raw, source=str(spec_path))
        chassis = library.load_chassis()
        shape = chassis.get(CHASSIS_SHAPE_KEY, "hlx")
        if shape == "hsp":
            data = generate_from_recipe(
                spec, library, irs=irs, chassis=chassis, source=str(spec_path)
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(data)
        else:
            generate_preset(spec_path, output_path, library, irs=irs)
    except (KeyError, LookupError, SpecError, ParamValidationError, GenerateError, FileNotFoundError) as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Wrote {output_path}")


@cli.command(name="view")
@click.argument("hsp_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o", "--output", "output_path", type=click.Path(path_type=Path), default=None,
    help="Write the projection here instead of stdout (non-authoritative — the .hsp stays the source of truth).",
)
@_library_option
@_irs_option
def view_cmd(
    hsp_path: Path, output_path: Path | None, library_path: Path | None, irs_dir: Path | None
) -> None:
    """Print a read-only projection of a Stadium .hsp preset."""
    library = _resolved_library(library_path)
    irs = _resolved_irs(irs_dir)
    try:
        body = read_hsp(hsp_path)
        projection = view_projection(body, library, irs=irs)
    except (KeyError, LookupError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    text = json.dumps(projection, indent=2)
    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text)
        click.echo(
            f"Wrote {output_path} (non-authoritative projection; {hsp_path} remains the source of truth)"
        )
    else:
        click.echo(text)


def _coerce_cli_value(raw: str):
    """Parse a CLI param value: bool, int, float, else string."""
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _run_mutation(preset_path: Path, library_path, irs_dir, mutation) -> None:
    """Read a `.hsp` in place, apply `mutation(body, library, irs)`, write it back.

    `mutation` mutates `body` in place and returns a `list[str]` of warnings
    (or `None`). No sidecar is read or written — the `.hsp` is the sole
    source of truth.
    """
    library = _resolved_library(library_path)
    irs = _resolved_irs(irs_dir)
    preset_path = Path(preset_path)
    try:
        body = read_hsp(preset_path)
        warnings = mutation(body, library, irs) or []
        write_hsp(preset_path, body)
    except (MutateError, KeyError, LookupError, SpecError,
            ParamValidationError, GenerateError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    for w in warnings:
        click.echo(f"warning: {w}", err=True)
    click.echo(f"Patched {preset_path}")


@cli.command(name="set-param")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("block")
@click.argument("param")
@click.argument("value")
@click.option("--path", "path_idx", type=int, default=None)
@click.option("--lane", type=int, default=None)
@click.option("--pos", type=int, default=None)
@_library_option
@_irs_option
def set_param_cmd(preset_path, block, param, value, path_idx, lane, pos, library_path, irs_dir):
    """Set a block param: helixgen set-param preset.hsp "Brit Amp" Drive 0.85"""
    def _mutation(body, library, irs):
        mutate.set_param(body, block, param, _coerce_cli_value(value), library,
                          path=path_idx, lane=lane, pos=pos)

    _run_mutation(preset_path, library_path, irs_dir, _mutation)


@cli.command(name="enable")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("block")
@click.option("--snapshot", default=None)
@click.option("--path", "path_idx", type=int, default=None)
@click.option("--lane", type=int, default=None)
@click.option("--pos", type=int, default=None)
@_library_option
@_irs_option
def enable_cmd(preset_path, block, snapshot, path_idx, lane, pos, library_path, irs_dir):
    """Enable (un-bypass) a block."""
    def _mutation(body, library, irs):
        mutate.set_enabled(body, block, True, library,
                            snapshot=snapshot, path=path_idx, lane=lane, pos=pos)

    _run_mutation(preset_path, library_path, irs_dir, _mutation)


@cli.command(name="disable")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("block")
@click.option("--snapshot", default=None)
@click.option("--path", "path_idx", type=int, default=None)
@click.option("--lane", type=int, default=None)
@click.option("--pos", type=int, default=None)
@_library_option
@_irs_option
def disable_cmd(preset_path, block, snapshot, path_idx, lane, pos, library_path, irs_dir):
    """Disable (bypass) a block."""
    def _mutation(body, library, irs):
        mutate.set_enabled(body, block, False, library,
                            snapshot=snapshot, path=path_idx, lane=lane, pos=pos)

    _run_mutation(preset_path, library_path, irs_dir, _mutation)


@cli.command(name="add-block")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("block")
@click.option("--path", "path_idx", type=int, default=0)
@click.option("--after", default=None)
@_library_option
@_irs_option
def add_block_cmd(preset_path, block, path_idx, after, library_path, irs_dir):
    """Add a block to a path (optionally after another block)."""
    def _mutation(body, library, irs):
        mutate.add_block(body, block, library, path=path_idx, after=after)

    _run_mutation(preset_path, library_path, irs_dir, _mutation)


@cli.command(name="remove-block")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("block")
@click.option("--path", "path_idx", type=int, default=None)
@click.option("--lane", type=int, default=None)
@click.option("--pos", type=int, default=None)
@_library_option
@_irs_option
def remove_block_cmd(preset_path, block, path_idx, lane, pos, library_path, irs_dir):
    """Remove a block from a path."""
    def _mutation(body, library, irs):
        mutate.remove_block(body, block, library, path=path_idx, lane=lane, pos=pos)

    _run_mutation(preset_path, library_path, irs_dir, _mutation)


@cli.command(name="swap-model")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("old")
@click.argument("new")
@click.option("--path", "path_idx", type=int, default=None)
@click.option("--lane", type=int, default=None)
@click.option("--pos", type=int, default=None)
@_library_option
@_irs_option
def swap_model_cmd(preset_path, old, new, path_idx, lane, pos, library_path, irs_dir):
    """Swap a block for another of the same category."""
    def _mutation(body, library, irs):
        return mutate.swap_model(body, old, new, library, path=path_idx, lane=lane, pos=pos)

    _run_mutation(preset_path, library_path, irs_dir, _mutation)


@cli.command(name="list-blocks")
@click.option("--category", default=None, help="Filter to one category.")
@_library_option
def list_blocks_cmd(category: str | None, library_path: Path | None) -> None:
    """List blocks in the library, grouped by category."""
    library = _resolved_library(library_path)
    blocks = library.list_blocks(category=category)
    if not blocks:
        click.echo("(no blocks in library)")
        return
    by_category: dict[str, list] = {}
    for b in blocks:
        by_category.setdefault(b.category, []).append(b)
    for cat in sorted(by_category):
        click.echo(f"{cat}:")
        for b in sorted(by_category[cat], key=lambda x: x.display_name):
            click.echo(f"  {b.display_name}  [{b.model_id}]")


@cli.command(name="show-block")
@click.argument("name_or_id")
@_library_option
def show_block_cmd(name_or_id: str, library_path: Path | None) -> None:
    """Print a block's schema (params, defaults, types) for spec authoring."""
    library = _resolved_library(library_path)
    try:
        block = library.find_block(name_or_id)
    except (KeyError, LookupError) as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"{block.display_name}  [{block.model_id}]")
    click.echo(f"category: {block.category}")
    if block.aliases:
        click.echo(f"aliases: {', '.join(block.aliases)}")
    click.echo("params:")
    for name, schema in block.params.items():
        meta_bits = [schema["type"], f"default={schema.get('default')!r}"]
        if "observed_range" in schema:
            meta_bits.append(f"observed={schema['observed_range']}")
        if "values" in schema:
            meta_bits.append(f"values={schema['values']}")
        click.echo(f"  {name}  ({', '.join(meta_bits)})")


@cli.command(name="controllers")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the full mapping as a JSON array instead of English lines.")
@click.option("--device", default="stadium_xl", help="Device key (default stadium_xl).")
def controllers_cmd(as_json: bool, device: str) -> None:
    """List the device's assignable controllers with their English name + position.

    Shows the canonical, device-accurate vocabulary (FS1–FS5, FS7–FS11, EXP1,
    EXP2, EXP1Toe). FS6 (MODE) and FS12 (TAP/Tuner) are reserved and not listed.
    """
    from helixgen import controllers as _controllers
    mapping = _controllers.controller_mapping(device)
    if as_json:
        click.echo(json.dumps(mapping, indent=2))
        return
    for row in mapping:
        click.echo(f"{row['id']:<8} {row['english']}")


@cli.command(name="bootstrap")
@click.option("--phelix-ref", "ref", default="main", help="Git ref of sensorium/phelix to clone.")
@_library_option
def bootstrap_cmd(ref: str, library_path: Path | None) -> None:
    """Clone sensorium/phelix and ingest its blocks/ folder."""
    library = _resolved_library(library_path)
    try:
        summary = bootstrap(library, ref=ref)
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e
    click.echo(_format_summary(summary, library))


@cli.command(name="register-irs")
@click.argument(
    "paths",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--force", is_flag=True, default=False, help="Overwrite existing hash mappings.")
@_irs_option
def register_irs_cmd(
    paths: tuple[Path, ...],
    force: bool,
    irs_dir: Path | None,
) -> None:
    """Register user impulse-response WAVs so generated presets can reference them.

    Two forms:

    \b
    - register-irs <preset.hsp> <wav1> <wav2> ...   bind preset's irhash slots
                                                    to the given wavs in order
    - register-irs <wav1> <wav2> ...                compute each wav's Stadium
                                                    hash directly (no device export
                                                    needed) and register it
    """
    paths_list = list(paths)
    first_ext = paths_list[0].suffix.lower()

    if first_ext in {".hsp", ".hlx"}:
        preset_path = paths_list[0]
        wav_paths = paths_list[1:]
        if not wav_paths:
            raise click.ClickException("at least one wav arg required after preset")
        raw = preset_path.read_bytes()
        if not raw.startswith(HSP_MAGIC):
            raise click.ClickException(f"{preset_path} is not a Stadium .hsp file")
        body = json.loads(raw[HSP_MAGIC_LEN:])
        hashes = extract_ir_hashes(body)
        if len(hashes) != len(wav_paths):
            raise click.ClickException(
                f"preset has {len(hashes)} IR blocks, got {len(wav_paths)} wav arg(s)"
            )
    else:
        wav_paths = paths_list
        for p in wav_paths:
            if p.suffix.lower() != ".wav":
                raise click.ClickException(
                    f"unexpected non-wav arg: {p} "
                    "(only the first arg may be .hsp/.hlx)"
                )
        try:
            hashes = [compute_stadium_irhash(w) for w in wav_paths]
        except (RuntimeError, NotImplementedError, FileNotFoundError) as e:
            raise click.ClickException(str(e)) from e

    mapping = _resolved_irs(irs_dir)
    try:
        for h, wav in zip(hashes, wav_paths):
            mapping.register(h, wav, force=force)
    except IrMappingError as e:
        raise click.ClickException(str(e)) from e
    mapping.save()
    click.echo(f"Registered {len(hashes)} IR(s) to {mapping.irs_dir / 'mapping.json'}")


@cli.command(name="ir-scan")
@click.argument(
    "directories",
    nargs=-1,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--rescan",
    is_flag=True,
    default=False,
    help="Recompute hashes even for files already in the cache.",
)
@click.option(
    "--remove",
    "remove_basename",
    type=str,
    default=None,
    help="Forget one entry by wav basename and exit.",
)
@_irs_option
def ir_scan_cmd(
    directories: tuple[Path, ...],
    rescan: bool,
    remove_basename: str | None,
    irs_dir: Path | None,
) -> None:
    """Recursively scan directories for .wav files and cache their Stadium hashes.

    Skips files already cached (by absolute path) unless --rescan. Skips files
    that can't be hashed (non-48 kHz, libsndfile errors) with a stderr warning;
    does not abort the scan.

    Use --remove <basename> to forget a single entry (no directory args).
    """
    mapping = _resolved_irs(irs_dir)

    if remove_basename is not None:
        if directories:
            raise click.ClickException("--remove takes no directory arguments")
        hits = [h for h, p in mapping.entries.items() if Path(p).name == remove_basename]
        if not hits:
            raise click.ClickException(f"no entry with basename {remove_basename!r}")
        if len(hits) > 1:
            paths = ", ".join(mapping.entries[h] for h in hits)
            raise click.ClickException(
                f"basename {remove_basename!r} matches multiple entries: {paths}"
            )
        del mapping.entries[hits[0]]
        mapping.save()
        click.echo(f"Removed {remove_basename}")
        return

    if not directories:
        raise click.ClickException(
            "at least one directory required (or use --remove <basename>)"
        )

    cached_paths = {Path(p).resolve() for p in mapping.entries.values() if Path(p).is_absolute()}
    cached_paths |= {(mapping.irs_dir / p).resolve() for p in mapping.entries.values()
                     if not Path(p).is_absolute()}

    scanned = 0
    added = 0
    skipped_cached = 0
    skipped_error = 0
    for root in directories:
        for wav in sorted(root.rglob("*")):
            if not wav.is_file() or wav.suffix.lower() != ".wav":
                continue
            scanned += 1
            wav_abs = wav.resolve()
            if not rescan and wav_abs in cached_paths:
                skipped_cached += 1
                continue
            try:
                h = compute_stadium_irhash(wav)
            except (NotImplementedError, RuntimeError, FileNotFoundError) as e:
                click.echo(f"skip {wav}: {e}", err=True)
                skipped_error += 1
                continue
            try:
                mapping.register(h, wav, force=rescan)
            except IrMappingError as e:
                click.echo(f"skip {wav}: {e}", err=True)
                skipped_error += 1
                continue
            cached_paths.add(wav_abs)
            added += 1

    mapping.save()
    click.echo(
        f"Scanned {scanned} wav(s): {added} added, "
        f"{skipped_cached} already cached, {skipped_error} skipped (errors)"
    )


@cli.command(name="list-irs")
@_irs_option
def list_irs_cmd(irs_dir: Path | None) -> None:
    """List registered IR hashes and their wav paths."""
    mapping = _resolved_irs(irs_dir)
    for hash_ in sorted(mapping.entries):
        click.echo(f"{hash_}  {mapping.entries[hash_]}")
