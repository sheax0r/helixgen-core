# helixgen-core

Core library + CLI that generates Line 6 Helix Stadium `.hsp` presets (and
legacy `.hlx`) from JSON tone specs, and controls a Stadium over the LAN. The
block library lives at `~/.helixgen/library/` (override with
`$HELIXGEN_LIBRARY`) and is built by ingesting real device exports.

**Repo family (all under `sheax0r`):** this repo (`helixgen-core`) is the
Python package `helixgen` — libs and the CLI (the **CLI is the only engine
surface**; the MCP server was removed in 0.20.0 — the per-verb `--help` text
is the agent-facing behavioral contract, pinned by `tests/test_cli_parity.py`);
[`helixgen`](https://github.com/sheax0r/helixgen) is the Claude Code
plugin/marketplace repo carrying the `setup`/`tone`/`device` skills;
[`helixgen-tui`](https://github.com/sheax0r/helixgen-tui) is the terminal UI.
The plugin and TUI consume this repo as a PyPI dependency (package name
`helixgen`, published to PyPI since 0.19.1).

**How this file works:** it carries the mental models and behavioral rules
that must stay in front of an agent, plus concise verb indexes. Reference
detail lives one pointer away and is authoritative there:
[`docs/CLI.md`](docs/CLI.md) (every verb, flag, and gotcha),
[`docs/recipe-reference.md`](docs/recipe-reference.md) (every recipe field),
and each verb's `--help` (the pinned contract). Read the pointed-at doc
before scripting against a verb you haven't used in this session.

**The project backlog lives at `docs/BACKLOG.md`** — check it before starting
new work (its "corrected mental models" preamble first); deferred work and
punted review findings get a numbered entry there, not a TODO comment.

## Home directory and git plumbing (`~/.helixgen`)

The artifact library carries three artifact kinds: **tones**
(`library/tones/<logical-slug>.json` + per-variant `.hsp`), **guitar
profiles** (`library/guitars/<slug>.json`), and **per-IR metadata**
(`library/irs/<pack>/<name>.json` sidecars next to copied WAVs — the WAV
bytes stay gitignored — plus `library/irs/mapping.json`, the `irhash →
wav-path` registry). Design:
`docs/superpowers/specs/2026-07-15-library-metadata-design.md`.

- **`$HELIXGEN_HOME`** (`src/helixgen/home.py`) is the root of everything
  helixgen persists — default `~/.helixgen`. Per-area overrides
  (`$HELIXGEN_LIBRARY`, `$HELIXGEN_IRS`, `$HELIXGEN_SETLISTS`,
  `$HELIXGEN_PREFS`, `$HELIXGEN_CACHE`, `$HELIXGEN_LOCKS`) keep working and
  always win over a `$HELIXGEN_HOME`-derived default.
- **The home auto-`git init`s on first write** (`src/helixgen/libinit.py` +
  `gitops.py`) whenever `git` is on PATH — unconditional, not
  preference-gated (its `.gitignore` excludes `devices/`, `cache/`,
  `tone3000/`, `*.bak*`, and IR audio). Library-mutating operations
  **auto-commit** afterward, gated by the `git_commit_tones` preference
  (default `"auto"`). All of it is advisory: a missing git binary or a
  failed commit warns to stderr and never fails the triggering operation.
- **The manifest lives at `~/.helixgen/setlists/manifest.json`** (override
  `$HELIXGEN_SETLISTS`) — manifest v3, **intent-only** (see "The tone
  library" below). A legacy `~/.helixgen/setlists.json` (v1/v2)
  auto-migrates up on first load (backup written first, legacy file renamed
  so re-runs never re-migrate). The first `device sync` after a v2→v3
  migration re-pushes every managed tone once — harmless and idempotent
  (the device's serial hasn't observed anything under its own file yet).
- **Per-device observed state lives in `~/.helixgen/devices/<serial>.json`**
  (`src/helixgen/device/observations.py`) — observed placement (`cid`/
  `posi`) plus, since 0.24.0, the device's **discovered address record**
  (`ip`, `model`, `firmware`). NOT the manifest and NOT committed
  (`devices/` is gitignored): placement is rebuilt wholesale by every
  `device sync`, so losing the file costs one re-`discover`.

## CLI (core verbs)

**Full per-verb reference: [`docs/CLI.md`](docs/CLI.md) "Commands" and "IR
commands".** Verb index: `list-blocks`, `show-block`, `generate`, `view`,
`ingest`, `bootstrap`, `register-irs`, `irhash`, `ir-scan`, `list-irs`,
`ir-cache`, `analyze-audio`, `controllers`, the surgical edit verbs +
`patch` ("Surgical edits" below), `describe` + `library …` and `device …`
(their own sections below).

Rules that must stay in front of you:

- **Run `helixgen show-block "<name>"` before writing or editing a spec** —
  param names are case-sensitive and the generator rejects unknown ones. If
  the validator fails with a list of valid names, run `show-block` and
  correct — don't guess.
- Verbs whose output agents consume take **`--json`** for machine-readable
  stdout; `view` prints JSON by default.
- `generate` with no `-o` writes into the tone library and authors tone
  metadata — name via `--artist`/`--song` (paired) or `--descriptor`
  (mutually exclusive), plus optional `--guitar`. An explicit `-o <out.hsp>`
  is the legacy path: writes there, auto-registers, naming flags ignored,
  **no metadata JSON**. Extension picks the format (`.hsp` Stadium, `.hlx`
  legacy Helix).
- IR registration (`register-irs`, `ir-scan`) **copies WAVs into
  `library/irs/<pack>/` with metadata sidecars** by default (`--no-copy`
  opts out). Direct hashing needs libsndfile and **48 kHz sources** — a
  helixgen input constraint, not the device's (the device normalizes any
  rate on its own import, so a non-48k IR still works once imported onto
  the hardware).

### `helixgen device` — network control of a Helix Stadium

Talks to a **Stadium** over the LAN directly (OSC-over-ZeroMQ; no editor
app; needs `pip install 'helixgen[device]'`). Run **`helixgen device
discover`** once to find and persist the Stadium's address; every verb then
resolves the IP as `--ip` > `$HELIXGEN_HELIX_IP` > the persisted record —
**no built-in default**; with none set, verbs fail fast pointing at
`device discover`. Discovery is used once; sessions stay direct-to-IP.
**Stadium-only.**

**The full per-verb reference — every flag and gotcha — lives in
[`docs/CLI.md`](docs/CLI.md) "Device commands".** The verb index:

- **Preset + edit buffer:** `list` / `setlists` / `info` / `active` (the
  ACTIVE preset — save/restore the player's selection) / `read` / `load` /
  `create` / `save` / `rename` / `delete` / `set-param` / `blocks` /
  `params` (numeric pids + CURRENT raw values — run it before `set-param`;
  block coordinates are DSP **grid slots**, 0-27) / `pull` / `push` /
  `restore` / `backup` / `local-list` / `watch` / `set-info` / `install`
  (transcodes a helixgen `.hsp` straight into device content — no template,
  full fidelity). `--setlist` takes `user` (the pool, default), `factory`,
  or a real device setlist name (its entries are references to pool
  presets).
- **Live ops (mutate the ACTIVE tone):** `snapshot` / `bypass` / `model` /
  `reorder` (direct DEVICE-side reorder — distinct from the local-manifest
  `slots reorder`; numeric args are **cid-first**) / `tuner` / `meters` /
  `measure` (read-only 2003 telemetry) / `normalize` (level-matching loop
  over `measure`: DRY-RUN by default, `--yes` writes dB trims into the
  **local `.hsp` only** — the device follows via `sync` — and records
  telemetry on library variants; holds `editbuffer` even in dry-run).
- **Global Settings + Global EQ:** `settings list|get|set`, `globaleq
  list|set` (**write-only** — no network read-back).
- **IRs on the device:** `list-irs` (distinct from the local `helixgen
  list-irs`) / `push-ir` / `pull-ir` / `delete-ir` / `rename-ir` /
  `ir-prune` (dry-run by default).
- **Setlists + sync:** `setlist create|rename|delete|duplicate`
  (device-side; never orphan pool presets), `setlist
  list|add|remove|create-local` (local manifest membership), `setlist
  import-hss` / `export-hss` (EXPERIMENTAL), `sync <setlist>` / `sync
  --all [--gc]`. `--repush` forces a content re-push of unchanged tones —
  use once after a transcoder upgrade (hash-based change detection can't
  see a transcoder-output change).
- **Tone library / slots:** `helixgen register`, `device add` / `unsync` /
  `library` / `slots [list|restore|reorder] [--verify]`, `device setlist
  sync-on|sync-off`.

**Device-write awareness.** Verbs that only read or list device state are
safe — e.g. `info`, `active`, `read`, `list`, `list-irs`, `blocks`,
`params`, `settings list`/`get`, `tuner`, `meters`, `measure`, `watch`,
`backup`, `pull`/`pull-ir`, plus the offline verbs (`local-list`,
`library`, `slots list`, `globaleq list`, `--list`/`--dry-run` variants).
Anything that writes content, properties, or files **mutates the device** —
the live-ops verbs change the ACTIVE tone immediately. When unsure, check
the verb's entry in [`docs/CLI.md`](docs/CLI.md). Practical posture for
device writes: prefer an empty/expendable slot when testing, take an
upfront `device backup`, tear down test artifacts afterwards, and expect
the #38 /CreateContent flakiness (re-run; slot-writing verbs fail safe on
an occupied slot).

**Machine-local advisory device locks.** Every device-mutating verb
auto-acquires a lease file (`~/.helixgen/locks/<ip>/<scope>.lock`) for its
duration, so concurrent helixgen processes on this machine never collide on
the device; read-only verbs take nothing. Scopes: `editbuffer`, `library`,
`irs`, `globals`, `all`. Hold scopes across calls with `device lock
--scope all --label <who>` (export the printed `HELIXGEN_LOCK_TOKEN` so
your own verbs pass through; same-shell calls pass through automatically);
inspect with `device lock --status`, release with `device unlock`.
Contended verbs wait `$HELIXGEN_LOCK_TIMEOUT` s (default 30) then error
naming the holder; stale leases are reclaimed, live ones never. Run
`device lock` from a long-lived shell, not a wrapper script (session
leases get only a 120 s dead-pid grace). `--no-lock` opts out (dangerous).
Advisory + machine-local only — other hosts and the Stadium desktop editor
are NOT covered. Full verb → scope table: [`docs/CLI.md`](docs/CLI.md)
"Device locks".

**The Stadium's network stack is flaky — if a sync/verb drops or stalls,
re-run it: `sync` and the live-ops verbs are idempotent + auto-reconnecting;
the slot-writing verbs (install/save/push/create) fail safe on an occupied
slot instead; `setlist import-hss` is the one NOT-idempotent retry. If it
keeps dropping, reboot the Helix.**

**The tone library is the single management record.** Every tone helixgen
generates auto-registers into the manifest
(`~/.helixgen/setlists/manifest.json`). A **tone** = content + identity +
management **intent**: its desired **user slot** (`null` = off device,
`"auto"` = wants device, or `"1A".."128D"`) plus its **setlist
memberships**. **"On the device" ⟺ the tone has a slot.** There is no
separate slot ledger. Presets are addressed by integer **CID**; a preset
lives once in the **pool** (`-2`) and is referenced by **setlists** under
the setlists root `-5`. **Sync is a managed-set mirror** — it
installs/updates/reorders/deletes only the tones helixgen manages and
**never touches untracked device presets**. A specific Helix's **observed**
placement is not part of the manifest — see "Home directory" above.

**Pushing tones to the device is driven by the `device` skill** (in the
plugin repo, `sheax0r/helixgen`), which runs after `tone` has authored the
`.hsp` and centers on `device sync <setlist>`. Read it before scripting a
setlist sync. Design + protocol refs: [`docs/CLI.md`](docs/CLI.md),
`docs/helix-protocol.md`, and
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

The **recipe** is the JSON author-input to `generate`. It is
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
[`docs/recipe-reference.md`](docs/recipe-reference.md).** The optional
sections, by name: per-path `input` (jack routing + Input-block params) and
`output` (level/pan); `split`/`join` in `blocks` (parallel splits +
merge-mixer wire params); top-level `snapshots` (≤8 named scenes: per-scene
`disable` + `params` deltas + per-snapshot `output` level/pan),
`footswitches` (FS1–FS5/FS7–FS11/EXP1Toe; FS6/FS12 reserved), `expression`
(EXP1/EXP2 sweeps), `midi` (EXPERIMENTAL #33), `commands` (Command Center;
EXPERIMENTAL #16); per-block `ir` (registered user IR by wav basename or
32-hex hash), `trails`, and `raw` (verbatim unmodeled state — emitted by
`view`, consumed by `generate`; editing an existing `.hsp` never needs it).
All recipe fields are **Stadium-only** unless the reference notes otherwise
(the legacy `.hlx` chassis ignores them).

**One-controller-per-param.** A `(block, param)` is driven by at most one of
footswitch-param / expression / MIDI across the whole spec (a block's *bypass*
may have several sources).

**Controller vocabulary & English rendering (agent behavior).** When
reporting a tone to a human, render controllers in English (via `helixgen
controllers`), never a bare `FS#` (e.g. `Footswitch 5 (top row, 5th from
left)`). When a human *describes* a control in plain language, translate it
to a canonical identifier with a dedicated small-model sub-agent fed
`helixgen controllers --json` — it returns exactly one identifier (or
`AMBIGUOUS`/`NONE`); validate it against the canonical set before writing it
into a recipe. `view` never drops controls it can't map — unmapped ones land
in `unknown_controllers` (round-trip safe). Full detail in
[`docs/recipe-reference.md`](docs/recipe-reference.md).

## User preferences (`preferences.json`)

The `setup` / `tone` skills (plugin repo) read explicit settings from
`~/.helixgen/preferences.json` (whole-file override `$HELIXGEN_PREFS`;
per-key override `HELIXGEN_<KEY>`, e.g. `HELIXGEN_FAVOR_IRS=1`). Loaded by
`src/helixgen/preferences.py`; precedence is env var > file value >
built-in default. Keys include `device.model`, `favor_irs`,
`reveal_in_finder`, `guard_paid_irs_in_git`, `author`, `git_commit_tones`
(default `"auto"`), and **`default_guitar`** — which guitar profile to use
when a tone request doesn't name one (when unset, the `tone` skill asks and
offers to save the answer). The keys `instruments` and `preset_output_dir`
are **deprecated** (replaced by guitar profiles and the `library/tones/`
default write location): still parsed for back-compat, warned about once
per process, and removed by `library migrate`.

## Tone naming and the library

**Naming schema (supersedes the old `"<Tone Name> — <Guitar>"` convention).**
A tone's display name is `"$Artist - $Song - $Guitar"`, or `"$Descriptor -
$Guitar"` when the tone has no artist/song (guitar = the target guitar's
short name). The guitar segment is omitted only for a tone that's explicitly
guitar-agnostic (generic patch). Filenames are the same schema, slugged
lowercase-with-dashes (e.g. `foo-fighters-white-limo-les-paul-jr.hsp`). Guitar
resolution order in the `tone` skill: a user-named guitar wins; else
`default_guitar`; else the skill asks and offers to save the choice as
`default_guitar`.

**Logical tone vs. variant.** A **logical tone** — one artist+song, or one
descriptor — owns exactly one metadata JSON at
`library/tones/<logical-slug>.json`, plus one or more **variants**, each a
real `.hsp` targeting a single guitar and keyed by that guitar's profile slug
(or `"generic"` for a guitar-agnostic variant). The manifest and the device
still key by the *variant's* display name — that's what a device preset is;
the metadata JSON just groups variants that share an identity. Creating a
new variant of an existing tone is `generate --guitar <other-guitar>`
against the same artist/song/descriptor. A tone's write-up lives in the
metadata's `description_md` (authored via `helixgen library doc` — **no
`.md` sidecar files**); per-variant notes live in that variant's `notes_md`.

## Guitar profiles

A **guitar profile** at `library/guitars/<slug>.json` (schema 1) is the
single source of truth for one guitar the user owns — it replaces
`preferences.instruments`. Fields: `name`, `short_name` (what appears in
preset display names / filename slugs), `type`, `active`, `pickups`,
`construction`, `character_md` (what the guitar is *for* — read by the
`tone` skill to adapt params), `genres[]`, and `controls[]` (the control
inventory a variant's `guitar_settings` keys validate against). A
`--guitar <label>` resolves by slug / name / short_name, case-insensitive,
most-specific tier first — ambiguity and unknown labels error; with **no**
profiles yet, a literal `slugify(label)` fallback keeps pre-migration
authoring working (full resolution rules in [`docs/CLI.md`](docs/CLI.md)
"Guitar profiles / resolution"). Profiles are seeded from
`preferences.instruments` by `library migrate`; scaffold a new one with
`helixgen library add-guitar` (also auto-commits); create/edit the details
via the `setup` skill.

## The `helixgen library` verb group

Verb group over the artifact library — tones, guitar profiles, and per-IR
metadata. Every library-mutating verb auto-commits the home repo afterward
(advisory, gated by `git_commit_tones`). **Full per-verb reference:
[`docs/CLI.md`](docs/CLI.md) "Library commands".** Verb index: `library
list` (all three sections; `--tones`/`--guitars`/`--irs` narrows), `library
show <name>` (tone-first resolution, then guitar profile), `describe
<tone>` (the longer human-oriented counterpart), `library doc` (author
`description_md` / a variant's `notes_md`), `library validate` (shape +
cross-link checks: **problems** exit 1, **warnings** don't), `library
add-guitar`, `library import` (bring an external `.hsp` under the naming
schema; never overwrites), `library migrate` (one-shot idempotent
pre-library → library migration; `--dry-run` first), `library ir-backfill`
(copy + scaffold metadata for IRs registered before the library layout).

## Surgical edits

Once a preset exists, don't re-author it to change one setting — use the
edit verbs. Each reads the `.hsp`, mutates its body **in place**, and writes
it back, reusing all of helixgen's validation, model-id translation, and IR
injection. Works on ANY `.hsp` — helixgen-authored or a raw device export —
with no decompile step and no sidecar. Fields helixgen doesn't model
(dual-cab slots, harness, `xyctrl`, …) are preserved untouched by
construction.

**Run `helixgen show-block "<block>"` first** to confirm the exact,
case-sensitive param name — the same guardrail `generate` already enforces.

The verbs — full signatures and per-flag detail in
[`docs/CLI.md`](docs/CLI.md) "Commands":

- `set-param <preset> <block> <param> <value>` — one param, auto-coerced. A
  **negative** value needs the `--` sentinel (`helixgen set-param t.hsp
  output level -- -3`). The block names `input` / `output` / `split` /
  `join` (`merge` alias) are **signal-flow pseudo-blocks** addressing the
  path's endpoints / split / merge mixer.
- `enable` / `disable <preset> <block>` — un-bypass / bypass at base level.
- `add-block`, `remove-block`, `swap-model` (same-category only; carries
  over shared params, warns on dropped ones — surface those warnings).
- `view <preset.hsp>` — read-only `.hsp` → recipe projection.

`--snapshot NAME-or-INDEX` on `set-param`/`enable`/`disable` targets ONE
snapshot's slot instead of the base. **Gotcha:** once a param's
per-snapshot array varies, the device applies it on every snapshot — a
later plain base edit of that param is inaudible on-device (`set-param`
warns). `--path`/`--lane`/`--pos` disambiguate duplicate block names —
block addressing is `(path, lane, pos)`; there is no `--index`.

For a multi-edit session, **`helixgen patch <preset.hsp> <ops.json|->`**
applies a JSON **list** of ops (`set_param`, `set_enabled`, `add_block`,
`remove_block`, `swap_model`) in one atomic invocation — an invalid op
anywhere leaves the `.hsp` untouched. Op fields mirror the single-op verbs'
flags. The agent edit loop is a single `patch` call on the file — no
decompile/regenerate round-trip:

```bash
echo '[{"op": "set_param", "block": "Tape Echo Stereo", "param": "Mix", "value": 0.3},
       {"op": "set_enabled", "block": "Plate Stereo", "enabled": false}]' \
  | helixgen patch MyTone.hsp -
```

## Generation notes

- The chassis is whatever was first ingested. A Stadium chassis (`_helixgen_chassis_shape: "hsp"`) produces `.hsp` output; a `.hlx` chassis produces `.hlx`. Carryover `meta.color` / `meta.info` / `device_id` from the originating export is currently expected.
- Some Stadium model IDs are translated on ingest (e.g. `HD2_DistScream808Mono` → `HD2_DrvScream808`); generate translates back when writing `.hsp`.

## Project layout

- `src/helixgen/` — `cli` (core verbs + entry point), `cli_device` (the `helixgen device` verb group, imported back into `cli`), `ingest`, `hsp`, `chassis`, `library`, `spec` (recipe parser/validator), `mutate` (in-place `.hsp` edit verbs), `recipe` (author `.hsp` from a recipe), `view` (read-only `.hsp` → recipe projection), `generate` (shared low-level `.hsp` builders + legacy `.hlx`), `controllers`, `preferences`, `bootstrap`, `ir`, `irhash_cache`, `locks` (machine-local advisory device locks), `home`/`libinit`/`gitops` (the `~/.helixgen` home root, its auto-init, and advisory auto-commit), `naming`, `tone_meta`, `guitars` (guitar profiles), `ir_meta` (per-IR sidecars), `migrate` (library migration), `cli_library` (the `helixgen library` verb group)
- `src/helixgen/device/` — network device control (OSC-over-ZeroMQ client, `transcode`, `modelmap`, `defs`, setlist manifest)
- `docs/` — `BACKLOG.md` (THE backlog), `CLI.md` (the full CLI + per-verb **device** reference), `recipe-reference.md` (the exhaustive recipe field reference), `superpowers/specs/` (design docs + review findings), `superpowers/plans/` (implementation plans), `features/` (per-feature deep dives), protocol references (`helix-protocol.md`, `helix-format-reference.md`, `helix-sftp-access.md`, `ir-hash-algorithm.md`)
- `tests/` — pytest suite (run with `PYTHONPATH=$PWD/src python -m pytest`); the golden-output contract (`tests/golden/`) and the 211-export real-device round-trip (`tests/test_decompile_acceptance.py`) pin `.hsp` fidelity
- `tests/live/` — **opt-in live integration suite** (backlog #66): drives the real CLI via subprocess against the real library and a real Stadium. Skipped unless `HELIXGEN_LIVE=1` (device tests also need the device reachable). Impact-area markers are registered in `pyproject.toml`; after a targeted change run its blast radius, e.g. `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python -m pytest -m "live and sync" tests/live`. Safety = fixtures (scratch env for ALL local state, upfront `device backup`, before/after device-state diff, `HGTEST`-prefixed artifacts with teardown, a session check that the real `~/.helixgen` is byte-identical afterwards); **`tests/live/conftest.py` documents the full safety model**, the deliberately excluded verbs, and the known-gotcha xfails.
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
- **Agent-facing surfaces ship in sync.** The CLI is the only engine surface;
  its per-verb `--help` text is the agent contract (pinned by
  `tests/test_cli_parity.py`). Any change to CLI-visible behavior updates, in
  the same PR, every surface in this repo that describes it: the verb's help
  text, this CLAUDE.md, and `docs/CLI.md`. Drift between code and these
  surfaces is a bug, not a docs chore. **Division of labor:** reference detail
  (flags, semantics, gotchas) belongs in `docs/CLI.md` /
  `docs/recipe-reference.md`; this CLAUDE.md carries the mental models,
  behavioral rules, and verb indexes — don't grow per-verb prose here when
  the reference doc is the right home. Behavior changes that skills describe
  also need a companion PR in the plugin repo (`sheax0r/helixgen`,
  `.claude/skills/*`) — land the two together and note the cross-repo pairing
  in both PR descriptions.
- **Backlog discipline.** `docs/BACKLOG.md` is the single project backlog.
  Deferred work gets a numbered entry there — not a TODO comment, not a
  side file.
- TDD throughout: failing test first, then minimal implementation. See existing test files for the established pattern.
- Pure stdlib + `click` for the CLI; no other runtime deps.
- Real-export fixtures live in `tests/fixtures/presets/` and are loaded by tests under skip-if-not-present guards so the suite stays green on a clean clone.

## Releasing

This repo releases the **`helixgen` PyPI package** (version in
`pyproject.toml` + `src/helixgen/__init__.py` — bump both together; the
version feeds generated presets' `meta`). Publishing is via the OIDC
trusted-publisher workflow (`.github/workflows/publish.yml`) on `vX.Y.Z`
tags pushed to `main` (first publish 0.19.1).

Plugin releases (the `stable` branch + `helixgen--vX.Y.Z` tags) live in the
**plugin repo** (`sheax0r/helixgen`) and are owned by its release workflow —
nothing in this repo moves those refs. When a core release changes behavior a
skill depends on, cut the core release first, then bump the plugin's pinned
`helixgen` version in its own PR.

## ralphex

Implementation tasks driven from the helix coordination workspace run via
[ralphex](https://github.com/umputun/ralphex) plan files in `docs/plans/`
(scaffold: `docs/plans/TEMPLATE.md`); completed plans move to
`docs/plans/completed/`. Config is the tracked `.ralphex/config`
(`default_branch` pinned to `main` because the remote is named `github`, so
ralphex can't auto-detect from `origin/HEAD`); runtime dirs
`.ralphex/worktrees/` + `.ralphex/progress/` are gitignored. The launcher
syncs local `main` from `github/main` before running. Review = ralphex's
built-in review pipeline (`external_review_tool = none`).
