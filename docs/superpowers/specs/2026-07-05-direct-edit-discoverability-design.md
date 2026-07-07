# Direct-edit discoverability

**Date:** 2026-07-05
**Status:** Design — audit + doc recommendation, pending approval

## Problem

The surgical-edit machinery (spec patch verbs, sidecar spec, decompile loop) is
**fully built and shipped** — CLI commands, MCP tools, and `patch.py` verbs all
exist and are tested. But it is nearly **invisible to a user** who wants to make
one targeted change ("change the delay mix to 0.3", "disable the reverb", "swap
the amp"):

- `CLAUDE.md` — the repo's own reference — documents `generate`, `ingest`,
  `register-irs`, `ir-scan`, `list-irs`, `list-blocks`, `show-block`, and the
  full spec schema, but **says nothing** about `set-param`, `enable`, `disable`,
  `add-block`, `remove-block`, `swap-model`, or `decompile`. A user reading the
  docs would conclude the only way to change a setting is to regenerate from a
  fresh spec.
- The one place edits *are* documented is the `tone` skill's "Adjusting an
  existing tone" section — but `tone` is scoped to *designing a sound from an
  artist/song/genre target*. Someone who just wants to nudge one knob on an
  existing preset may never trigger it, and it only describes the **MCP** path,
  not the CLI verbs.
- Param names are case-sensitive and must be discovered with `show-block` /
  `show_block`; nothing on the edit path tells a first-time user this, so their
  first attempt ("set `treble`" instead of `Treble`) fails.

This is a **discoverability + documentation** problem, not a missing-capability
problem. Almost everything needed already works. The deliverable is mostly
surfacing it, plus one small MCP parity fix.

## What exists today (inventory)

### 1. Patch verbs — `src/helixgen/patch.py`

Pure spec-dict transforms. Each deep-copies the spec, mutates, returns the new
dict (param-name/range validation is deferred to `generate.py` at regen time).
Block addressing is by display name, disambiguated by `(path, index)` and/or
`(lane, pos)` via `resolve_block()`.

| Function | Signature | Effect |
|---|---|---|
| `resolve_block` | `(spec, name, path, index, *, lane=None, pos=None) -> (pi, bi)` | Locate a placed block; raises `PatchError` on no-match / ambiguous-match with a helpful list of placed blocks. |
| `set_param` | `(spec, block, param, value, *, path, index, lane, pos) -> spec` | Set one param on one block. |
| `set_enabled` | `(spec, block, enabled, *, path, index, lane, pos, snapshot=None) -> spec` | Bypass/un-bypass a block at base level, or (with `snapshot=`) add/remove it from that snapshot's `disable` list. |
| `add_block` | `(spec, block, *, path=0, after=None, params=None, lane, pos) -> spec` | Insert a block (append, or after a named block; refuses ambiguous `after`). |
| `remove_block` | `(spec, block, *, path, index, lane, pos) -> spec` | Delete a block. |
| `swap_model` | `(spec, old, new, library, *, path, index, lane, pos) -> (spec, warnings)` | Replace a block with another **of the same category**; carries over params that exist on the target, drops the rest (warned), drops an `ir` ref if the target isn't an IR block (warned). |

`PatchError(ValueError)` is the single error type for bad addresses.

### 2. CLI commands — `src/helixgen/cli.py`

Six edit verbs plus `decompile`, all operating on a `preset_path` that may be a
`.hsp` **or** a `.spec.json`:

- `helixgen set-param <preset> <block> <param> <value>` — value is auto-coerced
  (`_coerce_cli_value`: bool → int → float → string).
- `helixgen enable <preset> <block> [--snapshot NAME]`
- `helixgen disable <preset> <block> [--snapshot NAME]`
- `helixgen add-block <preset> <block> [--after NAME]`
- `helixgen remove-block <preset> <block>`
- `helixgen swap-model <preset> <old> <new>`
- `helixgen decompile <preset.hsp> -o spec.json`

Every edit verb accepts `--path`, `--index`, `--lane`, `--pos` for
disambiguation, plus `--library` / `--irs`. `add-block` uses `--path` (default
0) and `--after`.

The shared pipeline is `_run_patch` → `_apply_and_save`:

1. `load_spec_for_preset(preset)` (see §4) returns `(spec_dict, spec_path)`.
2. Apply the mutate lambda → `(new_spec, warnings)`.
3. Write the spec back to `spec_path` (the sidecar).
4. If the target was a `.hsp`, **regenerate it** from the updated spec.
5. Echo warnings to stderr and `Patched <preset>`.

So the user *feels* like they edit the `.hsp` directly; under the hood the spec
stays authoritative and all of `generate.py`'s validation / model-id
translation / IR injection is reused.

### 3. MCP tools — `mcp_server/server.py` + `mcp_server/tools.py`

- **`patch_preset(model, spec, operations) -> {spec, warnings}`** — applies a
  list of ops to an **in-memory spec dict** (does *not* touch files; caller
  regenerates with `generate_preset`). Dispatch table `_PATCH_OPS` in
  `tools.py`. Supported ops and the params each reads:
  - `set_param` — `{op, block, param, value, [path], [index]}`
  - `set_enabled` — `{op, block, enabled, [path], [index], [snapshot]}`
  - `add_block` — `{op, block, [path], [after], [params]}`
  - `remove_block` — `{op, block, [path], [index]}`
  - `swap_model` — `{op, old, new, [path], [index]}`
- **`decompile_preset(model, hsp_b64) -> spec dict`** — decode a base64 `.hsp`
  blob (checks `HSP_MAGIC`), return an editable spec dict for `patch_preset` /
  `generate_preset`.
- Supporting tools for discovery: `list_blocks(model, [category])`,
  `show_block(model, name_or_id)` (returns params with types/defaults/ranges),
  `generate_preset(model, spec)`.

**MCP/CLI parity gap (genuine, small):** the `_PATCH_OPS` lambdas forward only
`path`/`index` — they **do not pass `lane`/`pos`** — while the CLI verbs and the
`patch.py` functions do. So the MCP path cannot disambiguate duplicate blocks by
`(lane, pos)`, even though the `tone` skill instructs agents to add `"pos"` /
`"lane"` to a `patch_preset` operation. `add_block` in MCP also omits `lane`/
`pos`. This is the one place that needs new code, not just docs (see
Recommendation §3).

### 4. Sidecar spec + load-or-decompile — `src/helixgen/preset_io.py`, `generate.py`

- `generate_preset` (generate.py ~L884) writes a **sidecar** next to every
  `.hsp`: `foo.hsp` → `foo.spec.json` (the raw spec, source of truth for edits).
- `load_spec_for_preset(preset_path)`:
  - `.json` input → load directly, return `(spec, path)`.
  - `.hsp` with a sidecar present → load the sidecar.
  - `.hsp` orphan (no sidecar) → **decompile**, write the sidecar, return it.
- `sidecar_path(hsp)` = `hsp.with_name(stem + ".spec.json")`.

This collapses "spec'd tone" and "orphan `.hsp`" into one edit code path:
*(decompile if needed) → patch spec → regenerate*.

### Summary: the loop already works, end to end

CLI: `helixgen set-param preset.hsp "Tape Echo Stereo" Mix 0.3` — done, one
command, regenerates the `.hsp`.
MCP: `decompile_preset` (if orphan) → `patch_preset` → `generate_preset`.

## The gap

1. **No user-facing docs for the edit verbs.** `CLAUDE.md` omits all six CLI
   edit commands + `decompile` + the sidecar convention. This is the single
   biggest miss — the project's own reference implies editing isn't possible.
2. **Edit instructions are trapped in `tone`.** The only prose about editing is
   in a skill scoped to artist/song tone design, and it only covers the MCP
   path. A "change one setting" request may not route through `tone` at all, and
   a CLI user gets nothing.
3. **Finding param names isn't surfaced on the edit path.** A user must know to
   run `show-block "<block>"` first (case-sensitive names). Nothing in the edit
   docs (because there are none) tells them, so the first edit attempt with a
   guessed lowercase name fails.
4. **No worked examples** of the three canonical edits (change a param, disable a
   block, swap an amp) anywhere a CLI user would look.
5. **MCP can't disambiguate duplicate blocks** by `(lane, pos)` despite the skill
   telling agents to — a latent runtime failure on any preset with two
   same-named blocks (dual-cab, split lanes). (Code gap, §Recommendation 3.)

## Recommendation

Prefer **surfacing existing capability over new code.** Concretely:

### 1. Add a "Surgical edits (direct editing)" section to `CLAUDE.md` — PRIMARY

This is where a user and any agent will look. Add a section after "spec.json
shape" (or right after the CLI list) covering:

- The mental model: **the spec is the source of truth**; editing a `.hsp` edits
  its sidecar `.spec.json` and regenerates the `.hsp`. Orphan `.hsp` files are
  auto-decompiled on first edit (a sidecar appears next to them).
- The six verbs + `decompile`, one line each (mirroring the existing CLI list),
  including the `--path/--index/--lane/--pos` disambiguation flags and
  `--snapshot` on enable/disable.
- **"Run `show-block '<block>'` first to get exact, case-sensitive param
  names"** — the same guardrail the generate flow already has.
- The three worked examples below.

### 2. Add a short pointer in the `setup` skill (and keep the `tone` section)

`setup` runs before any preset work. Add a one-paragraph "Editing an existing
preset" note that points at the CLI verbs and the MCP `patch_preset` /
`decompile_preset` loop, so conversational edits are discoverable even when the
request doesn't read as a `tone` design task. Leave the existing `tone`
"Adjusting an existing tone" section as-is (it's good); optionally add a
one-line cross-reference to the CLI verbs for users who prefer the shell.

### 3. Fix MCP `lane`/`pos` parity — the only new code

In `mcp_server/tools.py`, extend the `_PATCH_OPS` lambdas for `set_param`,
`set_enabled`, `remove_block`, `swap_model`, and `add_block` to forward
`o.get("lane")` and `o.get("pos")` (the `patch.py` functions already accept
them). Update the `patch_preset` docstring in `server.py` to list `[lane]` /
`[pos]` on each op and to point agents at `show_block` for param names. Small,
mechanical, unblocks editing any dual-cab / split-lane preset over MCP.

### 4. (Optional, low priority) `helixgen help-edit` command

A `helixgen help-edit` subcommand that prints the same worked examples as §1
would make the CLI self-documenting (`helixgen --help` already lists the verbs,
but not how to chain them with `show-block`). Nice-to-have; the `CLAUDE.md`
section covers the need without new code. Only build if the user wants
in-terminal discovery.

### Worked examples (to embed in CLAUDE.md §1)

**A. Change the delay mix to 0.3**

```bash
helixgen show-block "Tape Echo Stereo"        # confirm the param is "Mix"
helixgen set-param MyTone.hsp "Tape Echo Stereo" Mix 0.3
# → rewrites MyTone.spec.json and regenerates MyTone.hsp
```

MCP equivalent (agent holds the spec dict):

```json
{"op": "set_param", "block": "Tape Echo Stereo", "param": "Mix", "value": 0.3}
```

**B. Disable a block (kill the reverb)**

```bash
helixgen disable MyTone.hsp "Plate Stereo"
# add --snapshot Lead to bypass it only in the "Lead" snapshot
```

MCP: `{"op": "set_enabled", "block": "Plate Stereo", "enabled": false}`

**C. Swap an amp (Plexi → JCM-style)**

```bash
helixgen list-blocks --category amp          # find the exact target display name
helixgen swap-model MyTone.hsp "Brit Plexi Brt" "Brit 2204"
# same-category only; carries over shared params, warns on any it had to drop
```

MCP: `{"op": "swap_model", "old": "Brit Plexi Brt", "new": "Brit 2204"}`
(surface any returned `warnings` to the user).

**Disambiguating duplicates** (two same-named blocks, e.g. dual cab): add
`--pos N` (and `--lane 0|1`, `--path 0|1`) on the CLI, or `"pos"/"lane"/"path"`
on the MCP op — the latter requires the §3 fix.

## Open questions for the user

1. **Placement:** a dedicated `## Surgical edits` section in `CLAUDE.md`, or fold
   it into the existing CLI bullet list? (Recommend a dedicated section — the
   sidecar mental model needs a paragraph.)
2. **`setup`-skill pointer:** worth adding, or is the `tone` "Adjusting an
   existing tone" section enough for the conversational path? (Recommend adding
   the pointer so non-`tone` requests are covered.)
3. **`helixgen help-edit` command:** build it, or rely on docs + `--help`?
   (Recommend docs-only for now.)
4. **Ship the MCP `lane`/`pos` parity fix in this change**, or track it
   separately? It's the only code change and it's small — recommend including
   it so the documented "disambiguate with `pos`/`lane`" instruction is actually
   true on the MCP path.
5. Any preferred **canonical amp target** for the swap example (I used a generic
   "Brit 2204" placeholder — should confirm a real display name from the user's
   library, or leave it illustrative)?
