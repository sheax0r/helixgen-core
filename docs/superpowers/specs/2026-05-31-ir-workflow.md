# IR workflow refactor — design

**Date:** 2026-05-31 (revised after review)
**Status:** Draft (revised; for second-pass review)
**Source brief:** conversation 2026-05-31 (after `compute_stadium_irhash` landed in `!1`)

## Goal

Now that helixgen can compute Stadium IR hashes directly from any WAV file,
rework the IR-registration story so that all three user contexts get a
coherent IR workflow, even when their constraints differ:

- **Local Claude Code** users with an IR library at a known path on disk
- **claude.ai web** users who drag IR files into the chat
- **Local CLI** users running `helixgen` standalone (without Claude)

The current `helixgen register-irs` command was designed when computing the
hash required a Stadium device round-trip. With the hash now computable
offline for 48 kHz IRs, "registration" reduces to: a cache of (hash, path)
pairs for the local CLI, a stateless WAV → hash conversion endpoint for
hosted use, and a preset-binding fallback for IRs we still can't hash offline
(non-48 kHz, until libsamplerate is ported).

This spec also addresses three adjacent problems surfaced in the same
conversation:

1. The MCP server's `generate_preset` tool currently has no way to verify the
   user is on a Stadium device (vs. legacy Helix Floor / LT / Stomp). It just
   generates and hopes.
2. The Claude agent has no encoded workflow for asking the user up-front what
   model they have, where their IRs live, or what each IR is good for.
3. Hosted Claude users (claude.ai) have no IR registry today — they cannot
   meaningfully use `With Pan` IR blocks in generated presets, even after `!1`,
   because there's no place to put a (hash, file) mapping the device side can
   resolve.

## Non-goals

- **libsamplerate path for non-48 kHz IRs.** Stadium uses
  `SRC_SINC_BEST_QUALITY` for resampling; porting that to Python is its own
  reverse-engineering project. For now we keep raising
  `NotImplementedError` with the `sox` suggestion. The preset-binding form of
  `register-irs` (below) is the workaround.
- **Legacy Helix (.hlx) IR support.** Hashes don't exist there — IRs are
  identified by slot number — and the slot model is a separate problem.
- **A web UI for managing the IR cache.** CLI + MCP are sufficient.
- **Importing IRs onto the device from helixgen.** The user still does that
  via the Helix Stadium app's Librarian. Helixgen only deals with the
  preset-side hash references; the device side is the user's responsibility.
- **Research crawling.** We do not pre-fetch tonal descriptions for the
  user's entire IR library on startup. We only research on demand.
- **Cross-session IR memory on hosted Claude.** A WAV dragged into one
  claude.ai conversation does not persist to the next. Each session needs
  IRs re-dragged. Acceptable trade-off; the alternative is per-user storage
  this server doesn't have.

## Status quo (after !1)

- `compute_stadium_irhash(wav_path: Path | str) -> str` — the core primitive,
  in `src/helixgen/ir.py`. **Takes a filesystem path only** (calls libsndfile
  via ctypes which needs a path); does not accept raw bytes. Stateless.
  48 kHz sources only. ~1 ms per IR. Validated against 27 known
  (hash, wav) pairs.
- `helixgen register-irs <preset.hsp> <wavs...>` — original form. Binds each
  preset slot's irhash to a wav path. Used when the user has a preset
  exported from a device.
- `helixgen register-irs <wavs...>` — new auto-compute form. Computes each
  WAV's hash via `compute_stadium_irhash` and registers.
- `helixgen list-irs` — prints the mapping.
- MCP server (4 tools): `list_blocks`, `show_block`, `generate_preset`,
  `list_irs` (read-only). No write-side IR tool. No model parameter on any
  tool. `list_irs` on the hosted deploy is always empty.

## Proposed shape

### Layer 1 — `compute_stadium_irhash` (unchanged)

Stays path-based. The MCP handler for `compute_irhash` (Layer 2) writes
incoming base64 bytes to a `tempfile.NamedTemporaryFile` and calls the
existing function on the temp path. We do **not** add a bytes-accepting
variant; the temp-file route is fine for the 200 KB scale of typical IRs
and avoids duplicating the byte-management logic.

### Layer 2 — CLI changes

#### Add `ir-scan` (recursive cache builder)

```
helixgen ir-scan <directory>...           # recurse and cache hashes
helixgen ir-scan --rescan <directory>...  # recompute even if cached
helixgen ir-scan --remove <basename>      # forget one entry
```

Behavior:
- Recursively finds `*.wav` (case-insensitive) under each given directory.
- For each file: computes hash, registers (path, hash) into `mapping.json`.
- Skips files already in the cache by absolute path *unless* `--rescan`.
- Skips files that raise `NotImplementedError` (non-48 kHz) with a per-file
  stderr warning; does not abort the whole scan.
- Skips files that fail libsndfile open with a warning.

#### Keep `register-irs` (both forms)

The `register-irs <preset.hsp> <wavs...>` form **remains** because it solves
the non-48 kHz case `ir-scan` cannot: when the user has a non-48 kHz IR they
re-exported from a device into a preset, the preset still has the hash —
just bind from the preset.

The `register-irs <wavs...>` form is functionally equivalent to running
`ir-scan` on the parent directory of each WAV, but we keep it for
discoverability and because it's a strict subset of the new behavior.

No deprecation alias. Pre-1.0; the original commands continue to work.

#### `mapping.json` is now explicitly a *cache*

Same on-disk format for now. **`--rescan` is required to detect user-edited
files**; we do not add an mtime field in this iteration (would be a real
schema change, and the rescan flag is good enough until we observe pain).

#### Test/doc ripple

The CLI rename adds an alias rather than replacing — so the existing
`register-irs` invocations in tests and docs don't need to change. New tests
cover `ir-scan` specifically. Documentation gets one new bullet for `ir-scan`
and a sentence calling out when to use which form.

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
    ...
```

**Validation:** `model` must be `"stadium"` or `"stadium_xl"`. Anything else
raises `ValueError` (FastMCP translates this to an MCP `isError` content
block — there is no "structured error" type to return).

**Honest accounting of what this enforces:**

The `model` param is a **soft gate**, not a hard one. A misinformed agent
can fill in `"stadium_xl"` on behalf of an HX Stomp user; the tool will run
and silently produce a preset that won't load on the user's device. The
value of the param is twofold:

1. It forces the question into the agent's tool-call planning. An agent
   that's never seen the user mention a model has to either ask or guess;
   the required arg makes "ask" the path of least resistance.
2. It is the verification *hook* the `using-helixgen` skill uses. The skill
   confirms the device per session (not just per user) before allowing the
   agent to call any of these tools. The param is what the skill checks.

The skill is the real gate. The param is what the skill enforces against.
We acknowledge this is partial.

#### Add `compute_irhash`

Stateless. Takes base64-encoded WAV bytes. Returns the hash plus a
`reminder` field:

```json
{
  "irhash": "f42b15f382002ddc1069dd7f0bca639f",
  "reminder": "This hash will only resolve on the device if the same WAV is loaded onto your Helix Stadium via the Librarian's Cab IRs Import. Drag it in if you haven't already."
}
```

**Input validation (security):** the handler must (a) decode base64, (b)
size-check the decoded bytes (reject > 2 MB, well above any realistic IR),
(c) verify the first 4 bytes are `RIFF` and bytes 8–12 are `WAVE` before
handing to libsndfile. libsndfile has had CVEs; treating arbitrary internet
bytes as trusted input is unsafe. The size limit lives below the MCP
transport's per-message budget rather than at any specific number — 2 MB
of WAV → ~2.7 MB of base64, comfortably within JSON-RPC limits.

#### Add `discover_irs` (local-only)

Walks a server-side filesystem path and returns `[{path, hash, basename}, ...]`
without touching `mapping.json`.

**Hosted gating:** the handler checks `os.environ.get("HELIXGEN_HOSTED") == "1"`
(set in `render.yaml` on the hosted deploy). When set, the handler raises
`ValueError` with the text:

> `discover_irs` requires a local helixgen MCP server. The hosted deploy
> has no access to your filesystem. Drag IRs into the conversation and use
> `compute_irhash` per file instead.

Local deploys (env var unset) walk the directory freely.

#### Hosted IR resolution workflow (the critical gap)

This is the explicit answer to "how does a hosted user's preset get IR
hashes": the agent does **the binding in conversation**, not on the server.

1. User drags IR file(s) into chat. Claude has the bytes.
2. Agent calls `compute_irhash(model, wav_b64)` per file. Receives
   `{irhash, reminder}` per file.
3. Agent builds the preset spec inline. For each `With Pan`-style block,
   the agent puts the **32-char hex hash** in the slot's `ir` field
   (CLAUDE.md documents that `ir` accepts a hex hash *or* a basename).
4. Agent calls `generate_preset(model, spec)`. The hosted server resolves
   the `ir` field by accepting the hash literal (no mapping lookup
   needed — the hash IS what gets emitted into the `.hsp` file).
5. Agent surfaces the per-IR `reminder` to the user before returning the
   `.hsp`: "Before loading this preset, import these IRs onto the device
   via the Librarian: [list]."

What this requires on the implementation side:

- `helixgen generate` must accept a literal 32-char hex hash in the `ir`
  field without needing a mapping entry. Verify this is already true; if
  not, add a code path.
- The agent has to remember the hash within a single conversation. That's
  free — it's in tool-call history.
- The agent has to re-drag IRs if the conversation restarts. Documented as
  a non-goal above; we don't try to persist hosted state.

### Layer 3 — the `using-helixgen` skill

Ship a Claude Code skill at `.claude/skills/using-helixgen/SKILL.md`.

**Frontmatter:** `name: using-helixgen`, `description:` covering "Helix
preset design / IR registration / preset generation." Combined frontmatter
≤ 1024 chars (existing project skill convention; verified via the `tone`
skill).

**Skill body (revised after review):**

```markdown
## Before generating any preset

In order, every session that involves generating or modifying a preset:

1. **Confirm the device model.** Look up the existing user memory
   `user_device.md`. If absent, ask: "Which Helix do you have? Stadium,
   Stadium XL, or something else?" If older than ~3 months, confirm: "Still
   on Stadium XL?" Record/update. If the answer is *not* Stadium or
   Stadium XL, tell the user this project supports the Stadium family only
   for now.

2. **Locate IR library if applicable.** If the user mentions IRs or
   `With Pan` blocks, check memory for `user_ir_directory.md`. If absent
   and the user is on local Claude Code, ask: "Where do your impulse
   responses live? (Provide a directory path.)" Record. Skip on hosted —
   ask the user to drag IRs into the chat instead, per
   `compute_irhash` workflow.

3. **Recall IR preferences.** Check memory for `project_ir_notes.md`.
   Use these when choosing which IR to suggest.

4. **Check `feedback_no_paid_irs_in_repo.md`** to remember the user's IR
   collection includes commercial packs that must never be committed or
   pasted into fixtures.

## When the user mentions an IR you haven't seen before

1. **Try web research only on basenames matching a known commercial pack
   prefix** (`YA ` = York Audio, `OH ` = Ownhammer, `3SP ` = 3 Sigma, etc.).
   Search `<pack name> <basename> tonal description`.

2. **Never invent tonal descriptions from basename pattern-matching.** If
   web research returns nothing high-confidence, do *not* describe the IR
   from the filename. Ask the user: "What's `<basename>` meant for? Any
   specific tones you reach for it for?"

3. **Record findings.** Add a one-line entry to `project_ir_notes.md`
   keyed by basename.

## Interaction with the `tone` skill

`tone` covers tone-design choices (anti-fizz preferences, drive stacking,
etc.). This skill covers metadata and workflow. When both trigger,
`using-helixgen` runs first (verify model + IR setup), then `tone` informs
the actual block choices.

## After generating a preset that uses user IRs

Tell the user, in one sentence: "Make sure these IRs are loaded on your
Stadium via the Librarian → Cab IRs → Import before you load this preset,
or the IR block will show 'No Model'." List the IR basenames involved.

## What this skill does NOT enforce

This skill is advisory; the agent can technically skip these steps if the
user pushes for speed ("just generate it"). The `model` MCP param backs
the device check; otherwise this is goodwill. Do not promise the user
something this skill cannot deliver.
```

**Hosted users do not load Claude Code skills.** The workflow guidance for
hosted users comes through the MCP tool errors and the `reminder` field
on `compute_irhash`. We do *not* try to duplicate the skill body into tool
descriptions — descriptions are seen only during tool selection, not as
standing instructions, and stuffing them is wasted tokens. The hosted
tools' error messages and reminder fields are the only persistent guidance
the hosted agent gets.

### Memory schema (revised)

The skill names three memory files, using existing project conventions
(`user_`, `project_`, `feedback_` prefixes; see existing memory at
`~/.claude/projects/-Users-michael-shea-git-helixgen/memory/`):

| File                       | Type     | Holds                              |
|----------------------------|----------|------------------------------------|
| `user_device.md`           | user     | "Stadium" / "Stadium XL"           |
| `user_ir_directory.md`     | user     | one directory path                 |
| `project_ir_notes.md`      | project  | one bullet per IR the user uses    |

`user_device.md` already exists — the skill reads and updates it; it does
not create a parallel `helix_device.md`. The "confirm if older than
~3 months" rule above handles device upgrades without forcing the user to
edit memory by hand.

For users who own multiple Stadium devices (one Stadium + one Stadium XL
in different rigs), `user_device.md` can hold either a single string or a
list — the skill normalizes. We do *not* try to track per-device IR
libraries; if it matters in practice we revisit.

The skill should also read `feedback_no_paid_irs_in_repo.md` (already
present in this user's memory) so it knows IR fixtures are gitignored and
test fixtures must be synthetic or freely licensed.

## Hosted deploy requirements

The hosted MCP server runs on Render. To make `compute_irhash` work there:

1. **`render.yaml` must install `libsndfile`** via `aptPackages: [libsndfile1]`.
   The current `render.yaml` does not, so `compute_irhash` would 500 on first
   call. The implementation MR adds this.
2. **`HELIXGEN_HOSTED=1` environment variable** set in `render.yaml`. Used
   by `discover_irs` to refuse the call (see Layer 2 above).
3. **`compute_irhash` payload size limit** enforced before base64 decode
   (2 MB cap), then format validation (`RIFF`/`WAVE` magic bytes) before
   libsndfile call.

## Migration

This is a **breaking change for the hosted MCP server** (`model` becomes
required on `generate_preset` and `list_irs`). Acceptable: the deployed
server has very few consumers and is pre-1.0. The MR that adds `model`
also bumps the server's version string.

The CLI does not break: existing `register-irs` invocations continue to
work. The new `ir-scan` is additive.

## Open questions

1. **`compute_irhash` payload form.** Spec proposes base64. Some MCP
   transports might support binary blobs natively; investigate before
   the implementation MR. If binary is supported, prefer it.

2. **`ir-scan` concurrency.** Two concurrent `ir-scan` runs on overlapping
   directories race on `mapping.json`. Cheap fix: lockfile via
   `fcntl.flock` around the read-modify-write. Implementer's call.

3. **Multi-device memory shape.** Single string vs. list in
   `user_device.md`. Spec recommends list-of-strings; revisit if real
   ergonomic pain shows up.

## Test plan

- **CLI:** `ir-scan` on a tmp tree of synthesized 48 kHz WAVs; verifies
  recursion, `--rescan`, non-48 kHz warning behavior, and `mapping.json`
  shape. Existing `register-irs` tests stay green.
- **MCP unit tests:** `model` param accepts `stadium`/`stadium_xl`,
  rejects anything else with the expected `ValueError`.
- **`compute_irhash` tests:** synthetic base64 WAV → expected hash; size
  cap rejects oversize; non-RIFF rejects with a clear error.
- **`discover_irs` tests:** local works, hosted (`HELIXGEN_HOSTED=1`)
  refuses with the documented message.
- **Skill frontmatter test:** a tiny pytest parses `SKILL.md` and asserts
  `name` and `description` are present and combined frontmatter is
  ≤ 1024 chars. (No existing linter; ~10 lines of pytest.)
- **Manual:** real Claude Code session with the skill installed; verify
  the agent asks model + IR-dir before generating.
- **Manual on hosted:** drag a real IR, confirm `compute_irhash` returns
  a hash matching `compute_stadium_irhash` on the same WAV locally;
  confirm `discover_irs` errors helpfully.
