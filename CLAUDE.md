# helixgen

CLI that generates Line 6 Helix Stadium `.hsp` presets (and legacy `.hlx`) from
JSON tone specs. The library lives at `~/.helixgen/library/` (override with
`$HELIXGEN_LIBRARY`) and is built by ingesting real device exports.

User IRs (impulse responses) registered with `helixgen register-irs` live at
`~/.helixgen/irs/` by default (override with `$HELIXGEN_IRS`). The mapping
file `mapping.json` records `irhash → wav-path`. See `helixgen list-irs`.

## CLI

- `helixgen list-blocks [--category amp|cab|drive|delay|reverb|modulation|filter|eq|dynamics|pitch|volume|send]` — list blocks, optionally filtered.
- `helixgen show-block "<name>"` — print a block's exact param names, types, defaults, and observed ranges. **Run this before writing a spec** — param names are case-sensitive and the generator rejects unknown ones.
- `helixgen generate <spec.json> -o <out.hsp>` — generate a preset. The `-o` flag is required. Output extension `.hsp` writes a Stadium-format file (8-byte magic + compact JSON); `.hlx` writes pretty JSON for the original Helix.
- `helixgen ingest <path>` — ingest a `.hsp`/`.hlx`/`.json` file or recurse a directory; first encountered file sets the chassis.
- `helixgen register-irs <preset.hsp> <wav1> <wav2> ...` — bind each unknown `irhash` in the preset (path-then-position order) to the corresponding wav arg. Use `--force` to overwrite existing mappings.
- `helixgen register-irs <wav1> <wav2> ...` — compute each WAV's Stadium hash directly (no device export needed) and register. Requires libsndfile (`brew install libsndfile` on macOS). Only 48 kHz sources supported; non-48 kHz raises an error suggesting `sox`. Stereo WAVs are reduced to the left channel (matches Stadium's import).
- `helixgen ir-scan <dir>... [--rescan] [--remove <basename>]` — recursively walk one or more directories for `*.wav`, compute each Stadium hash, and cache. Files already cached are skipped by absolute path unless `--rescan`. Per-file failures (non-48 kHz, libsndfile errors) print a stderr warning and the scan continues. `--remove <basename>` forgets a single entry. Use this to bulk-register a whole IR library at once; use `register-irs` for one-off binding from a preset.
- `helixgen list-irs` — print `<hash>  <wav-path>` for every registered IR.

Example: `helixgen ir-scan ~/IRs && helixgen list-irs | wc -l`.

## IR cab-pack catalog (character reference)

The IR library at `irs/` (gitignored — paid packs stay local) carries a
grep-first tonal catalog at `irs/_catalog/`. It answers "which IR is beefiest /
brightest / best for a vintage clean / tightest for modern metal" without
re-analysing WAVs. Start at `irs/_catalog/README.md` (index + controlled tag
vocabulary + mic legend + example greps); one file per pack holds per-mix mic
combos and character tags.

**When a new IR pack is added to `irs/`, catalog it before moving on:**
1. Read the pack's `*Manual*.pdf` — cab/speaker/amp, mic legend, per-mix mic
   combos, and any artist/usage notes.
2. `ls` the pack's `Mixes/` folder for the exact WAV basenames (these are what a
   preset's cab block references via `mapping.json`).
3. Optionally FFT-analyse each Mix WAV (stdlib `wave` + `numpy`, 5 guitar bands)
   for measured bright/dark/beefy/tight tags — relative *within* the pack.
4. Write `irs/_catalog/<slug>.md` from the template in the catalog README, using
   ONLY the controlled vocabulary; add a row to the README index table.

Don't invent character the manual doesn't state, but well-established general
knowledge is fine (Greenback = classic-rock, V30 = modern metal, ribbon = warm
top, SM7 = fat). The catalog README's "Adding a new pack" section is the
authoritative procedure and self-documenting template.

## spec.json shape

```json
{
  "name": "Preset Display Name",
  "author": "you",
  "paths": [
    {
      "blocks": [
        {"block": "Compulsive Drive", "params": {"Gain": 0.45, "Tone": 0.55}},
        {"block": "Brit Plexi Brt",   "params": {"Drive": 0.7, "Master": 0.5}},
        {"block": "Mic Ir_4x12 Greenback 25 With Pan"},
        {"block": "Tape Echo Stereo", "params": {"Mix": 0.18}},
        {"block": "Plate Stereo",     "params": {"Mix": 0.12}}
      ]
    }
  ]
}
```

- `paths` is 1–2 entries (each maps to one DSP); parallel splits inside a path are not supported in v1.
- `block` matches the display_name from `list-blocks` (e.g. "Brit Plexi Brt") — case-sensitive. If ambiguous, use the model_id in brackets (e.g. "HD2_AmpBritPlexiBrt").
- `params` values are floats 0.0–1.0 for most knobs; some are ints/bools/Hz. Verify ranges with `show-block`.

### Optional: per-path input routing

Each path entry may carry an optional `"input"` field with one of:
- `"inst1"` — Instrument 1 jack only
- `"inst2"` — Instrument 2 jack only
- `"both"` — both jacks (stereo) — **default on paths[0]**
- `"none"` — input disabled — **default on paths[1]**

Stadium-only; ignored with a warning for `.hlx` (legacy Helix) chassis.

### Optional: snapshots (Stadium scenes)

Add a top-level `snapshots` array (up to 8 entries) to define named scenes that override block bypass and param values within one preset:

```json
"snapshots": [
  {"name": "Rhythm"},
  {"name": "Lead",  "params": {"Brit Plexi Brt": {"Drive": 0.85}, "Tape Echo Stereo": {"Mix": 0.30}}},
  {"name": "Clean", "disable": ["Compulsive Drive"], "params": {"Brit Plexi Brt": {"Drive": 0.30}}}
]
```

- Each snapshot is a delta from path-level base values. Snapshot 0 (the first) is active on load.
- `disable: [...]` bypasses those blocks in that snapshot; `params` overrides values.
- Block references must resolve to a block already placed in `paths`.
- Omit `snapshots` entirely to use the device's defaults (8 unnamed slots, no variation).

When a snapshot references a block whose display name is ambiguous (multiple
placed blocks humanize to the same name, e.g. two "Stereo" blocks across a
split), carry a `(lane, pos)` coordinate:

- `disable` entries may be objects instead of bare strings:
  `"disable": [{"block": "Stereo", "lane": 1, "pos": 2}]`
- `params` may be a list instead of a name-keyed object:
  `"params": [{"block": "Stereo", "lane": 1, "pos": 2, "params": {"Mix": 0.3}}]`

Coordinates are only needed to disambiguate; the bare string / name-keyed object
forms remain valid for uniquely-named blocks. `path` (0 or 1) is added only when
the same name is ambiguous across both DSP paths.

### Optional: footswitches

Assign blocks to physical footswitches on the device. Stadium XL exposes
`FS1`..`FS10`, plus `EXP1Toe` — the toe switch under the onboard expression
pedal (push the pedal fully forward to click it).

```json
"footswitches": [
  {"switch": "FS3", "block": "Compulsive Drive"},
  {"switch": "FS4", "block": "Tape Echo Stereo", "behavior": "momentary"},
  {"switch": "EXP1Toe", "block": "Teardrop 310 Mono"}
]
```

- `switch` — `"FS1"`..`"FS10"`, or `"EXP1Toe"` (expression-pedal toe switch).
- `block` — must reference a block placed in `paths`.
- `behavior` — `"latching"` (default; toggle) or `"momentary"` (on while held).
- One switch may be assigned at most one block; one block may be on at most one switch.
- **Wah/expression auto-engage:** assign the wah's bypass to `EXP1Toe` (with
  `EXP1` sweeping its `Pedal` param) so pressing the pedal toe-down engages the
  wah — the standard Helix wah behavior. A regular `FS` works too but requires a
  separate stomp.

### Optional: expression pedal

Sweep one or more parameters with the expression pedal(s). Stadium XL
exposes `EXP1` and `EXP2`.

```json
"expression": [
  {
    "pedal": "EXP1",
    "targets": [{"block": "Teardrop 310 Mono", "param": "Pedal"}]
  },
  {
    "pedal": "EXP2",
    "targets": [
      {"block": "Brit Plexi Brt",   "param": "Master", "min": 0.0, "max": 0.7},
      {"block": "Tape Echo Stereo", "param": "Mix",    "min": 0.0, "max": 0.4}
    ]
  }
]
```

- `pedal` — `"EXP1"` or `"EXP2"`.
- `targets` — non-empty list. Each target sweeps one param on one block.
- `min`/`max` — normalized 0..1 floats; default `0.0`/`1.0`; must satisfy `min ≤ max`.
- One pedal may have many targets. One `(block, param)` pair may be driven by at most one pedal.
- v1 only sweeps 0..1-style float params (knob values). Hz/int/bool params are out of scope.

### Optional: per-block IR reference

For IR blocks (`"block": "With Pan"` and other `HX2_ImpulseResponse*` variants),
add an optional `ir` field to load a registered user IR:

```json
{"block": "With Pan", "ir": "YA DXVB 112 Mix 01.wav",
 "params": {"HighCut": 6500.0, "LowCut": 90.0, "Mix": 1.0}}
```

- `ir` accepts a wav basename (looked up in `mapping.json` values) or a
  32-char hex hash (looked up in keys).
- If `ir` is omitted, the block uses the canonical `irhash` recorded during
  ingest of an IR-bearing preset.
- Register IRs first with `helixgen register-irs`; see `list-irs` for what's
  available.

Stadium-only; ignored without warning for `.hlx` (legacy Helix) chassis output.

### Optional: delay/reverb trails (`trails`)

Delay and reverb blocks may carry an optional `"trails"` boolean that controls
harness spillover — whether the block's echoes / reverb tail keep ringing when
the block is **bypassed** (manually or via a footswitch):

```json
{"block": "Tape Echo Stereo", "params": {"Mix": 0.25}, "trails": true},
{"block": "Plate Stereo",     "params": {"Mix": 0.15}, "trails": true}
```

- `trails: true` / `false` sets the block's bNN `harness.params.Trails`.
  - `true` → tail rings out and fades when you bypass the block.
  - `false` → tail cuts off abruptly the instant you bypass the block.
- Trails governs tail spillover on **block bypass** (footswitch or manual). To
  hear it, bypass the block — ideally while palm-muting so the guitar's natural
  sustain doesn't mask the wet tail. (Footswitch/manual-bypass behavior is
  hardware-validated on Stadium XL.)
- Omitting `trails` leaves the device default (or whatever a decompiled
  `raw.harness` carried) untouched.
- **Delay and reverb only.** Setting `trails` on any other block category is a
  generate error.
- `decompile` lifts an existing `Trails` out of `raw.harness` into this clean
  `trails` field (delay/reverb blocks only), so it round-trips as a first-class
  setting. If both `trails` and a `raw.harness` are present, `trails` wins.
- Stadium-only; ignored for `.hlx` (legacy Helix) chassis (no harness emitted).

### Optional: per-block verbatim state (`raw`)

Blocks may carry an optional `"raw"` object holding verbatim Stadium bNN state
that helixgen does not model but preserves for round-trip fidelity:

- `"harness"` — the bNN-level `harness` dict (carries structural fields like
  `dual`, `upper`, `bypass`, `EvtIdx`, and its own `@enabled`). Non-deterministic;
  preserved verbatim. The one author-facing harness field, `Trails`
  (delay/reverb spillover), is modeled separately as the block-level `trails`
  field above and is lifted out of `raw.harness` on decompile.
- `"slots"` — additional slots beyond the first (`slot[1:]`), i.e. the second
  cab of a dual-cab block.

`raw` is emitted by `decompile` and re-attached by `generate`. It is normally
authored only by the decompiler; hand-editing it is unnecessary for typical
tone specs. Stadium-only.

## Surgical edits

Once a preset exists, don't hand-edit the whole spec to change one setting —
use the edit verbs below. They mutate a spec in place and regenerate the
`.hsp`, reusing all of `generate.py`'s validation, model-id translation, and IR
injection.

**Mental model:** the spec is the source of truth. Every `generate` writes a
sidecar next to the `.hsp` (`MyTone.hsp` → `MyTone.spec.json`); an edit verb
loads that sidecar, applies the change, writes it back, and regenerates the
`.hsp`. Point an edit verb at an orphan `.hsp` (no sidecar — e.g. an old export
you never generated with helixgen) and it auto-decompiles first, so a sidecar
appears next to it before the edit is applied. You can also run `decompile`
directly to get a spec.json to inspect or hand-edit.

**Run `helixgen show-block "<block>"` first** to confirm the exact,
case-sensitive param name — the same guardrail `generate` already enforces.

- `helixgen set-param <preset> <block> <param> <value> [--path/--index/--lane/--pos]` — set one param on one block; `<value>` is auto-coerced (bool → int → float → string).
- `helixgen enable <preset> <block> [--snapshot NAME] [--path/--index/--lane/--pos]` — un-bypass a block at base level, or (with `--snapshot`) remove it from that snapshot's `disable` list.
- `helixgen disable <preset> <block> [--snapshot NAME] [--path/--index/--lane/--pos]` — bypass a block at base level, or (with `--snapshot`) add it to that snapshot's `disable` list.
- `helixgen add-block <preset> <block> [--path N] [--after NAME]` — insert a block (append to `--path`, default 0, or after a named block).
- `helixgen remove-block <preset> <block> [--path/--index/--lane/--pos]` — delete a block.
- `helixgen swap-model <preset> <old> <new> [--path/--index/--lane/--pos]` — replace a block with another of the **same category**; carries over params the target shares, warns on any it has to drop.
- `helixgen decompile <preset.hsp> -o spec.json` — reconstruct a spec.json from an `.hsp` (this is what runs automatically on an orphan edit; run it directly to inspect or hand-edit).

`--path`/`--index`/`--lane`/`--pos` disambiguate when a block name appears more
than once in the preset (e.g. dual-cab, both lanes of a split). `--snapshot`
applies only to `enable`/`disable`.

MCP tools mirror the CLI for agent-driven edits: `patch_preset(model, spec,
operations)` applies a list of `{op, ...}` operations to an in-memory spec
dict (`set_param`, `set_enabled`, `add_block`, `remove_block`, `swap_model`),
and `decompile_preset(model, hsp_b64)` turns a base64 `.hsp` blob into an
editable spec dict. The orphan-`.hsp` loop over MCP is: `decompile_preset` →
`patch_preset` → `generate_preset`.

### Worked examples

**Change a delay's Mix:**

```bash
helixgen show-block "Tape Echo Stereo"        # confirm the param is "Mix"
helixgen set-param MyTone.hsp "Tape Echo Stereo" Mix 0.3
# rewrites MyTone.spec.json and regenerates MyTone.hsp
```

MCP: `{"op": "set_param", "block": "Tape Echo Stereo", "param": "Mix", "value": 0.3}`

**Disable a block (kill the reverb):**

```bash
helixgen disable MyTone.hsp "Plate Stereo"
# add --snapshot Lead to bypass it only in the "Lead" snapshot
```

MCP: `{"op": "set_enabled", "block": "Plate Stereo", "enabled": false}`

**Swap an amp:**

```bash
helixgen list-blocks --category amp          # find the exact target display name
helixgen swap-model MyTone.hsp "Brit Plexi Brt" "Brit 2204"
# same-category only; carries over shared params, warns on any it had to drop
```

MCP: `{"op": "swap_model", "old": "Brit Plexi Brt", "new": "Brit 2204"}`
(surface any returned `warnings` to the user)

Disambiguate duplicate block names (e.g. two cabs across a split) with
`--pos`/`--lane`/`--path` on the CLI, or `"pos"`/`"lane"`/`"path"` on the MCP
op.

## Generation notes

- The chassis is whatever was first ingested. A Stadium chassis (`_helixgen_chassis_shape: "hsp"`) produces `.hsp` output; a `.hlx` chassis produces `.hlx`. Carryover `meta.color` / `meta.info` / `device_id` from the originating export is currently expected.
- Some Stadium model IDs are translated on ingest (e.g. `HD2_DistScream808Mono` → `HD2_DrvScream808`); generate translates back when writing `.hsp`.
- If the param validator fails with a list of valid names, run `show-block` and correct the spec — don't guess.

## Project layout

- `src/helixgen/` — `cli`, `ingest`, `hsp`, `chassis`, `library`, `spec`, `generate`, `bootstrap`, `ir`
- `tests/` — pytest suite (286 tests, run with `pytest`)
- `tests/fixtures/` — synthetic + real-export fixtures
- `data/` (gitignored) — the user's personal `.hsp` exports
- `docs/superpowers/plans/` — implementation plan history

## Development conventions

- TDD throughout: failing test first, then minimal implementation. See existing test files for the established pattern.
- Pure stdlib + `click` for the CLI; no other runtime deps.
- Real-export fixtures live in `tests/fixtures/presets/` and are loaded by tests under skip-if-not-present guards so the suite stays green on a clean clone.

## Releasing (automated — do NOT move `stable` or push tags by hand)

Releases are published by `.github/workflows/release.yml`, which fires when
`.claude-plugin/plugin.json` or `.claude-plugin/marketplace.json` changes on
`main`. The plugin is installed from the GitHub **`stable` branch**, so merging
to `main` does NOT ship a release — only the version bump + workflow does.

To cut a release:

1. Bump the version in **both** `.claude-plugin/plugin.json` and
   `.claude-plugin/marketplace.json` (the workflow fails the build if they
   disagree). Conventionally also bump the lib version in `pyproject.toml` and
   `src/helixgen/__init__.py` (separate `0.1.x` line; feeds preset `meta`).
2. Commit `release X.Y.Z — …`, open a PR, merge to `main`.
3. The workflow then auto-creates the annotated tag `helixgen--vX.Y.Z` and
   fast-forwards `stable` to that commit. It is idempotent (no-op if the tag
   exists) and refuses to force-push if `stable` diverged.

Do **not** manually `git branch -f stable …`, push `stable`, or push a
`helixgen--v*` tag — the workflow owns those refs. The release is live once the
workflow has run; users then get it via `/plugin` update.

The plugin's MCP server loads its **bundled** `helixgen` + `mcp_server` from
`${CLAUDE_PLUGIN_ROOT}` (set via `PYTHONPATH` in `.mcp.json`), not a global
`pip install`. Only the `mcp` SDK + `click` must exist in the environment.
