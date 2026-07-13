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
    helixgen IR mapping, then SFTP-pushing it (device auto-registers)."""
    from helixgen.ir import IrMapping
    from helixgen.device import sftp as _sftp

    try:
        irmap = IrMapping.load()
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(
            f"--auto-irs needs your local IR mapping.json: {e}")
    for hh in hashes:
        try:
            path = irmap.resolve_by_hash(hh)
        except Exception:  # noqa: BLE001 - not registered locally
            click.echo(f"warning: referenced IR {hh} not found locally; register "
                       f"it (helixgen register-irs) — cab may be silent", err=True)
            continue
        res = _sftp.push_ir(ip, str(path))
        # push_ir registers instantly (2001 subscription). The device computes
        # its own hash; if it differs from the preset's hash (hh) the cab won't
        # resolve — surface that (it's the irhash-algorithm edge case).
        if res.get("already"):
            click.echo(f"IR {hh} already on device")
        elif res.get("ok") and res.get("registered") and res.get("hash_match"):
            click.echo(f"imported IR {res.get('name') or path.name} ({hh})")
        elif res.get("ok") and res.get("registered"):
            click.echo(f"warning: {path.name} registered as {res.get('device_hash')} "
                       f"but the preset references {hh} — cab won't resolve "
                       f"(irhash-algorithm edge case for this file)", err=True)
        elif res.get("ok"):
            click.echo(f"warning: uploaded {path.name} ({hh}) but not yet "
                       f"registered — retry shortly", err=True)
        else:
            click.echo(f"warning: failed to upload {path.name} ({hh})", err=True)


def _utc_now() -> str:
    """ISO-8601 UTC timestamp for ledger entries (injected so the module stays
    deterministic)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _record_placement(*, setlist: str, posi: int, name: str, cid: int | None,
                      source_kind: str, source_path: str | None = None,
                      model: str | None = None) -> None:
    """Record a device placement in the slot ledger. Best-effort: a ledger
    failure warns but never fails the device command (the write already
    succeeded)."""
    try:
        from helixgen.device.ledger import SlotLedger

        led = SlotLedger.load()
        led.record(setlist=setlist, posi=posi, name=name, cid=cid,
                   source_kind=source_kind, source_path=source_path, model=model,
                   now=_utc_now())
        led.save()
    except Exception as e:  # noqa: BLE001 — ledger is advisory, never fatal
        click.echo(f"warning: could not update slot ledger: {e}", err=True)


def _ledger_rename(cid: int, new_name: str) -> None:
    """Best-effort: reflect a device rename in the slot ledger."""
    try:
        from helixgen.device.ledger import SlotLedger

        led = SlotLedger.load()
        if led.rename(cid=cid, new_name=new_name, now=_utc_now()):
            led.save()
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not update slot ledger: {e}", err=True)


def _ledger_remove(cid: int) -> None:
    """Best-effort: drop a deleted preset from the slot ledger."""
    try:
        from helixgen.device.ledger import SlotLedger

        led = SlotLedger.load()
        if led.remove(cid=cid):
            led.save()
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not update slot ledger: {e}", err=True)


def _install_hsp_open(h, body: dict, container: int, pos: int, name: str, *,
                      setlist_label: str, auto_irs: bool = False,
                      ip: str | None = None) -> int:
    """Install a parsed .hsp ``body`` onto an already-open client at
    ``(container, pos)`` and return the new cid. Shared by ``device install``
    and ``device slots restore``. Raises ClickException on any failure.

    Template-free: the ``.hsp`` is transcoded straight into a device
    ``_sbepgsm`` blob (:func:`transcode.hsp_to_sbepgsm`) and written into an
    empty slot — no device template is loaded, so the active tone is untouched.
    """
    from helixgen.device import bridge, transcode

    if h.find_by_pos(container, pos) is not None:
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


@device.command(name="pull")
@click.argument("cid", type=int)
@click.argument("outfile", type=click.Path(dir_okay=False, path_type=Path))
@_device_option
def device_pull(cid: int, outfile: Path, ip: str, port: int) -> None:
    """Load a preset and save its raw edit-buffer content blob (a .sbe backup)."""
    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            if not h.load_preset(cid):
                raise click.ClickException(f"failed to load preset cid {cid}")
            blob = h.get_edit_buffer()
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
    """Create an empty setlist in the LOCAL manifest.

    Device-side setlist creation is deferred (backlog #8) — the 2002 create
    command is uncaptured. Create the setlist by hand in the Stadium app too;
    `device sync` then resolves it by name.
    """
    from helixgen.device.manifest import SetlistManifest

    m = SetlistManifest.load()
    m.create_setlist(setlist)
    m.save()
    click.echo(f"created local setlist {setlist!r} — also create it in the "
               f"Stadium app before syncing (device-side creation is deferred)")


@device.command(name="sync")
@click.argument("setlist_name", metavar="SETLIST", required=False)
@click.option("--all", "all_setlists", is_flag=True, default=False,
              help="Sync every setlist in the manifest (the whole-library reconcile).")
@click.option("--gc", is_flag=True, default=False,
              help="Garbage-collect pool presets no setlist references (only with --all).")
@click.option("--exclude-irs", is_flag=True, default=False,
              help="Install tones only; do not upload their referenced IRs.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the raw engine result dict as JSON.")
@_device_option
def device_sync(setlist_name: str | None, all_setlists: bool, gc: bool,
                exclude_irs: bool, as_json: bool,
                ip: str, port: int) -> None:
    """Sync the manifest's setlists onto the device (pool + references).

    Give a single SETLIST name, or --all for every manifest setlist. The engine
    reconciles the preset pool (install/update/skip), then rebuilds each
    setlist's references to manifest order — never orphaning a still-referenced
    pool preset. --gc (only with --all) prunes pool presets no setlist wants any
    more. A setlist the device doesn't have is reported as a clear error (create
    it in the Stadium app first). EXPERIMENTAL.
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
                            setlists=setlists, gc=gc, exclude_irs=exclude_irs)
    except HelixError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps(res, indent=2))
        return

    pool = res.get("pool", {})
    click.echo(f"pool: {len(pool.get('installed', []))} installed, "
               f"{len(pool.get('updated', []))} updated, "
               f"{len(pool.get('skipped', []))} skipped")
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
              help="Emit raw JSON (entries, or verify records with --verify).")
@_device_option
def device_slots_list(verify: bool, as_json: bool, ip: str, port: int) -> None:
    """List recorded placements in order. Offline unless --verify."""
    from helixgen.device.ledger import SlotLedger

    led = SlotLedger.load()
    entries = led.entries_in_order()

    if verify:
        from helixgen.device import HelixClient, HelixError

        device_presets = []
        try:
            with HelixClient(ip, port) as h:
                for sl in sorted({e.get("setlist") for e in entries if e.get("setlist")}):
                    container = _setlist_container(sl)
                    for p in h.list_presets(container):
                        device_presets.append({**p, "setlist": sl})
        except (HelixError, OSError) as e:
            raise click.ClickException(str(e)) from e
        records = led.verify(device_presets)
        if as_json:
            click.echo(json.dumps(records, indent=2))
        else:
            for r in records:
                click.echo(f"{r.get('slot_label', ''):<4} {r.get('status', ''):<9} "
                           f"{r.get('name', '')}  cid={r.get('cid')}")
        return

    if as_json:
        click.echo(json.dumps(entries, indent=2))
        return
    for e in entries:
        click.echo(f"{e.get('slot_label', ''):<4} {e.get('name', ''):<28} "
                   f"cid={e.get('cid')}  {e.get('source_kind', '')}")


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
    from helixgen.device.ledger import SlotLedger

    led = SlotLedger.load()
    entry = led.find(name=target)
    if entry is None:
        entry = next((e for e in led.entries_in_order()
                      if e.get("slot_label") == target), None)
    if entry is None:
        raise click.ClickException(f"no ledger entry matching {target!r} "
                                   f"(try `helixgen device slots`)")

    src_kind = entry.get("source_kind")
    src_path = entry.get("source_path")
    dest_setlist = setlist or entry.get("setlist")
    dest_pos = pos if pos is not None else entry.get("posi")
    container = _setlist_container(dest_setlist)

    if src_kind not in ("hsp", "sbe") or not src_path:
        raise click.ClickException(
            f"no local source recorded for {entry.get('name')!r} "
            f"(source_kind={src_kind!r}); back it up first (helixgen device pull / backup)")
    src = Path(src_path)
    if not src.is_file():
        raise click.ClickException(f"recorded source no longer exists: {src}")

    from helixgen.device import HelixClient, HelixError

    try:
        with HelixClient(ip, port) as h:
            if src_kind == "sbe":
                if h.find_by_pos(container, dest_pos) is not None and not force:
                    raise click.ClickException(
                        f"{dest_setlist} slot {dest_pos} is not empty (use --force)")
                cid = h._raw.push_to_slot(container, dest_pos, entry.get("name"),
                                     src.read_bytes())
            else:  # hsp
                from helixgen.hsp import read_hsp

                cid = _install_hsp_open(h, read_hsp(src), container, dest_pos,
                                        entry.get("name"), setlist_label=dest_setlist, ip=ip)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if cid is None:
        raise click.ClickException(f"failed to restore {entry.get('name')!r}")
    click.echo(f"restored {entry.get('name')!r} to {dest_setlist} slot {dest_pos} "
               f"(cid {cid}) from {src.name}")
    _record_placement(setlist=dest_setlist, posi=dest_pos, name=entry.get("name"),
                      cid=cid, source_kind=src_kind, source_path=str(src),
                      model=entry.get("model"))


@device_slots.command(name="reorder")
@click.argument("target")
@click.option("--to", "to_index", type=int, required=True,
              help="New 0-based position within the tone's setlist order.")
def device_slots_reorder(target: str, to_index: int) -> None:
    """Move a recorded tone to a new position in its setlist's order.

    Local only — reorders the ledger; run `device slots sync` to apply it to the
    device. TARGET is the tone name.
    """
    from helixgen.device.ledger import SlotLedger

    led = SlotLedger.load()
    if not led.reorder(name=target, to_index=to_index):
        raise click.ClickException(f"no ledger entry matching {target!r} "
                                   f"(try `helixgen device slots`)")
    led.save()
    seq = ", ".join(e.get("name", "") for e in led.entries_in_order())
    click.echo(f"reordered; order is now: {seq}")


@device_slots.command(name="sync")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print the plan without touching the device.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the confirmation prompt.")
@click.option("--no-backup", is_flag=True, default=False,
              help="Skip the safety backup of affected setlists (not recommended).")
@_device_option
def device_slots_sync(dry_run: bool, yes: bool, no_backup: bool,
                      ip: str, port: int) -> None:
    """Reconcile the device so tracked tones sit in the ledger's order.

    Rearranges each affected setlist's tracked presets **among the slots they
    already occupy** — untracked presets are never disturbed. Destructive: it
    pulls each preset's content, deletes it, and re-pushes it in order. Affected
    setlists are backed up first (unless --no-backup), and every pull is verified
    before any delete, so an interruption is recoverable. EXPERIMENTAL.
    """
    from helixgen.device.ledger import SlotLedger
    from helixgen.device import HelixClient, HelixError
    from helixgen.device.client import slot_label

    led = SlotLedger.load()
    entries = led.entries_in_order()
    setlists = sorted({e.get("setlist") for e in entries if e.get("setlist")})

    try:
        with HelixClient(ip, port) as h:
            device_presets = []
            for sl in setlists:
                for p in h.list_presets(_setlist_container(sl)):
                    device_presets.append({**p, "setlist": sl})

            plan = led.sync_plan(device_presets)
            if not plan:
                click.echo("device slots already in ledger order")
                return

            click.echo("planned moves:")
            for m in plan:
                click.echo(f"  {m['name']}: {slot_label(m['from'])} -> {slot_label(m['to'])}")
            if dry_run:
                click.echo("(dry run — no changes made)")
                return
            if not yes:
                click.confirm(f"Rearrange {len(plan)} preset(s) on the device?",
                              abort=True)

            affected = sorted({m["setlist"] for m in plan})
            dev_posi = {(p.get("setlist"), p.get("cid", p.get("cid_"))): p.get("posi")
                        for p in device_presets}

            if not no_backup:
                from helixgen.device import backup as _backup
                for sl in affected:
                    _backup.backup_setlist(h, _setlist_container(sl), now=_utc_now())
                click.echo(f"backed up {len(affected)} setlist(s) before reordering")

            # Phase A — pull + verify EVERY affected blob before deleting anything.
            work = {}
            for sl in affected:
                present = [e for e in entries if e.get("setlist") == sl
                           and (sl, e.get("cid")) in dev_posi]
                targets = sorted(dev_posi[(sl, e.get("cid"))] for e in present)
                pulled = []
                for e in present:
                    h.load_preset(e.get("cid"))
                    blob = h.get_edit_buffer()
                    if not blob:
                        raise click.ClickException(
                            f"aborting: empty content pulled for {e.get('name')!r} "
                            f"(cid {e.get('cid')}); device left unchanged")
                    pulled.append((e, blob))
                work[sl] = (present, targets, pulled)

            # Phase B — delete then re-push in ledger order to the occupied slots.
            for sl in affected:
                container = _setlist_container(sl)
                present, targets, pulled = work[sl]
                for e in present:
                    h._raw.delete(container, [e.get("cid")])
                for (e, blob), target in zip(pulled, targets):
                    new_cid = h._raw.push_to_slot(container, target, e.get("name"), blob)
                    e["posi"] = target
                    e["slot_label"] = slot_label(target)
                    e["cid"] = new_cid
            led.save()
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"synced {len(plan)} move(s) to the device")


@device.command(name="backup")
@click.option("--setlist", type=click.Choice(["user", "factory", "throwaway"]),
              default="user", show_default=True, help="Setlist to back up.")
@click.option("--dir", "out_dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Output dir (default ~/.helixgen/device-backups/ "
                                 "or $HELIXGEN_DEVICE_BACKUPS).")
@_device_option
def device_backup(setlist: str, out_dir, ip: str, port: int) -> None:
    """Back up every preset in a setlist to local .sbe files + a manifest.

    Note: this loads each preset in turn (changes the device's active preset),
    then restores the first one. Works offline afterwards via `device local-list`.
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
