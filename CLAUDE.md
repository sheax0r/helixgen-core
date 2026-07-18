# helixgen-core

Core library + CLI. Generates Line 6 Helix Stadium `.hsp` presets (plus legacy `.hlx`) from JSON tone specs, controls Stadium over LAN. Block library lives at `~/.helixgen/library/` (override `$HELIXGEN_LIBRARY`), built by ingesting real device exports.

**Repo family (all under `sheax0r`):** this repo (`helixgen-core`) = Python package `helixgen` — libs + CLI (**CLI is only engine surface**; MCP server removed 0.20.0 — per-verb `--help` text is agent-facing behavioral contract, pinned by `tests/test_cli_parity.py`); [`helixgen`](https://github.com/sheax0r/helixgen) = Claude Code plugin/marketplace repo carrying `setup`/`tone`/`device` skills; [`helixgen-tui`](https://github.com/sheax0r/helixgen-tui) = terminal UI. Plugin + TUI consume this repo as PyPI dependency (package name `helixgen`, on PyPI since 0.19.1).

**How this file works:** carries mental models + behavioral rules that must stay in front of agent, plus concise verb indexes. Reference detail lives one pointer away, authoritative there: [`docs/CLI.md`](docs/CLI.md) (every verb, flag, gotcha), [`docs/recipe-reference.md`](docs/recipe-reference.md) (every recipe field), each verb's `--help` (pinned contract). Read pointed-at doc before scripting against verb not used this session.

**Project backlog lives at `docs/BACKLOG.md`** — check before starting new work ("corrected mental models" preamble first); deferred work + punted review findings get numbered entry there, not TODO comment.

## Home directory and git plumbing (`~/.helixgen`)

Artifact library carries three artifact kinds: **tones** (`library/tones/<logical-slug>.json` + per-variant `.hsp`), **guitar profiles** (`library/guitars/<slug>.json`), **per-IR metadata** (`library/irs/<pack>/<name>.json` sidecars next to copied WAVs — WAV bytes stay gitignored — plus `library/irs/mapping.json`, the `irhash →
wav-path` registry). Design: `docs/superpowers/specs/2026-07-15-library-metadata-design.md`.

- **`$HELIXGEN_HOME`** (`src/helixgen/home.py`) = root of everything helixgen persists — default `~/.helixgen`. Per-area overrides (`$HELIXGEN_LIBRARY`, `$HELIXGEN_IRS`, `$HELIXGEN_SETLISTS`, `$HELIXGEN_PREFS`, `$HELIXGEN_CACHE`, `$HELIXGEN_LOCKS`) keep working, always win over `$HELIXGEN_HOME`-derived default.
- **Home auto-`git init`s on first write** (`src/helixgen/libinit.py` + `gitops.py`) whenever `git` on PATH — unconditional, not preference-gated (its `.gitignore` excludes `devices/`, `cache/`, `tone3000/`, `*.bak*`, IR audio). Library-mutating operations **auto-commit** after, gated by `git_commit_tones` preference (default `"auto"`). All advisory: missing git binary or failed commit warns to stderr, never fails triggering operation.
- **Manifest lives at `~/.helixgen/setlists/manifest.json`** (override `$HELIXGEN_SETLISTS`) — manifest v3, **intent-only** (see "The tone library" below). Legacy `~/.helixgen/setlists.json` (v1/v2) auto-migrates up on first load (backup written first, legacy file renamed so re-runs never re-migrate). First `device sync` after v2-to-v3 migration re-pushes every managed tone once — harmless, idempotent (device serial observed nothing under own file yet).
- **Per-device observed state in `~/.helixgen/devices/<serial>.json`** (`src/helixgen/device/observations.py`) — observed placement (`cid`/`posi`) plus, since 0.24.0, device's **discovered address record** (`ip`, `model`, `firmware`, and — #77 — `port` when nonstandard). NOT manifest, NOT committed (`devices/` gitignored): placement rebuilt wholesale by every `device sync`, so losing file costs one re-`discover`.

## CLI (core verbs)

**Full per-verb reference: [`docs/CLI.md`](docs/CLI.md) "Commands" and "IR commands".** Verb index: `list-blocks`, `show-block`, `generate`, `view`, `ingest`, `bootstrap`, `register-irs`, `irhash`, `ir-scan`, `list-irs`, `ir-cache`, `analyze-audio`, `controllers`, surgical edit verbs + `patch` ("Surgical edits" below), `describe` + `library …` + `device …` (own sections below).

Rules that must stay in front of you:

- **Run `helixgen show-block "<name>"` before writing or editing spec** — param names case-sensitive, generator rejects unknown ones. Validator fails with list of valid names: run `show-block`, correct — don't guess.
- Verbs whose output agents consume take **`--json`** for machine-readable stdout; `view` prints JSON by default.
- `generate` with no `-o` writes into tone library, authors tone metadata — name via `--artist`/`--song` (paired) or `--descriptor` (mutually exclusive), plus optional `--guitar`. Explicit `-o <out.hsp>` = legacy path: writes there, auto-registers, naming flags ignored, **no metadata JSON**. Extension picks format (`.hsp` Stadium, `.hlx` legacy Helix).
- IR registration (`register-irs`, `ir-scan`) **copies WAVs into `library/irs/<pack>/` with metadata sidecars** by default (`--no-copy` opts out). Direct hashing needs libsndfile + **48 kHz sources** — helixgen input constraint, not device's (device normalizes any rate on own import, so non-48k IR still works once imported onto hardware).

### `helixgen device` — network control of a Helix Stadium

Talks to **Stadium** over LAN directly (OSC-over-ZeroMQ; no editor app; needs `pip install 'helixgen[device]'`). Run **`helixgen device
discover`** once to find + persist Stadium address; every verb then resolves IP as `--ip` > `$HELIXGEN_HELIX_IP` > persisted record — **no built-in default**; none set, verbs fail fast pointing at `device discover`. Empty/whitespace-only `--ip` is rejected (nonzero exit; omit flag to fall back), #77. `--port` likewise defaults to the record's persisted RPC port (2002 unless discovery saw a nonstandard advertised port, #77) — explicit `--port` wins. `device discover --forget <serial-or-ip>` prunes a stale persisted record (no network; clear error, not traceback, on unknown target or absent records). Discovery used once; sessions stay direct-to-IP. **Stadium-only.**

**Full per-verb reference — every flag + gotcha — lives in [`docs/CLI.md`](docs/CLI.md) "Device commands".** Verb index:

- **Preset + edit buffer:** `list` / `setlists` / `info` / `active` (ACTIVE preset — save/restore player's selection) / `read` / `load` / `create` / `save` / `rename` / `delete` / `set-param` / `blocks` / `params` (numeric pids + CURRENT raw values — run before `set-param`; block coordinates = DSP **grid slots**, 0-27) / `pull` / `push` / `restore` / `backup` / `local-list` / `watch` / `set-info` / `install` (transcodes helixgen `.hsp` straight into device content — no template, full fidelity). `--setlist` takes `user` (pool, default), `factory`, or real device setlist name (entries = references to pool presets).
- **Live ops (mutate ACTIVE tone):** `snapshot` / `bypass` / `model` / `reorder` (direct DEVICE-side reorder — distinct from local-manifest `slots reorder`; numeric args **cid-first**) / `tuner` / `meters` / `measure` (read-only 2003 telemetry) / `normalize` (level-matching loop over `measure`: DRY-RUN by default, `--yes` writes dB trims into **local `.hsp` only** — device follows via `sync` — records telemetry on library variants; holds `editbuffer` even in dry-run).
- **Global Settings + Global EQ:** `settings list|get|set`, `globaleq
  list|set` (**write-only** — no network read-back).
- **IRs on device:** `list-irs` (distinct from local `helixgen
  list-irs`) / `push-ir` / `pull-ir` / `delete-ir` / `rename-ir` / `ir-prune` (dry-run by default).
- **Setlists + sync:** `setlist create|rename|delete|duplicate` (device-side; never orphan pool presets), `setlist
  list|add|remove|create-local` (local manifest membership), `setlist
  import-hss` / `export-hss` (EXPERIMENTAL), `sync <setlist>` / `sync
  --all [--gc]`. `--repush` forces content re-push of unchanged tones — use once after transcoder upgrade (hash-based change detection can't see transcoder-output change).
- **Tone library / slots:** `helixgen register`, `device add` / `unsync` / `library` / `slots [list|restore|reorder] [--verify]`, `device setlist
  sync-on|sync-off`.

**Device-write awareness.** Read/list verbs safe — e.g. `info`, `active`, `read`, `list`, `list-irs`, `blocks`, `params`, `settings list`/`get`, `tuner`, `meters`, `measure`, `watch`, `backup`, `pull`/`pull-ir`, plus offline verbs (`local-list`, `library`, `slots list`, `globaleq list`, `--list`/`--dry-run` variants). Anything writing content, properties, files **mutates device** — live-ops verbs change ACTIVE tone immediately. Unsure: check verb's entry in [`docs/CLI.md`](docs/CLI.md). Posture for device writes: prefer empty/expendable slot when testing, take upfront `device backup`, tear down test artifacts after, expect #38 /CreateContent flakiness (re-run; slot-writing verbs fail safe on occupied slot).

**Machine-local advisory device locks.** Every device-mutating verb auto-acquires lease file (`~/.helixgen/locks/<ip>/<scope>.lock`) for duration, so concurrent helixgen processes on this machine never collide on device; read-only verbs take nothing. Scopes: `editbuffer`, `library`, `irs`, `globals`, `all`. Hold scopes across calls with `device lock
--scope all --label <who>` (export printed `HELIXGEN_LOCK_TOKEN` so own verbs pass through; same-shell calls pass through automatically); inspect with `device lock --status`, release with `device unlock`. Contended verbs wait `$HELIXGEN_LOCK_TIMEOUT` s (default 30) then error naming holder; stale leases reclaimed, live ones never. Run `device lock` from long-lived shell, not wrapper script (session leases get only 120 s dead-pid grace). `--no-lock` opts out (dangerous). Advisory + machine-local only — other hosts + Stadium desktop editor NOT covered. Full verb → scope table: [`docs/CLI.md`](docs/CLI.md) "Device locks".

**Stadium network stack flaky — sync/verb drops or stalls: re-run. `sync` + live-ops verbs idempotent + auto-reconnecting; slot-writing verbs (install/save/push/create) fail safe on occupied slot instead; `setlist import-hss` is the one NOT-idempotent retry. Keeps dropping: reboot Helix.**

**Tone library = single management record.** Every tone helixgen generates auto-registers into manifest (`~/.helixgen/setlists/manifest.json`). **Tone** = content + identity + management **intent**: desired **user slot** (`null` = off device, `"auto"` = wants device, or `"1A".."128D"`) plus **setlist memberships**. **"On device" ⟺ tone has slot.** No separate slot ledger. Presets addressed by integer **CID**; preset lives once in **pool** (`-2`), referenced by **setlists** under setlists root `-5`. **Sync = managed-set mirror** — installs/updates/reorders/deletes only tones helixgen manages, **never touches untracked device presets**. Specific Helix's **observed** placement not part of manifest — see "Home directory" above.

**Pushing tones to device driven by `device` skill** (plugin repo, `sheax0r/helixgen`) — runs after `tone` authored `.hsp`, centers on `device sync <setlist>`. Read before scripting setlist sync. Design + protocol refs: [`docs/CLI.md`](docs/CLI.md), `docs/helix-protocol.md`, `docs/superpowers/specs/2026-07-13-tone-library-model-redesign.md`.

## IR cab-pack catalog (character reference)

IR library at `irs/` (gitignored — paid packs stay local) carries grep-first tonal catalog at `irs/_catalog/`. Answers "which IR beefiest / brightest / best for vintage clean / tightest for modern metal" without re-analysing WAVs. Start at `irs/_catalog/README.md` (index + controlled tag vocabulary + mic legend + example greps); one file per pack holds per-mix mic combos + character tags.

**New IR pack added to `irs/` — catalog before moving on:**
1. Read pack's `*Manual*.pdf` — cab/speaker/amp, mic legend, per-mix mic combos, artist/usage notes.
2. `ls` pack's `Mixes/` folder for exact WAV basenames (what preset's cab block references via `mapping.json`).
3. Optionally FFT-analyse each Mix WAV (stdlib `wave` + `numpy`, 5 guitar bands) for measured bright/dark/beefy/tight tags — relative *within* pack.
4. Write `irs/_catalog/<slug>.md` from template in catalog README, ONLY controlled vocabulary; add row to README index table.

Don't invent character manual doesn't state; well-established general knowledge fine (Greenback = classic-rock, V30 = modern metal, ribbon = warm top, SM7 = fat). Catalog README "Adding a new pack" section = authoritative procedure + self-documenting template.

## Architecture: `.hsp` is the source of truth

`.hsp` file = 8-byte magic `rpshnosj` followed by JSON document — **is** canonical, editable artifact. No persisted intermediary spec, **no `.spec.json` sidecar**. Two flows act on it:

- **Author** new preset by feeding transient **recipe** (JSON shape below) to `generate`; helixgen clones chassis template, replays recipe as in-place mutations. Recipe input-only — not written to disk, never read back as truth.
- **Edit** existing `.hsp` with surgical verbs (`set-param`, `enable`, `add-block`, …); each reads `.hsp`, mutates body in place, writes `.hsp` back. No recompile, no sidecar.

Read `.hsp` back into recipe shape (inspection or hand-authoring similar preset): `helixgen view <preset.hsp>` — read-only projection.

## recipe shape (author input to `generate`)

**Recipe** = JSON author-input to `generate`. Input-only — never written to disk, never read back as truth. Base shape:

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

- `paths` = 1–2 entries (each maps to one DSP).
- `block` matches display_name from `list-blocks` — case-sensitive. Ambiguous: use model_id in brackets (e.g. "HD2_AmpBritPlexiBrt").
- `params` values floats 0.0–1.0 for most knobs; some ints/bools/Hz. Verify ranges with `show-block`.

**Exhaustive per-field reference — every optional section, full schema, defaults, ranges, examples — lives in [`docs/recipe-reference.md`](docs/recipe-reference.md).** Optional sections by name: per-path `input` (jack routing + Input-block params) + `output` (level/pan); `split`/`join` in `blocks` (parallel splits + merge-mixer wire params); top-level `snapshots` (≤8 named scenes: per-scene `disable` + `params` deltas + per-snapshot `output` level/pan), `footswitches` (FS1–FS5/FS7–FS11/EXP1Toe; FS6/FS12 reserved), `expression` (EXP1/EXP2 sweeps), `midi` (EXPERIMENTAL #33), `commands` (Command Center; EXPERIMENTAL #16); per-block `ir` (registered user IR by wav basename or 32-hex hash), `trails`, `raw` (verbatim unmodeled state — emitted by `view`, consumed by `generate`; editing existing `.hsp` never needs it). All recipe fields **Stadium-only** unless reference notes otherwise (legacy `.hlx` chassis ignores them).

**One-controller-per-param.** `(block, param)` driven by at most one of footswitch-param / expression / MIDI across whole spec (block's *bypass* may have several sources).

**Controller vocabulary & English rendering (agent behavior).** Reporting tone to human: render controllers in English (via `helixgen
controllers`), never bare `FS#` (e.g. `Footswitch 5 (top row, 5th from
left)`). Human *describes* control in plain language: translate to canonical identifier with dedicated small-model sub-agent fed `helixgen controllers --json` — returns exactly one identifier (or `AMBIGUOUS`/`NONE`); validate against canonical set before writing into recipe. `view` never drops controls it can't map — unmapped ones land in `unknown_controllers` (round-trip safe). Full detail: [`docs/recipe-reference.md`](docs/recipe-reference.md).

## User preferences (`preferences.json`)

`setup` / `tone` skills (plugin repo) read explicit settings from `~/.helixgen/preferences.json` (whole-file override `$HELIXGEN_PREFS`; per-key override `HELIXGEN_<KEY>`, e.g. `HELIXGEN_FAVOR_IRS=1`). Loaded by `src/helixgen/preferences.py`; precedence env var > file value > built-in default. Keys include `device.model`, `favor_irs`, `reveal_in_finder`, `guard_paid_irs_in_git`, `author`, `git_commit_tones` (default `"auto"`), **`default_guitar`** — guitar profile used when tone request doesn't name one (unset: `tone` skill asks, offers to save answer). Keys `instruments` + `preset_output_dir` **deprecated** (replaced by guitar profiles + `library/tones/` default write location): still parsed for back-compat, warned once per process, removed by `library migrate`.

## Tone naming and the library

**Naming schema (supersedes old `"<Tone Name> — <Guitar>"` convention).** Tone display name = `"$Artist - $Song - $Guitar"`, or `"$Descriptor -
$Guitar"` when no artist/song (guitar = target guitar's short name). Guitar segment omitted only for explicitly guitar-agnostic tone (generic patch). Filenames same schema, slugged lowercase-with-dashes (e.g. `foo-fighters-white-limo-les-paul-jr.hsp`). Guitar resolution order in `tone` skill: user-named guitar wins; else `default_guitar`; else skill asks, offers to save choice as `default_guitar`.

**Logical tone vs. variant.** **Logical tone** — one artist+song, or one descriptor — owns exactly one metadata JSON at `library/tones/<logical-slug>.json`, plus one or more **variants**, each real `.hsp` targeting single guitar, keyed by guitar's profile slug (or `"generic"` for guitar-agnostic variant). Manifest + device still key by *variant's* display name — that's what device preset is; metadata JSON just groups variants sharing identity. New variant of existing tone = `generate --guitar <other-guitar>` against same artist/song/descriptor. Tone write-up lives in metadata's `description_md` (authored via `helixgen library doc` — **no `.md` sidecar files**); per-variant notes in variant's `notes_md`.

## Guitar profiles

**Guitar profile** at `library/guitars/<slug>.json` (schema 1) = single source of truth for one guitar user owns — replaces `preferences.instruments`. Fields: `name`, `short_name` (appears in preset display names / filename slugs), `type`, `active`, `pickups`, `construction`, `character_md` (what guitar is *for* — read by `tone` skill to adapt params), `genres[]`, `controls[]` (control inventory variant's `guitar_settings` keys validate against). `--guitar <label>` resolves by slug / name / short_name, case-insensitive, most-specific tier first — ambiguity + unknown labels error; with **no** profiles yet, literal `slugify(label)` fallback keeps pre-migration authoring working (full resolution rules: [`docs/CLI.md`](docs/CLI.md) "Guitar profiles / resolution"). Profiles seeded from `preferences.instruments` by `library migrate`; scaffold new one with `helixgen library add-guitar` (also auto-commits); create/edit details via `setup` skill.

## The `helixgen library` verb group

Verb group over artifact library — tones, guitar profiles, per-IR metadata. Every library-mutating verb auto-commits home repo after (advisory, gated by `git_commit_tones`). **Full per-verb reference: [`docs/CLI.md`](docs/CLI.md) "Library commands".** Verb index: `library
list` (all three sections; `--tones`/`--guitars`/`--irs` narrows), `library
show <name>` (tone-first resolution, then guitar profile), `describe
<tone>` (longer human-oriented counterpart), `library doc` (author `description_md` / variant's `notes_md`), `library validate` (shape + cross-link checks: **problems** exit 1, **warnings** don't), `library
add-guitar`, `library import` (bring external `.hsp` under naming schema; never overwrites), `library migrate` (one-shot idempotent pre-library-to-library migration; `--dry-run` first), `library ir-backfill` (copy + scaffold metadata for IRs registered before library layout).

## Surgical edits

Preset exists: don't re-author to change one setting — use edit verbs. Each reads `.hsp`, mutates body **in place**, writes back, reusing all helixgen validation, model-id translation, IR injection. Works on ANY `.hsp` — helixgen-authored or raw device export — no decompile step, no sidecar. Fields helixgen doesn't model (dual-cab slots, harness, `xyctrl`, …) preserved untouched by construction.

**Run `helixgen show-block "<block>"` first** to confirm exact case-sensitive param name — same guardrail `generate` enforces.

Verbs — full signatures + per-flag detail in [`docs/CLI.md`](docs/CLI.md) "Commands":

- `set-param <preset> <block> <param> <value>` — one param, auto-coerced. **Negative** value needs `--` sentinel (`helixgen set-param t.hsp
  output level -- -3`). Block names `input` / `output` / `split` / `join` (`merge` alias) = **signal-flow pseudo-blocks** addressing path's endpoints / split / merge mixer.
- `enable` / `disable <preset> <block>` — un-bypass / bypass at base level.
- `add-block`, `remove-block`, `swap-model` (same-category only; carries over shared params, warns on dropped ones — surface those warnings).
- `view <preset.hsp>` — read-only `.hsp` → recipe projection.

`--snapshot NAME-or-INDEX` on `set-param`/`enable`/`disable` targets ONE snapshot's slot instead of base. **Gotcha:** once param's per-snapshot array varies, device applies it on every snapshot — later plain base edit of that param inaudible on-device (`set-param` warns). `--path`/`--lane`/`--pos` disambiguate duplicate block names — block addressing = `(path, lane, pos)`; no `--index`.

Multi-edit session: **`helixgen patch <preset.hsp> <ops.json|->`** applies JSON **list** of ops (`set_param`, `set_enabled`, `add_block`, `remove_block`, `swap_model`) in one atomic invocation — invalid op anywhere leaves `.hsp` untouched. Op fields mirror single-op verbs' flags. Agent edit loop = single `patch` call on file — no decompile/regenerate round-trip:

```bash
echo '[{"op": "set_param", "block": "Tape Echo Stereo", "param": "Mix", "value": 0.3},
       {"op": "set_enabled", "block": "Plate Stereo", "enabled": false}]' \
  | helixgen patch MyTone.hsp -
```

## Generation notes

- Chassis = whatever first ingested. Stadium chassis (`_helixgen_chassis_shape: "hsp"`) produces `.hsp` output; `.hlx` chassis produces `.hlx`. Carryover `meta.color` / `meta.info` / `device_id` from originating export currently expected.
- Some Stadium model IDs translated on ingest (e.g. `HD2_DistScream808Mono` → `HD2_DrvScream808`); generate translates back when writing `.hsp`.

## Project layout

- `src/helixgen/` — `cli` (core verbs + entry point), `cli_device` (`helixgen device` verb group, imported back into `cli`), `ingest`, `hsp`, `chassis`, `library`, `spec` (recipe parser/validator), `mutate` (in-place `.hsp` edit verbs), `recipe` (author `.hsp` from recipe), `view` (read-only `.hsp` → recipe projection), `generate` (shared low-level `.hsp` builders + legacy `.hlx`), `controllers`, `preferences`, `bootstrap`, `ir`, `irhash_cache`, `locks` (machine-local advisory device locks), `home`/`libinit`/`gitops` (`~/.helixgen` home root, auto-init, advisory auto-commit), `naming`, `tone_meta`, `guitars` (guitar profiles), `ir_meta` (per-IR sidecars), `migrate` (library migration), `cli_library` (`helixgen library` verb group)
- `src/helixgen/device/` — network device control (OSC-over-ZeroMQ client, `transcode`, `modelmap`, `defs`, setlist manifest)
- `docs/` — `BACKLOG.md` (THE backlog), `CLI.md` (full CLI + per-verb **device** reference), `recipe-reference.md` (exhaustive recipe field reference), `superpowers/specs/` (design docs + review findings), `superpowers/plans/` (implementation plans), `features/` (per-feature deep dives), protocol references (`helix-protocol.md`, `helix-format-reference.md`, `helix-sftp-access.md`, `ir-hash-algorithm.md`)
- `tests/` — pytest suite (run with `PYTHONPATH=$PWD/src python -m pytest`); golden-output contract (`tests/golden/`) + 211-export real-device round-trip (`tests/test_decompile_acceptance.py`) pin `.hsp` fidelity. **Runs parallel by default** — `addopts = -ra -n auto` (pytest-xdist, a `[dev]` dep; needs `pip install -e '.[dev]'`, else plain `pytest` errors `unrecognized arguments: -n`). Force serial with `-n0` (debugging: prints/pdb/order-sensitive). One lock test (`test_locks.py::test_all_vs_scope_create_race_yields_exactly_one_winner`) is `xfail(strict=False)` ONLY under xdist per backlog #88 — serial runs still enforce the invariant. Live suite forces itself serial (`tests/live/conftest.py` `pytest_configure`).
- `tests/live/` — **opt-in live integration suite** (backlog #66): drives real CLI via subprocess against real library + real Stadium. Skipped unless `HELIXGEN_LIVE=1` (device tests also need device reachable). Impact-area markers registered in `pyproject.toml`; after targeted change run its blast radius, e.g. `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python -m pytest -m "live and sync" tests/live`. Safety = fixtures (scratch env for ALL local state, upfront `device backup`, before/after device-state diff, `HGTEST`-prefixed artifacts with teardown, session check that real `~/.helixgen` byte-identical after); **`tests/live/conftest.py` documents full safety model**, deliberately excluded verbs, known-gotcha xfails.
- `tests/fixtures/` — synthetic + real-export fixtures
- `data/` (gitignored) — user's personal `.hsp` exports
- `irs/` (gitignored) — paid commercial IR packs; character catalog at `irs/_catalog/`

## Development workflow

- **Worktrees, branched from fresh `github/main`.** All non-trivial work in git worktree whose branch starts from freshly-fetched `github/main` (GitHub remote named **`github`**, not `origin`) — never commit directly on local `main`; may be stale. Fetch again before picking release version number (concurrent PR once released 2.10.0 mid-flight, collided with in-progress bump).
- **Adversarial review before shipping.** Before merging PR, dispatch at least one independent review subagent prompted to *break* change (find bugs, regressions, spec violations — not summarize). Confirmed findings fixed or explicitly deferred to `docs/BACKLOG.md`. Major changes also get committed review doc in `docs/superpowers/specs/` (see PR #31 review for shape).
- **Agent-facing surfaces ship in sync.** CLI = only engine surface; per-verb `--help` text = agent contract (pinned by `tests/test_cli_parity.py`). Any change to CLI-visible behavior updates, same PR, every surface in this repo describing it: verb's help text, this CLAUDE.md, `docs/CLI.md`. Drift between code + these surfaces = bug, not docs chore. **Division of labor:** reference detail (flags, semantics, gotchas) belongs in `docs/CLI.md` / `docs/recipe-reference.md`; this CLAUDE.md carries mental models, behavioral rules, verb indexes — don't grow per-verb prose here when reference doc is right home. Behavior changes skills describe also need companion PR in plugin repo (`sheax0r/helixgen`, `.claude/skills/*`) — land two together, note cross-repo pairing in both PR descriptions.
- **Backlog discipline.** `docs/BACKLOG.md` = single project backlog. Deferred work gets numbered entry there — not TODO comment, not side file.
- TDD throughout: failing test first, then minimal implementation. See existing test files for established pattern.
- Pure stdlib + `click` for CLI; no other runtime deps.
- Real-export fixtures in `tests/fixtures/presets/`, loaded under skip-if-not-present guards so suite stays green on clean clone.

## Releasing

This repo releases **`helixgen` PyPI package** (version in `pyproject.toml` + `src/helixgen/__init__.py` — bump both together; version feeds generated presets' `meta`). Publishing via OIDC trusted-publisher workflow (`.github/workflows/publish.yml`) on `vX.Y.Z` tags pushed to `main` (first publish 0.19.1).

Plugin releases (`stable` branch + `helixgen--vX.Y.Z` tags) live in **plugin repo** (`sheax0r/helixgen`), owned by its release workflow — nothing in this repo moves those refs. Core release changes behavior skill depends on: cut core release first, then bump plugin's pinned `helixgen` version in its own PR.

## ralphex

Implementation tasks driven from helix coordination workspace run via [ralphex](https://github.com/umputun/ralphex) plan files in `docs/plans/` (scaffold: `docs/plans/TEMPLATE.md`); completed plans move to `docs/plans/completed/`. Config = tracked `.ralphex/config` (`default_branch` pinned `main` — remote named `github`, ralphex can't auto-detect from `origin/HEAD`); runtime dirs `.ralphex/worktrees/` + `.ralphex/progress/` gitignored. Launcher syncs local `main` from `github/main` before run. Review = ralphex built-in pipeline (`external_review_tool = none`).
