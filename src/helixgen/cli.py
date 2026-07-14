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
from helixgen.irhash_cache import IrHashCache, cached_irhash
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
            _auto_register_tone(output_path)
        else:
            generate_preset(spec_path, output_path, library, irs=irs)
    except (KeyError, LookupError, SpecError, ParamValidationError, GenerateError, FileNotFoundError) as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Wrote {output_path}")


def _auto_register_tone(hsp_path: Path) -> None:
    """Record a freshly-authored .hsp in the tone library (off-device by default).

    Advisory: a registration failure warns but never fails ``generate`` (the
    .hsp is already written)."""
    try:
        from helixgen.device.manifest import SetlistManifest

        m = SetlistManifest.load()
        m.register_tone(hsp_path, source="authored")
        m.save()
    except Exception as e:  # noqa: BLE001 — registration is advisory
        click.echo(f"warning: could not register tone in library: {e}", err=True)


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
        cache = IrHashCache.load()
        try:
            hashes = [cached_irhash(w, cache=cache) for w in wav_paths]
        except (RuntimeError, NotImplementedError, FileNotFoundError) as e:
            raise click.ClickException(str(e)) from e
        cache.save()

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

    Skips a WAV only when it is already registered AND its cached hash is still
    valid for the file on disk (matching mtime + size) — an edited or replaced
    WAV is detected and re-hashed. Pass --rescan to recompute unconditionally.
    Skips files that can't be hashed (non-48 kHz, libsndfile errors) with a
    stderr warning; does not abort the scan.

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

    registered_paths = {Path(p).resolve() for p in mapping.entries.values() if Path(p).is_absolute()}
    registered_paths |= {(mapping.irs_dir / p).resolve() for p in mapping.entries.values()
                         if not Path(p).is_absolute()}

    cache = IrHashCache.load()

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
            # Skip only when already registered AND the cached hash is still
            # valid for the file on disk (stat unchanged). An edited/replaced
            # WAV misses the cache and is recomputed below.
            if not rescan and wav_abs in registered_paths and cache.get(wav) is not None:
                skipped_cached += 1
                continue
            try:
                if rescan:
                    h = compute_stadium_irhash(wav)
                    cache.put(wav, h)
                else:
                    h = cached_irhash(wav, cache=cache)
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
            registered_paths.add(wav_abs)
            added += 1

    mapping.save()
    cache.save()
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


# --- device: network control of a Line 6 Helix Stadium --------------------

def _device_option(f):
    """Add shared --ip / --port options for the networked device commands."""
    f = click.option(
        "--ip",
        envvar="HELIXGEN_HELIX_IP",
        default="192.168.4.84",
        show_default=True,
        help="Helix device IP address ($HELIXGEN_HELIX_IP).",
    )(f)
    f = click.option(
        "--port",
        default=2002,
        show_default=True,
        type=int,
        help="Helix device control port.",
    )(f)
    return f


def _setlist_container(name: str) -> int:
    """Map a --setlist name (user/factory/throwaway) to its container constant."""
    from helixgen.device import USER, FACTORY, THROWAWAY

    mapping = {"user": USER, "factory": FACTORY, "throwaway": THROWAWAY}
    try:
        return mapping[name]
    except KeyError as e:  # pragma: no cover - click Choice guards this
        raise click.ClickException(f"unknown setlist {name!r}") from e


def _auto_upload_irs(ip: str, hashes) -> None:
    """Upload each missing IR hash by resolving it to a local wav via the
    helixgen IR mapping, then SFTP-pushing it (device auto-registers).

    Thin echo-formatting wrapper around the shared core in
    ``helixgen.device.ir_upload`` (backlog #6 — the same core also backs
    ``device sync`` and the MCP ``device_install_preset``). Unlike those two
    (which tolerate a per-IR upload failure and keep going — a preset install
    or sync run shouldn't be all-or-nothing on IR trouble), the CLI's
    ``--auto-irs`` still **aborts the whole install** on a hard upload error
    (``push_ir`` itself failing, e.g. a dropped connection) — matching the
    original behavior of never installing a preset when an IR it references
    couldn't be pushed. It now does so via a clean ``ClickException`` instead
    of letting the raw exception surface, and after echoing every hash's
    outcome (not just the first failure) so the user sees the full picture
    before the command exits non-zero."""
    from helixgen.device import ir_upload

    upload_errors = []
    for entry in ir_upload.upload_missing_irs(ip, list(hashes)):
        outcome = entry.get("outcome")
        if outcome == "no_mapping":
            # Applies identically to every hash (mapping.json itself failed
            # to load) — abort the whole command, matching the original
            # upfront-check behavior.
            raise click.ClickException(entry["note"])
        if outcome in ("already", "imported"):
            click.echo(entry["note"])
        else:
            click.echo(f"warning: {entry['note']}", err=True)
            if outcome == "upload_error":
                upload_errors.append(entry["note"])
    if upload_errors:
        raise click.ClickException(
            "IR upload failed: " + "; ".join(upload_errors))


def _utc_now() -> str:
    """ISO-8601 UTC timestamp for ledger entries (injected so the module stays
    deterministic)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _tone_by_cid(m, cid: int):
    """Return the manifest tone name whose observed device cid matches, or None."""
    for name, rec in m.tones.items():
        dev = rec.get("device")
        if isinstance(dev, dict) and dev.get("cid") == cid:
            return name
    return None


def _record_placement(*, setlist: str, posi: int, name: str, cid: int | None,
                      source_kind: str, source_path: str | None = None,
                      model: str | None = None) -> None:
    """Record a device placement in the tone-library manifest. Best-effort: a
    failure warns but never fails the device command (the write already
    succeeded)."""
    try:
        from helixgen.device.manifest import SetlistManifest

        m = SetlistManifest.load()
        if name not in m.tones:
            if source_path and str(source_path).endswith(".hsp"):
                name = m.register_tone(source_path, source="import-local")
            elif source_path:
                # a pushed .sbe (or other local source): store the path verbatim
                m.tones[name] = {"path": str(source_path), "content_hash": None,
                                 "doc": None, "source": "push", "slot": None,
                                 "device": None}
            else:
                m.register_pathless(name, source="save" if source_kind == "save" else "create")
        slot = _slot_from_posi(posi)
        if slot:
            m.mark_on_device(name, slot)
        if cid is not None:
            m.tones[name]["device"] = {"cid": cid, "posi": posi}
        if setlist and setlist != "user":
            m.add_to_setlist(setlist, name)
        m.save()
    except Exception as e:  # noqa: BLE001 — advisory, never fatal
        click.echo(f"warning: could not update tone library: {e}", err=True)


def _slot_from_posi(posi):
    from helixgen.device.manifest import _posi_to_slot
    return _posi_to_slot(posi)


def _ledger_rename(cid: int, new_name: str) -> None:
    """Best-effort: reflect a device rename in the tone library."""
    try:
        from helixgen.device.manifest import SetlistManifest

        m = SetlistManifest.load()
        old = _tone_by_cid(m, cid)
        if old and old != new_name:
            m.tones[new_name] = m.tones.pop(old)
            for rec in m.setlists_map.values():
                rec["tones"] = [new_name if t == old else t for t in rec["tones"]]
            m.save()
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not update tone library: {e}", err=True)


def _ledger_remove(cid: int) -> None:
    """Best-effort: drop a deleted preset from the tone library (membership +
    on-device state; the tone stays in the library)."""
    try:
        from helixgen.device.manifest import SetlistManifest

        m = SetlistManifest.load()
        name = _tone_by_cid(m, cid)
        if name:
            m.tones[name]["slot"] = None
            m.tones[name]["device"] = None
            m.save()
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not update tone library: {e}", err=True)


def _install_hsp_open(h, body: dict, container: int, pos: int, name: str, *,
                      setlist_label: str, auto_irs: bool = False,
                      force: bool = False, ip: str | None = None) -> int:
    """Install a parsed .hsp ``body`` onto an already-open client at
    ``(container, pos)`` and return the new cid. Shared by ``device install``
    and ``device slots restore``. Raises ClickException on any failure.

    Template-free: the ``.hsp`` is transcoded straight into a device
    ``_sbepgsm`` blob (:func:`transcode.hsp_to_sbepgsm`) and written into an
    empty slot — no device template is loaded, so the active tone is untouched.

    ``force`` skips the slot-emptiness check so the push proceeds at an
    occupied posi (``device slots restore --force`` — #25; the occupant is
    NOT deleted, matching the ``.sbe`` path); without it an occupied slot is
    refused.
    """
    from helixgen.device import bridge, transcode

    if not force and h.find_by_pos(container, pos) is not None:
        raise click.ClickException(f"{setlist_label} slot {pos} is not empty")
    missing = sorted(bridge.check_irs(h, body)["missing"])
    if missing and auto_irs:
        _auto_upload_irs(ip, missing)
    else:
        for m in missing:
            click.echo(
                f"warning: IR {m} is referenced but not on the device; "
                f"re-run with --auto-irs, or import it (helixgen register-irs / "
                f"the editor), or the cab will be silent", err=True)
    try:
        blob = transcode.hsp_to_sbepgsm(body, strict=True)
    except bridge.UnresolvedModel as e:
        raise click.ClickException(str(e)) from e
    with h.mutating():
        cid = h._raw.push_to_slot(container, pos, name, blob)
    if cid is None:
        raise click.ClickException("failed to install preset")
    return cid


@cli.group(name="device")
def device() -> None:
    """Drive a networked Line 6 Helix Stadium over the LAN.

    Requires the ``device`` extra (``pip install 'helixgen[device]'``) for the
    pyzmq/msgpack transport. Point at the device with --ip / --port or set
    $HELIXGEN_HELIX_IP.
    """


@device.command(name="list")
@click.option(
    "--setlist",
    type=click.Choice(["user", "factory", "throwaway"]),
    default="user",
    show_default=True,
    help="Which setlist to list.",
)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the preset list as JSON.")
@_device_option
def device_list(setlist: str, as_json: bool, ip: str, port: int) -> None:
    """List the presets in a setlist (default: user)."""
    from helixgen.device import HelixClient, HelixError, slot_label

    container = _setlist_container(setlist)
    try:
        with HelixClient(ip, port) as h:
            presets = h.list_presets(container)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(presets, indent=2))
        return
    for m in presets:
        click.echo(f"{slot_label(m.get('posi')):<4} cid={m.get('cid_')}  {m.get('name', '')}")


@device.command(name="setlists")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the setlist list as JSON.")
@_device_option
def device_setlists(as_json: bool, ip: str, port: int) -> None:
    """List the device's setlist containers."""
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            setlists = h.list_setlists()
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(setlists, indent=2))
        return
    for m in setlists:
        click.echo(f"cid={m.get('cid_')}  {m.get('name', '')}")


@device.command(name="info")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the device info as JSON (includes the raw reply).")
@_device_option
def device_info(as_json: bool, ip: str, port: int) -> None:
    """Show the connected device's identity: model, firmware, serial, storage.

    Read-only (`/ProductInfoGet` — part of the editor's own connect
    handshake); never touches presets or the edit buffer.
    """
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            info = h.product_info()
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return

    def _gb(n):
        return f"{n / 1e9:.1f} GB" if isinstance(n, (int, float)) else "?"

    model = info.get("model") or "?"
    if info.get("helixgen_model"):
        model = f"{model} ({info['helixgen_model']})"
    click.echo(f"model:     {model}")
    click.echo(f"device id: {info.get('device_id')}")
    click.echo(f"serial:    {info.get('serial')}")
    fw = info.get("firmware") or "?"
    build = info.get("firmware_build")
    date = info.get("firmware_date")
    extra = " ".join(str(x) for x in (f"build {build}" if build else None,
                                      date) if x)
    click.echo(f"firmware:  {fw}{f'  ({extra})' if extra else ''}")
    click.echo(f"storage:   {_gb(info.get('sd_available_bytes'))} free of "
               f"{_gb(info.get('sd_total_bytes'))}")


@device.group(name="settings")
def device_settings() -> None:
    """Read/write the device's **Global Settings** over the network.

    The Stadium exposes its Global Settings pages (Ins/Outs, Switches/Pedals,
    Displays, Preferences, Songs, Tempo/Click, MIDI, Date/Time) plus Tuner and
    Wireless as device *properties*. `list` browses the catalog, `get` reads a
    live value, `set` writes one — no Stadium app needed. Keys are grouped into
    pages; run `helixgen device settings list` to see them.
    """


@device_settings.command(name="list")
@click.option("--page", "page", default=None,
              help="Only this page (e.g. ins-outs, midi, tuner). Omit for all.")
@click.option("--values", is_flag=True, default=False,
              help="Also fetch each key's live value + range from the device.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit as JSON.")
@_device_option
def device_settings_list(page, values, as_json, ip, port):
    """List Global-Settings keys, grouped by page (offline unless --values)."""
    from helixgen.device import settings as S
    from helixgen.device import HelixClient, HelixError

    try:
        catalog = {page: S.keys_for_page(page)} if page else S.pages()
    except KeyError:
        raise click.ClickException(
            f"unknown page {page!r}; choose from {', '.join(S.page_names())}")

    if not values:
        if as_json:
            click.echo(json.dumps(catalog, indent=2))
            return
        for pg in sorted(catalog):
            click.echo(f"\n[{pg}]")
            for k in catalog[pg]:
                click.echo(f"  {k}")
        return

    rows = []
    aborted = None
    try:
        with HelixClient(ip, port) as h:
            for pg in sorted(catalog):
                for k in catalog[pg]:
                    try:
                        d = h.get_property_def(k)
                        v = h.get_property(k)
                        rows.append({"page": pg, "key": k, "name": d.name,
                                     "value": v.value,
                                     "display": S.render_value(d, v.value),
                                     "type": d.type, "min": d.vmin, "max": d.vmax,
                                     "enum": d.enum})
                    except (HelixError, ValueError) as e:
                        rows.append({"page": pg, "key": k, "error": str(e)})
                    # a dead socket (reconnect exhausted) makes every remaining
                    # key fast-fail — stop and report a clean partial result.
                    if h.sock is None:
                        aborted = k
                        break
                if aborted:
                    break
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        out = {"settings": rows}
        if aborted:
            out["aborted_at"] = aborted
        click.echo(json.dumps(out, indent=2))
        return
    cur = None
    for r in rows:
        if r["page"] != cur:
            cur = r["page"]
            click.echo(f"\n[{cur}]")
        if "error" in r:
            click.echo(f"  {r['key']:<40} <err: {r['error']}>")
        else:
            rng = (f"  {{{', '.join(r['enum'])}}}" if r["enum"]
                   else f"  [{r['min']}..{r['max']}]")
            click.echo(f"  {r['key']:<40} = {r['display']:<16} {r['name']}{rng}")
    if aborted:
        click.echo(f"\n(connection lost — stopped at {aborted}; re-run to continue)")


@device_settings.command(name="get")
@click.argument("key")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit as JSON.")
@_device_option
def device_settings_get(key, as_json, ip, port):
    """Read one Global-Settings value (with its name, range, and enum labels)."""
    from helixgen.device import settings as S
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            d = h.get_property_def(key)
            v = h.get_property(key)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps({
            "key": key, "name": d.name, "value": v.value,
            "display": S.render_value(d, v.value), "type": d.type,
            "min": d.vmin, "max": d.vmax, "default": d.default,
            "enum": d.enum, "page": S.page_for_key(key)}, indent=2))
        return
    rng = (f"{{{', '.join(d.enum)}}}" if d.enum else f"[{d.vmin}..{d.vmax}]")
    click.echo(f"{key}")
    click.echo(f"  name    {d.name}")
    click.echo(f"  value   {S.render_value(d, v.value)}")
    click.echo(f"  range   {rng}   (default {d.default})")


@device_settings.command(name="set")
@click.argument("key")
@click.argument("value")
@_device_option
def device_settings_set(key, value, ip, port):
    """Write one Global-Settings value. VALUE may be a number or an enum label
    (e.g. `helixgen device settings set global.tuner.type Strobe`)."""
    from helixgen.device import settings as S
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            d = h.get_property_def(key)
            coerced = S.coerce_value(d, value)
            ok = h.set_property(key, d.type, coerced)
            readback = h.get_property(key)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"device did not confirm the write to {key}")
    click.echo(f"{key} = {S.render_value(d, readback.value)}  ({d.name})")


@device.group(name="globaleq")
def device_globaleq() -> None:
    """Write the device's **Global EQ** over the network (no Stadium app).

    The Stadium has three independent Global EQs — one per output layer: 1/4"
    (`qtr`), XLR (`xlr`), Phones (`pho`) — each a 7-band EQ (lowcut, lowshelf,
    low, mid, high, highshelf, highcut) plus an output level. `list` prints the
    catalog; `set` writes one band parameter. Global EQ is **write-only** over
    the network (the device serves no per-key read-back), so there is no `get`.
    """


@device_globaleq.command(name="list")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit as JSON.")
def device_globaleq_list(as_json):
    """List the Global EQ outputs, bands, and their valid params (offline)."""
    from helixgen.device import globaleq as G

    cat = G.catalog()
    if as_json:
        click.echo(json.dumps(cat, indent=2))
        return
    cur = None
    for r in cat:
        if r["output"] != cur:
            cur = r["output"]
            click.echo(f"\n[{r['output']}]  {r['output_name']}")
        if r["band"]:
            freq = f"  (default {r['default_freq']:g} Hz)" if r["default_freq"] else ""
            click.echo(f"  {r['band']:<10} #{r['band_index']}  "
                       f"params: {', '.join(r['params'])}{freq}")
        else:
            click.echo(f"  {'(output)':<10}      params: {', '.join(r['params'])}")
    click.echo("\nExample: helixgen device globaleq set qtr low gain 3.5")


@device_globaleq.command(
    name="set", context_settings={"ignore_unknown_options": True})
@click.argument("output")
@click.argument("band")
@click.argument("param")
@click.argument("value")
@_device_option
def device_globaleq_set(output, band, param, value, ip, port):
    """Write one Global EQ parameter.

    OUTPUT ∈ qtr/xlr/pho. BAND ∈ lowcut/lowshelf/low/mid/high/highshelf/highcut
    (or use `-` with PARAM `level` for the output level). PARAM ∈
    enable/freq/gain/q/slope/level. Examples:

      helixgen device globaleq set qtr low gain 3.5

      helixgen device globaleq set xlr lowcut enable off

      helixgen device globaleq set pho - level -2.0
    """
    from helixgen.device import globaleq as G
    from helixgen.device import HelixClient, HelixError

    band_arg = "" if band.strip() in ("-", "") else band
    try:
        key = G.key_for(output, band_arg, param)  # validates before connecting
        with HelixClient(ip, port) as h:
            ok = h.set_globaleq(output, band_arg, param, value)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"device did not confirm the Global EQ write ({key})")
    click.echo(f"{key} = {value}")


@device.command(name="read")
@click.argument("cid", type=int)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the content ref as JSON.")
@_device_option
def device_read(cid: int, as_json: bool, ip: str, port: int) -> None:
    """Read the content ref for a CID (name/slot/parent)."""
    from helixgen.device import HelixClient, HelixError, slot_label

    try:
        with HelixClient(ip, port) as h:
            ref = h.get_ref(cid)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if ref is None:
        raise click.ClickException(f"no content ref for cid {cid}")
    if as_json:
        click.echo(json.dumps(ref, indent=2))
        return
    click.echo(f"name:   {ref.get('name', '')}")
    click.echo(f"cid:    {ref.get('cid_', cid)}")
    click.echo(f"parent: {ref.get('cpid')}")
    click.echo(f"slot:   {slot_label(ref.get('posi'))}")


@device.command(name="load")
@click.argument("cid", type=int)
@_device_option
def device_load(cid: int, ip: str, port: int) -> None:
    """Load a preset into the edit buffer by CID."""
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            ok = h.load_preset(cid)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to load preset cid {cid}")
    click.echo(f"loaded cid {cid}")


@device.command(name="create")
@click.option("--from", "src_cid", type=int, required=True,
              help="Source preset CID to copy from.")
@click.option("--setlist", type=click.Choice(["user", "factory", "throwaway"]),
              default="user", show_default=True, help="Destination setlist.")
@click.option("--pos", type=int, required=True, help="Destination slot (posi).")
@_device_option
def device_create(src_cid: int, setlist: str, pos: int, ip: str, port: int) -> None:
    """Copy a preset into a setlist slot; prints the new CID."""
    from helixgen.device import HelixClient, HelixError

    container = _setlist_container(setlist)
    try:
        with HelixClient(ip, port) as h:
            new_cid = h._raw.create_from(src_cid, container, pos)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if new_cid is None:
        raise click.ClickException(
            f"failed to copy cid {src_cid} into {setlist} slot {pos}")
    click.echo(f"created cid {new_cid}")
    _record_placement(setlist=setlist, posi=pos, name=f"(copy of cid {src_cid})",
                      cid=new_cid, source_kind="copy")


@device.command(name="rename")
@click.argument("cid", type=int)
@click.argument("new_name")
@_device_option
def device_rename(cid: int, new_name: str, ip: str, port: int) -> None:
    """Rename the preset at CID."""
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            ok = h.rename(cid, new_name)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to rename cid {cid}")
    click.echo(f"renamed cid {cid} -> {new_name!r}")
    _ledger_rename(cid, new_name)


@device.command(name="delete")
@click.argument("cid", type=int)
@click.option("--setlist", type=click.Choice(["user", "factory", "throwaway"]),
              default="user", show_default=True, help="Setlist the preset lives in.")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@_device_option
def device_delete(cid: int, setlist: str, yes: bool, ip: str, port: int) -> None:
    """Delete the preset at CID from a setlist."""
    from helixgen.device import HelixClient, HelixError

    if not yes:
        click.confirm(f"Delete cid {cid} from {setlist} setlist?", abort=True)
    container = _setlist_container(setlist)
    try:
        with HelixClient(ip, port) as h:
            ok = h._raw.delete(container, [cid])
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to delete cid {cid}")
    click.echo(f"deleted cid {cid}")
    _ledger_remove(cid)


@device.command(name="set-param")
@click.argument("path", type=int)
@click.argument("block", type=int)
@click.argument("param_id", type=int)
@click.argument("value", type=float)
@_device_option
def device_set_param(path: int, block: int, param_id: int, value: float,
                     ip: str, port: int) -> None:
    """Set one param in the edit buffer (path block param_id value)."""
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            ok = h.set_param(path, block, param_id, value)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(
            f"failed to set param {param_id} on path {path} block {block}")
    click.echo(f"set path {path} block {block} param {param_id} = {value}")


@device.command(name="snapshot")
@click.argument("index", type=int)
@_device_option
def device_snapshot(index: int, ip: str, port: int) -> None:
    """Recall a snapshot (0-based, 0..7) on the live device.

    Changes the ACTIVE tone's current snapshot immediately (like stepping the
    snapshot footswitch). `/activateSnapshot`.
    """
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            h.activate_snapshot(index)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"recalled snapshot {index}")


@device.command(name="blocks")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit as JSON.")
@_device_option
def device_blocks(as_json: bool, ip: str, port: int) -> None:
    """List the live edit buffer's blocks with their (path, block) coordinates.

    These are the coordinates `device bypass` / `device model` / `device
    set-param` address. Reads the active edit buffer (does not change the tone).
    The on/off shown is the preset's *saved* base bypass; a volatile live
    `device bypass` toggle is not reflected here until the preset is saved.
    """
    from helixgen.device import HelixClient, HelixError
    from helixgen.ingest import humanize_model_id

    try:
        with HelixClient(ip, port) as h:
            blocks = h.edit_buffer_blocks()
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(blocks, indent=2))
        return
    if not blocks:
        click.echo("no blocks (empty edit buffer?)")
        return
    for b in blocks:
        name = humanize_model_id(b["model"]) if b.get("model") else f"?model {b['model_id']}"
        state = "on " if b["enabled"] else "OFF"
        click.echo(f"  path {b['path']} block {b['block']:>2}  [{state}]  {name}")


@device.command(name="bypass")
@click.argument("path", type=int)
@click.argument("block", type=int)
@click.argument("state", type=click.Choice(["on", "off"]))
@_device_option
def device_bypass(path: int, block: int, state: str, ip: str, port: int) -> None:
    """Enable/bypass a block in the live edit buffer (PATH BLOCK on|off).

    `on` = active, `off` = bypassed. Find coordinates with `device blocks`.
    Changes the ACTIVE tone immediately (`/BlockEnableSet`). Note: the toggle is
    a *volatile* live state — audible at once, but not written to the preset
    (so `device blocks`, which reads the saved base state, won't reflect it)
    until you save the preset.
    """
    from helixgen.device import HelixClient, HelixError

    enable = state == "on"
    try:
        with HelixClient(ip, port) as h:
            h.set_block_enable(path, block, enable)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"path {path} block {block} -> {'on' if enable else 'bypassed'}")


@device.command(name="model")
@click.argument("path", type=int)
@click.argument("block", type=int)
@click.argument("model")
@_device_option
def device_model(path: int, block: int, model: str, ip: str, port: int) -> None:
    """Set a block's model in the live edit buffer (PATH BLOCK MODEL).

    MODEL is a numeric model id or a model-id string like `HD2_AmpBritPlexiNrm`
    (see `list-blocks`). The device rejects a cross-category swap. Changes the
    ACTIVE tone. `/ModelSet`.
    """
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import defs as _defs

    if model.lstrip("-").isdigit():
        model_id = int(model)
    else:
        model_id = _defs.model_id_for(model)
        if model_id is None:
            raise click.ClickException(
                f"unknown model {model!r}; pass a numeric model id or an exact "
                "model-id string (see `helixgen list-blocks`)")
    try:
        with HelixClient(ip, port) as h:
            h.set_block_model(path, block, model_id)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"path {path} block {block} -> model {model} ({model_id})")


@device.command(name="reorder")
@click.argument("setlist")
@click.argument("target")
@click.option("--to", "to_index", type=int, required=True,
              help="New 0-based position within the container.")
@_device_option
def device_reorder(setlist: str, target: str, to_index: int,
                   ip: str, port: int) -> None:
    """Move a preset to a new position within a setlist (`/ReorderContainerContent`).

    SETLIST is a setlist display name (e.g. `throwaway`) or a literal
    container cid; TARGET is a preset display name or a literal cid within
    that setlist. Pass `setlists` as SETLIST to instead reorder the top-level
    setlist list itself (TARGET is then a setlist name/cid) — a real setlist
    literally named "setlists" must be addressed by its container cid.

    This is a direct, immediate DEVICE-side write — distinct from the local
    manifest's `device slots reorder`, which only edits the tone library's
    recorded order and takes effect on the device on the next `device sync`
    (which may then reorder things right back to the manifest's order).
    """
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import reorder as R

    try:
        with HelixClient(ip, port) as h:
            res = R.reorder_setlist_item(h, setlist, target, to_index)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    for w in res.get("warnings", []):
        click.echo(f"warning: {w}", err=True)
    click.echo(f"moved cid {res['moved_cid']} to position {res['new_pos']} "
               f"in {setlist!r} ({len(res['items'])} item(s) now listed)")


@device.command(name="pull")
@click.argument("cid", type=int)
@click.argument("outfile", type=click.Path(dir_okay=False, path_type=Path))
@_device_option
def device_pull(cid: int, outfile: Path, ip: str, port: int) -> None:
    """Save a preset's raw content blob (a .sbe backup) without activating it.

    Reads via the non-activating ``/GetContentData`` — the device's live tone is
    never disturbed.
    """
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            blob = h.get_content(cid)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    outfile.write_bytes(blob)
    click.echo(f"wrote {len(blob)} bytes to {outfile}")


@device.command(name="save")
@click.argument("name")
@click.option("--setlist", type=click.Choice(["user", "factory", "throwaway"]),
              default="user", show_default=True, help="Destination setlist.")
@click.option("--pos", type=int, required=True, help="Destination slot (posi).")
@_device_option
def device_save(name: str, setlist: str, pos: int, ip: str, port: int) -> None:
    """Save the device's CURRENT edit buffer as a new preset; prints the new CID.

    Mirrors the editor's "Save Preset As -> Save As New". The target slot must be
    empty. Whatever preset/edits are live on the device are persisted.
    """
    from helixgen.device import HelixClient, HelixError

    container = _setlist_container(setlist)
    try:
        with HelixClient(ip, port) as h:
            if h.find_by_pos(container, pos) is not None:
                raise click.ClickException(
                    f"{setlist} slot {pos} is not empty; refusing to overwrite")
            new_cid = h._raw.save_edit_buffer_to(container, pos, name)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if new_cid is None:
        raise click.ClickException(f"failed to save edit buffer to {setlist} slot {pos}")
    click.echo(f"saved edit buffer as cid {new_cid} ({name!r}) in {setlist} slot {pos}")
    _record_placement(setlist=setlist, posi=pos, name=name, cid=new_cid,
                      source_kind="edit-buffer")


@device.command(name="list-irs")
@click.option("--json", "as_json", is_flag=True, default=False)
@_device_option
def device_list_irs(as_json: bool, ip: str, port: int) -> None:
    """List the impulse responses on the device (name + hash)."""
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            irs = h.list_irs()
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(irs, indent=2))
        return
    for m in irs:
        click.echo(f"{m.get('hash','')}  {'stereo' if not m.get('mono') else 'mono'}  {m.get('name','?')}")


@device.command(name="delete-ir")
@click.argument("name_or_hash")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.option("--force-wedge", is_flag=True, default=False,
              help="If a 32-hex hash isn't in the IR registry but its file "
                   "still resolves on the device (the delete->quick-reimport "
                   "wedge), remove the orphaned file. Do NOT use on an IR you "
                   "just imported — its listing may merely be lagging.")
@_device_option
def device_delete_ir(name_or_hash: str, yes: bool, force_wedge: bool,
                     ip: str, port: int) -> None:
    """Delete one user IR from the device, by name or 32-hex hash.

    Removes the IR's registry entry (container -11) AND its backing .wav on
    the device (best-effort). Presets that referenced it will show a silent
    cab until it is re-imported. See ``ir-prune`` to clean up ALL unreferenced
    IRs at once.
    """
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import maintenance as mt

    if not yes:
        click.confirm(
            f"Delete IR {name_or_hash!r} from the device?", abort=True)
    try:
        with HelixClient(ip, port) as h:
            res = mt.delete_device_ir(h, name_or_hash, ip=ip,
                                      force_wedge=force_wedge)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not res["ok"]:
        raise click.ClickException(f"failed to delete IR {name_or_hash!r}")
    if res.get("cid") is None:
        click.echo(f"removed orphaned IR file for {res['name']!r} "
                   f"({res['hash']}) — it had no registry entry (wedged)")
    else:
        click.echo(f"deleted IR {res['name']!r} ({res['hash']})"
                   + ("" if res["file_removed"] else
                      "  (warning: its .wav lingers on the device filesystem)"))


@device.command(name="rename-ir")
@click.argument("name_or_hash")
@click.argument("new_name")
@_device_option
def device_rename_ir(name_or_hash: str, new_name: str, ip: str, port: int) -> None:
    """Rename a user IR on the device (match by name or 32-hex hash).

    Renaming changes only the display name — the IR's hash (which presets
    reference) is untouched, so nothing breaks.
    """
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import maintenance as mt

    try:
        with HelixClient(ip, port) as h:
            target = mt.resolve_device_ir_live(h, name_or_hash)
            ok = h.rename(target["cid_"], new_name)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to rename IR {name_or_hash!r}")
    click.echo(f"renamed IR {target.get('name')!r} -> {new_name!r}")


@device.command(name="ir-prune")
@click.option("--yes", is_flag=True, default=False,
              help="Actually delete (default is a dry-run report).")
@click.option("--force", is_flag=True, default=False,
              help="Also delete IRs referenced only by local off-device .hsp files.")
@click.option("--ignore-warnings", "ignore_warnings", is_flag=True, default=False,
              help="Proceed even if some local tones' IR references can't be "
                   "verified (missing/unreadable .hsp).")
@click.option("--only", default=None, metavar="NAME_OR_HASH",
              help="Restrict deletion to this one IR.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the result dict as JSON.")
@_device_option
def device_ir_prune(yes: bool, force: bool, ignore_warnings: bool,
                    only: str | None, as_json: bool,
                    ip: str, port: int) -> None:
    """Delete device IRs that no preset references any more (DRY-RUN by default).

    Diffs the device's user IRs against every IR hash referenced by the
    presets on the device (non-activating content reads across the pool),
    by the live edit buffer, and by your local tone-library .hsp files. IRs
    referenced on the device are never touched; IRs referenced only by a
    local off-device tone are "protected" (need --force). Local tones whose
    recorded .hsp can't be read are surfaced as warnings, and executing over
    warnings needs --ignore-warnings (a separate consent from --force).
    Nothing is deleted without --yes, and the plan is re-scanned and
    re-verified immediately before any delete (a disagreement aborts with
    nothing deleted).
    """
    from helixgen.device import HelixError
    from helixgen.device import maintenance as mt

    try:
        res = mt.ir_prune(ip=ip, port=port, execute=yes, force=force,
                          ignore_warnings=ignore_warnings, only=only)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(res, indent=2))
        return
    for w in res.get("warnings", []):
        click.echo(f"warning: {w}", err=True)
    click.echo(f"device IRs: {res['device_irs']}  "
               f"referenced: {len(res['referenced'])}  "
               f"protected: {len(res['protected'])}  "
               f"orphans: {len(res['orphans'])}")
    for m in res["protected"]:
        click.echo(f"  protected  {m.get('hash')}  {m.get('name')}  "
                   f"(local: {', '.join(m.get('local_tones', []))})")
    for m in res["orphans"]:
        click.echo(f"  orphan     {m.get('hash')}  {m.get('name')}")
    if res["dry_run"]:
        if res["orphans"] or (force and res["protected"]):
            click.echo("dry-run: nothing deleted — re-run with --yes to delete"
                       + (" (add --force for protected IRs)"
                          if res["protected"] and not force else ""))
        else:
            click.echo("dry-run: nothing to prune")
    else:
        for m in res["deleted"]:
            click.echo(f"  deleted    {m.get('hash')}  {m.get('name')}")
        click.echo(f"deleted {len(res['deleted'])} IR(s)")
    for e in res["errors"]:
        click.echo(f"error: {e}", err=True)
    if not res["ok"]:
        raise click.ClickException("ir-prune finished with errors (see above)")


@device.command(name="set-info")
@click.argument("cids", nargs=-1, type=int, required=True)
@click.option("--color", default=None,
              help="Preset color: a name (auto, white, red, dark orange, light "
                   "orange, yellow, green, turquoise, blue, violet, pink, off) "
                   "or a raw index 0-11.")
@click.option("--notes", default=None, help="Preset notes text (Preset Info panel).")
@_device_option
def device_set_info(cids: tuple[int, ...], color: str | None, notes: str | None,
                    ip: str, port: int) -> None:
    """Set preset color and/or notes on one or more CIDs (batch-capable).

    Color is a content attr; notes are written via a non-activating content
    round-trip — the device's live tone is never disturbed.
    """
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import maintenance as mt

    if color is None and notes is None:
        raise click.ClickException("give --color and/or --notes")
    if color is not None:
        try:
            mt.color_index(color)  # validate once, before touching any preset
        except ValueError as e:
            raise click.ClickException(str(e)) from e
    failures = []
    try:
        with HelixClient(ip, port) as h:
            for cid in cids:
                try:
                    out = mt.set_preset_info(h, cid, color=color, notes=notes)
                except HelixError as e:
                    failures.append(cid)
                    click.echo(f"cid {cid}: FAILED ({e})", err=True)
                    continue
                bits = ", ".join(f"{k}={'ok' if v else 'FAILED'}"
                                 for k, v in out.items())
                click.echo(f"cid {cid}: {bits}")
                if not all(out.values()):
                    failures.append(cid)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if failures:
        raise click.ClickException(
            f"{len(failures)} of {len(cids)} preset(s) failed: "
            + ", ".join(str(c) for c in failures))


@device.command(name="push-ir")
@click.argument("wav", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--ip", envvar="HELIXGEN_HELIX_IP", default="192.168.4.84", show_default=True)
def device_push_ir(wav: Path, ip: str) -> None:
    """Import an impulse-response .wav onto the device — instantly, like the editor.

    Two things make an external upload behave exactly like the editor's own
    import: (1) subscribing to the device's 2001 change stream activates its
    watched-directory monitor, so the file registers in ~0.1-1 s instead of on
    the device's slow ~15-20 min scan; (2) the uploaded IR embeds a ``HASH``
    chunk holding helixgen's ``irhash`` (as the editor's file does), so the
    device registers it under exactly that hash and the preset resolves.
    """
    from helixgen.device import HelixError
    from helixgen.device import sftp as _sftp

    try:
        res = _sftp.push_ir(ip, str(wav))
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    if not res.get("ok"):
        raise click.ClickException(f"upload of {wav.name} failed")
    hh = res.get("helixgen_hash")
    dh = res.get("device_hash")
    if res.get("already"):
        click.echo(f"already on device: {res['name']} ({hh})")
    elif res.get("registered") and res.get("hash_match"):
        click.echo(f"imported + registered instantly: {res['name']} ({hh})")
    elif res.get("registered"):
        click.echo(f"registered {res['name']} but under {dh}, not the expected "
                   f"{hh} — the preset may not resolve this IR", err=True)
    else:
        click.echo(f"uploaded {res['name']} ({hh}) — {res.get('note')}", err=True)


@device.command(name="pull-ir")
@click.argument("filename")
@click.argument("outfile", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--ip", envvar="HELIXGEN_HELIX_IP", default="192.168.4.84", show_default=True)
def device_pull_ir(filename: str, outfile: Path, ip: str) -> None:
    """Download an IR .wav from the device by its on-disk filename.

    Use `device sftp-ls` semantics: pass the exact `.wav` basename (see the
    device's ir/ directory).
    """
    from helixgen.device import HelixError
    from helixgen.device import sftp as _sftp

    try:
        with _sftp.HelixSFTP(ip) as s:
            s.download_ir(filename, str(outfile))
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"downloaded {filename} -> {outfile}")


@device.command(name="install")
@click.argument("hsp_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("name")
@click.option("--pos", type=int, required=True, help="Destination slot (posi); must be empty.")
@click.option("--setlist", type=click.Choice(["user", "factory", "throwaway"]),
              default="user", show_default=True, help="Destination setlist.")
@click.option("--auto-irs", is_flag=True, default=False,
              help="Upload any referenced IRs that aren't on the device yet "
                   "(resolved from your local IR mapping.json).")
@_device_option
def device_install(hsp_file: Path, name: str, pos: int, setlist: str,
                   auto_irs: bool, ip: str, port: int) -> None:
    """Author a helixgen .hsp onto the device as a new, playable preset.

    Transcodes the .hsp straight into the device's native content format and
    installs it into an empty slot — any block chain, full fidelity, no
    template. With --auto-irs, missing IRs are uploaded first. EXPERIMENTAL.
    """
    from helixgen.hsp import read_hsp
    from helixgen.device import HelixClient, HelixError

    body = read_hsp(hsp_file)
    container = _setlist_container(setlist)
    try:
        with HelixClient(ip, port) as h:
            cid = _install_hsp_open(h, body, container, pos, name,
                                    setlist_label=setlist,
                                    auto_irs=auto_irs, ip=ip)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"installed {hsp_file.name} as cid {cid} ({name!r}) in {setlist} slot {pos}")
    _record_placement(setlist=setlist, posi=pos, name=name, cid=cid,
                      source_kind="hsp", source_path=str(hsp_file.resolve()))


# --- device setlist: the local manifest of desired setlist membership -------

@device.group(name="setlist")
def device_setlist() -> None:
    """Manage the local setlist manifest (~/.helixgen/setlists.json).

    A tone is added to a setlist here (desired membership); `device sync` then
    pushes that membership onto the device as a preset pool + references. The
    manifest is never hand-edited — use these verbs.
    """


@device_setlist.command(name="list")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the whole manifest document as JSON.")
def device_setlist_list(as_json: bool) -> None:
    """List the manifest's setlists with their tone counts and members."""
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    if as_json:
        click.echo(json.dumps(m.to_dict(), indent=2))
        return
    setlists = m.setlists()
    if not setlists:
        click.echo("(no setlists in manifest)")
        return
    for sl in setlists:
        tones = m.tones_in(sl)
        click.echo(f"{sl}  ({len(tones)} tone{'s' if len(tones) != 1 else ''})")
        for t in tones:
            click.echo(f"    {t}")


@device_setlist.command(name="add")
@click.argument("setlist")
@click.argument("hsp_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--pos", type=int, default=None,
              help="Insert at this 0-based position (default: append).")
def device_setlist_add_cmd(setlist: str, hsp_file: Path, pos: int | None) -> None:
    """Add an authored .hsp tone to a setlist's membership (auto-creates the setlist).

    A tone may belong to many setlists (it's referenced once in the device pool
    and shared) — adding one that's already elsewhere is expected, not a dup.
    Idempotent within a setlist; only errors if the tone's name is already
    registered to a different .hsp file (names must be unique).
    """
    from helixgen.device.manifest import SetlistManifest, ManifestError

    m = SetlistManifest.load()
    try:
        name = m.add_tone(setlist, hsp_file, pos=pos)
    except ManifestError as e:
        raise click.ClickException(str(e)) from e
    m.save()
    where = "appended to" if pos is None else f"inserted at {pos} in"
    click.echo(f"added {name!r} ({where} setlist {setlist!r})")


@device_setlist.command(name="remove")
@click.argument("setlist")
@click.argument("tone_name")
def device_setlist_remove_cmd(setlist: str, tone_name: str) -> None:
    """Drop a tone from a setlist's membership (TONE_NAME is the tone's display name)."""
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    if not m.remove_tone(setlist, tone_name):
        raise click.ClickException(
            f"{tone_name!r} is not in setlist {setlist!r} "
            f"(try `helixgen device setlist list`)")
    m.save()
    click.echo(f"removed {tone_name!r} from setlist {setlist!r}")


@device_setlist.command(name="create-local")
@click.argument("setlist")
def device_setlist_create_local(setlist: str) -> None:
    """Create an empty setlist in the LOCAL manifest only (no device).

    To also create it on the device, run `helixgen device setlist create`
    (which records it locally too).
    """
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    m.create_setlist(setlist)
    m.save()
    click.echo(f"created local setlist {setlist!r} (manifest only — "
               f"`device setlist create` also creates it on the device)")


@device_setlist.command(name="create")
@click.argument("setlist")
@_device_option
def device_setlist_create_cmd(setlist: str, ip: str, port: int) -> None:
    """Create a new empty setlist ON THE DEVICE (and in the local manifest).

    Uses the device's own create command (/CreateContent under the setlists
    root) — no Stadium app needed. Errors if a setlist with that name already
    exists on the device.
    """
    from helixgen.device import HelixClient, HelixError
    from helixgen.device.manifest import SetlistManifest

    try:
        with HelixClient(ip, port) as h:
            existing = h.resolve_setlist_cid(setlist)
            if existing is not None:
                raise click.ClickException(
                    f"setlist {setlist!r} already exists on the device "
                    f"(cid {existing})")
            cid = h.create_setlist(setlist)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if cid is None:
        raise click.ClickException(f"device refused to create setlist {setlist!r}")
    try:
        m = SetlistManifest.load()
        m.create_setlist(setlist)
        m.save()
    except Exception as e:  # noqa: BLE001 — advisory; the device write succeeded
        click.echo(f"warning: could not update tone library: {e}", err=True)
    click.echo(f"created setlist {setlist!r} on the device (cid {cid})")


@device_setlist.command(name="rename")
@click.argument("setlist")
@click.argument("new_name")
@_device_option
def device_setlist_rename_cmd(setlist: str, new_name: str, ip: str, port: int) -> None:
    """Rename a setlist ON THE DEVICE (and in the local manifest, if tracked)."""
    from helixgen.device import HelixClient, HelixError
    from helixgen.device.manifest import SetlistManifest, ManifestError

    try:
        with HelixClient(ip, port) as h:
            cid = h.resolve_setlist_cid(setlist)
            if cid is None:
                raise click.ClickException(
                    f"setlist {setlist!r} not found on the device")
            if h.resolve_setlist_cid(new_name) is not None:
                raise click.ClickException(
                    f"a setlist named {new_name!r} already exists on the device")
            ok = h.rename(cid, new_name)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to rename setlist {setlist!r}")
    try:
        m = SetlistManifest.load()
        if m.rename_setlist(setlist, new_name):
            m.save()
    except ManifestError as e:
        click.echo(f"warning: device renamed, but the local manifest kept "
                   f"{setlist!r}: {e}", err=True)
    except Exception as e:  # noqa: BLE001 — advisory
        click.echo(f"warning: could not update tone library: {e}", err=True)
    click.echo(f"renamed setlist {setlist!r} -> {new_name!r} (cid {cid})")


@device_setlist.command(name="delete")
@click.argument("setlist")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@_device_option
def device_setlist_delete_cmd(setlist: str, yes: bool, ip: str, port: int) -> None:
    """Delete a setlist ON THE DEVICE. Its references die with it — the pool
    presets they pointed at are NEVER deleted (never-orphan).

    A local manifest setlist of the same name is kept as a local-only draft
    (marked unsynced).
    """
    from helixgen.device import HelixClient, HelixError
    from helixgen.device.manifest import SetlistManifest

    try:
        with HelixClient(ip, port) as h:
            cid = h.resolve_setlist_cid(setlist)
            if cid is None:
                raise click.ClickException(
                    f"setlist {setlist!r} not found on the device")
            if not yes:
                click.confirm(
                    f"Delete setlist {setlist!r} (cid {cid}) from the device? "
                    f"(its presets stay in the pool)", abort=True)
            ok = h.delete_setlist(cid)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to delete setlist {setlist!r}")
    try:
        m = SetlistManifest.load()
        if setlist in m.setlists_map:
            m.set_setlist_synced(setlist, False)
            m.save()
    except Exception as e:  # noqa: BLE001 — advisory
        click.echo(f"warning: could not update tone library: {e}", err=True)
    click.echo(f"deleted setlist {setlist!r} from the device — its pool "
               f"presets were not touched")


@device_setlist.command(name="duplicate")
@click.argument("src")
@click.argument("dst")
@_device_option
def device_setlist_duplicate_cmd(src: str, dst: str, ip: str, port: int) -> None:
    """Duplicate a setlist ON THE DEVICE: copy SRC's references into DST.

    DST is created on the device if absent; if it exists it must be empty.
    References are pointers — the pool presets are shared, not copied.
    """
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            src_cid = h.resolve_setlist_cid(src)
            if src_cid is None:
                raise click.ClickException(f"setlist {src!r} not found on the device")
            dst_cid = h.resolve_setlist_cid(dst)
            created = False
            if dst_cid is None:
                dst_cid = h.create_setlist(dst)
                created = True
                if dst_cid is None:
                    raise click.ClickException(
                        f"device refused to create setlist {dst!r}")
            copied = h.duplicate_setlist_refs(src_cid, dst_cid)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if created:
        try:
            from helixgen.device.manifest import SetlistManifest

            m = SetlistManifest.load()
            m.create_setlist(dst)
            m.save()
        except Exception as e:  # noqa: BLE001 — advisory; device write succeeded
            click.echo(f"warning: could not update tone library: {e}", err=True)
    click.echo(f"duplicated setlist {src!r} -> {dst!r} "
               f"({'created, ' if created else ''}{copied} reference(s) copied)")


@device_setlist.command(name="import-hss")
@click.argument("hss_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--list", "list_only", is_flag=True, default=False,
              help="List the bundle's contents only — offline, no device write.")
@click.option("--setlist", "setlist_name", default=None,
              help="Destination setlist name (default: the bundle's own name).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would be installed/created without writing to the device.")
@_device_option
def device_setlist_import_hss(hss_file: Path, list_only: bool, setlist_name: str | None,
                              dry_run: bool, ip: str, port: int) -> None:
    """EXPERIMENTAL: import a `.hss` setlist-bundle export (backlog #31, READ side).

    A `.hss` is the Stadium app's "export setlist" file: a 24-byte header +
    gzip + tar of `manifest.json` + 128 fixed slot files. `--list` decodes it
    fully offline (no device needed) and prints each slot's filled/empty state
    and preset name. Without `--list`, each filled slot is installed into the
    device POOL (non-activating) and referenced into a device setlist (created
    if absent) in the bundle's slot order — reusing the same install +
    setlist-create + reference primitives as `device install` / `device sync`.

    Container framing (header/gzip/tar/manifest/128-slot/empty-sentinel) is
    pinned against a real captured export. The FILLED-SLOT byte framing is an
    inferred assumption — pinned only against synthesized fixtures, not a real
    non-empty `.hss` export — see `src/helixgen/device/hss.py`.

    Imported presets are recorded in the local tone library as PATHLESS tones
    (source `import-hss`) with membership in the destination setlist, so a
    later `device sync <setlist>` preserves their references instead of
    stripping them. They have no local `.hsp`, so `device slots restore`
    can't re-author them.

    NOT idempotent on retry: re-running after a partial failure installs and
    references the already-succeeded slots AGAIN (duplicate pool presets +
    references). After a partial failure, delete the setlist + the orphaned
    pool presets (or import into a fresh setlist) before retrying.
    """
    from helixgen.device import hss as hss_mod

    try:
        bundle = hss_mod.read_hss(hss_file)
    except hss_mod.HssFormatError as e:
        raise click.ClickException(str(e)) from e

    filled = bundle.filled_slots

    if list_only:
        click.echo(f"{hss_file.name}: setlist {bundle.name!r} "
                   f"({len(filled)}/{len(bundle.slots)} slots filled)")
        for s in bundle.slots:
            state = "filled" if s.filled else "empty"
            click.echo(f"  {s.pos:>3}  {state:6}  {hss_mod.slot_label(s) if s.filled else ''}")
        return

    target_setlist = setlist_name or bundle.name
    if not target_setlist:
        raise click.ClickException(
            "the bundle has no setlist name in its manifest; pass --setlist explicitly")

    if not filled:
        click.echo(f"no filled slots in {hss_file.name}; nothing to import")
        return

    if dry_run:
        click.echo(f"DRY RUN: would import {len(filled)} preset(s) into "
                   f"setlist {target_setlist!r}:")
        for s in filled:
            note = ("" if hss_mod.looks_like_content_blob(s.blob)
                    else "  (would SKIP: payload isn't a recognized content blob)")
            click.echo(f"  slot {s.pos}: {hss_mod.slot_label(s)}{note}")
        return

    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            result = hss_mod.import_bundle(h, bundle, setlist=setlist_name)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    installed = result["installed"]
    errors = result["errors"]
    click.echo(f"imported {len(installed)}/{len(filled)} preset(s) from {hss_file.name} "
               f"into setlist {result['setlist']!r} "
               f"({'created, ' if result['created'] else ''}cid {result['cid']})")
    # Record the imported presets in the tone library (pathless, source
    # "import-hss") + the setlist's membership — load-bearing: without it a
    # later targeted `device sync <setlist>` computes desired=[] and strips
    # every reference the import just wrote. Best-effort like
    # _record_placement (the device write already succeeded).
    try:
        from helixgen.device.manifest import SetlistManifest

        m = SetlistManifest.load()
        for w in hss_mod.record_import_in_manifest(m, result):
            click.echo(f"warning: {w}", err=True)
        m.save()
    except Exception as e:  # noqa: BLE001 — advisory; device write succeeded
        click.echo(f"warning: could not update local manifest: {e}", err=True)
    if errors:
        for e in errors:
            click.echo(f"  warning: {e}", err=True)
        raise click.ClickException(
            f"{len(errors)}/{len(filled)} preset(s) failed to import; see warnings above")


@device_setlist.command(name="sync-on")
@click.argument("setlist")
def device_setlist_sync_on(setlist: str) -> None:
    """Mark a setlist as device-synced (marks all its tones for the device)."""
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    m.set_setlist_synced(setlist, True)
    m.save()
    click.echo(f"setlist {setlist!r} is now synced; run `helixgen device sync {setlist}`")


@device_setlist.command(name="sync-off")
@click.argument("setlist")
def device_setlist_sync_off(setlist: str) -> None:
    """Mark a setlist as a local-only draft (not mirrored to the device)."""
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    m.set_setlist_synced(setlist, False)
    m.save()
    click.echo(f"setlist {setlist!r} is now a local-only draft")


@cli.command(name="register")
@click.argument("hsp_path", type=click.Path(exists=True, path_type=Path))
@click.option("--doc", type=click.Path(exists=True, path_type=Path), default=None,
              help="Optional companion markdown description to record.")
def register_cmd(hsp_path: Path, doc: Path | None) -> None:
    """Register an existing local .hsp into the tone library (off-device)."""
    from helixgen.device.manifest import SetlistManifest, ManifestError

    m = SetlistManifest.load()
    try:
        name = m.register_tone(hsp_path, source="import-local", doc=doc)
    except ManifestError as e:
        raise click.ClickException(str(e)) from e
    m.save()
    click.echo(f"registered {name!r} in the tone library (off-device)")


@device.command(name="add")
@click.argument("tone")
@click.option("--slot", default="auto",
              help="Desired user slot ('1A'..'128D') or 'auto' (default; sync picks).")
def device_add_cmd(tone: str, slot: str) -> None:
    """Mark a library tone for the device (placed on the next `device sync`)."""
    from helixgen.device.manifest import SetlistManifest, ManifestError

    m = SetlistManifest.load()
    try:
        m.mark_on_device(tone, slot)
    except ManifestError as e:
        raise click.ClickException(str(e)) from e
    m.save()
    click.echo(f"{tone!r} marked for device (slot {slot})")


@device.command(name="unsync")
@click.argument("tone")
def device_unsync_cmd(tone: str) -> None:
    """Take a tone off the device on next sync (keeps it in the library)."""
    from helixgen.device.manifest import SetlistManifest, ManifestError

    m = SetlistManifest.load()
    try:
        pulled = m.unsync(tone)
    except ManifestError as e:
        raise click.ClickException(str(e)) from e
    m.save()
    msg = f"{tone!r} unsynced (deleted from device on next sync)"
    if pulled:
        msg += f"; removed from synced setlists: {', '.join(pulled)}"
    click.echo(msg)


@device.command(name="library")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit raw JSON.")
def device_library_cmd(as_json: bool) -> None:
    """List every library tone: slot, on/off device, setlist memberships."""
    from helixgen.device.manifest import SetlistManifest

    rows = SetlistManifest.load().library()
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        sls = ", ".join(row["setlists"])
        click.echo(f"{(row['slot'] or '-'):<4} {row['name']:<28} "
                   f"{'on' if row['on_device'] else 'off':<3}  [{sls}]")


@device.command(name="sync")
@click.argument("setlist_name", metavar="SETLIST", required=False)
@click.option("--all", "all_setlists", is_flag=True, default=False,
              help="Sync every setlist in the manifest (the whole-library reconcile).")
@click.option("--gc", is_flag=True, default=False,
              help="Garbage-collect pool presets no setlist references (only with --all).")
@click.option("--exclude-irs", is_flag=True, default=False,
              help="Install tones only; do not upload their referenced IRs.")
@click.option("--repush", is_flag=True, default=False,
              help="Force re-transcode + re-push every in-scope tone's content, "
                   "even when its recorded .hsp hash already matches the pool "
                   "(use after a transcoder upgrade to refresh already-synced "
                   "tones).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the raw engine result dict as JSON.")
@_device_option
def device_sync(setlist_name: str | None, all_setlists: bool, gc: bool,
                exclude_irs: bool, repush: bool, as_json: bool,
                ip: str, port: int) -> None:
    """Sync the manifest's setlists onto the device (pool + references).

    Give a single SETLIST name, or --all for every manifest setlist. The engine
    reconciles the preset pool (install/update/skip), then rebuilds each
    setlist's references to manifest order — never orphaning a still-referenced
    pool preset. --gc (only with --all) prunes pool presets no setlist wants any
    more. --repush treats every in-scope tone already in the pool as changed —
    re-pushing its content via the same non-activating SetContentData-on-the-
    existing-cid path an ordinary hash-triggered update uses — even when its
    .hsp content hash hasn't changed (a transcoder upgrade can change what an
    unchanged .hsp produces, which hash-based change detection can't see).
    A setlist the device doesn't have is reported as a clear error (create
    it first with `helixgen device setlist create <name>`). EXPERIMENTAL.
    """
    from helixgen.device.manifest import SetlistManifest
    from helixgen.device.setlist_sync import sync_setlists
    from helixgen.device import HelixError

    if bool(setlist_name) == bool(all_setlists):
        raise click.ClickException(
            "give exactly one of a SETLIST name or --all (not both, not neither)")
    if gc and not all_setlists:
        click.echo("warning: --gc is ignored without --all "
                   "(a single-setlist sync never garbage-collects)", err=True)
        gc = False

    setlists = None if all_setlists else [setlist_name]
    try:
        res = sync_setlists(SetlistManifest.load(), ip=ip, port=port,
                            setlists=setlists, gc=gc, exclude_irs=exclude_irs,
                            repush=repush)
    except HelixError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps(res, indent=2))
        return

    pool = res.get("pool", {})
    click.echo(f"pool: {len(pool.get('installed', []))} installed, "
               f"{len(pool.get('updated', []))} updated, "
               f"{len(pool.get('skipped', []))} skipped")
    pool_deleted = pool.get("deleted", [])
    if pool_deleted:
        click.echo(f"pool: deleted {len(pool_deleted)} unsynced preset(s): "
                   f"{', '.join(pool_deleted)}")
    for name in pool.get("delete_skipped", []):
        click.echo(f"pool: kept {name!r} (unsynced, but another device setlist "
                   f"still references it — sync that setlist or use --all)")
    for sl, diff in res.get("references", {}).items():
        click.echo(f"setlist {sl!r}: +{len(diff.get('added', []))} references, "
                   f"-{len(diff.get('removed', []))} references")
    deleted = res.get("gc", {}).get("deleted", [])
    if deleted:
        click.echo(f"gc: deleted {len(deleted)} orphan pool preset(s): "
                   f"{', '.join(deleted)}")
    for er in res.get("errors", []):
        click.echo(f"error: {er}", err=True)
    synced = res.get("setlists", [])
    click.echo(f"synced {len(synced)} setlist(s): {', '.join(synced) or '(none)'}")
    drafts = res.get("skipped_draft_setlists", [])
    if drafts:
        click.echo(f"note: skipped {len(drafts)} local-only draft setlist(s): "
                   f"{', '.join(drafts)} — run `device sync <setlist>` or "
                   f"`device setlist sync-on <setlist>` to mirror one")


@device.command(name="push")
@click.argument("infile", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("name")
@click.option("--setlist", type=click.Choice(["user", "factory", "throwaway"]),
              default="user", show_default=True, help="Destination setlist.")
@click.option("--pos", type=int, required=True, help="Destination slot (posi); must be empty.")
@_device_option
def device_push(infile: Path, name: str, setlist: str, pos: int, ip: str, port: int) -> None:
    """Install a local content file (.sbe backup) into a new preset slot.

    Restores a backup / clones a preset / installs authored content. The target
    slot must be empty.
    """
    from helixgen.device import HelixClient, HelixError

    container = _setlist_container(setlist)
    blob = infile.read_bytes()
    try:
        with HelixClient(ip, port) as h:
            if h.find_by_pos(container, pos) is not None:
                raise click.ClickException(f"{setlist} slot {pos} is not empty")
            new_cid = h._raw.push_to_slot(container, pos, name, blob)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if new_cid is None:
        raise click.ClickException(f"failed to push {infile} into {setlist} slot {pos}")
    click.echo(f"pushed {infile.name} as cid {new_cid} ({name!r}) in {setlist} slot {pos}")
    _record_placement(setlist=setlist, posi=pos, name=name, cid=new_cid,
                      source_kind="sbe", source_path=str(infile.resolve()))


@device.command(name="restore")
@click.argument("infile", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("cid", type=int)
@_device_option
def device_restore(infile: Path, cid: int, ip: str, port: int) -> None:
    """Overwrite an EXISTING preset's content from a local file (.sbe).

    Warning: replaces the content at CID in place.
    """
    from helixgen.device import HelixClient, HelixError

    blob = infile.read_bytes()
    try:
        with HelixClient(ip, port) as h:
            ok = h._raw.set_content_data(cid, blob)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to restore content to cid {cid}")
    click.echo(f"restored content of cid {cid} from {infile.name}")


@device.group(name="slots", invoke_without_command=True)
@click.pass_context
def device_slots(ctx: click.Context) -> None:
    """The local record of which tone helixgen put in which device slot.

    Placement commands (install / save / push / create) record here; rename and
    delete keep it in sync. Bare `device slots` lists the record offline.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(device_slots_list)


@device_slots.command(name="list")
@click.option("--verify", is_flag=True, default=False,
              help="Cross-check the live device and flag drift (needs the Helix).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit raw JSON (the library view, or verify records with --verify).")
@_device_option
def device_slots_list(verify: bool, as_json: bool, ip: str, port: int) -> None:
    """List every library tone: slot, on/off device, setlists. Offline unless --verify."""
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    rows = m.library()

    if verify:
        from helixgen.device import HelixClient, HelixError

        on_device = {}
        try:
            with HelixClient(ip, port) as h:
                for p in h.list_presets(_setlist_container("user")):
                    on_device[p.get("name")] = p
        except (HelixError, OSError) as e:
            raise click.ClickException(str(e)) from e
        records = []
        for row in rows:
            if not row["on_device"]:
                status = "offline"
            elif row["name"] in on_device:
                status = "ok"
            else:
                status = "missing"
            records.append({**row, "status": status})
        for name in on_device:
            if name not in m.tones:
                records.append({"name": name, "slot": None, "status": "untracked"})
        if as_json:
            click.echo(json.dumps(records, indent=2))
        else:
            for r in records:
                click.echo(f"{(r.get('slot') or '-'):<4} {r.get('status', ''):<9} {r.get('name', '')}")
        return

    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        sls = ", ".join(row["setlists"])
        click.echo(f"{(row['slot'] or '-'):<4} {row['name']:<28} "
                   f"{'on' if row['on_device'] else 'off':<3}  [{sls}]")


@device_slots.command(name="restore")
@click.argument("target")
@click.option("--pos", type=int, default=None,
              help="Override the destination slot (default: the recorded slot).")
@click.option("--setlist", type=click.Choice(["user", "factory", "throwaway"]),
              default=None, help="Override the destination setlist.")
@click.option("--force", is_flag=True, default=False,
              help="Push even if the destination slot is occupied.")
@_device_option
def device_slots_restore(target: str, pos: int | None, setlist: str | None,
                         force: bool, ip: str, port: int) -> None:
    """Put a recorded tone back in its slot. TARGET is the tone name or slot label.

    Re-installs the recorded source: an .hsp (from `install`) is re-authored; an
    .sbe (from `push`) is re-pushed. Tones saved from the live edit buffer or
    copied on-device have no local source and can't be restored this way.
    """
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    name = target if target in m.tones else None
    if name is None:  # try to match a slot label
        name = next((n for n, r in m.tones.items() if r.get("slot") == target), None)
    if name is None:
        raise click.ClickException(f"no library tone matching {target!r} "
                                   f"(try `helixgen device slots`)")

    rec = m.tones[name]
    src_path = rec.get("path")
    dest_setlist = setlist or "user"
    # Slot resolution (#25): an explicit --pos wins; else the recorded slot
    # label; else the last observed device posi (a synced tone records its
    # concrete position under ``device.posi`` even when ``slot`` is unresolved).
    dest_pos = pos
    if dest_pos is None:
        dest_pos = _posi_from_slot(rec.get("slot"))
    if dest_pos is None:
        dev = rec.get("device")
        if isinstance(dev, dict) and isinstance(dev.get("posi"), int):
            dest_pos = dev["posi"]
    if dest_pos is None:
        raise click.ClickException(f"{name!r} has no recorded slot; pass --pos")
    container = _setlist_container(dest_setlist)

    if not src_path:
        raise click.ClickException(
            f"no local source recorded for {name!r} "
            f"(pathless save/create); back it up first (helixgen device pull / backup)")
    src = Path(src_path)
    if not src.is_file():
        raise click.ClickException(f"recorded source no longer exists: {src}")

    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            if src.suffix == ".sbe":
                if h.find_by_pos(container, dest_pos) is not None and not force:
                    raise click.ClickException(
                        f"{dest_setlist} slot {dest_pos} is not empty (use --force)")
                cid = h._raw.push_to_slot(container, dest_pos, name, src.read_bytes())
            else:  # hsp
                from helixgen.hsp import read_hsp

                cid = _install_hsp_open(h, read_hsp(src), container, dest_pos,
                                        name, setlist_label=dest_setlist,
                                        force=force, ip=ip)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if cid is None:
        raise click.ClickException(f"failed to restore {name!r}")
    click.echo(f"restored {name!r} to {dest_setlist} slot {dest_pos} "
               f"(cid {cid}) from {src.name}")
    _record_placement(setlist=dest_setlist, posi=dest_pos, name=name,
                      cid=cid, source_kind=src.suffix.lstrip("."), source_path=str(src))


@device_slots.command(name="reorder")
@click.argument("target")
@click.option("--to", "to_index", type=int, required=True,
              help="New 0-based position within the setlist order.")
@click.option("--setlist", "setlist_name", default="user",
              help="Which setlist's order to change (default: user).")
def device_slots_reorder(target: str, to_index: int, setlist_name: str) -> None:
    """Move a tone to a new position within a setlist's order.

    Local only — reorders the manifest; run `device sync <setlist>` to apply it to
    the device. TARGET is the tone name.
    """
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    members = m.tones_in(setlist_name)
    if target not in members:
        raise click.ClickException(
            f"{target!r} is not in setlist {setlist_name!r} "
            f"(try `helixgen device slots`)")
    members.remove(target)
    members.insert(max(0, to_index), target)
    m.setlists_map[setlist_name]["tones"] = members
    m.save()
    click.echo(f"reordered {setlist_name}; order is now: {', '.join(members)}")


def _posi_from_slot(slot):
    from helixgen.device.manifest import _SLOT_LABELS
    if slot in (None, "auto"):
        return None
    try:
        return _SLOT_LABELS.index(slot)
    except ValueError:
        return None


@device.command(name="backup")
@click.option("--setlist", type=click.Choice(["user", "factory", "throwaway"]),
              default="user", show_default=True, help="Setlist to back up.")
@click.option("--dir", "out_dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Output dir (default ~/.helixgen/device-backups/ "
                                 "or $HELIXGEN_DEVICE_BACKUPS).")
@_device_option
def device_backup(setlist: str, out_dir, ip: str, port: int) -> None:
    """Back up every preset in a setlist to local .sbe files + a manifest.

    Reads each preset via the non-activating `/GetContentData`, so the device's
    live tone is never disturbed. Works offline afterwards via `device local-list`.
    """
    from helixgen.device import HelixClient, HelixError
    from helixgen.device import backup as _backup
    from datetime import datetime, timezone

    container = _setlist_container(setlist)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with HelixClient(ip, port) as h:
            entries = _backup.backup_setlist(h, container, out_dir, now=now)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    dest = out_dir or _backup.default_backup_dir()
    click.echo(f"backed up {len(entries)} preset(s) to {dest}")


@device.command(name="local-list")
@click.option("--dir", "out_dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Backup dir to read (offline; no device needed).")
@click.option("--json", "as_json", is_flag=True, default=False)
def device_local_list(out_dir, as_json: bool) -> None:
    """List locally backed-up presets (works with the Helix disconnected)."""
    from helixgen.device import backup as _backup

    entries = _backup.local_list(out_dir)
    if as_json:
        click.echo(json.dumps(entries, indent=2))
        return
    for e in entries:
        click.echo(f"{e.get('slot_label',''):<4} {e.get('name','?'):<28} "
                   f"[{e.get('fmt','?')}] {e.get('file','')}")


@device.command(name="watch")
@click.option("--seconds", type=float, default=5.0, show_default=True,
              help="How long to watch the device's live event streams.")
@click.option("--filter", "filter_addr", multiple=True,
              help="Only show these OSC addresses (repeatable).")
@_device_option
def device_watch(seconds: float, filter_addr, ip: str, port: int) -> None:
    """Watch the device's live property/telemetry streams (ports 2001/2003)."""
    from helixgen.device.subscribe import HelixSubscriber
    from helixgen.device import HelixError

    flt = set(filter_addr) or None
    try:
        with HelixSubscriber(ip) as sub:
            for ev in sub.stream(duration=seconds, filter_addrs=flt):
                click.echo(f"{ev.port}  {ev.addr:<20} {ev.args}")
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e


@device.command(name="tuner")
@click.option("--seconds", type=float, default=15.0, show_default=True,
              help="How long to run the tuner (streams live pitch).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit one JSON reading per line instead of a live display.")
@_device_option
def device_tuner(seconds: float, as_json: bool, ip: str, port: int) -> None:
    """Live network tuner — reads the device's always-on pitch detector.

    Subscribes to the 2003 telemetry stream and decodes the pitch readout (no
    Stadium app, and no need to engage the hardware tuner — the detector is
    always live). Play a note and watch the note/cents update. Ctrl-C to stop.
    """
    from helixgen.device.subscribe import HelixSubscriber
    from helixgen.device import HelixError
    from helixgen.device import tuner as T

    def _bar(cents: int) -> str:
        # 21-cell meter, centre = in tune; ◀/▶ show flat/sharp direction
        pos = max(-10, min(10, round(cents / 5)))
        cells = ["·"] * 21
        cells[10] = "|"
        cells[10 + pos] = "◀" if pos < 0 else ("▶" if pos > 0 else "●")
        return "".join(cells)

    last = None
    try:
        with HelixSubscriber(ip) as sub:
            for ev in sub.stream(duration=seconds, filter_addrs={"/dspEvent"},
                                 include_noise=True):
                r = T.reading_from_event_args(ev.args)
                if r is None:
                    continue
                if as_json:
                    click.echo(json.dumps({
                        "signal": r.signal, "note": r.name, "cents": r.cents,
                        "hz": round(r.hz, 2), "midi": round(r.midi, 3)}))
                    continue
                if not r.signal:
                    line = "  —   (no signal)".ljust(48)
                else:
                    line = (f"  {r.name:<4} {r.cents:+3d}c  "
                            f"{r.hz:7.2f} Hz  {_bar(r.cents)}").ljust(48)
                if line != last:
                    click.echo("\r" + line, nl=False)
                    last = line
        if not as_json:
            click.echo("")  # finish the live line
    except KeyboardInterrupt:
        if not as_json:
            click.echo("")
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e


@device.command(name="meters")
@click.option("--seconds", type=float, default=15.0, show_default=True,
              help="How long to run the meters (streams live level telemetry).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit one JSON reading per line instead of a live display.")
@_device_option
def device_meters(seconds: float, as_json: bool, ip: str, port: int) -> None:
    """Live network level meters — reads the device's grid-level telemetry.

    Subscribes to the 2003 telemetry stream and decodes the two meter arrays
    (`/dspEvent` eid_=1, mid_=796/800 — 128-float grid level data) that ride
    the same burst as the network tuner (no Stadium app needed). Read-only.
    Ctrl-C to stop.
    """
    from helixgen.device.subscribe import HelixSubscriber
    from helixgen.device import HelixError
    from helixgen.device import meters as M

    def _bar(peak: float, scale: float = 0.08, cells: int = 24) -> str:
        n = max(0, min(cells, round((peak / scale) * cells)))
        return "#" * n + "-" * (cells - n)

    last: dict = {}
    try:
        with HelixSubscriber(ip) as sub:
            for ev in sub.stream(duration=seconds, filter_addrs={"/dspEvent"},
                                 include_noise=True):
                for r in M.readings_from_event_args(ev.args):
                    if as_json:
                        click.echo(json.dumps({
                            "mid": r.mid, "peak": round(r.peak, 4),
                            "values": [round(v, 4) for v in r.values]}))
                        continue
                    last[r.mid] = r
                    line = "  ".join(
                        f"{mid}: {_bar(last[mid].peak)} {last[mid].peak:.3f}"
                        for mid in sorted(last))
                    click.echo("\r" + line.ljust(70), nl=False)
        if not as_json:
            click.echo("")  # finish the live line
    except KeyboardInterrupt:
        if not as_json:
            click.echo("")
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="ir-cache")
@click.option("--stats", is_flag=True, default=False,
              help="Show entry count, cache path, and file size.")
@click.option("--clear", "clear_", is_flag=True, default=False,
              help="Delete the cache file.")
@click.option("--prune", is_flag=True, default=False,
              help="Drop entries whose backing WAV no longer exists.")
def ir_cache_cmd(stats: bool, clear_: bool, prune: bool) -> None:
    """Inspect or maintain the IR-hash cache (perf layer, not mapping.json).

    The cache lives at $HELIXGEN_IRHASH_CACHE, else $HELIXGEN_CACHE/irhash.json,
    else ~/.helixgen/cache/irhash.json. Exactly one action is required.
    """
    if sum((stats, clear_, prune)) != 1:
        raise click.ClickException("choose exactly one of --stats, --clear, --prune")

    cache = IrHashCache.load()

    if stats:
        size = cache.path.stat().st_size if cache.path.exists() else 0
        click.echo(f"entries: {len(cache.entries)}")
        click.echo(f"path:    {cache.path}")
        click.echo(f"size:    {size} bytes")
        return

    if clear_:
        n = len(cache.entries)
        cache.clear()
        click.echo(f"Cleared IR-hash cache ({n} entr{'y' if n == 1 else 'ies'}) at {cache.path}")
        return

    # prune
    dropped = cache.prune_missing()
    cache.save()
    click.echo(f"Pruned {dropped} missing entr{'y' if dropped == 1 else 'ies'}")


if __name__ == "__main__":  # allow `python -m helixgen.cli ...`
    cli()
