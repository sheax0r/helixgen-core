# Explicit user-preferences file (formalize "memory" behaviours) — design

**Date:** 2026-07-05
**Status:** Draft — pending user review of this written spec
**Source brief:** `project_tone_skill_backlog.md` item 5 — formalize the
memory-driven skill behaviours into an explicit, user-editable preferences file
the `setup` / `tone` skills read, so behaviour is controlled by a file, not by
implicit Claude memory.

## Goal

Today several `helixgen:setup` and `helixgen:tone` behaviours are driven by
things the agent recalls from Claude **memory** — a fragile, invisible control
surface the user can't inspect or edit directly:

- **`favor_irs`** — prefer user IR blocks over modeled/stock cabs. Currently
  gated on a "feedback memory" the tone skill checks in prose (step 3, "if a
  feedback memory says the user prefers IRs over stock cabs when available").
- **Reveal-in-Finder** — run `open -R "<path>"` before telling the user to
  import a file (`feedback_reveal_file_in_finder.md`).
- **No-paid-IRs guard** — never commit/copy paid commercial WAVs into the repo
  or fixtures (`feedback_no_paid_irs_in_repo.md`).
- **Device model** — Stadium vs Stadium XL (`user_device.md`); gates validation
  and chassis assumptions.
- **Instrument list** — the user's four guitars (`user_guitars.md`), consumed by
  the instrument-recommendations backlog feature (item 3).

Memory is the wrong home for a *setting*. It's not user-editable in place, it's
point-in-time (memories carry an "N days old, verify before asserting" warning),
and its recall is non-deterministic. This feature introduces an explicit,
user-editable **preferences file** that the skills read as the authority, with
memory demoted to a one-time *seed / fallback*.

## Non-goals (this feature)

- **Secrets or credentials.** Reconciling the global rule "config from ENV, no
  file-based config for secrets or runtime settings" — the file holds **user
  preferences only** (never API keys, tokens, paths to secrets). Env still wins
  (see Precedence), and nothing secret lives in the file.
- **Per-IR tonal notes** (`project_ir_notes.md`) and **commercial-pack prefix
  table** (`YA`→York Audio, …). These are *data / knowledge*, not settings;
  they stay in memory / the skill body. The file controls *behaviour toggles and
  facts*, not a tonal knowledge base.
- **The IR hash↔wav mapping** (`mapping.json`). Already has a home under
  `$HELIXGEN_IRS`; unchanged.
- **A settings GUI or interactive editor.** The file is hand-editable JSON; the
  scaffolder writes a commented starter. A tiny `helixgen prefs` CLI is proposed
  but the file is always the source of truth.
- **Migrating legacy `.hlx` behaviour.** Preferences apply to the Stadium
  workflow the skills drive.

## File format + location

### Format: JSON

`preferences.json` — pretty-printed JSON, mirroring the existing
`mapping.json` under `$HELIXGEN_IRS` and the `library/*.json` convention. JSON
is chosen over TOML because:

- **Stdlib-only.** `json` is in the stdlib; `tomllib` is read-only and 3.11+
  (helixgen must not add a write-side TOML dep to emit the scaffold, and the
  project pins to pure stdlib + `click`).
- **Consistency.** Everything helixgen already reads/writes on disk is JSON
  (`mapping.json`, `chassis.json`, the block library, the spec format). One
  format to learn.
- **The MCP server can round-trip it** with `json.load` / `json.dump` and no
  extra import.

Trade-off accepted: JSON has no comments. The scaffolder compensates by writing
a `"_comment"` key and a `"_docs"` URL, and every key name is self-describing.

### Location + precedence

| Source | Path / mechanism | Wins over |
|--------|------------------|-----------|
| Explicit env file | `$HELIXGEN_PREFS` (absolute path to a `.json` file) | everything below |
| Default file | `~/.helixgen/preferences.json` | memory + built-in |
| Per-key env override | `HELIXGEN_<KEY>` (e.g. `HELIXGEN_FAVOR_IRS=1`) | the file's value for that key |
| Claude memory | `user_device.md`, `user_guitars.md`, the feedback memories | built-in defaults only (seed/fallback) |
| Built-in default | hard-coded in `preferences.py` | — |

Resolution order **per key**, first hit wins:

1. `HELIXGEN_<KEY>` env var, if set (respects the ENV-first spirit of the global
   rule — any single setting can be overridden from the environment without
   touching the file, e.g. CI can force `HELIXGEN_REVEAL_IN_FINDER=0`).
2. The value in the resolved preferences file (`$HELIXGEN_PREFS` or the default
   path).
3. Claude memory — used **only** to *seed* the file the first time (see
   Scaffolding) and as a documented fallback the skill may consult if the file
   is absent and un-scaffoldable. Once the file exists, the file is authoritative
   and memory is not re-read for these keys.
4. Built-in default.

`$HELIXGEN_PREFS` overrides the whole-file location (parallel to
`$HELIXGEN_LIBRARY` / `$HELIXGEN_IRS`). It must point at a file, not a
directory; a missing file at an explicitly-set `$HELIXGEN_PREFS` is an error
(the user asked for that file), whereas a missing file at the *default* path is
"not scaffolded yet" and triggers first-run scaffolding.

## Key schema

Top-level object. `schema_version` enables future migrations. All keys optional
on read (missing key ⇒ built-in default); the scaffolder writes them all.

| key | type | default | env override | consumed by | notes |
|-----|------|---------|--------------|-------------|-------|
| `schema_version` | int | `1` | — | loader | Bumped on breaking schema changes; loader migrates older files forward. |
| `device.model` | enum `"Stadium"` \| `"Stadium XL"` | `null` (unset ⇒ ask) | `HELIXGEN_DEVICE_MODEL` | setup §1 | Replaces `user_device.md` as the control. `null` ⇒ setup asks and writes the answer back. |
| `favor_irs` | bool | `false` | `HELIXGEN_FAVOR_IRS` | tone §3 | Prefer a matching user IR block over a stock cab when `list_irs` is non-empty. Replaces the "feedback memory" gate. |
| `reveal_in_finder` | bool | `true` | `HELIXGEN_REVEAL_IN_FINDER` | setup (after-generate), tone §8 | Run `open -R "<path>"` before "import this" messages. Skill also checks it's on macOS; the toggle lets the user disable it (e.g. headless/Linux). |
| `guard_paid_irs_in_git` | bool | `true` | `HELIXGEN_GUARD_PAID_IRS` | setup / any git action | When true, the agent refuses to `git add`/commit/copy WAVs into tracked paths or fixtures and uses free/synthesized fixtures. Formalizes `feedback_no_paid_irs_in_repo.md`. |
| `preset_output_dir` | string (path) | `null` (⇒ `/tmp`) | `HELIXGEN_PRESET_DIR` | tone §7/§7a | Where generated `.hsp` + companion `.md` are written. `null` keeps today's `/tmp/<slug>.{hsp,md}` behaviour. `~` expanded. |
| `author` | string | `null` (⇒ OS username) | `HELIXGEN_AUTHOR` | tone §5 (spec `author`) | Default value for the spec `author` field, instead of the placeholder `"you"`. |
| `default_guitar` | string | `null` (⇒ ask) | `HELIXGEN_DEFAULT_GUITAR` | tone §6 | Which of the user's `instruments` to default to when a tone request doesn't name a guitar (used for tone-naming — title + filename + description). `null` ⇒ the tone skill asks and offers to save the answer here. |
| `instruments` | array<Instrument> | `[]` | — (structured; not env-overridable) | tone §1/§6, instrument-recs feature | The user's guitars/basses. Replaces `user_guitars.md` as the machine-readable control for the instrument-recommendations feature. |

### `Instrument` object

Structured but modest — enough for the instrument-recommendations sibling
feature to propose pickup/selector/volume/tone settings without re-deriving them
from prose each time.

| field | type | required | notes |
|-------|------|----------|-------|
| `name` | string | yes | Display name, e.g. `"Gibson Les Paul Junior"`. |
| `type` | enum `"guitar"` \| `"bass"` | yes | Coarse instrument class. |
| `pickups` | string | no | Free-text pickup description, e.g. `"one bridge P-90 (single-coil soapbar)"`. |
| `selector` | enum `"none"` \| `"3-way"` \| `"5-way"` \| string | no | Switch type; drives the switch-language the tone report uses. `"none"` for vol/tone-only guitars (LP Jr). |
| `active` | bool | no | Active vs passive pickups (affects response); omit if unknown. |
| `genres` | array<string> | no | Style hints for auto-selecting a guitar when the user doesn't name one (`["punk","garage","raw rock"]`). Mirrors the "how to apply" mapping in `user_guitars.md`. |
| `notes` | string | no | One-line free text (e.g. "breaks up early"). |

Example file the scaffolder writes:

```json
{
  "schema_version": 1,
  "_comment": "helixgen user preferences. Edit freely. Env vars HELIXGEN_<KEY> and $HELIXGEN_PREFS override this file. See CLAUDE.md.",
  "device": { "model": "Stadium XL" },
  "favor_irs": false,
  "reveal_in_finder": true,
  "guard_paid_irs_in_git": true,
  "preset_output_dir": null,
  "author": null,
  "default_guitar": null,
  "instruments": [
    {
      "name": "Gibson Les Paul Junior",
      "type": "guitar",
      "pickups": "one bridge P-90 (single-coil soapbar)",
      "selector": "none",
      "genres": ["punk", "garage", "raw rock", "blues"],
      "notes": "breaks up early; vol + tone only"
    },
    {
      "name": "ESP LTD EC-1000",
      "type": "guitar",
      "pickups": "2 humbuckers (stock active EMG 81/60 — confirm active vs passive)",
      "selector": "3-way",
      "active": true,
      "genres": ["modern metal", "metalcore", "hard rock"]
    },
    {
      "name": "Strandberg Boden Essential 6",
      "type": "guitar",
      "pickups": "2 humbuckers (Suhr-licensed) w/ coil splits",
      "selector": "5-way",
      "genres": ["prog", "djent", "fusion", "clean"]
    },
    {
      "name": "Ibanez Prestige",
      "type": "guitar",
      "pickups": "HH/HSH DiMarzio (confirm model)",
      "selector": "5-way",
      "genres": ["classic rock", "hard rock", "shoegaze"]
    }
  ]
}
```

## How the skills read + apply it

### Loader (`preferences.py`)

New pure-stdlib module `src/helixgen/preferences.py`:

- `default_prefs_path()` → `$HELIXGEN_PREFS` if set, else
  `~/.helixgen/preferences.json` (honours the `.helixgen` dir convention shared
  with `$HELIXGEN_LIBRARY` / `$HELIXGEN_IRS`).
- `load()` → a `Preferences` dataclass. Applies the per-key precedence
  (env > file > default). Missing default file ⇒ all defaults, `scaffolded=False`.
  Missing **explicit** `$HELIXGEN_PREFS` file ⇒ raises (user named a file that
  isn't there). Malformed JSON ⇒ raises with the path and parse error.
- `scaffold(seed: dict) -> Path` → writes the default file atomically (tmp +
  rename) with the `seed` values merged over built-in defaults. Refuses to
  overwrite an existing file unless `force=True`.
- Typed accessors: `prefs.favor_irs`, `prefs.reveal_in_finder`,
  `prefs.device_model`, `prefs.instruments`, etc.
- Env parsing: booleans from `1/0/true/false/yes/no` (case-insensitive);
  anything else raises so a typo'd `HELIXGEN_FAVOR_IRS=ture` fails loud.

The MCP server may expose a `read_preferences` tool (thin wrapper over `load()`)
so the `tone` skill gets the resolved values in one round-trip instead of
shelling out. Optional; the skills can also read the file directly.

### `setup` skill changes

`setup` becomes the **owner** of the preferences file.

- **§0.5 (new) — Load preferences.** Early in the setup pass, load the file. If
  the **default** file is absent, *scaffold* it (see below) and tell the user in
  one line: `"Created ~/.helixgen/preferences.json — edit it to change these
  defaults (favor_irs, device model, instruments, …)."` Then `open -R` it if
  `reveal_in_finder` and macOS.
- **§1 — Confirm the device model.** Read `device.model` from prefs instead of
  `user_device.md`. If unset (`null`), ask as today and **write the answer back
  to the file** (not just memory). If set, trust it (drop the "≤3 months old"
  memory-age dance — a file doesn't go stale). Memory `user_device.md` becomes a
  seed for the first scaffold only.
- **§3 / IR handling.** The no-paid-IRs guard reads `guard_paid_irs_in_git`.
- **Reveal-in-Finder** everywhere setup surfaces a file: gated on
  `reveal_in_finder` (+ macOS check) rather than an unconditional rule.

### `tone` skill changes

- **§3 (pick blocks / IR gate).** Replace the prose "if a feedback memory says
  the user prefers IRs" with: read `favor_irs` from prefs. `favor_irs == true`
  AND `list_irs()` non-empty ⇒ prefer a matching IR block over the stock cab
  (rest of the IR-matching logic unchanged). `favor_irs == false` ⇒ stock cabs,
  as today. The "flip it on by saying 'prefer IRs'" instruction becomes "set
  `favor_irs: true` in preferences.json (or say so and I'll set it for you)."
- **§1 / §6 (guitar).** When the user doesn't name a guitar, consult
  `instruments` + their `genres` to infer one; use `selector` to pick the
  switch-language for the report. This is the machine-readable feed for the
  instrument-recommendations backlog feature (item 3).
- **§5 (spec author).** Default the spec `author` to `prefs.author` (falls back
  to OS username), not the `"you"` placeholder.
- **§7/§7a (save location).** Write `.hsp` + `.md` to `preset_output_dir` when
  set, else `/tmp`. Replaces "wherever the user's convention puts presets
  (memory or a stated rule)".
- **§8 (report / import).** `open -R` gated on `reveal_in_finder`.

In all cases the skill prose changes from *"recall from memory"* to *"read from
preferences (setup already loaded them)"*, and each place that today "saves a
feedback memory" instead **writes the file** (and may additionally note it in
memory as a convenience, but the file is the control).

### Scaffolding on first run

When `setup` finds no default file:

1. Gather a **seed** from Claude memory if present: `device.model` from
   `user_device.md`; `instruments` from `user_guitars.md`;
   `guard_paid_irs_in_git`/`reveal_in_finder` stay at their `true` defaults
   (that's what the feedback memories already say). `favor_irs` seeds `false`
   unless a "prefer IRs" feedback memory exists, in which case seed `true`.
2. `preferences.scaffold(seed)` writes the file atomically with comments.
3. Tell the user it was created and where, and that env vars / `$HELIXGEN_PREFS`
   override it.

If memory is empty (fresh user), scaffold pure defaults with `device.model:
null` and `instruments: []`, then §1 asks for the device and writes it back.

A small optional CLI mirrors this for non-agent use:

- `helixgen prefs init` — scaffold the default file (from memory-less defaults);
  `--force` to overwrite.
- `helixgen prefs show` — print the resolved values (after env + file merge),
  so the user can see what actually takes effect.
- `helixgen prefs set <key> <value>` — convenience writer for scalar keys.

## Migration

No code data-migration is needed (there's no prior file). Migration is *from
memory to file*, done once by `setup`'s scaffolder:

| Today (memory) | Becomes (preferences.json key) | How |
|----------------|-------------------------------|-----|
| `user_device.md` → "Stadium XL" | `device.model: "Stadium XL"` | Seeded on scaffold; §1 writes future changes to the file. |
| `user_guitars.md` (4 guitars) | `instruments: [...]` | Seeded on scaffold from the memory's structured facts. |
| "prefer IRs" feedback memory (if present) | `favor_irs: true` | Seeded if such a memory exists, else `false`. |
| `feedback_reveal_file_in_finder.md` | `reveal_in_finder: true` | Default `true`; toggle exposed. |
| `feedback_no_paid_irs_in_repo.md` | `guard_paid_irs_in_git: true` | Default `true`; toggle exposed. |
| `user_ir_directory.md` (path) | *not migrated* — stays `$HELIXGEN_IRS` | IR dir already has an env home; see Open questions. |

The memory entries are **not deleted** — they remain as human-readable context
and as the seed source if the file is ever removed. But after scaffolding, the
skills stop *depending* on memory for these behaviours; the file is the control.
`schema_version` guards future changes: on load, a file with an older
`schema_version` is migrated forward in `preferences.py` (add missing keys with
defaults, rename as needed) and rewritten.

## Testing (TDD)

| file | covers |
|------|--------|
| `tests/test_preferences_load.py` | default when file absent; load existing file; env-key override beats file; `$HELIXGEN_PREFS` redirects location; missing explicit `$HELIXGEN_PREFS` file raises; malformed JSON raises with path; bool env parsing (valid + typo raises). |
| `tests/test_preferences_scaffold.py` | scaffold writes atomically; refuses to clobber without `force`; seed merges over defaults; scaffolded file re-loads to the seeded values. |
| `tests/test_preferences_schema.py` | unknown key tolerated (forward-compat); `Instrument` optional-field handling; `schema_version` forward-migration adds missing keys. |
| `tests/test_prefs_cli.py` | `prefs init` / `--force`; `prefs show` reflects env override; `prefs set` round-trips. |

Skill behaviour (favor_irs gate, reveal_in_finder gate) is exercised by the
skills, not pytest; the module tests cover the resolution logic those skills
depend on.

## Module touch list

- **New:** `src/helixgen/preferences.py` — loader, `Preferences` dataclass,
  `Instrument`, `default_prefs_path()`, `scaffold()`, env-merge. Pure stdlib.
- **New (optional):** `helixgen prefs` command group in `cli.py`.
- **Edits:** `mcp_server` — optional `read_preferences` tool.
- **Docs:** `CLAUDE.md` — document `preferences.json`, `$HELIXGEN_PREFS`, the
  key schema, and the env-override convention (alongside `$HELIXGEN_LIBRARY` /
  `$HELIXGEN_IRS`).
- **Skills:** `.claude/skills/setup/SKILL.md` (own + scaffold + read),
  `.claude/skills/tone/SKILL.md` (read `favor_irs`, `instruments`, `author`,
  `preset_output_dir`, `reveal_in_finder`). Done as a final edit after the module
  lands, per the established pattern.

## Open questions for the user

1. **`ir_library_dir` in the file too?** The IR directory lives at
   `$HELIXGEN_IRS` (env) and its path is also in `user_ir_directory.md`. Should
   the preferences file carry an `ir_library_dir` that *seeds* `$HELIXGEN_IRS`
   when the env var is unset — or keep IR-dir strictly env-only to avoid two
   sources of truth? (Leaning env-only, per the global "config from ENV" rule.)
2. **Instrument schema depth.** Is the proposed `Instrument` shape (name / type
   / pickups / selector / active / genres / notes) the right granularity for the
   instrument-recommendations feature, or do you want richer per-pickup /
   per-position detail (the `user_guitars.md` memory has 5-way position maps for
   the Strandberg)? More structure = more upkeep by hand.
3. **Reveal-in-Finder default off non-macOS.** Default `reveal_in_finder: true`
   but the skill also checks the OS. Acceptable, or would you rather the default
   itself be platform-derived (true on macOS, false elsewhere) so the file
   reflects reality?
4. **Auto-write vs confirm.** When §1 learns the device model or you say "prefer
   IRs", should the skill write the file **silently** (fast) or **confirm first**
   ("I'll set `favor_irs: true` in preferences.json — ok?")? Silent is smoother;
   confirm is safer for a file you also hand-edit.
5. **Repo-local override?** Should a `./helixgen.preferences.json` in the current
   working directory (a project) override the home file, the way many tools do
   project-local config? Useful if you keep per-project preset conventions;
   skipped in this draft to keep precedence simple (env > home file > memory).
6. **`guard_paid_irs_in_git` scope.** This governs *agent* behaviour (refuse to
   commit WAVs). It can't enforce anything at the git level (`.gitignore` does
   that). Do you want it in the preferences file at all, or is it better left as
   a hard policy in the skill/memory since it's a safety rule, not a taste knob?
