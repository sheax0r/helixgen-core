"""CLI entry points for helixgen."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import click

from helixgen import gitops, home, libinit, mutate, naming, tone_meta
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
    """helixgen — author, edit, and install Line 6 Helix Stadium presets.

    This CLI is the complete engine surface (agents included: per-verb --help
    is the behavioral contract). Verb groups:

    \b
      library    ingest, bootstrap, list-blocks, show-block
      author     generate (recipe JSON -> .hsp), view (read-only projection)
      edit       patch (batch ops), set-param, enable, disable, add-block,
                 remove-block, swap-model
      IRs        irhash, register-irs, ir-scan, list-irs, ir-cache
      tones      register, controllers
      device     `helixgen device ...` — network control of a Helix Stadium
                 (see `helixgen device --help`)

    Mental models that keep you out of trouble:

    \b
      * Run `show-block "<name>"` BEFORE writing params for a block — param
        names are case-sensitive and the generator rejects unknown ones.
      * The .hsp file is the sole source of truth. Recipes are input-only
        (never written back); `view` output is a non-authoritative
        projection; there is no sidecar spec file.
      * Edit an existing .hsp with `patch` (or the single-op verbs) — never
        regenerate it to change one setting.
      * `helixgen device` verbs that write MUTATE the hardware (some change
        the ACTIVE tone immediately); reads are safe. Each verb's --help
        says which it is.
      * The Stadium's network stack is flaky: if a device verb drops or
        stalls, re-run it — `device sync` and the live-ops verbs are
        idempotent; the slot-writing verbs (install/save/push/create) fail
        safe on an occupied slot instead. If it keeps dropping, reboot
        the Helix.
      * Verbs whose output agents consume support --json (machine-readable
        stdout).

    SEE ALSO: docs/CLI.md (full per-verb reference), docs/recipe-reference.md
    (every recipe field), CLAUDE.md (repo mental models).
    """


@cli.command(name="ingest")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@_library_option
def ingest_cmd(path: Path, library_path: Path | None) -> None:
    """Ingest a .hsp/.hlx/.json preset file — or recurse a directory of
    them — into the block library. The first file ever ingested sets the
    library's chassis (a Stadium .hsp chassis generates .hsp output; a
    legacy .hlx chassis generates .hlx)."""
    library = _resolved_library(library_path)
    try:
        summary = ingest_path(path, library)
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e
    click.echo(_format_summary(summary, library))


@cli.command(name="generate")
@click.argument("spec_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o", "--output", "output_path", type=click.Path(path_type=Path),
    default=None, required=False,
    help=("Write the .hsp exactly here (legacy behavior) and skip the library: "
          "no metadata JSON, naming flags ignored. Omit to write into the tone "
          "library at ~/.helixgen/library/tones/<slug>.hsp."),
)
@click.option("--artist", default=None, help="Song identity: artist (needs --song).")
@click.option("--song", default=None, help="Song identity: song title (needs --artist).")
@click.option(
    "--descriptor", default=None,
    help="Descriptor identity (e.g. \"Warm Jazz Clean\"); mutually exclusive with --artist/--song.",
)
@click.option(
    "--guitar", "guitar", default=None,
    help="Target guitar label; slugified and appended to the display name + filename.",
)
@_library_option
@_irs_option
def generate_cmd(
    spec_path: Path,
    output_path: Path | None,
    artist: str | None,
    song: str | None,
    descriptor: str | None,
    guitar: str | None,
    library_path: Path | None,
    irs_dir: Path | None,
) -> None:
    """Generate a preset from a JSON recipe.

    The recipe is input-only (never written back; the .hsp is the sole source
    of truth -- no sidecar). Minimal shape: {"name", optional "author",
    "paths": [1-2 entries, each {"blocks": [{"block", "params"?}, ...]}]}.
    Optional sections: per-path "input"/"output", "split"/"join" entries
    (parallel routing), top-level "snapshots" / "footswitches" /
    "expression" / "midi" / "commands", per-block "ir" (a registered IR by
    wav basename or 32-hex hash) and "trails". Full field reference:
    docs/recipe-reference.md.

    "block" matches a display_name from `list-blocks` (case-sensitive; use
    the model_id if ambiguous). On an `Unknown param(s)` error, run
    `show-block "<block>"` and correct the recipe -- don't guess.

    Output modes:

    \b
      * DEFAULT (no -o): writes into the tone library at
        ~/.helixgen/library/tones/<variant-slug>.hsp and records per-tone
        metadata. Resolve the name from flags, else the recipe's "name" field
        becomes the descriptor. Naming flags: exactly one identity of
        (--artist + --song) OR --descriptor, plus an optional --guitar
        (appended to the display name + slug). The .hsp's meta.name is set to
        the resolved display name, the logical tone JSON gains a variant, and
        the tone auto-registers in the library manifest. A slug collision
        (the target .hsp already exists) is an error with a rename suggestion
        -- the existing file is never overwritten.
      * LEGACY (-o OUT): writes the .hsp exactly at OUT and auto-registers it,
        but writes NO metadata JSON; naming flags are ignored. Output
        extension picks the format: .hsp = Stadium (8-byte magic + JSON),
        .hlx = legacy Helix pretty JSON.

    After generating with user IRs, the same WAVs must also be on the device
    for the cabs to resolve (`device install --auto-irs` / `device sync`
    upload them; or import via the Stadium app).
    """
    library = _resolved_library(library_path)
    irs = _resolved_irs(irs_dir)
    try:
        # Parse+validate the recipe before touching the chassis, matching the
        # legacy error-ordering tests rely on (a malformed recipe reports its
        # own error rather than being masked by a missing-chassis error).
        raw = json.loads(spec_path.read_text())
        spec = parse_spec(raw, source=str(spec_path))
        chassis = library.load_chassis()
        shape = chassis.get(CHASSIS_SHAPE_KEY, "hlx")

        if output_path is not None:
            # LEGACY -o: exactly today's behavior. No metadata; naming flags
            # (if any) are ignored by design.
            output_path = Path(output_path)
            if shape == "hsp":
                data = generate_from_recipe(
                    spec, library, irs=irs, chassis=chassis, source=str(spec_path)
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(data)
                _auto_register_tone(output_path)
            else:
                generate_preset(spec_path, output_path, library, irs=irs)
            click.echo(f"Wrote {output_path}")
            return

        # DEFAULT: write into the tone library with resolved naming.
        if shape != "hsp":
            raise click.ClickException(
                "default library write needs a Stadium (.hsp) chassis; use -o "
                "to write a legacy .hlx preset to an explicit path"
            )

        guitar_slug, guitar_short = (
            _resolve_guitar(guitar) if guitar else (None, None)
        )
        # Flags win; otherwise the recipe's own name becomes the descriptor.
        if artist or song or descriptor:
            r_artist, r_song, r_descriptor = artist, song, descriptor
        else:
            r_artist, r_song, r_descriptor = None, None, spec.name
        try:
            preset_name = naming.display_name(
                artist=r_artist, song=r_song, descriptor=r_descriptor,
                guitar_short=guitar_short,
            )
            logical = naming.logical_slug(
                artist=r_artist, song=r_song, descriptor=r_descriptor,
            )
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        variant = naming.variant_slug(logical, guitar_slug)

        libinit.ensure_initialized()
        tones = home.tones_dir()
        tones.mkdir(parents=True, exist_ok=True)
        out = tones / f"{variant}.hsp"
        if out.exists():
            raise click.ClickException(
                f"a tone already exists at {out} -- refusing to overwrite. "
                f"Rename this one (change --descriptor/--artist/--song or "
                f"--guitar), or edit the existing .hsp in place."
            )

        # meta.name carries the resolved display name so auto-register keys by it.
        spec.name = preset_name
        data = generate_from_recipe(
            spec, library, irs=irs, chassis=chassis, source=str(spec_path)
        )
        out.write_bytes(data)

        # Metadata JSON (creates or extends the logical tone), then manifest.
        existing = (
            tone_meta.load_tone_meta(logical)
            if tone_meta.meta_path(logical).exists()
            else None
        )
        meta = tone_meta.upsert_variant(
            existing,
            artist=r_artist, song=r_song, descriptor=r_descriptor,
            guitar_slug=guitar_slug, guitar_short=guitar_short,
            hsp_path=out,
        )
        tone_meta.save_tone_meta(meta)  # atomic write + advisory commit
        _auto_register_tone(out)
        # One coherent commit sweeps up the manifest write too (advisory).
        gitops.auto_commit(
            home.helixgen_home(), f"helixgen: generate tone {variant}"
        )

        click.echo(f"Wrote {out}")
        click.echo(f"Preset name: {preset_name}")
        click.echo(f"Logical tone: {logical}")
    except (KeyError, LookupError, SpecError, ParamValidationError, GenerateError, FileNotFoundError) as e:
        raise click.ClickException(str(e)) from e


def _resolve_guitar(label: str) -> tuple[str, str]:
    """Resolve a --guitar label into ``(slug, short_name)``.

    PR 2 seam: the slug is ``naming.slugify(label)`` and the short name is the
    literal label. PR 3 (guitar profiles) swaps the internals to look the label
    up as a profile by slug/name/short_name; callers keep this signature.
    """
    return naming.slugify(label), label


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
    """Print a read-only recipe-shape projection of a .hsp preset (JSON).

    Use it to inspect any .hsp's blocks, params, snapshots, footswitches, and
    expression wiring before deciding what to edit. Stdout is a JSON document
    (machine-readable as-is; no flag needed). The projection is for
    comprehension only — it is NOT authoritative and is not the edit surface;
    edit the .hsp itself with `patch` / `set-param` / etc. Controllers that
    can't be mapped are preserved under a top-level `unknown_controllers`
    list rather than dropped.
    """
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
    """Set one param on one block: `set-param preset.hsp "Brit Amp" Drive 0.85`.

    Mutates the .hsp in place (no sidecar). Run `show-block` first — param
    names are case-sensitive. VALUE is auto-coerced (bool -> int -> float ->
    string). A NEGATIVE value needs the `--` sentinel after any flags:
    `helixgen set-param t.hsp output level -- -3`.

    Besides library blocks, BLOCK may be a signal-flow pseudo-block:
    `input` / `output` / `split` / `join` (`merge` = alias), addressing the
    path's endpoints / split / merge mixer (`--path` picks the DSP; `--pos`
    disambiguates two splits; `--lane` does not apply). Input params use the
    recipe vocabulary (impedance, pad, trim, gate, threshold, decay, link);
    output params are level/pan; split/join params are the wire names
    (`BalanceA`, `Frequency`, `"A Level"`, ...). Use `--path`/`--lane`/`--pos`
    when a block name appears more than once in the preset.
    """
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
@click.option("--path", "path_idx", type=int, default=0, show_default=True,
              help="DSP path (0 or 1) to add the block to.")
@click.option("--after", default=None, metavar="BLOCK_NAME",
              help="Insert after this named block instead of appending to "
                   "the end of the path.")
@_library_option
@_irs_option
def add_block_cmd(preset_path, block, path_idx, after, library_path, irs_dir):
    """Add a block to a path (append, or --after a named block). BLOCK is a
    library display name or model id — case-sensitive, same rules as
    `generate` (check with `show-block`)."""
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


@cli.command(name="patch")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("ops", type=click.File("r"))
@click.option("--json", "as_json", is_flag=True, default=False,
              help='Emit {"path", "warnings"} as JSON instead of text.')
@_library_option
def patch_cmd(preset_path: Path, ops, as_json: bool, library_path) -> None:
    """Apply a LIST of surgical edits to a .hsp file, in place, atomically.

    OPS is a JSON file (or `-` for stdin) holding a list of operations:

    \b
      [{"op": "set_param",   "block": "Tape Echo Stereo",
        "param": "Mix", "value": 0.3},
       {"op": "set_enabled", "block": "Plate Stereo", "enabled": false},
       {"op": "add_block",   "block": "LA Studio Comp", "path": 0},
       {"op": "remove_block","block": "Plate Stereo"},
       {"op": "swap_model",  "old": "Brit Plexi Brt", "new": "Brit 2204"}]

    All ops are applied to an in-memory copy and the file is written ONCE at
    the end — an invalid op anywhere in the list (unknown op, bad param,
    unresolvable block) aborts with the .hsp untouched, never half-patched.
    Prefer this over a sequence of single-op verbs when making several edits.

    Op fields mirror the single-op verbs: optional "path"/"lane"/"pos" ints
    disambiguate a block name placed more than once (dual-cab, both lanes of
    a split); "set_enabled" takes an optional "snapshot" name. "set_param"
    also accepts the signal-flow pseudo-blocks `input` / `output` / `split` /
    `join` (`merge` = alias) — see `set-param --help`. Run `show-block` first
    to confirm exact, case-sensitive param names.

    Warnings (e.g. swap_model params it had to drop) go to stderr, or into
    the --json result's "warnings" list. Exit 0 = file patched.
    """
    try:
        operations = json.load(ops)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"OPS is not valid JSON: {e}") from e

    library = _resolved_library(library_path)
    preset_path = Path(preset_path)
    try:
        body = read_hsp(preset_path)
        warnings_out = mutate.apply_operations(body, operations, library)
        write_hsp(preset_path, body)
    except (MutateError, KeyError, LookupError, SpecError,
            ParamValidationError, GenerateError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps({"path": str(preset_path), "warnings": warnings_out}))
        return
    for w in warnings_out:
        click.echo(f"warning: {w}", err=True)
    click.echo(f"Patched {preset_path} ({len(operations)} op(s))")


@cli.command(name="list-blocks")
@click.option("--category", default=None,
              help="Filter to one category: amp, cab, drive, delay, reverb, "
                   "modulation, filter, eq, dynamics, pitch, volume, send.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a JSON array of {display_name, model_id, category}.")
@_library_option
def list_blocks_cmd(category: str | None, as_json: bool, library_path: Path | None) -> None:
    """List blocks in the library, grouped by category.

    Each line is `<display_name>  [<model_id>]`. Use the display name (or the
    model_id, if the name is ambiguous) as the `block` value in recipes and
    edit verbs, then run `show-block` for its exact param names before
    writing params.
    """
    library = _resolved_library(library_path)
    blocks = library.list_blocks(category=category)
    if as_json:
        click.echo(json.dumps([
            {"display_name": b.display_name, "model_id": b.model_id,
             "category": b.category}
            for b in sorted(blocks, key=lambda x: (x.category, x.display_name))
        ], indent=2))
        return
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
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the schema as JSON ({display_name, model_id, "
                   "category, aliases, params}).")
@_library_option
def show_block_cmd(name_or_id: str, as_json: bool, library_path: Path | None) -> None:
    """Print a block's schema: exact param names, types, defaults, ranges.

    Accepts the display name (e.g. "Brit Plexi Brt"), the model id
    (e.g. "HD2_AmpBritPlexiBrt"), or an alias. ALWAYS run this before
    writing params for a block — param names are case-sensitive and
    `generate`/`patch` reject unknown ones. Most knob params are floats
    0.0-1.0; some are ints/bools/Hz — check the type and observed range here
    rather than guessing.
    """
    library = _resolved_library(library_path)
    try:
        block = library.find_block(name_or_id)
    except (KeyError, LookupError) as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps({
            "display_name": block.display_name,
            "model_id": block.model_id,
            "category": block.category,
            "aliases": list(block.aliases or []),
            "params": block.params,
        }, indent=2))
        return

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

    Prints each `<hash>  <wav>` pair as it registers. REMINDER: registering
    only updates the local mapping.json — the hash resolves on the hardware
    only once the same WAV is also imported onto the device (`device
    push-ir`, `device install --auto-irs`, `device sync`, or the Stadium
    app's Librarian). Direct hashing requires libsndfile and 48 kHz sources
    (see `irhash --help` for the constraint details).
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
        except (RuntimeError, NotImplementedError, FileNotFoundError,
                ValueError) as e:
            raise click.ClickException(str(e)) from e
        cache.save()

    mapping = _resolved_irs(irs_dir)
    try:
        for h, wav in zip(hashes, wav_paths):
            mapping.register(h, wav, force=force)
    except IrMappingError as e:
        raise click.ClickException(str(e)) from e
    mapping.save()
    for h, wav in zip(hashes, wav_paths):
        click.echo(f"{h}  {wav}")
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
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a per-category summary as JSON: {registered, "
                   "already_registered, conflicts, failed}.")
@_irs_option
def ir_scan_cmd(
    directories: tuple[Path, ...],
    rescan: bool,
    remove_basename: str | None,
    as_json: bool,
    irs_dir: Path | None,
) -> None:
    """Recursively scan directories for .wav files and cache their Stadium hashes.

    Skips a WAV only when it is already registered AND its cached hash is still
    valid for the file on disk (matching mtime + size) — an edited or replaced
    WAV is detected and re-hashed. Pass --rescan to recompute unconditionally.
    Skips files that can't be hashed (non-48 kHz, libsndfile errors) with a
    stderr warning; does not abort the scan.

    --json emits a per-category summary instead of the count line:
    {"registered": [names], "already_registered": [names],
    "conflicts": [names whose hash already maps to a DIFFERENT path;
    re-run with --rescan to overwrite], "failed": [{basename, reason}]}.
    Partial success is persisted either way.

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
    registered: list[str] = []
    already: list[str] = []
    conflicts: list[str] = []
    failed: list[dict[str, str]] = []
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
                already.append(wav.name)
                continue
            try:
                if rescan:
                    h = compute_stadium_irhash(wav)
                    cache.put(wav, h)
                else:
                    h = cached_irhash(wav, cache=cache)
            except (NotImplementedError, RuntimeError, FileNotFoundError,
                    ValueError) as e:
                click.echo(f"skip {wav}: {e}", err=True)
                failed.append({"basename": wav.name, "reason": str(e)})
                continue
            try:
                mapping.register(h, wav, force=rescan)
            except IrMappingError as e:
                click.echo(f"skip {wav}: {e}", err=True)
                conflicts.append(wav.name)
                continue
            registered_paths.add(wav_abs)
            registered.append(wav.name)

    mapping.save()
    cache.save()
    if as_json:
        click.echo(json.dumps({
            "registered": registered,
            "already_registered": already,
            "conflicts": conflicts,
            "failed": failed,
        }, indent=2))
        return
    click.echo(
        f"Scanned {scanned} wav(s): {len(registered)} added, "
        f"{len(already)} already cached, "
        f"{len(conflicts) + len(failed)} skipped (errors)"
    )


@cli.command(name="irhash")
@click.argument(
    "paths", nargs=-1, required=True,
    type=click.Path(exists=True, path_type=Path),
)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a JSON array of {hash, path, basename}.")
def irhash_cmd(paths: tuple[Path, ...], as_json: bool) -> None:
    """Compute Stadium IR hashes for WAVs — stateless (nothing is registered).

    Each PATH is a .wav file or a directory (recursed for *.wav). Prints
    `<hash>  <path>` per WAV; the 32-hex hash is exactly what a preset's
    `irhash` field carries, so it can be embedded in a recipe's `ir` field
    directly. Unlike `register-irs`/`ir-scan`, this never writes to
    mapping.json — use those to persist a mapping.

    Requires libsndfile (`brew install libsndfile`). 48 kHz sources only —
    non-48 kHz fails with a `sox in.wav -r 48000 out.wav` suggestion (the
    DEVICE accepts any rate and resamples internally; helixgen just can't
    hash non-48k off-device). Stereo is reduced to the left channel
    (matching Stadium's import). REMINDER: a hash only resolves on the
    hardware once the same WAV is imported onto the device.

    A file that fails to hash: fatal when named explicitly; a stderr
    warning (file skipped) when found via a directory walk.
    """
    cache = IrHashCache.load()
    results: list[dict[str, str]] = []
    seen: set[Path] = set()

    def _hash_one(wav: Path, *, fatal: bool) -> None:
        resolved = wav.resolve()
        if resolved in seen:
            return
        try:
            h = cached_irhash(wav, cache=cache)
        except (NotImplementedError, RuntimeError, FileNotFoundError,
                ValueError) as e:
            if fatal:
                raise click.ClickException(str(e)) from e
            click.echo(f"skip {wav}: {e}", err=True)
            return
        seen.add(resolved)
        results.append({"hash": h, "path": str(wav), "basename": wav.name})

    try:
        for p in paths:
            if p.is_dir():
                for wav in sorted(p.rglob("*")):
                    if wav.is_file() and wav.suffix.lower() == ".wav":
                        _hash_one(wav, fatal=False)
            else:
                _hash_one(p, fatal=True)
    finally:
        cache.save()  # keep hashes computed before any fatal error

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return
    for r in results:
        click.echo(f"{r['hash']}  {r['path']}")


@cli.command(name="list-irs")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a JSON array of {hash, path}.")
@_irs_option
def list_irs_cmd(as_json: bool, irs_dir: Path | None) -> None:
    """List the LOCALLY registered user IRs: `<hash>  <wav-path>` per line.

    This is helixgen's own mapping.json (irhash -> wav path), not the
    device's IR list (`helixgen device list-irs` reads that). Check this
    before choosing a cab in a recipe: empty output -> use a stock cab
    (`Mic Ir_*` block); otherwise a registered IR can be referenced by wav
    basename (or hash) in a `With Pan` block's `ir` field. The hash only
    resolves on the hardware once the same WAV is also on the device
    (`device push-ir`, `device install --auto-irs`, `device sync`, or the
    Stadium app's Librarian import).
    """
    mapping = _resolved_irs(irs_dir)
    if as_json:
        click.echo(json.dumps([
            {"hash": h, "path": mapping.entries[h]}
            for h in sorted(mapping.entries)
        ], indent=2))
        return
    for hash_ in sorted(mapping.entries):
        click.echo(f"{hash_}  {mapping.entries[hash_]}")


@cli.command(name="register")
@click.argument("hsp_path", type=click.Path(exists=True, path_type=Path))
def register_cmd(hsp_path: Path) -> None:
    """Register an existing local .hsp into the tone library (off-device)."""
    from helixgen.device.manifest import SetlistManifest, ManifestError

    m = SetlistManifest.load()
    try:
        name = m.register_tone(hsp_path, source="import-local")
    except ManifestError as e:
        raise click.ClickException(str(e)) from e
    m.save()
    click.echo(f"registered {name!r} in the tone library (off-device)")


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

# The device verb group lives in `cli_device` (a pure extraction of this
# module's former `# --- device` section); import it here so `helixgen
# device ...` registers on the core `cli` group and `helixgen.cli:cli` stays
# the single entry point. `_auto_upload_irs` is re-exported for callers that
# import it from `helixgen.cli`.
from helixgen.cli_device import device, _auto_upload_irs  # noqa: E402,F401

cli.add_command(device)


if __name__ == "__main__":  # allow `python -m helixgen.cli ...`
    cli()
