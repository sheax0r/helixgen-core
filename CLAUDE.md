# helixgen

CLI that generates Line 6 Helix Stadium `.hsp` presets (and legacy `.hlx`) from
JSON tone specs. The library lives at `~/.helixgen/library/` (override with
`$HELIXGEN_LIBRARY`) and is built by ingesting real device exports.

User IRs (impulse responses) registered with `helixgen register-irs` live at
`~/.helixgen/irs/` by default (override with `$HELIXGEN_IRS`). The mapping
file `mapping.json` records `irhash → wav-path`. See `helixgen list-irs`.

**The project backlog lives at `docs/BACKLOG.md`** — check it before starting
new work (its "corrected mental models" preamble first); deferred work and
punted review findings get a numbered entry there, not a TODO comment.

## CLI

- `helixgen list-blocks [--category amp|cab|drive|delay|reverb|modulation|filter|eq|dynamics|pitch|volume|send]` — list blocks, optionally filtered.
- `helixgen show-block "<name>"` — print a block's exact param names, types, defaults, and observed ranges. **Run this before writing a spec** — param names are case-sensitive and the generator rejects unknown ones.
- `helixgen generate <recipe.json> -o <out.hsp>` — author a preset from a transient recipe (no sidecar is written). The `-o` flag is required. Output extension `.hsp` writes a Stadium-format file (8-byte magic + compact JSON); `.hlx` writes pretty JSON for the original Helix.
- `helixgen view <preset.hsp> [-o recipe.json]` — read-only projection of a `.hsp` back into the recipe shape (replaces the old `decompile`; `-o` dump is non-authoritative).
- `helixgen ingest <path>` — ingest a `.hsp`/`.hlx`/`.json` file or recurse a directory; first encountered file sets the chassis.
- `helixgen register-irs <preset.hsp> <wav1> <wav2> ...` — bind each unknown `irhash` in the preset (path-then-position order) to the corresponding wav arg. Use `--force` to overwrite existing mappings.
- `helixgen register-irs <wav1> <wav2> ...` — compute each WAV's Stadium hash directly (no device export needed) and register. Requires libsndfile (`brew install libsndfile` on macOS). Only 48 kHz sources supported; non-48 kHz raises an error suggesting `sox`. This 48 kHz limit is a **helixgen** input constraint (it does not resample) — the **device** itself accepts any sample rate and normalizes internally, so a non-48k IR still works once imported onto the hardware; you just can't hash it off-device with helixgen without resampling first. Stereo WAVs are reduced to the left channel (matches Stadium's import).
- `helixgen ir-scan <dir>... [--rescan] [--remove <basename>]` — recursively walk one or more directories for `*.wav`, compute each Stadium hash, and cache. A WAV is skipped only when it is already registered **and** its cached hash is still valid for the file on disk (matching mtime + size), so an edited or replaced WAV is detected and re-hashed; `--rescan` recomputes unconditionally. Per-file failures (non-48 kHz, libsndfile errors) print a stderr warning and the scan continues. `--remove <basename>` forgets a single entry. Use this to bulk-register a whole IR library at once; use `register-irs` for one-off binding from a preset.
- `helixgen list-irs` — print `<hash>  <wav-path>` for every registered IR.
- `helixgen ir-cache --stats | --clear | --prune` — inspect/maintain the IR-hash **cache** (a pure-local perf layer that memoizes expensive Stadium-hash computes, keyed by absolute path + mtime + size; **not** `mapping.json`). `--stats` prints entry count, path, and size; `--clear` deletes the cache file; `--prune` drops entries whose backing WAV is gone. Default location `~/.helixgen/cache/irhash.json` (override with `$HELIXGEN_IRHASH_CACHE`, or `$HELIXGEN_CACHE` for the cache dir). All IR-hashing paths (`register-irs`, `ir-scan`, MCP IR tools) share it transparently.

Example: `helixgen ir-scan ~/IRs && helixgen list-irs | wc -l`.

### `helixgen device` — network control of a Helix Stadium (2.0+)

Talks to a **Stadium** over the LAN directly (OSC-over-ZeroMQ; no editor app).
Requires the `device` extra (`pip install 'helixgen[device]'` → pyzmq+msgpack).
Point at the device with `--ip`/`--port` or `$HELIXGEN_HELIX_IP` (default
`192.168.4.84`). **Stadium-only**; these verbs **mutate the device** — prefer an
empty/expendable slot when testing.

**The full per-verb reference — every flag, gotcha, and MCP-mirror name — lives
in [`docs/CLI.md`](docs/CLI.md) "Device commands".** The rest of this section is
the verb index plus the mental-model rules that must stay in front of an agent.

- **Preset + edit buffer:** `device list` / `setlists` / `info` / `read` /
  `load` / `create` / `save` / `rename` / `delete` / `set-param` / `blocks` /
  `pull` / `push` / `restore` / `backup` / `local-list` / `watch` / `set-info` /
  `install`. `install` transcodes a helixgen `.hsp` straight into device content
  (`_sbepgsm`) — no template, full fidelity (dual-amp, parallel splits,
  snapshots, footswitch/EXP assignments all synthesized); `--auto-irs` uploads
  referenced IRs (EXPERIMENTAL).
- **Live device ops (mutate the ACTIVE tone):** `device snapshot <index>`
  (recall a snapshot), `device bypass <path> <block> <on|off>` (volatile block
  bypass), `device model <path> <block> <model>` (live model swap), `device
  reorder <setlist> <target> --to <N>` (direct DEVICE-side preset reorder —
  distinct from the local-manifest `device slots reorder`; numeric args are
  **cid-first**), `device tuner` / `device meters` (read-only 2003 telemetry).
  Decoded + HW-validated 2026-07-14.
- **Global Settings + Global EQ:** `device settings list|get|set` (161 `global.*`
  keys; enum labels validated) and `device globaleq list|set <output> <band>
  <param> <value>` (three per-output-layer 7-band EQs; **write-only** — no
  network read-back).
- **IRs on the device:** `device list-irs` (read-only; the device's user IRs —
  distinct from the local `helixgen list-irs`), `device push-ir` (instant import
  under helixgen's exact `irhash`), `device pull-ir` (EXPERIMENTAL), `device
  delete-ir`, `device rename-ir`, `device ir-prune` (delete unreferenced IRs;
  dry-run by default, two independent consents `--force` / `--ignore-warnings`).
- **Setlists + sync:** `device setlist create|rename|delete|duplicate`
  (device-side; never orphan pool presets), `device setlist
  list|add|remove|create-local` (local manifest membership), `device setlist
  import-hss` (EXPERIMENTAL `.hss` bundle import, READ side), `device sync
  <setlist>` / `device sync --all [--gc]` (pool-first, reference-rebuilding,
  IR-uploading, idempotent; **not** a destructive mirror). `--repush` (either
  form) forces a content re-push of every in-scope tone already in the pool
  even when its `.hsp` hash is unchanged — use once after a helixgen
  transcoder upgrade, since hash-based change detection can't see a
  transcoder-output change on its own (backlog #25 residual).
- **Tone library / slots:** `helixgen register`, `device add` / `unsync` /
  `library` / `slots [list|restore|reorder] [--verify]`, `device setlist
  sync-on|sync-off`.

**Device-write gating.** Verbs that only read or list device state are safe —
e.g. `info`, `read`, `list`, `list-irs`, `blocks`, `settings list`/`get`,
`tuner`, `meters`, `watch`, `backup`, `pull`/`pull-ir`, plus the offline verbs
(`local-list`, `library`, `slots list`, `globaleq list`, `--list`/`--dry-run`
variants). Anything that writes content, properties, or files **mutates the
device** — the live-ops verbs change the ACTIVE tone immediately. When unsure,
check the verb's entry in [`docs/CLI.md`](docs/CLI.md). Prefer an
empty/expendable slot when testing.

**The Stadium's network stack is flaky — if a sync/verb drops or stalls, just
re-run it (the mutating paths are idempotent + auto-reconnecting); if it keeps
dropping, reboot the Helix.**

**The tone library is the single management record.** Every tone helixgen
generates auto-registers into the manifest `~/.helixgen/setlists.json` (override
`$HELIXGEN_SETLISTS`). A **tone** = content + identity + management state; its
desired **user slot** (`null` = off device, `"auto"` = wants device, or
`"1A".."8D"`) plus its **setlist memberships**. **"On the device" ⟺ the tone has
a slot.** There is no separate slot ledger. Presets are addressed by integer
**CID**; a preset lives once in the **pool** (`-2`) and is referenced by
**setlists** under the setlists root `-5`. **Sync is a managed-set mirror** —
it installs/updates/reorders/deletes only the tones helixgen manages and
**never touches untracked device presets**.

**Pushing tones to the device is driven by the `device` skill**
(`.claude/skills/device/`), which runs after `tone` has authored the `.hsp` and
centers on `device sync <setlist>` / `device_sync_setlist`. Read it before
scripting a setlist sync. Design + protocol refs:
[`docs/CLI.md`](docs/CLI.md), `docs/helix-protocol.md`, and
`docs/superpowers/specs/2026-07-13-tone-library-model-redesign.md`.

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

## Architecture: `.hsp` is the source of truth

A `.hsp` file is the 8-byte magic `rpshnosj` followed by a JSON document — it
**is** the canonical, editable artifact. There is no persisted intermediary
spec and **no `.spec.json` sidecar**. Two flows act on it:

- **Author** a new preset by feeding a transient **recipe** (the JSON shape
  below) to `generate`; helixgen clones the chassis template and replays the
  recipe as in-place mutations. The recipe is input-only — it is not written to
  disk and is never read back as truth.
- **Edit** an existing `.hsp` with the surgical verbs (`set-param`, `enable`,
  `add-block`, …); each reads the `.hsp`, mutates its body in place, and writes
  the `.hsp` back. No recompile, no sidecar.

To read a `.hsp` back into the recipe shape (for inspection or hand-authoring a
similar preset), use `helixgen view <preset.hsp>` — a read-only projection.

## recipe shape (author input to `generate`)

The **recipe** is the JSON author-input to `generate` / `generate_preset`. It is
input-only — never written to disk, never read back as truth. The base shape:

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

- `paths` is 1–2 entries (each maps to one DSP).
- `block` matches the display_name from `list-blocks` — case-sensitive. If ambiguous, use the model_id in brackets (e.g. "HD2_AmpBritPlexiBrt").
- `params` values are floats 0.0–1.0 for most knobs; some are ints/bools/Hz. Verify ranges with `show-block`.

**The exhaustive per-field reference — every optional section with its full
schema, defaults, ranges, and examples — lives in
[`docs/recipe-reference.md`](docs/recipe-reference.md).** The optional top-level
/ per-path fields, one line each:

- **`input`** (per path) — jack routing (`inst1`/`inst2`/`both`/`none`) plus the Input-block params (impedance ladder, pad, trim, gate, StereoLink).
- **`output`** (per path) — output block `level` (dB) + `pan`.
- **`split`/`join`** (in `blocks`) — parallel splits: split `type` (`y`/`ab`/`crossover`/`dynamic`) + merge-mixer wire params (`"A Level"`, `"B Pan"`, master `"Level"` — default **+3 dB**, write `0.0` for unity).
- **`snapshots`** (top-level, ≤8) — named scenes: per-scene `disable` + `params` deltas; snapshot 0 active on load.
- **`footswitches`** (top-level) — assign blocks/params to `FS1`–`FS5`/`FS7`–`FS11`/`EXP1Toe` (FS6/FS12 reserved); merge switches, param toggles, scribble `label`/`color`, response `curve`.
- **`expression`** (top-level) — sweep params with `EXP1`/`EXP2`; per-target `min`/`max` (reverse sweep supported).
- **`midi`** (top-level, EXPERIMENTAL #33) — bind MIDI CC# to param sweeps / bypass toggles. CC-only; realized on `device install`/`sync`.
- **`commands`** (top-level, EXPERIMENTAL #16) — Command Center: a footswitch / `Instant` slot **sends** a MIDI PC/CC/Note/MMC message or a Preset/Snapshot action.
- **`ir`** (per IR block) — load a registered user IR by wav basename or 32-hex hash.
- **`trails`** (per delay/reverb/FX-loop block) — bool: whether the wet tail rings out on bypass / snapshot switch.
- **`raw`** (per block) — verbatim unmodeled bNN state (`harness`, extra `slots`); emitted by `view`, consumed by `generate`. Editing an existing `.hsp` never needs it.

**One-controller-per-param.** A `(block, param)` is driven by at most one of
footswitch-param / expression / MIDI across the whole spec (a block's *bypass*
may have several sources).

**Controller vocabulary & English rendering (agent behavior).** When reporting a
tone to a human, render controllers in English (via
`controllers.english_for_controller` / the `controller_mapping` MCP tool), never
a bare `FS#` (e.g. `Footswitch 5 (top row, 5th from left)`). When a human
*describes* a control in plain language, translate it to a canonical identifier
with a dedicated small-model sub-agent fed `controller_mapping(stadium_xl)` — it
returns exactly one identifier (or `AMBIGUOUS`/`NONE`); validate it against the
canonical set before writing it into a recipe. `view` never drops controls it
can't map — it keeps them under a top-level `unknown_controllers` list
(round-trip safe). Full detail in [`docs/recipe-reference.md`](docs/recipe-reference.md).

All recipe fields are **Stadium-only** unless noted; the legacy `.hlx` chassis
ignores the Stadium-specific ones (with or without a warning per field — see the
reference).

## User preferences (`preferences.json`)

The `setup` / `tone` skills read explicit settings from a user-editable JSON
file — `~/.helixgen/preferences.json` (override the whole-file location with
`$HELIXGEN_PREFS`; override any single key with `HELIXGEN_<KEY>`, e.g.
`HELIXGEN_FAVOR_IRS=1`). Loaded by `src/helixgen/preferences.py`; per-key
precedence is env var > file value > built-in default. Keys include
`device.model`, `favor_irs`, `reveal_in_finder`, `guard_paid_irs_in_git`,
`preset_output_dir`, `author`, `default_guitar`, `instruments`, and
`git_commit_tones` (default `"auto"` — the skills git-commit changed tone/IR
artifacts when the target directory is git-managed; see the skill files).

- **`default_guitar`** (string, default `null`) — which of the user's
  `instruments` to default to when a tone request doesn't name a guitar. Env
  override `HELIXGEN_DEFAULT_GUITAR`. When unset and the `tone` skill needs a
  guitar, the skill asks the user and offers to save the answer here.

**Preset naming convention.** Generated presets are named for the guitar they
target — the target guitar is appended to the preset **display title**, the
`.hsp`/`.md` **filename** slug, and stated near the top of the companion
markdown **description** (format `"<Tone Name> — <Guitar>"`, e.g. `White Limo
Lead — Les Paul Jr`). The guitar is omitted only when a tone is explicitly not
targeted at a specific guitar (a guitar-agnostic/generic patch). Guitar
resolution order in the `tone` skill: a user-named guitar wins; else
`default_guitar`; else the skill asks and offers to save the choice as
`default_guitar`.

## Surgical edits

Once a preset exists, don't re-author it to change one setting — use the edit
verbs below. Each reads the `.hsp`, mutates its body **in place**, and writes
the `.hsp` back, reusing all of helixgen's validation, model-id translation,
and IR injection. Works on ANY `.hsp` — one helixgen authored or a raw device
export — with no decompile step and no sidecar.

**Mental model:** the `.hsp` is the source of truth. An edit verb loads it,
applies one change to the verbose device-native JSON, and saves it. Fields
helixgen doesn't model (dual-cab slots, harness, `xyctrl`, …) are preserved
untouched by construction.

**Run `helixgen show-block "<block>"` first** to confirm the exact,
case-sensitive param name — the same guardrail `generate` already enforces.

- `helixgen set-param <preset> <block> <param> <value> [--path/--lane/--pos]` — set one param on one block; `<value>` is auto-coerced (bool → int → float → string). A **negative** value needs the `--` sentinel after any flags (`helixgen set-param t.hsp output level -- -3`). The block names `input` / `output` / `split` / `join` (`merge` = alias) are **signal-flow pseudo-blocks** addressing the path's endpoints / split / merge mixer (`--path` picks the DSP; `--pos` disambiguates two splits; `--lane` does not apply): input params use the recipe vocabulary (`impedance`, `pad`, `trim`, `gate`, `threshold`, `decay`, `link`), output params are `level`/`pan`, split/join params are the wire names (`BalanceA`, `Frequency`, `"A Level"`, …).
- `helixgen enable <preset> <block> [--snapshot NAME] [--path/--lane/--pos]` — un-bypass a block at base level, or (with `--snapshot`) enable it in that snapshot.
- `helixgen disable <preset> <block> [--snapshot NAME] [--path/--lane/--pos]` — bypass a block at base level, or (with `--snapshot`) bypass it in that snapshot.
- `helixgen add-block <preset> <block> [--path N] [--after NAME]` — insert a block (append to `--path`, default 0, or after a named block).
- `helixgen remove-block <preset> <block> [--path/--lane/--pos]` — delete a block.
- `helixgen swap-model <preset> <old> <new> [--path/--lane/--pos]` — replace a block with another of the **same category**; carries over params the target shares, warns on any it has to drop.
- `helixgen view <preset.hsp> [-o recipe.json]` — read-only projection of a `.hsp` into the recipe shape (replaces `decompile`; the dump is non-authoritative).

`--path`/`--lane`/`--pos` disambiguate when a block name appears more than once
in the preset (e.g. dual-cab, both lanes of a split). (`--index` was removed in
1.0.0 — block addressing is `(path, lane, pos)`.) `--snapshot` applies only to
`enable`/`disable`.

MCP tools mirror the CLI for agent-driven edits, operating on `.hsp` **file
paths** (no base64 — the bytes never round-trip through agent context):
`generate_preset(model, recipe, out_path)` authors a `.hsp` from a recipe,
writes it to `out_path`, and returns `{path, warnings}`; `patch_preset(model,
hsp_path, operations)` applies a list of `{op, ...}` operations (`set_param`,
`set_enabled`, `add_block`, `remove_block`, `swap_model`) to the file **in
place** and returns `{path, warnings}`; `view_preset(model, hsp_path)` returns
the read-only recipe-shape projection. The agent edit loop is just a single
`patch_preset` call on the file — no decompile/regenerate round-trip.

### Worked examples

**Change a delay's Mix:**

```bash
helixgen show-block "Tape Echo Stereo"        # confirm the param is "Mix"
helixgen set-param MyTone.hsp "Tape Echo Stereo" Mix 0.3
# mutates MyTone.hsp in place (no sidecar)
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
- If the param validator fails with a list of valid names, run `show-block` and correct the recipe — don't guess.

## Project layout

- `src/helixgen/` — `cli`, `ingest`, `hsp`, `chassis`, `library`, `spec` (recipe parser/validator), `mutate` (in-place `.hsp` edit verbs), `recipe` (author `.hsp` from a recipe), `view` (read-only `.hsp` → recipe projection), `generate` (shared low-level `.hsp` builders + legacy `.hlx`), `controllers`, `preferences`, `bootstrap`, `ir`, `irhash_cache`
- `src/helixgen/device/` — network device control (OSC-over-ZeroMQ client, `transcode`, `modelmap`, `defs`, setlist manifest)
- `mcp_server/` — the MCP server the plugin bundles; tool descriptions here are agent-facing behavioral contracts
- `.claude/skills/` — the three plugin skills: `setup` (device/prefs onboarding), `tone` (author a `.hsp` from a tone request), `device` (push/sync authored tones onto the hardware)
- `.claude-plugin/` — `plugin.json` + `marketplace.json`; bumping the version here on `main` is what triggers a release (see Releasing)
- `docs/` — `BACKLOG.md` (THE backlog), `CLI.md` (the full CLI + per-verb **device** reference), `recipe-reference.md` (the exhaustive recipe field reference), `superpowers/specs/` (design docs + review findings), `superpowers/plans/` (implementation plans), `features/` (per-feature deep dives), protocol references (`helix-protocol.md`, `helix-format-reference.md`, `helix-sftp-access.md`, `ir-hash-algorithm.md`)
- `tests/` — pytest suite (run with `PYTHONPATH=$PWD/src python -m pytest`); the golden-output contract (`tests/golden/`) and the 211-export real-device round-trip (`tests/test_decompile_acceptance.py`) pin `.hsp` fidelity
- `tests/fixtures/` — synthetic + real-export fixtures
- `data/` (gitignored) — the user's personal `.hsp` exports
- `irs/` (gitignored) — paid commercial IR packs; character catalog at `irs/_catalog/`

## Development workflow

- **Worktrees, branched from fresh `github/main`.** All non-trivial work happens
  in a git worktree whose branch starts from freshly-fetched `github/main` (the
  GitHub remote is named **`github`**, not `origin`) — never commit directly on
  local `main`; it may be stale. Fetch again before picking a release version
  number (a concurrent PR once released 2.10.0 mid-flight and collided with an
  in-progress bump).
- **Adversarial review before shipping.** Before merging a PR, dispatch at least
  one independent review subagent prompted to *break* the change (find bugs,
  regressions, spec violations — not summarize it). Confirmed findings are fixed
  or explicitly deferred to `docs/BACKLOG.md`. Major changes also get a committed
  review doc in `docs/superpowers/specs/` (see the PR #31 review for the shape).
- **Agent-facing surfaces ship in sync.** Any change to CLI-, MCP-, or
  skill-visible behavior updates, in the same PR, every surface that describes
  it: `.claude/skills/*`, this CLAUDE.md, the MCP tool descriptions in
  `mcp_server/`, and `docs/CLI.md`. Drift between code and these surfaces is a
  bug, not a docs chore.
- **Backlog discipline.** `docs/BACKLOG.md` is the single project backlog.
  Deferred work gets a numbered entry there — not a TODO comment, not a
  side file.
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
