# helixgen redesign: `.hsp`-canonical (eliminate the intermediary spec format)

**Date:** 2026-07-08
**Status:** Approved — implementation authorized (big-bang rewrite, fan-out to sub-agents)
**Branch:** `redesign-hsp-canonical`

## Problem

helixgen today carries a two-stage pipeline: an author writes a high-level
JSON **spec**, and `generate.py` *compiles* it into the Stadium-native `.hsp`
document. Surgical edits reverse this (`decompile` → `patch` → `generate`) and
persist a `.spec.json` **sidecar** next to every `.hsp`.

This intermediary format taxes the project on every axis the user cited:

1. **Two sources of truth** — the `.spec.json` sidecar and the `.hsp` can drift;
   keeping them in sync is machinery (`decompile`-on-orphan, sidecar rewrite).
2. **Abstraction leaks** — the spec cannot fully model `.hsp`, so a `raw` escape
   hatch (`harness`, extra `slots`) and a documented set of round-trip residuals
   exist and keep needing patches. `trails` (0.5.3) is the latest field that had
   to be lifted out of `raw.harness` by hand.
3. **Maintenance burden** — `generate.py` (930 lines) plus `spec.py`, `patch.py`,
   `decompile.py`, and the model-id translation tables are a large compiler to
   carry.
4. **Agent ergonomics** — editing a real-device `.hsp` requires a spec to exist
   first (auto-decompile), a round-trip the agent shouldn't need.
5. **Per-operation latency** — every surgical edit re-runs the full compile.

## Core principle

**The `.hsp` file is the single source of truth. Its parsed JSON dict *is* the
in-memory model.** There is no rich object layer, no persisted spec, no sidecar.

Every operation is one of two things:

- **Edit** — `read_hsp` → mutate the dict in place → `write_hsp`.
- **Author** — clone the chassis template → replay a *transient, write-only*
  recipe as mutations → `write_hsp`.

**Headline win — `raw` disappears.** Its only purpose was round-trip fidelity
through a lossy spec. When mutations operate on the real dict in place, every
unmodeled field (`harness`, dual-cab `slot[1:]`, `xyctrl`, `sources`,
`clip`, `cursor`) is preserved *by construction* — the code never touches it.
Two-sources-of-truth and per-edit recompile latency vanish for the same reason.

## What `.hsp` actually is

8-byte ASCII magic `rpshnosj` followed by a UTF-8 JSON document. `read_hsp`
already strips 8 bytes and `json.loads` the rest. So `.hsp` *is* JSON — but the
device-native JSON is verbose: every value is wrapped (`{"value": x}`, stereo
`{"1":…,"2":…}`, controlled `{"controller":…,"value":x}`); footswitch/EXP/wah
controllers are integer **source bitfields** (`16843008`, `16908545`,
`0x01010500`) scattered across block harnesses and params; snapshots are sparse
8-element arrays; routing is a `b00..b13` scaffold. The domain logic that
computes all this does not disappear — it **moves from `generate.py` (build from
scratch) into `mutate.py` (mutate in place)**, where it is usually less code
because the scaffold already exists in the file.

## Module map (target state)

| Module | Fate |
|---|---|
| `hsp.py` | Keep. Add `write_hsp(path, body: dict)` (compact JSON + magic). Keep read, model-id translation, `extract_blocks_from_hsp`. |
| `chassis.py` | Keep. `extract_chassis_from_hsp` already yields the stripped-export template a new preset clones. |
| `mutate.py` (replaces `patch.py`) | **The heart.** Mutates a verbose `.hsp` body dict in place. Absorbs `generate.py`'s value-wrapping, snapshot densification, `controllers` bitfield wiring, and every invariant below. |
| `controllers.py` | Keep. Bitfield encode/decode, called by `mutate.py`. |
| `view.py` (replaces `decompile.py`) | Read-only projection `.hsp` body → today's readable spec shape. Lossy, **never persisted**. Powers `view`/`view_preset` and agent comprehension. |
| `recipe.py` (replaces most of `generate.py`) | `apply_recipe(chassis, recipe, library, irs) -> body`: clone chassis, replay the recipe (today's spec shape) as `mutate` calls. The only consumer of the recipe shape; write-only/transient. |
| `spec.py` | Slim to validation: param name/type/range + recipe-shape input validation. The `Spec`/`BlockEntry` dataclasses may survive as the recipe parser. |
| `ingest.py`, `library.py`, `ir.py`, `bootstrap.py`, `preferences.py` | Largely unaffected. `library` gains importance: `add_block` needs a verbose slot skeleton per model (see Open Question). |

## The mutation library (`mutate.py`)

Operates on a parsed `.hsp` body dict (`{"meta":…, "preset":{"flow":[…], …}}`).
Block addressing is by display name, disambiguated by `(path, lane, pos)` —
carried over verbatim from `patch.resolve_block`, but resolving to a `bNN` slot
in `preset.flow` instead of a spec index.

Operations (each mutates in place; validation via `spec.py` at call time):

- `set_param(body, block, param, value, *, coords)` — locate slot, coerce type
  (the `_coerce_param_value` float-vs-int guard), write into the correct wrapper
  (`{"value":…}` / stereo / preserve existing `controller`).
- `set_enabled(body, block, enabled, *, snapshot=None, coords)` — flip
  `bNN["@enabled"].value` at base, or the snapshot slot; densify the snapshot
  array (null→base); maintain `value == snapshots[activesnapshot]`.
- `add_block(body, model, *, path, after, params)` — allocate a `bNN` key,
  splice a verbose slot skeleton for `model`, set `position`/`path`/`type`,
  renumber positions.
- `remove_block(body, block, *, coords)` — delete the `bNN` key, renumber.
- `swap_model(body, old, new, library, *, coords)` — same-category replace,
  carry shared params, drop others with warnings (port `patch.swap_model`).
- `wire_footswitch(body, switch, block, behavior)` — compute the controller
  source bitfield (`controllers` + `_build_fs_controller`), attach to the
  block's `@enabled`, register in `sources`.
- `wire_expression(body, pedal, targets)` — bitfield into each target param's
  `controller` (`_build_exp_controller`), register in `sources`.
- `wire_wah_toe(body, block)` — `EXP1Toe` source `0x01010500` (0.5.1 behavior).
- `set_trails(body, block, trails)` — set `harness.params.Trails` (delay/reverb
  only); trivial now — no lift out of `raw` needed.
- `set_ir(body, block, ir, irs)` — inject the resolved `irhash` (port
  `_resolve_irhash`).
- `set_input(body, path, jack)` — per-path input routing endpoint rewrite
  (`_reshape_input_params` / `_rewrite_input_endpoint`).

## Invariants carried over (device-validated; do not regress)

- Dense snapshot arrays: `null → base` (sparse-snapshot bug, Category 4).
- `value == snapshots[activesnapshot]` (snapshot value==active invariant, 0.5.1).
- Block base bypass lives at `bNN @enabled` level (Category 5).
- Dual-cab `slot[1:]` and `harness` preserved verbatim (now free — never touched).
- Wah toe-switch `EXP1Toe` source `0x01010500` (0.5.1).
- Float-vs-int param coercion (silent-block guard).
- IR-hash resolution priority: explicit `ir` > canonical block default > error.
- Model-id namespace translation on write (`translate_to_hsp`).

## Data flow

- **Author:** transient recipe → `apply_recipe` onto chassis clone → `write_hsp`.
- **Edit:** `read_hsp` → `mutate.*` in place → `write_hsp`.
- **Inspect:** `read_hsp` → `view` → readable projection (display only; optional
  non-authoritative `-o` dump for humans).

## CLI (target)

- `generate <recipe.json> -o out.hsp` — recipe is transient input; **no sidecar
  written**.
- `view <preset.hsp> [-o spec.json]` — replaces `decompile`; `-o` dump is
  explicitly non-authoritative.
- `set-param / enable / disable / add-block / remove-block / swap-model
  <preset.hsp> …` — mutate the `.hsp` directly, in place; no sidecar, no
  recompile.
- Unchanged: `show-block`, `list-blocks`, `ingest`, `register-irs`, `ir-scan`,
  `list-irs`.
- Removed: the sidecar concept; `decompile` as a persisted-spec step (survives
  as `view`).

## MCP (target)

- `generate_preset(model, recipe) -> hsp_b64`.
- `view_preset(model, hsp_b64) -> readable dict` (replaces `decompile_preset`).
- `patch_preset(model, hsp_b64, operations) -> hsp_b64` — applies ops directly
  to the `.hsp` blob. **The agent edit loop collapses from
  `decompile → patch → generate` to just `patch`.**

## Testing strategy (big-bang, TDD, with a migration safety net)

A big-bang rewrite changes the tests' shape (spec-assertions → hsp-assertions),
which would remove the regression net that protects device-validated behavior.
Mitigation:

1. **Golden-output contract (build FIRST, before touching any core module).**
   Capture, from the *current* pipeline on `main`, the exact `.hsp` bytes for a
   corpus of specs (the existing fixtures + real-export round-trips). Store them
   as golden files. The rewrite must reproduce each golden `.hsp`
   byte-for-byte (or, where compaction differs, semantically via parsed-dict
   equality). This net is independent of internal test shape and survives the
   rewrite.
2. **`mutate.py` built TDD-first** against golden real-export fixtures,
   including a *"mutate one param, assert only that param's bytes changed"* test
   that proves minimal in-place mutation and the death of `raw`.
3. Rewrite the suite by area: `generate`→`recipe`, `patch`→`mutate`,
   `decompile`→`view`, MCP tools. Round-trip tests become near-trivial identity.
4. Full suite green + golden-output parity before release.

## Open question for the plan

`add_block` needs a verbose slot skeleton per model. Source is the `library`
(built from ingested real exports). Confirm the library stores enough verbose
structure to splice a valid `bNN` slot, or derive the skeleton from a stored
real block instance / a per-category template. Resolve in Phase 1 before
`add_block` is implemented.

## Fan-out plan (sub-agent phases)

Dependency-ordered; interfaces frozen by this spec before Phase 2 parallelizes.

- **Phase 0 (serial, me):** golden-output capture harness + corpus; `write_hsp`.
- **Phase 1 (serial, one agent):** `mutate.py` foundation + `resolve_block` on
  `.hsp`; resolve the slot-skeleton open question. Review gate.
- **Phase 2 (parallel):** `recipe.py` · `view.py` · CLI migration · MCP
  migration — each its own area, interfaces frozen.
- **Phase 3 (serial, me):** integration, full-suite green, golden parity,
  delete dead code (`patch.py`, spec-compile paths, sidecar logic, `raw`).
- **Phase 4:** **major** version bump (0.5.3 → **1.0.0**), CLAUDE.md update, PR
  to `main`, release via the CI workflow; then return to `main`.

## Out of scope

- Legacy `.hlx` (original Helix) authoring beyond what current tests assert.
  The `.hlx` compile path in `generate.py` (`_compose_preset_hlx`) is retained
  as-is behind the recipe front-end unless a test forces otherwise.
- No new tone/setup-skill features; the skills keep emitting the recipe shape.
