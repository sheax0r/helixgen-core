# `.hsp`-canonical Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `.hsp` file the single source of truth — mutate it in place for edits, build it by replaying a transient recipe onto a chassis for authoring — eliminating the persisted spec/sidecar and the `raw` escape hatch.

**Architecture:** The parsed `.hsp` body dict *is* the model. `mutate.py` mutates that verbose dict in place (absorbing `generate.py`'s wrapping / snapshot / controller logic); `recipe.py` authors by cloning the chassis and replaying the recipe shape as `mutate` calls; `view.py` projects `.hsp` → readable shape read-only. A golden-output contract captured from the pre-rewrite pipeline guards semantic parity.

**Tech Stack:** Python 3, stdlib + `click`; `pytest` + `testify`-style asserts; MCP SDK for `mcp_server`.

## Global Constraints

- Pure stdlib + `click` runtime deps only; `mcp` SDK + `click` for the server.
- Run tests with `PYTHONPATH=$PWD/src python -m pytest` (editable-install shadow guard).
- Every device-validated invariant in the spec's "Invariants carried over" list must hold; the golden-output contract is the enforcement.
- `.hsp` output stays **compact** JSON (device reads compact) with the 8-byte `rpshnosj` magic.
- Block addressing: display name disambiguated by `(path, lane, pos)`, matching `patch.resolve_block` semantics.
- Version target: **1.0.0** (major bump from 0.5.3).
- Commit after every green step; never leave the suite red between tasks.

---

## Phase 0 — Safety net & I/O primitive (serial)

### Task 0a: `write_hsp` round-trip primitive

**Files:**
- Modify: `src/helixgen/hsp.py`
- Test: `tests/test_hsp.py`

**Interfaces:**
- Produces: `write_hsp(path: Path | str, body: dict) -> None` — serialize `body` as compact UTF-8 JSON (`json.dumps(body, separators=(",", ":"), ensure_ascii=False)`), prepend `HSP_MAGIC`, write bytes. `dumps_hsp(body: dict) -> bytes` helper returns the same bytes without writing (used by MCP + golden tests).

- [ ] **Step 1:** Write a failing test: read a real fixture `.hsp` with `read_hsp`, `write_hsp` it to a temp path, `read_hsp` back, assert the two parsed dicts are equal (`assert reloaded == original`).
- [ ] **Step 2:** Run it; expect `AttributeError: module ... has no attribute 'write_hsp'`.
- [ ] **Step 3:** Implement `dumps_hsp` + `write_hsp` in `hsp.py`.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit `feat(hsp): add write_hsp/dumps_hsp round-trip primitive`.

### Task 0b: Golden-output contract harness

**Files:**
- Create: `tests/golden/conftest.py` (corpus loader), `tests/golden/README.md`
- Create: `tests/golden/capture_golden.py` (one-shot capture script, run on pre-rewrite code)
- Create: `tests/golden/corpus/` (committed `.hsp` goldens + their source recipes)

**Interfaces:**
- Produces: a pytest `test_golden_parity` that, for each `(recipe.json, expected.hsp)` in the corpus, runs the **current** authoring path and asserts the produced bytes' parsed dict equals the golden's parsed dict.

- [ ] **Step 1:** Build the corpus: pick every spec fixture already under `tests/fixtures/` plus 3–5 real-export round-trips from `data/*.hsp` (decompile→regenerate on current code). For each, capture the current `generate` output bytes into `tests/golden/corpus/<name>.hsp` and copy its spec into `tests/golden/corpus/<name>.recipe.json`. Do this on the **current** `main`-equivalent pipeline (still present at this point).
- [ ] **Step 2:** Write `test_golden_parity` parametrized over the corpus; it must PASS now (identity against the current pipeline).
- [ ] **Step 3:** Run; expect PASS (this pins current behavior).
- [ ] **Step 4:** Commit `test: golden-output contract pinning current .hsp bytes`.

> This test stays green through the whole rewrite by pointing at the eventual `recipe.apply_recipe`. When the authoring entry point moves (Task 3), update only the harness's call site, never the goldens.

---

## Phase 1 — `mutate.py` foundation (serial, ONE agent, review gate)

### Task 1a: Resolve the slot-skeleton open question

**Files:** Read-only investigation; record the answer in `tests/golden/README.md`.

- [ ] **Step 1:** Determine how `add_block` obtains a verbose `bNN` slot skeleton for a model. Check `library.Block` for stored verbose structure; if absent, decide: (a) store a canonical exemplar slot per model at ingest, or (b) synthesize from `Block.params` defaults + a per-category `type`/`harness` template derived from `generate._to_hsp_bnn`. Prefer (b) — reuse the existing `_to_hsp_bnn` logic being ported into `mutate`.
- [ ] **Step 2:** Write the decision (one paragraph) into the golden README; no code yet.

### Task 1b: `resolve_block` on `.hsp` body

**Files:**
- Create: `src/helixgen/mutate.py`
- Test: `tests/test_mutate.py`

**Interfaces:**
- Produces: `class MutateError(ValueError)`; `resolve_slot(body, name, *, path=None, lane=None, pos=None) -> tuple[int, str, int]` returning `(flow_index, bnn_key, slot_index)`. Name matching uses `library.humanize`/display-name of the slot model; disambiguation mirrors `patch.resolve_block` (raises on ambiguity with the placed-block list).

- [ ] **Step 1:** Failing test: load a fixture `.hsp`, `resolve_slot(body, "<known block>")` returns the correct `(flow_index, bnn_key, 0)`; ambiguous name raises `MutateError`; missing name raises `MutateError` listing placed blocks.
- [ ] **Step 2:** Run; expect import/attribute failure.
- [ ] **Step 3:** Implement `resolve_slot` (walk `preset.flow[*]` `bNN` keys, skip `b00/b13` + `P35_` chassis models, match display name via `Library`).
- [ ] **Step 4:** Run; PASS.
- [ ] **Step 5:** Commit `feat(mutate): resolve_slot block addressing on .hsp body`.

### Task 1c: `set_param` in place

**Interfaces:**
- Consumes: `resolve_slot`.
- Produces: `set_param(body, block, param, value, library, *, path=None, lane=None, pos=None) -> None` — validate the name against `library` (`generate.validate_params`), coerce type (`generate._coerce_param_value`), write into the value wrapper preserving any existing `controller` and stereo `{"1":…,"2":…}` shape.

- [ ] **Step 1:** Failing tests: (a) setting a mono float param updates `slot.params.NAME.value` and nothing else; (b) an unknown param raises `MutateError`/`ParamValidationError`; (c) a param that currently has a `controller` keeps the controller and only updates `value`; (d) a stereo param updates both channels; (e) int passed to a float-schema param is coerced to float.
- [ ] **Step 2:** Run; fail.
- [ ] **Step 3:** Implement, porting `validate_params` + `_coerce_param_value` + the stereo/controller-aware wrapper writer.
- [ ] **Step 4:** Run; PASS. Add a golden micro-test: mutate one param on a real export, assert the parsed dict differs at exactly that one path and `raw`-style fields are byte-identical.
- [ ] **Step 5:** Commit `feat(mutate): set_param with type coercion + wrapper preservation`.

### Task 1d: `set_enabled` + snapshot invariants

**Interfaces:**
- Produces: `set_enabled(body, block, enabled, *, snapshot=None, path=None, lane=None, pos=None) -> None`. Base: set `bNN["@enabled"].value`. Snapshot: flip that snapshot's slot in `bNN["@enabled"].snapshots`; densify the 8-element array (null→base); maintain `value == snapshots[activesnapshot]` (read `preset` activesnapshot).

- [ ] **Step 1:** Failing tests: (a) base disable flips `@enabled.value` to False; (b) snapshot disable sets the right array index and densifies nulls to the base value; (c) after any snapshot edit, `@enabled.value == snapshots[active]`; (d) base bypass lives at `bNN @enabled`, not inside `slot`.
- [ ] **Step 2–4:** Fail → implement (port `_wrap_value_with_snapshots` densification + the value==active invariant from 0.5.1) → PASS.
- [ ] **Step 5:** Commit `feat(mutate): set_enabled honoring snapshot density + value==active`.

### Task 1e: `add_block` / `remove_block`

**Interfaces:**
- Produces: `add_block(body, model, library, *, path=0, after=None, params=None) -> str` (returns the new `bNN` key); `remove_block(body, block, *, path=None, lane=None, pos=None) -> None`. `add_block` allocates the next free `bNN` between `b01..b12`, splices the slot skeleton (Task 1a decision), sets `type`/`position`/`path`, renumbers `position`. `remove_block` deletes the `bNN` and renumbers.

- [ ] **Step 1:** Failing tests: add a drive block to path 0 → appears as a new `bNN` with correct model, `type`, sequential `position`; add `after="<name>"` inserts at the right position; remove deletes it and renumbers; round-trips through `write_hsp`/`read_hsp`.
- [ ] **Step 2–4:** Fail → implement (port `_to_hsp_bnn`, `_hsp_type_for_block`, `_assign_positions`) → PASS.
- [ ] **Step 5:** Commit `feat(mutate): add_block/remove_block with position renumbering`.

### Task 1f: `swap_model`, `set_ir`, `set_trails`, `set_input`

**Interfaces:**
- Produces: `swap_model(body, old, new, library, *, coords…) -> list[str]` (warnings); `set_ir(body, block, ir, irs, *, coords…) -> None`; `set_trails(body, block, trails: bool, library, *, coords…) -> None`; `set_input(body, path, jack: str) -> None`.

- [ ] **Step 1:** Failing tests per verb: swap carries shared params + warns on dropped (port `patch.swap_model` semantics onto slots); `set_ir` injects the resolved `irhash` (port `_resolve_irhash`, basename + hash forms); `set_trails` sets `harness.params.Trails` and rejects non-delay/reverb; `set_input` rewrites the path input endpoint (`_rewrite_input_endpoint`).
- [ ] **Step 2–4:** Fail → implement → PASS.
- [ ] **Step 5:** Commit `feat(mutate): swap_model/set_ir/set_trails/set_input`.

### Task 1g: controller wiring — `wire_footswitch` / `wire_expression` / `wire_wah_toe`

**Interfaces:**
- Produces: `wire_footswitch(body, switch, block, behavior, library) -> None`; `wire_expression(body, pedal, targets, library) -> None`; `wire_wah_toe(body, block, library) -> None`. Port `_build_fs_controller`, `_build_exp_controller`, and the `EXP1Toe` source `0x01010500` path; register in `preset.sources`.

- [ ] **Step 1:** Failing tests: FS assignment writes the `targetbypass` controller with the right `source` bitfield onto the block `@enabled` and a `sources` entry; EXP target writes a `param` controller with `min/max`; `EXP1Toe` uses source `0x01010500`; double-assignment of one switch or one `(block,param)` pair raises.
- [ ] **Step 2–4:** Fail → implement (port from `generate` + `controllers.py`) → PASS.
- [ ] **Step 5:** Commit `feat(mutate): footswitch/expression/wah-toe controller wiring`.

**REVIEW GATE:** Full `pytest tests/test_mutate.py` green; golden micro-tests green. Human/orchestrator review before Phase 2 parallelizes.

---

## Phase 2 — consumers (parallel; interfaces frozen by Phase 1)

### Task 2a: `recipe.py` authoring front-end

**Files:**
- Create: `src/helixgen/recipe.py`
- Modify: `src/helixgen/generate.py` (retain `.hlx` path + shared helpers; delete the `.hsp` compose path once parity holds)
- Test: `tests/test_recipe.py`

**Interfaces:**
- Consumes: all of `mutate.*`, `chassis.extract_chassis_from_hsp`, `spec.parse_spec`.
- Produces: `apply_recipe(recipe: dict, library, *, chassis: dict, irs=None) -> dict` (returns an `.hsp` body); `generate_from_recipe(recipe, library, *, irs, chassis) -> bytes`.

- [ ] **Step 1:** Failing test: a minimal recipe (name + one path + two blocks) → `apply_recipe` → body whose `flow` contains the two blocks in order with defaults. Then the **golden corpus** test repointed at `apply_recipe` must reproduce every golden.
- [ ] **Step 2:** Run; fail.
- [ ] **Step 3:** Implement: clone chassis, iterate recipe paths/blocks calling `mutate.add_block` + `set_param`; apply snapshots (`set_enabled`), footswitches, expression, input, trails, ir via the corresponding `mutate` verbs. Set `meta`/provenance (port `_provenance`).
- [ ] **Step 4:** Run `test_recipe.py` + `tests/golden/` ; expect PASS (golden parity is the acceptance bar).
- [ ] **Step 5:** Commit `feat(recipe): author .hsp by replaying recipe onto chassis`.

### Task 2b: `view.py` read-only projection

**Files:**
- Create: `src/helixgen/view.py` (port `decompile.py` logic)
- Delete (Phase 3): `src/helixgen/decompile.py`
- Test: `tests/test_view.py`

**Interfaces:**
- Produces: `view(body: dict, library, *, irs=None) -> dict` (readable recipe-shape projection; **no file write**).

- [ ] **Step 1:** Failing test: `view(read_hsp(fixture))` returns a dict with `name`, `paths[*].blocks[*].block`, recovered snapshots/footswitches/expression — reuse the existing `decompile` assertions, minus any sidecar-writing behavior.
- [ ] **Step 2–4:** Fail → port `decompile_body` as `view` (drop the `-o`/sidecar write path) → PASS.
- [ ] **Step 5:** Commit `feat(view): read-only .hsp → recipe-shape projection`.

### Task 2c: CLI migration

**Files:**
- Modify: `src/helixgen/cli.py`
- Test: `tests/test_cli*.py`

**Interfaces:**
- Consumes: `recipe.generate_from_recipe`, `view.view`, all `mutate.*`, `hsp.write_hsp`.

- [ ] **Step 1:** Failing tests (via `click.testing.CliRunner`): `generate recipe.json -o out.hsp` writes a valid `.hsp` and **no** `.spec.json` sidecar; `set-param out.hsp <block> <param> <val>` mutates the `.hsp` in place (reload shows the change) with no sidecar; `view out.hsp` prints the projection; `decompile` removed / aliased to `view`.
- [ ] **Step 2–4:** Fail → rewrite the edit verbs to `read_hsp → mutate.* → write_hsp`; rewrite `generate` to load recipe → `generate_from_recipe` → write; replace `decompile` with `view` → PASS.
- [ ] **Step 5:** Commit `feat(cli): direct .hsp editing, recipe authoring, view`.

### Task 2d: MCP migration

**Files:**
- Modify: `mcp_server/tools.py`, `mcp_server/server.py`
- Test: `tests/mcp_server/test_tools.py`, `tests/mcp_server/test_patch_tools.py`

**Interfaces:**
- Produces: `generate_preset(model, recipe) -> hsp_b64`; `view_preset(model, hsp_b64) -> dict`; `patch_preset(model, hsp_b64, operations) -> hsp_b64` (operations apply via `mutate.*` directly on the decoded body).

- [ ] **Step 1:** Failing tests: `patch_preset` applies `set_param`/`set_enabled`/`add_block`/`remove_block`/`swap_model` to a base64 `.hsp` and returns a new base64 `.hsp` reflecting the change (no spec round-trip); `view_preset` returns the projection; `generate_preset` builds from a recipe.
- [ ] **Step 2–4:** Fail → rewrite tools to operate on the `.hsp` blob; map each op name to a `mutate` verb → PASS.
- [ ] **Step 5:** Commit `feat(mcp): patch/view/generate operate directly on .hsp blobs`.

---

## Phase 3 — integration & dead-code removal (serial)

### Task 3a: Delete the intermediary

**Files:**
- Delete: `src/helixgen/patch.py`, `src/helixgen/decompile.py`, the `.hsp` compose path in `generate.py`, all sidecar (`.spec.json`) logic, the `raw` field handling across `spec.py`/`recipe`/`view`.
- Modify: any remaining imports.

- [ ] **Step 1:** Grep for `raw`, `spec.json`, `sidecar`, `decompile`, `patch` across `src/` and `mcp_server/`; remove each dead reference.
- [ ] **Step 2:** Run the FULL suite `PYTHONPATH=$PWD/src python -m pytest -q`; fix fallout until green.
- [ ] **Step 3:** Run `tests/golden/` ; confirm byte/dict parity holds end-to-end.
- [ ] **Step 4:** Commit `refactor: remove spec sidecar, patch.py, decompile.py, raw field`.

### Task 3b: Full-suite green + verification

- [ ] **Step 1:** `PYTHONPATH=$PWD/src python -m pytest -q` → all green.
- [ ] **Step 2:** End-to-end smoke: author a recipe → `.hsp`; edit a param in place; `view` it; confirm the edited value; confirm no sidecar exists. Record output.
- [ ] **Step 3:** Commit any fixes `test: full-suite + e2e green on .hsp-canonical`.

---

## Phase 4 — release (serial)

### Task 4: Version bump, docs, PR, release

**Files:**
- Modify: `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` (both → `1.0.0`), `pyproject.toml` + `src/helixgen/__init__.py` (lib version line), `CLAUDE.md` (spec-shape → recipe/direct-edit language; drop `raw`/sidecar docs).

- [ ] **Step 1:** Bump both plugin manifests to `1.0.0` (they must agree or CI fails). Bump the lib version line.
- [ ] **Step 2:** Update `CLAUDE.md`: reframe "spec.json shape" as the transient recipe; document direct `.hsp` editing (no sidecar); remove the `raw` section; rename `decompile` → `view`.
- [ ] **Step 3:** Full suite green one more time.
- [ ] **Step 4:** Commit `release 1.0.0 — .hsp-canonical redesign`; push branch; open PR to `main`.
- [ ] **Step 5:** Merge to `main`; the release workflow auto-tags `helixgen--v1.0.0` and fast-forwards `stable`. Do NOT move `stable`/tags by hand.
- [ ] **Step 6:** Return to `main`, `git pull --ff-only`.

## Self-review notes

- Spec coverage: every module in the spec's table maps to a task (hsp→0a, mutate→1b–1g, recipe→2a, view→2b, cli→2c, mcp→2d, deletion→3a, release→4). Invariants → 1c–1g tests + golden contract. Open question → 1a.
- The golden contract (0b) is the parity enforcement that lets the big-bang proceed safely; it is repointed once (2a) and otherwise immutable.
- `.hlx` legacy path is retained inside `generate.py` behind the recipe front-end (spec "Out of scope").
