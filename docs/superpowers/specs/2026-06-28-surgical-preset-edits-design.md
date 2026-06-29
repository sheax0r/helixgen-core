# Surgical preset edits via spec patches + decompiler

**Date:** 2026-06-28
**Status:** Design approved, pending implementation plan

## Problem

Improving a tone today means re-articulating the whole spec and regenerating
from scratch. For small changes — "replace this cab with that one", "set this
param to this value", "kill the reverb" — that is more friction than the change
deserves. We want terse, surgical edits ("jq-ish") that the user can drive by
CLI *and* the `tone` skill can drive conversationally.

The naive version — hand-poking the `.hsp` with jq — is a trap: the `.hsp` is a
**compiled artifact** (8-byte `rpshnosj` magic + compact JSON, with values
wrapped as `{"value": x}` / stereo `{"1":…,"2":…}` / controllers, Stadium-
namespace model IDs, embedded irhashes). Editing it directly desyncs the
`spec.json` source of truth, blows the tweak away on the next regenerate, and
forces us to re-implement everything `generate.py` already does (value
wrapping, model-id translation e.g. `HD2_DistScream808Mono` ↔ `HD2_DrvScream808`,
stereo dual-slot cabs, irhash injection, param range validation).

The user works **both** from spec'd tones (helixgen-generated) and orphan
`.hsp` files (ingested / exported from the device / from other people), roughly
equally. The design must cover both.

## Approach (chosen)

**Decompiler + spec patches**, unified by a **sidecar spec** convention.

- The **spec is always the source of truth.** Edit verbs mutate the spec and
  regenerate the `.hsp`; they never touch wrapped `.hsp` JSON directly.
- Every `.hsp` gets a **sidecar spec** beside it: `foo.hsp` ↔ `foo.spec.json`.
  `generate` writes both. Verbs target the preset, edit the sidecar spec, and
  regenerate the `.hsp` atomically. The user *feels* like they are editing the
  `.hsp`; the implementation keeps the spec authoritative and reuses all of
  `generate.py`'s validation/translation/IR logic.
- A true **orphan** `.hsp` (no sidecar) is brought into the spec world by the
  **decompiler** (`.hsp → spec.json`) before any edit. So "both equally"
  collapses to a single code path: *(decompile if needed) → patch spec →
  regenerate*.

Rejected alternatives:
- **Two patch engines** (spec-patch + a separate in-place `.hsp` patcher):
  duplicates `generate` logic, two code paths to keep in sync, orphan edits run
  blind without full-spec context.
- **One in-memory preset model** (both spec and `.hsp` load into a single
  editable model): cleanest in theory, but the largest refactor of
  `generate.py` for the least immediate payoff.

## Components

### 1. Decompiler — `decompile.py`

`decompile(hsp_path) -> spec dict`, plus CLI `helixgen decompile <preset.hsp>
-o spec.json`.

Today `hsp.py`/`ingest` only extract a flat block list (that was all the library
needed). The decompiler extends this to reconstruct the **full spec** that
`generate.py` consumes:

- **paths** + per-path `input` routing (`inst1`/`inst2`/`both`/`none`).
- **blocks** + `params` (reusing existing `_unwrap_value` + reverse model-id
  translation `_translate_model_id`).
- **snapshots** — names + per-block bypass/param deltas, recovered as deltas
  from the path-level base values.
- **footswitches** — `FS1..FS10` block assignments + latching/momentary
  behavior.
- **expression** — `EXP1`/`EXP2` targets, read back from `controller` wrappers
  on params, including `min`/`max`.
- **per-block IR refs** — `irhash → wav basename` via reverse `mapping.json`
  lookup; falls back to the raw hash when no mapping exists.
- **meta** — `name`, `author`, and carryover `meta.color`/`meta.info`/
  `device_id`.

**Fidelity bar: semantic, not byte-identical.** The success criterion is that
regenerating the decompiled spec produces an **equivalent-loading preset**
across every feature. Byte-level differences (meta ordering, key order) are
expected and acceptable.

### 2. Patch verbs — `patch.py`

Each verb operates on a preset (sidecar spec or explicit `spec.json`), mutates
the spec, validates, and (for `.hsp`-targeted invocations) regenerates the
`.hsp`. Exposed as **both** CLI subcommands and MCP tools so the human and the
skill share one implementation.

- `set-param <preset> "<Block>".<Param> <value>` — mechanical spec edit;
  range/name validation happens on regenerate via `generate.py`.
- `enable` / `disable <preset> "<Block>" [--snapshot <name>]` — flip
  `@enabled`, optionally within a snapshot.
- `add-block <preset> "<Block>" [--path N] [--after "<Block>"]` /
  `remove-block <preset> "<Block>"` — chain topology edits within a path.
- `swap-model <preset> "<Old>" "<New>"` — the only verb with real smarts:
  - resolves both names to the library,
  - **requires the same category** (cab→cab, amp→amp, …),
  - carries over same-named params,
  - fills defaults for params new to the target,
  - **warns on dropped params**,
  - for IR cabs, preserves the `ir` ref where the target accepts one.

### 3. Block addressing

Blocks are referenced by **display name**, reusing the existing
snapshot/footswitch resolver. Duplicates are disambiguated with
`--path N --index M`; `[model_id]` brackets work as they do in `generate`
(e.g. `"[HD2_AmpBritPlexiBrt]"`).

### 4. Skill wiring (`tone` skill)

The `tone` skill learns the patch loop: when the user asks for an *adjustment*
to an existing tone ("brighter cab", "swap to a Plexi", "more delay"), **prefer
patching the sidecar spec over regenerating from a paragraph.** Map the request
to the narrowest verb (e.g. "brighter" → `set-param` on `HighCut`) rather than
re-deriving the whole chain.

## Data flow

```
spec.json --generate--> foo.hsp  +  foo.spec.json (sidecar)
                              ^
   set-param / swap-model / enable / add-block ...
                              |
            edit sidecar spec, validate, regenerate

orphan.hsp (no sidecar)
   --decompile--> orphan.spec.json --[same patch path as above]
```

## Error handling

- Unknown block name → list candidates (reuse existing resolver error).
- Ambiguous block name (duplicate) → require `--path/--index`, show the matches.
- Out-of-range / unknown param → surface `generate.py`'s existing validation
  error; do not write a broken `.hsp`.
- `swap-model` across categories → refuse with the two categories named.
- `swap-model` dropping params → proceed, but **warn** with the dropped names.
- Decompile of a feature we can recover only partially → covered by the
  semantic-fidelity round-trip test; if a real export surfaces an
  unrecoverable construct, that is a decompiler bug to fix, not a warn-and-skip.

## Testing (TDD)

- **Decompiler round-trip** (backbone): for each fixture, `spec → generate →
  hsp → decompile → spec'`, assert semantic equality across paths, routing,
  snapshots, footswitches, expression, IR refs, and meta. Run against synthetic
  fixtures and (skip-guarded) real exports.
- **Each verb:** happy path + validation rejects (unknown block, bad range,
  cross-category swap, ambiguous name without disambiguation).
- **swap-model:** param carryover, default-fill, dropped-param warning, IR-ref
  preservation.
- **Orphan flow:** `.hsp` with no sidecar → verb auto-decompiles → edit lands
  and regenerates.
- **Sidecar:** `generate` writes `foo.spec.json`; verb edits update it.

## Out of scope for v1

- Parallel-path topology edits (already a v1 `generate` limitation).
- Reordering blocks within a path.
- Byte-identical `.hsp` round-trips.

## Project layout impact

- New `src/helixgen/decompile.py` and `src/helixgen/patch.py`.
- New CLI subcommands in `cli.py`; new MCP tools in the server.
- `generate` gains sidecar-spec emission.
- Tests under `tests/` following the established skip-guarded fixture pattern.
