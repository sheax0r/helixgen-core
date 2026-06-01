# IR workflow refactor — design

**Date:** 2026-05-31
**Status:** Draft (for review)
**Source brief:** conversation 2026-05-31 (after `compute_stadium_irhash` landed in `!1`)

## Goal

Now that helixgen can compute Stadium IR hashes directly from any WAV file,
rework the IR-registration story so the same code path serves three users
cleanly:

- **Local Claude Code** users with an IR library at a known path on disk
- **claude.ai web** users who drag IR files into the chat
- **Local CLI** users running `helixgen` standalone (without Claude)

The current `helixgen register-irs` command was designed when computing the
hash required a Stadium device round-trip. With the hash now computable
offline, "registration" reduces to two simpler concerns: a cache of
(hash, path) pairs for the local CLI, and a stateless WAV → hash conversion
endpoint usable from anywhere.

This spec also addresses two adjacent problems surfaced in the same
conversation:

1. The MCP server's `generate_preset` tool currently has no way to verify the
   user is on a Stadium device (vs. legacy Helix Floor / LT / Stomp). It just
   generates and hopes.
2. The Claude agent has no enforced workflow for asking the user up-front
   what model they have, where their IRs live, or what each IR is good for.

## Non-goals

- **libsamplerate path for non-48 kHz IRs.** Stadium uses
  `SRC_SINC_BEST_QUALITY` for resampling; porting that to Python is its own
  reverse-engineering project. For now we keep raising
  `NotImplementedError` with the `sox` suggestion.
- **Legacy Helix (.hlx) IR support.** Hashes don't exist there — IRs are
  identified by slot number — and the slot model is a separate problem.
- **A web UI for managing the IR cache.** CLI + MCP are sufficient.
- **Importing IRs onto the device from helixgen.** The user still does that
  via the Helix Stadium app's Librarian. Helixgen only deals with the
  preset-side hash references.
- **Research crawling.** We do not pre-fetch tonal descriptions for the
  user's entire IR library on startup. We only research on demand.

## Status quo (after !1)

- `compute_stadium_irhash(wav_path) -> str` — the core primitive. ctypes →
  libsndfile. 48 kHz only. ~1 ms per IR. Validated against 27 known
  (hash, wav) pairs.
- `helixgen register-irs <preset.hsp> <wavs...>` — original form. Binds each
  preset slot's irhash to a wav path. Used when the user has a preset
  exported from a device.
- `helixgen register-irs <wavs...>` — new auto-compute form. Computes each
  WAV's hash via `compute_stadium_irhash` and registers.
- `helixgen list-irs` — prints the mapping.
- MCP server: `list_blocks`, `show_block`, `generate_preset`, `list_irs`
  (read-only). No write-side IR tool. No model parameter on any tool.

## Proposed shape

### Three layers

```
┌──────────────────────────────────────────────────────────┐
│ Layer 1: compute_stadium_irhash() — pure primitive       │
│   already exists; stateless; bytes-or-path in, hash out  │
└──────────────────────────────────────────────────────────┘
            │                            │
            ▼                            ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│ Layer 2: CLI cache       │  │ Layer 2: MCP tools       │
│   helixgen ir-scan       │  │   compute_irhash()       │
│   helixgen list-irs      │  │   discover_irs()         │
│   mapping.json as cache  │  │   model: required        │
└──────────────────────────┘  └──────────────────────────┘
            │                            │
            ▼                            ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│ Layer 3: helixgen CLI    │  │ Layer 3: Claude (CC/web) │
│   helixgen generate      │  │   guided by skill +      │
│   reads mapping for ir   │  │   memory; sends bytes    │
│   lookup by basename     │  │   or paths to MCP        │
└──────────────────────────┘  └──────────────────────────┘
```

### Layer-1 stays as-is.

Already shipped in `!1`. No changes proposed.

### Layer 2 — CLI changes

#### Rename `register-irs` → `ir-scan`

The new mental model is "scan a directory and cache the hashes I find,"
not "register these specific files." The user points at their IR library
once; helixgen walks it.

```
helixgen ir-scan <directory>...           # recurse and cache hashes
helixgen ir-scan --rescan <directory>...  # recompute even if cached
helixgen ir-scan --remove <basename>      # forget one entry
helixgen list-irs                          # unchanged
```

Behavior:
- Recursively finds `*.wav` (case-insensitive) under each given directory.
- For each file: computes hash, registers (path, hash) into `mapping.json`.
- Skips files already in the cache by absolute path *unless* `--rescan`.
- Skips files that raise `NotImplementedError` (non-48k) with a per-file
  stderr warning; does not abort the whole scan.
- Skips files that fail libsndfile open with a warning.

Keep the old `helixgen register-irs` syntax as a deprecation alias for one
release cycle; print a "use `ir-scan` instead" notice on stderr. Remove
after.

#### `mapping.json` is now explicitly a *cache*

Same on-disk format as today. Comment update in the schema; no migration
needed. Add a `helixgen ir-purge` command for "drop the whole cache and
start over" — useful if the user moves their IR library.

### Layer 2 — MCP tools

#### Add `model` parameter to every preset/IR tool

```python
@app.tool()
def generate_preset(model: str, spec: dict[str, Any]) -> EmbeddedResource:
    ...

@app.tool()
def compute_irhash(model: str, wav_b64: str) -> dict[str, str]:
    ...

@app.tool()
def discover_irs(model: str, ir_directory: str) -> list[dict[str, str]]:
    """Walk a directory, return [{path, hash, basename}, ...].
    Local-only (path must exist on the server's filesystem). Errors on hosted."""
    ...
```

Validation: `model` must be `"stadium"` or `"stadium_xl"`. Anything else
returns a structured error:

```
unsupported model: 'helix_floor'. helixgen currently supports only
'stadium' and 'stadium_xl'. Ask the user to confirm their device.
```

The structured error gives Claude actionable text — it knows to ask the user
again.

Adding the param to existing tools is technically a breaking change for
the deployed server. Acceptable since it's pre-1.0 and currently has very
few consumers.

#### Add `compute_irhash`

Stateless. Takes base64-encoded WAV bytes (drag-and-drop friendly). Returns
the hash plus the upload-to-device reminder embedded in the response.

```json
{
  "irhash": "f42b15f382002ddc1069dd7f0bca639f",
  "reminder": "This hash will only resolve on the device if the same WAV is loaded onto your Helix Stadium via the Librarian's Cab IRs Import. Drag it in if you haven't already."
}
```

Errors structurally on non-48k, libsndfile-open-failure, or `len(wav_b64)`
above a sanity limit (e.g. 10 MB).

#### Add `discover_irs` (local MCP only)

Walks a path on the server's filesystem, returns the (hash, path) pairs
without touching `mapping.json`. Hosted deploys reject this with
`"discover_irs requires a local helixgen MCP server; the hosted deploy
has no access to your filesystem"`. Local deploys use it freely.

This is the bridge to "the agent looks up the user's IR library when
generating a preset." The skill (next section) tells the agent when to
call it.

### Layer 3 — the `using-helixgen` skill

Ship a Claude Code skill at `.claude/skills/using-helixgen/SKILL.md`.

The skill encodes a workflow that's hard to enforce purely via tool
schemas:

```markdown
---
name: using-helixgen
description: Use when the user wants to design, generate, or modify a
  Helix preset (.hsp / .hlx). Establishes device model, IR library
  location, and tonal preferences before generating.
---

## Before generating any preset

In order:

1. **Confirm the device model.** Check memory for `helix_device.md`. If
   absent, ask: "Which Helix do you have? Stadium, Stadium XL, or
   something else?" Record the answer. If it's not Stadium or Stadium XL,
   tell the user this skill only supports Stadium-family devices for now.

2. **Locate IR library if applicable.** If the user mentions IRs or
   `With Pan` blocks, check memory for `helix_ir_directory.md`. If
   absent, ask: "Where do your impulse responses live? (Provide a
   directory path.)" Record. Skip this step if the user is on hosted
   Claude — they'll drag IRs into the chat instead.

3. **Recall IR preferences.** Check memory for `helix_ir_notes.md`
   (one entry per IR the user has discussed). Use these when choosing
   which IR to suggest.

## When the user mentions an IR you haven't seen before

1. **Try web research.** If the basename looks like a known commercial
   pack (`YA ` = York Audio, `OH ` = Ownhammer, `3SP ` = 3 Sigma, etc.),
   web-search `<pack name> <ir basename> tonal description` to find what
   amp/cab/mic combination it models and its character.

2. **If web research fails, ask the user.** "What's `<basename>` meant
   for? Any specific tones you reach for it for?"

3. **Record findings.** Add a one-line entry to `helix_ir_notes.md`
   keyed by basename: `- YA DXVB 112 Mix 03 — vintage Marshall-leaning,
   bright top end; user reaches for it on clean tones`.

## After generating a preset that uses user IRs

Tell the user, in one sentence: "Make sure these IRs are loaded on your
Stadium via the Librarian → Cab IRs → Import before you load this preset,
or the IR block will show 'No Model'."

List the IR basenames in the preset so the user can verify.
```

Two important properties of this skill:

- **It is a Skills file, not a workflow stored in the MCP server.** Skills
  load into Claude Code's context before the agent acts. The user doesn't
  have to configure anything.
- **Claude.ai web doesn't load Claude Code skills.** For hosted users, the
  same logic is repeated in the MCP tool descriptions. The downside is
  duplication; the upside is each interface gets the guidance it needs.

### Memory schema

The skill names three memory files:

| File                       | Type    | Holds                                |
|----------------------------|---------|--------------------------------------|
| `helix_device.md`          | user    | "Stadium" / "Stadium XL"             |
| `helix_ir_directory.md`    | user    | one directory path                   |
| `helix_ir_notes.md`        | project | one bullet per IR the user uses      |

We already have `helix_device.md`-ish memory in the user's profile (the
existing `user_device.md`). The skill should look there first; only ask if
absent.

`helix_ir_notes.md` is a single growing file. It is the right size for
memory: a few dozen entries at most, each one line. For users with 800 IRs
who only care about 20, we never write the other 780. This is the
"on-demand research" property the user explicitly called out.

## Migration

### CLI

| Before                              | After                          |
|-------------------------------------|--------------------------------|
| `register-irs <preset.hsp> <wavs>`  | unchanged; deprecation notice  |
| `register-irs <wavs>`               | `ir-scan <wavs>` (deprecation alias keeps old form) |
| (nothing)                           | `ir-scan <dir>` — recursive    |
| `list-irs`                          | unchanged                      |
| (nothing)                           | `ir-purge`                     |

One release cycle of deprecation, then remove `register-irs` entirely.

### MCP

This is a breaking change for the deployed server (the `model` param is
required, no default). Bump the server version and update the connector
docs. Existing in-flight conversations using the old tool shape will get a
clear error.

### Mapping file

No schema change. The same `mapping.json` is now a cache rather than a
registration source-of-truth. The CLI documentation gets a sentence to that
effect; the file itself doesn't move or change shape.

## Open questions

1. **Cache invalidation.** Do we mtime-check WAVs on `ir-scan` to detect
   user-edited files? Or always trust the existing hash unless `--rescan`?
   Argument for mtime check: it's cheap and silently does the right thing
   if the user re-exports a WAV. Argument against: complicates the cache
   format. *Recommendation: mtime check; add an `mtime` field to mapping.json
   entries (back-compat: missing mtime = always rescan).*

2. **Should the deprecation alias survive longer than one release?** It's
   a low-cost compatibility shim. *Recommendation: keep it through 0.x,
   remove only at 1.0.*

3. **Hosted MCP — do we expose a `discover_irs` no-op that returns a
   helpful error, or omit the tool entirely on hosted?** *Recommendation:
   expose it; the error text teaches the agent what's possible.*

4. **`model` granularity.** Stadium vs Stadium XL differs in 6 vs 10
   footswitches and 1 vs 2 expression pedals. The generator already
   handles both. Should `model` accept just `"stadium_xl"` or distinguish
   sub-variants? *Recommendation: accept `"stadium"` and `"stadium_xl"` as
   distinct values; the generator routes accordingly. Reject everything
   else.*

5. **IR notes memory — what if the user has 50+ IRs they actually use?**
   The memory budget caps `MEMORY.md` (the index) at ~200 lines, but
   `helix_ir_notes.md` itself can be larger since it's only loaded when
   the skill requests it. *Recommendation: don't pre-cap; if it ever
   exceeds 500 lines, split by pack name.*

## Implementation order

1. **MCP `model` param + `compute_irhash` + `discover_irs`** — small,
   self-contained, unblocks the hosted use case
2. **`using-helixgen` skill** — drops in alongside; no code changes
   needed elsewhere
3. **CLI rename + deprecation alias** — purely cosmetic; can land any
   time
4. **`ir-scan --rescan` and mtime cache field** — quality-of-life; last

Each is its own MR. (1) and (2) ship together since the skill references
the new tools.

## Test plan

- **MCP unit tests** for the `model` param: stadium / stadium_xl accepted,
  everything else returns the structured error
- **`compute_irhash` MCP test** using the existing `_write_synth_wav`
  helper, base64-encoded
- **`discover_irs` MCP test** against a tmp dir of synth wavs
- **CLI deprecation alias** test: `register-irs` still works, prints
  deprecation warning to stderr
- **`ir-scan` integration test** using a tmp dir tree of synth wavs
- **Skill linting** — the skill should be loadable by Claude Code (basic
  frontmatter check)
- Manual: run a full session in Claude Code with the new skill installed
  and verify the agent asks for model + IR dir before generating
