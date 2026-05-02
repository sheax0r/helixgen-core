# helixgen — handoff

Self-contained context for a fresh agent picking up this project. Written 2026-05-02 after compaction-prone session left v1 mostly built but mid-pivot on a wire-format reality check.

---

## 1. Mission

Build a **Helix Preset Generator** — two layers:

- **Layer 1 (this repo, in progress):** a deterministic Python CLI that compiles a strict JSON tone spec into a Line 6 Helix `.hlx` preset file. Also ingests real preset exports to build up a reusable library of block schemas (model IDs, param keys, types, observed value ranges).
- **Layer 2 (deferred, not started):** a Claude skill that talks to the user about guitars / target tones / songs / artists, and emits the JSON spec format Layer 1 consumes. Layer 2 lives outside this repo and is not in scope for the current rewrite.

The user owns a **Helix Stadium**. Stadium can *import* `.hlx` files but only *exports* `.hsp` files. So:

- **Generate target:** `.hlx` (importable into Stadium *and* original Helix devices — more portable).
- **Ingest sources:** primarily `.hsp` (what the user has 206 of); also `.hlx` (for the [phelix](https://github.com/sensorium/phelix) seed bootstrap and miscellaneous exports the user finds online).

---

## 2. Decisions already made — DO NOT RE-LITIGATE

These were settled in brainstorming with the user. Don't ask again.

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | user's preference |
| Spec format | strict JSON | user values strictness; rejects YAML for v1 |
| CLI framework | `click` | user picked |
| Test framework | `pytest` | user picked |
| Param values in spec | wire values only (0–1 floats, Hz ints, enum strings). **No** display 0–10 translation in v1 | scope reduction |
| Parallel A/B routing | **deferred** to a later feature. v1 is single serial chain per DSP | scope reduction; see `docs/features/parallel-paths.md` |
| Snapshots | not in v1 user-facing scope, but chassis must preserve them | see §6 |
| Workflow | feature branch `v1-implementation`, conventional commits, no worktrees | user picked |
| TDD | yes — write failing test, run, implement, run, commit | user picked Subagent-Driven Development; reviewer subagents on Sonnet |
| Library accretes over time | yes — user shouldn't have to re-export the same blocks | core requirement |
| Bulk ingest | yes — point at a directory, recurses | core requirement |

---

## 3. Architecture

### 3.1 The library

`~/.helixgen/library/` (override with `--library` or `HELIXGEN_LIBRARY` env). Contents:

```
~/.helixgen/library/
├── blocks/
│   ├── amp/
│   │   └── HD2_AmpBrit2204.json        # one file per block model
│   ├── cab/
│   ├── drive/
│   ├── reverb/
│   └── …
├── chassis/
│   └── chassis.json                    # captured "empty preset" template
└── index.json                          # display-name → file path
```

Each block file holds: `model_id`, `category`, `display_name`, `params` (schema: type + default + observed_range), an `exemplar` (full raw block dict — used as overlay during generate), and `first_seen` provenance.

### 3.2 The chassis pattern

Real `.hlx` exports contain a lot of scaffolding that isn't user-block content: `inputA/inputB`, `outputA/outputB`, `split`, `join`, `global` (Variax/tempo), `snapshot0..snapshot7`, `footswitch`, `controller`. We can't synthesize all of this from scratch — it has device-version stamps, controller maps, etc.

So on the **first** preset ingest, we extract a "chassis": deep-copy the preset, blank out the user-placeable block slots, and save it. On generate, we deep-copy the chassis, drop user-resolved blocks into the position slots, deep-merge their exemplar params, set meta name/author/provenance, and write.

### 3.3 The pipeline

**ingest:** detect shape (full preset vs single block) → extract block dicts → for each: derive `model_id`, `category`, `display_name`, schema → dedup against library (NEW / MATCH / CONFLICT — conflict writes `<model_id>.v2.json`) → on first full preset, also save chassis.

**generate:** parse JSON spec → resolve each block by `block` field (display name OR model_id) against library → validate user params against block schema → compose: copy chassis, place blocks into slots, overlay exemplar, set meta → write `.hlx`.

---

## 4. State of the code

### 4.1 What's done (T01–T33, T35)

- `src/helixgen/library.py` — `Block` dataclass, `Library` class with save/load/find/list/dedup/chassis, `IngestStatus` enum
- `src/helixgen/ingest.py` — shape detection, schema extraction, model-ID humanization, category inference, file/directory ingest with chassis capture
- `src/helixgen/chassis.py` — `extract_chassis(preset)` — deep-copies preset, captures position keys, clears block dicts
- `src/helixgen/spec.py` — `Spec`/`PathEntry`/`BlockEntry` dataclasses, `parse_spec()` validator (rejects parallel entries with pointer to deferred feature doc)
- `src/helixgen/generate.py` — `resolve_blocks` / `validate_params` / `compose_preset` / `generate_preset`
- `src/helixgen/bootstrap.py` — `clone_or_pull_phelix(cache_dir)` + `bootstrap(library)` to seed from sensorium/phelix
- `src/helixgen/cli.py` — `click` group with subcommands `ingest`, `generate`, `list-blocks`, `show-block`, `bootstrap`
- 113 tests, all passing against synthetic fixtures
- `tests/fixtures/presets/sample_serial.json` — the synthetic 5-block fixture
- `tests/fixtures/specs/goldfinger.json` — canonical "Goldfinger Superman Rhythm" tone spec
- `README.md`, `docs/superpowers/specs/2026-05-01-helix-preset-generator-design.md` (the v1 design), `docs/superpowers/plans/2026-05-01-helix-preset-generator.md` (35-task plan), `docs/features/parallel-paths.md` (deferred feature note)

CLI smoke-tests cleanly:
```
helixgen ingest tests/fixtures/presets/sample_serial.json   → +5 new blocks, chassis extracted
helixgen list-blocks                                          → 5 blocks grouped by category
helixgen show-block "Brit 2204 Custom"                        → schema with params/types/defaults
helixgen generate tests/fixtures/specs/goldfinger.json -o /tmp/goldfinger.hlx
```

26 commits on branch `v1-implementation`, branched from `main`.

### 4.2 What's wrong with what's done

**The synthetic `sample_serial.json` was a guess at the `.hlx` shape and is wrong in three concrete ways** (discovered today by inspecting the user's first real `Possum.hlx`):

1. **Block keys are `block0`, `block1`, ...** — not `dsp0_block_0`. They live as direct children of `data.tone.dsp0`, mixed in with infrastructure (`inputA`, `inputB`, `outputA`, `outputB`, `split`, `join`, `cab0`, `global`, `snapshot0..7`, `footswitch`, `controller`).

2. **Cabs are not chained as blocks.** They live as sibling keys (`cab0`, `cab1`) and are linked from the amp block via `"@cab": "cab0"`. So an amp+cab is one logical placement that writes two siblings.

3. **Real param keys are camelCase / no-space:** `ChVol`, `LowCut`, `HighCut`, `EarlyReflections`, `BiasX`. The synthetic fixture's `"Ch Vol"` (with space) won't match. Spec input either accepts the wire keys directly or builds a display-name → wire-key map.

**Bonus surprise:** Noise Gate isn't a separate block in `.hlx` at all — it's three params on `inputA`: `noiseGate: bool, threshold: int, decay: float`. The current Goldfinger spec lists Noise Gate as a block — that won't survive the rewrite as-is.

So the v1 architecture (library + chassis + spec + compose) is **conceptually correct**, but the wire-format adapter at the bottom (the `RAW_BLOCK_*` and `PRESET_*` constants in `src/helixgen/ingest.py`, and the slot-placement logic in `src/helixgen/generate.py:compose_preset`) needs to be rewritten against real exports. The synthetic fixture should be replaced. Tests will need to be updated.

### 4.3 What's not done (T34)

The original plan's T34 was "real-export validation pass." That's now expanded into the pivot work in §5.

---

## 5. The reconciliation work — concrete next steps

### 5.1 What we know about the wire formats

**`.hlx` (canonical generate target):**
- Pure JSON, no header
- Top: `{ version: 6, schema: "L6Preset", data: { device, device_version, meta, tone } }`
- `data.meta`: `{ name, author, song, band, application, appversion, modifieddate, build_sha, tnid }`
- `data.tone.dsp0` and `data.tone.dsp1` are sibling-key dicts. User blocks under keys `block0`, `block1`, ...; cabs under `cab0`, `cab1`; infrastructure under `inputA`, `inputB`, `outputA`, `outputB`, `split`, `join`. Amps have `"@cab": "cab0"` link.
- Each block: `@model`, `@enabled`, `@position`, `@path`, `@type` (numeric category), plus param keys at top level (no `{value: …}` wrapping)
- `data.tone.snapshot0..snapshot7` siblings of dsp0/dsp1. `snapshot0` carries the active block params; 1–7 carry only metadata + variation deltas. Out of v1 scope but chassis preserves them.
- `data.tone.global` holds Variax / tempo / topology — preserve via chassis.
- `data.tone.footswitch` and `data.tone.controller` — preserve via chassis.

**`.hsp` (Helix Stadium export — ingest only, never our output):**
- 8-byte ASCII magic header `rpshnosj` then JSON. Skip 8 bytes when reading.
- Top: `{ meta: {name, …}, preset: { flow, params, snapshots, sources, cursor } }`
- `preset.flow` is a length-2 list: `flow[0]` = path 0, `flow[1]` = path 1. Each is a dict keyed `b00..b13` (b00 = input endpoint, b13 = output endpoint, b01..b12 = user blocks).
- Each block: `{ slot: [{model, params, version, @enabled}, ...], type, position, path, harness, @enabled, favorite }`. **Slots are arrays.** Cabs are dual-slot (stereo cab + a "no-cab" pairing), most other blocks single-slot.
- Param values are wrapped: `{"value": 0.62}`. Stereo: `{"1": {"value": ...}, "2": {"value": ...}}`. Controlled: `{"controller": {...}, "value": ...}`.
- Snapshots are first-class (`preset.snapshots` × 8). `preset.sources` maps controller IDs.
- Model ID prefixes: `HD2_*` for FX/amp/cab, `P35_*` for input/output endpoints (Stadium-specific).

**Model-ID translation between formats (sample, from one inspection):**
| `.hsp` (Stadium) | `.hlx` (older/portable) |
|---|---|
| `HD2_AmpBrit2204` | `HD2_AmpBrit2204` (same) |
| `HD2_DistCompulsiveDriveMono` | `HD2_DistCompulsiveDrive` (Mono suffix only on Stadium) |
| `HD2_CabMicIr_4x121960AT75WithPan` | `HD2_Cab4x121960T75` (naming diverges) |
| `HD2_VolPanVolMono` | `HD2_VolPanVol` |

Stadium auto-translates on import, so **emit `.hlx`-namespace IDs.** Build a small `.hsp → .hlx` model-ID normalizer for ingest.

### 5.2 Available ground truth

- `data/Possum.hlx` (1 file, 3.8 KB) — pure `.hlx`. Single dsp0 path: 1 distortion (`HD2_DistCompulsiveDrive`, bypassed) + 1 amp (`HD2_AmpBrit2204`) linked to cab `HD2_Cab4x121960T75` + 1 vol pedal (`HD2_VolPanVol`). dsp1 is empty infrastructure.
- `data/*.hsp` (206 files) — full breadth of user's collection. `data/Goldfinger.hsp` is present and aligns with the canonical Goldfinger Superman Rhythm test target. `data/` is gitignored.
- `https://github.com/sensorium/phelix` — `helixgen bootstrap` already wired to clone + ingest. Likely contains many more `.hlx` examples we haven't inspected yet.

### 5.3 Recommended task sequence

The architecture survives; the wire-format adapter changes. Approach as a focused rewrite, TDD as before.

**Step A — Recon phelix (cheap, high info-value).** Run `helixgen bootstrap` (or just `git clone https://github.com/sensorium/phelix /tmp/phelix-src`), find the actual `.hlx` files in there, and skim 5–10 to confirm the shape we observed in `Possum.hlx` is canonical, not a one-off. Pay attention to: Plate Reverb shape, delay/mod block shapes, and any preset that actually uses dsp1.

**Step B — Ingest one `.hsp` end-to-end.** Write a minimal `.hsp` reader (strip 8-byte magic, parse, walk `flow[0..1].b01..b12`, unwrap `{value: ...}`, normalize model IDs `.hsp → .hlx` via a small mapping table, drop controllers/snapshots into the chassis side). Try it on `data/Goldfinger.hsp` and `data/Possum (1).hsp`. Confirm the library accretes sensible block files.

**Step C — Bulk ingest all 206 `.hsp` exports.** Verify dedup behavior, look at conflict outputs, sanity-check the model namespace coverage. This gives the user a real working library.

**Step D — Rewrite generate against real `.hlx`.**
- Replace `tests/fixtures/presets/sample_serial.json` with a fixture derived from `data/Possum.hlx` (or another real one).
- Update `compose_preset` for the three structural corrections in §4.2: block keys (`block0`, `block1`, ...), cab linkage via `@cab`, real param key names.
- Update Noise Gate handling: it's `inputA` params, not a block. Spec entry `{"block": "Noise Gate", "params": {...}}` should compile to `inputA.noiseGate=true`, `inputA.threshold=...`, `inputA.decay=...`. Or revisit whether Noise Gate stays in the spec as a block.
- Update `tests/fixtures/specs/goldfinger.json` to use real param keys; verify the Brit 2204 / 4x12 / Tube Screamer / Plate Reverb chain compiles to a `.hlx` Stadium will accept.

**Step E — User-driven validation.** User imports the generated `.hlx` into Stadium, confirms it loads and sounds approximately right. This is the final acceptance criterion. Iterate on any model IDs / param keys Stadium rejects.

**Step F — Optional: ingest support for `.hlx` too.** Already partially supported; tighten up so phelix bootstrap can run cleanly.

After all that, v1 is done and Layer 2 (the skill) can begin.

### 5.4 Things to watch out for

- **Don't lose the architecture.** Library + chassis + spec + compose are correct. Only the wire-format layer needs to change.
- **Tests will need adjusting** for new block keys, fixture changes, model IDs. Don't delete tests wholesale — adapt them.
- **Round-trip integration test (`tests/test_roundtrip.py`)** is currently the strongest safety net; keep it green through the rewrite.
- **The `regex` in `humanize_model_id`** has an `(?<![x])` exception to keep cab sizes like "4x12" together. Don't simplify it without checking the parametric tests.
- **Avoid `git add -A`.** There's already history of accidentally committing `.claude/`. Stage explicitly. Both `.claude/` and `data/` are in `.gitignore` already.
- **Param key naming policy is a real design choice for v1.** Either: (a) spec accepts only wire keys exactly as they appear in `.hlx` (`ChVol`, `LowCut`); or (b) library stores both wire key + display name and accepts either in spec. (b) is friendlier but bigger. (a) is safer and shippable; the Layer 2 skill handles humanization. Recommend (a) for v1.

---

## 6. Repo orientation

```
helixgen/
├── README.md                              quickstart + spec format
├── pyproject.toml                         Python project config
├── input.md                               original user prompt that kicked off the project
├── docs/
│   ├── handoff.md                         this file
│   ├── superpowers/
│   │   ├── specs/2026-05-01-helix-preset-generator-design.md   ★ approved v1 design
│   │   └── plans/2026-05-01-helix-preset-generator.md          ★ 35-task TDD plan
│   └── features/
│       └── parallel-paths.md              deferred feature: parallel A/B routing
├── src/helixgen/
│   ├── __init__.py
│   ├── library.py
│   ├── ingest.py
│   ├── chassis.py
│   ├── spec.py
│   ├── generate.py
│   ├── bootstrap.py
│   └── cli.py
├── tests/
│   ├── conftest.py                        fixtures: tmp_library, sample_amp_block, sample_cab_block, sample_serial_preset
│   ├── test_library.py
│   ├── test_ingest.py
│   ├── test_chassis.py
│   ├── test_spec.py
│   ├── test_generate.py
│   ├── test_bootstrap.py
│   ├── test_cli.py
│   ├── test_roundtrip.py
│   └── fixtures/
│       ├── presets/sample_serial.json     ⚠ synthetic — TO BE REPLACED with a fixture derived from data/Possum.hlx
│       └── specs/goldfinger.json          ⚠ uses humanized param names — TO BE UPDATED to wire keys
└── data/                                  gitignored. 1 .hlx + 206 .hsp user-supplied exports.
```

The two ★ documents are the most important reading after this handoff.

---

## 7. How to run

```bash
# Env (uv is used because the sandbox lacked system pip/venv)
curl -LsSf https://astral.sh/uv/install.sh | sh        # if not installed
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Tests
pytest

# CLI smoke
helixgen --help
helixgen ingest tests/fixtures/presets/sample_serial.json
helixgen list-blocks
helixgen show-block "Brit 2204 Custom"
helixgen generate tests/fixtures/specs/goldfinger.json -o /tmp/gf.hlx
```

Branch: `v1-implementation`. Base: `main`. Don't push without user confirmation.

---

## 8. User working preferences (from accumulated session memory)

- **TDD discipline:** failing test → run → implement → run → commit, one task at a time
- **Conventional commits** (`feat:`, `test:`, `docs:`, `fix:`, `refactor:`)
- **Terse responses.** No long preambles. No trailing recap of what was just done. Don't explain what the code obviously does.
- **No premature abstraction or hypothetical-future scaffolding.** Three similar lines is better than a half-finished framework.
- **No comments unless the *why* is non-obvious.** The user reads diffs.
- **Confirm before destructive or shared-state actions** (force-push, branch deletion, dropping data, sending external messages). Don't bypass git hooks (`--no-verify`) unless asked.
- **No emojis.**
- **One bundled PR over many small ones for related refactor work** (per prior validated judgment).
- **Strictness over comments** — the reason JSON was chosen over YAML for the spec.
- The user **chose Subagent-Driven Development for v1** but will likely just want this rewrite done in-flow rather than per-task subagent dispatch. Confirm before re-launching subagents.

---

## 9. Open questions for the user

These don't need to be answered before you start, but flag them when you hit them:

1. **Param key naming policy** — do we accept only the wire keys (`ChVol`) in spec input, or also display names (`Ch Vol`)? Recommend wire-only for v1 simplicity; defer humanization to Layer 2.
2. **Noise Gate as a block vs. as `inputA` params** — does the user want to keep specifying it block-style in JSON, or switch the spec to express it as an attribute of the input?
3. **Cab specification** — the spec currently lists cab as a separate block in the chain. With real `.hlx` linking cabs to amps via `@cab`, does the user want the spec to nest cab inside amp, or keep it flat and let the compiler auto-link?
4. **Phelix bootstrap timing** — run it now (and use real phelix `.hlx` data to validate), or defer until the rewrite is locally green?

---

## 10. Things explicitly OUT of v1 scope

Don't be tempted. These are real features but the user wants v1 shipped first.

- Parallel A/B routing (split + join in dsp1 used as a real path) — `docs/features/parallel-paths.md`
- Snapshot variation / per-snapshot block param overrides
- Display-value (0–10) translation
- Footswitch / controller / MIDI assignment in the spec
- Any second-format target beyond `.hlx`
- Layer 2 (the conversational skill) — separate project, not in this repo

---

End of handoff.
