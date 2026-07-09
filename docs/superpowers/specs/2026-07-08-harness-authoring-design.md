# Harness authoring — design

**Date:** 2026-07-08
**Status:** approved (autonomous agent design; reviewed by review-subagent)

## Problem

`.hsp` blocks carry a bNN-level `harness` dict alongside their `slot`. Today
helixgen only PRESERVES it verbatim (`BlockEntry.raw.harness`) for round-trip
fidelity — a spec author cannot AUTHOR any harness feature. The one harness
field that is genuinely musician-facing, **`Trails`** (delay/reverb spillover),
is therefore only reachable by hand-editing an opaque `raw.harness` blob that
the docs explicitly say authors should not touch.

Goal: model the author-facing harness feature(s) cleanly in the spec schema,
generate them correctly, round-trip them through decompile, and keep the
existing `raw.harness` verbatim mechanism working for everything else.

## What "harness" actually is (investigation results)

Scanned 7 real Stadium XL `.hsp` exports (91 user blocks, 48 with a harness).
The `harness` dict is a sibling of `slot` at the bNN level. Observed shape:

```json
"harness": {
  "@enabled": {"value": true},
  "params": {
    "EvtIdx": {"value": -1},
    "bypass": {"value": false},
    "upper":  {"value": true},
    "Trails": {"value": false},   // delays + reverbs only
    "dual":   {"value": true}     // dual-cab IR blocks only
  }
}
```

Distinct `harness.params` fields and how they behave across the corpus:

| field    | type | observed values | appears on | verdict |
|----------|------|-----------------|------------|---------|
| `Trails` | bool | `false` (both seen) | delay + reverb blocks | **MODEL — author-facing** |
| `dual`   | bool | `true` | IR/cab block with a 2nd slot | structural — leave verbatim (tied to `raw.slots`) |
| `upper`  | bool | `true` (always) | most blocks | structural constant — leave verbatim / synthesize |
| `bypass` | bool | `false` (always) | most blocks | structural constant — leave verbatim / synthesize |
| `EvtIdx` | int  | `-1` (always) | most blocks | structural constant — leave verbatim / synthesize |
| `@enabled` | dict | `{"value": true}` (always) | every harness | structural constant — leave verbatim / synthesize |

`ControlSource` — named in CLAUDE.md's `raw` description — **does not occur in
any Stadium `.hsp` at all** (0 occurrences at any nesting level across the
corpus). It is a stale/legacy-Helix artifact in the docs. Out of scope; the
CLAUDE.md wording will be corrected.

### Decision: model `Trails`, leave the rest verbatim

`Trails` is the only harness field an author would ever want to set. It controls
whether a delay's echoes / a reverb's tail keep ringing ("spill over") when the
block is bypassed or you switch snapshots — a standard, musically-meaningful
Helix behavior. Everything else is either a device-constant (`upper`, `bypass`,
`EvtIdx`, `@enabled`) or structural and already handled elsewhere (`dual` is a
consequence of a dual-cab, which the existing `raw.slots` mechanism carries).
`dual`, therefore, stays verbatim: authoring it independently of a second cab
slot would be meaningless.

## Spec surface

New optional **block-level** field `"trails"`, a boolean:

```json
{"block": "Tape Echo Stereo", "params": {"Mix": 0.25}, "trails": true},
{"block": "Plate Stereo",     "params": {"Mix": 0.15}, "trails": true}
```

- `trails: true` → the block's `harness.params.Trails.value` is `true`.
- `trails: false` → explicitly `false`.
- field omitted → helixgen does not assert a Trails value: it emits whatever
  `raw.harness` carried (verbatim, backward-compat), or nothing (a freshly
  authored block with no harness, exactly as today).

Rationale for block-level (not a dedicated top-level `harness` object): Trails
is per-block, attaches to exactly the block it modifies, and mirrors how `ir`,
`enabled`, and `raw` already live on the block entry. A separate top-level
object keyed by block name would reintroduce the ambiguity problems that
snapshots/footswitches need `(lane,pos,path)` coordinates to resolve — pointless
when the field can just live on the entry.

Trails is **base-level only** — it does not vary per snapshot. No real preset
was observed varying it per snapshot, and the harness params carry no snapshot
array. (Per-snapshot Trails is a possible future extension; explicitly YAGNI
now.)

## Validation rules

1. **Type (spec.py, parse time):** `trails`, if present, must be a `bool`;
   otherwise `SpecError`. Stored as `BlockEntry.trails: bool | None` (default
   `None` = unset).
2. **Category (generate.py, compose time — needs the resolved library block):**
   `trails` may only be set on blocks whose `category` is `delay` or `reverb`.
   Setting it on any other block raises `GenerateError` naming the block and the
   allowed categories. (Guardrail, consistent with helixgen's strict
   unknown-param rejection. Easy to relax later if a non-delay/reverb block is
   found to honor Trails.)
3. Interaction with `raw.harness`: allowed together. When both are present the
   `trails` field wins (see generate behavior). No error.

## Generate behavior

In `_to_hsp_bnn`, add a `trails: bool | None = None` parameter (passed from
`block_entry.trails` at the call site, alongside the existing `raw=`).

Harness construction order:

1. If `raw` carries a `harness` dict, deep-copy it into `bnn["harness"]`
   (existing behavior, unchanged).
2. If `trails is not None`:
   a. If `bnn` has no `harness` yet, synthesize a complete, device-plausible
      one using the observed constants:
      ```json
      {"@enabled": {"value": true},
       "params": {"EvtIdx": {"value": -1}, "Trails": {"value": <trails>},
                  "bypass": {"value": false}, "upper": {"value": true}}}
      ```
      (Key order matches the observed corpus order for delay harnesses —
      `EvtIdx, Trails, bypass, upper` — so even a future byte comparison agrees;
      dict `==` is already order-insensitive.)
   b. Ensure `harness["params"]` exists, then set
      `harness["params"]["Trails"] = {"value": trails}` (override if present).

This makes `trails` authoritative over any `raw.harness.Trails`, synthesizes a
full harness for freshly authored blocks (rather than a bare `{"Trails": …}`
that the device might reject), and preserves all other verbatim harness fields.

The category guard is enforced in the block-placement loop (where `block`
and `block_entry` are both in scope) before calling `_to_hsp_bnn`.

## Decompile behavior

In `_block_entry` (the resolved library `block` is in scope, so
`block.category` is available):

1. Keep copying the full `harness` into `entry["raw"]["harness"]` (unchanged),
   EXCEPT:
2. **Only when `block.category in {"delay", "reverb"}`** (symmetric with the
   generate-side guard — see below) AND `harness.params.Trails` exists: set
   `entry["trails"] = bool(harness["params"]["Trails"]["value"])` AND delete the
   `Trails` key from the copied `raw.harness.params` so there is a single source
   of truth.
   - For any other category, `Trails` (if somehow present) is left inside
     `raw.harness` verbatim exactly as today — never lifted, never a `trails`
     field. This keeps decompile and generate symmetric: a block that could not
     be regenerated with a `trails` field is never given one.
   - Defensive access: use `(harness.get("params") or {}).get("Trails")` and
     tolerate a missing `value` key; a malformed harness degrades to verbatim
     rather than raising.
   - After removing `Trails`, the harness is still copied verbatim (its
     remaining constants — `@enabled`, `EvtIdx`, `bypass`, `upper` — are
     non-modeled and must round-trip). We do NOT drop the harness; we only lift
     `Trails` out of it.

Result: a decompiled delay/reverb with Trails produces
`{"block": "...", "trails": <bool>, "raw": {"harness": {…without Trails…}}}`.
Regenerating re-injects `Trails` into `harness.params`, reproducing the original
harness (dict equality ignores key order — the sonic-fidelity test's
`sb.get("harness") == rb.get("harness")` still holds).

### Blocking issue this fixes (review finding B1)

Without the category gate on decompile, the existing test
`tests/test_decompile.py::test_decompile_captures_harness_and_extra_slots` —
which puts `Trails` on `Tube Drive` (category `drive`, per `conftest.py`) —
would lift `Trails` to a field on a drive block, and regenerating it would raise
the delay/reverb `GenerateError`, breaking round-trip. With the symmetric gate,
that drive-block `Trails` stays verbatim and the test passes unchanged. A new
companion test asserts a *delay* block's `Trails` IS lifted.

### Type-fidelity note (review finding S2)

`Trails` is JSON `bool` in 100% of the observed corpus, so `bool(...)` on
decompile + writing a Python `bool` on generate is type-stable. If a future
export stored `Trails` as int `0/1`, the sonic-fidelity assertion would not
catch the bool-vs-int rewrite (Python `0 == False`). Documented as a known,
low-risk assumption; the field is defined as `bool`.

## Backward-compatibility story

- **Old sidecar specs** that carry Trails inside `raw.harness` (generated before
  this feature) still generate identically: `trails` is unset (`None`), so
  generate emits `raw.harness` verbatim including its `Trails`.
- **`raw.harness` verbatim mechanism** is otherwise untouched — it still carries
  `dual`, `upper`, `bypass`, `EvtIdx`, `@enabled`, and any future unknown field.
- **New decompiles** lift `Trails` to the field; the resulting spec is
  equivalent and regenerates byte-for-byte-equivalent harness dicts.
- **`.hlx` (legacy Helix) chassis:** harness is a Stadium concept. `trails` on a
  `.hlx` target is ignored (no harness emitted for `.hlx`); we mirror the
  existing IR-field convention. (Investigation: legacy `.hlx` fixtures carry no
  harness.)

## Files touched

- `src/helixgen/spec.py` — parse + validate `trails`; add `BlockEntry.trails`.
- `src/helixgen/generate.py` — `_to_hsp_bnn` `trails` param + harness
  synth/merge; category guard at the call site.
- `src/helixgen/decompile.py` — lift `Trails` out of `raw.harness` into
  `entry["trails"]`.
- `CLAUDE.md` — document the block-level `trails` field; correct the stale
  `ControlSource` mention in the `raw.harness` description.
- `tests/` — TDD coverage (below).

## Test plan (TDD — failing test first for each)

**spec.py (tests/test_spec.py):**
- `trails: true`/`false` parses onto `BlockEntry.trails`.
- omitted → `BlockEntry.trails is None`.
- non-bool `trails` → `SpecError`.
- `trails` coexists with `raw.harness` without error.

**generate.py (tests/test_generate.py):**
- `trails: true` on a delay with no prior harness → synthesized harness with
  `params.Trails.value is True` and the constant fields present.
- `trails: false` → `params.Trails.value is False`.
- `trails` field overrides a conflicting `raw.harness.Trails`.
- `trails` on a non-delay/reverb block → `GenerateError`.
- `trails` unset + `raw.harness` present → harness emitted verbatim (unchanged).

**decompile.py (tests/test_decompile.py):**
- delay/reverb bNN harness with `Trails` → `entry["trails"]` set, `Trails`
  removed from `entry["raw"]["harness"]["params"]`, other harness constants
  retained.
- **drive** bNN harness with `Trails` → NOT lifted; `Trails` stays in
  `raw.harness` (this is exactly the existing
  `test_decompile_captures_harness_and_extra_slots`, which must keep passing).
- bNN harness without `Trails` → no `trails` key; `raw.harness` verbatim as today.
- round-trip: decompile→parse→generate reproduces the original harness dict
  (guarded by existing sonic-fidelity test; add a focused unit round-trip too).

**patch.py (tests/test_patch.py) — review finding N2:**
- `swap_model` on a delay→delay swap preserves the `trails` field (it operates
  on the raw spec dict and never touches `trails`; same-category guard already
  blocks delay→drive). One confirming test.

## Out of scope (explicit YAGNI)

- Per-snapshot Trails.
- Authoring `dual` / `upper` / `bypass` / `EvtIdx` / harness `@enabled`
  independently (structural constants; stay verbatim).
- `ControlSource` (does not exist in Stadium presets).
- A dedicated `set-trails` CLI verb / MCP op. The spec/sidecar is the source of
  truth and can be hand-edited or regenerated; a surgical verb is a possible
  follow-up but not required for authoring.

## Demonstrator

Generate a demo `.hsp` with a delay + reverb both `trails: true` (plus an amp
and cab) so the user can verify on real Stadium XL hardware that the tails spill
over on bypass / snapshot change. Provide step-by-step device-test instructions.
