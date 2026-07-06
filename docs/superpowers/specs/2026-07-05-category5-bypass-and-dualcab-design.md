# Category 5 — block-bypass fidelity + dual-cab / per-block verbatim state

**Date:** 2026-07-05
**Status:** Design (brainstormed, measured against the full `data/` corpus).
**Parent:** `docs/superpowers/specs/2026-07-03-decompiler-round-trip-residuals.md`
(Category 5, items #1 and #3).
**Branch:** `hardening/category5-bypass-and-dualcab`

## Summary

The 211/211 acceptance bar compares slot-0 **models** only; a Stadium XL hardware
test of a regenerated preset proved the round-trip is not a sonic clone. This
cycle closes the two highest-impact audio gaps:

1. **Block base-bypass state** (item #1) — the device reads bypass at the `bNN`
   level (`bNN.@enabled.value`); decompile reads the *slot* level and generate
   *writes* the slot level (hardcoding `bNN.value = True`). Base-bypassed blocks
   therefore round-trip as **enabled** (they play when they must be silent).
   Affects **150/211** presets.
2. **Dual-cab + per-block verbatim state** (item #3) — generate rebuilds each
   `bNN` from the library exemplar and drops everything it does not model:
   the **second cab slot** (`slot[1]`, on **261** blocks — 55 real dual-cabs, 206
   `NoCab` placeholders), the **`harness`** sub-dict (on **all 2172** blocks;
   carries `Trails`, `ControlSource`, `dual`, its own `@enabled`), and
   `favorite`. Preserving these verbatim makes each `bNN` a sonic clone.

Deferred (documented, not in this cycle): #2 input-block params, #4
`preset.params` (tempo/impedance), and the top-level unmodeled state (`sources`
scribble labels, `meta.info`, `preset.xyctrl`, snapshot `valid`/`expsw`) — all
invisible in a self-round-trip and lower audio impact.

## Measurements (whole corpus, 211 real exports)

Established empirically before design (not assumed):

| Fact | Value | Consequence |
|---|---|---|
| Presets with ≥1 base-bypassed user block | 150/211 | Item #1 is majority-of-corpus |
| slot-level `@enabled.value == False` anywhere | **1** block (a dual-cab artifact) | slot level is ~always `True`; safe `True` baseline |
| `bNN.@enabled.value != snapshots[0]` | 95 cases | base bypass is **independent** state, not "active snapshot" |
| Dual-slot (`len(slot) == 2`) blocks | 261 (55 real 2nd cab, 206 `NoCab`) | slot[1] dropped today, invisible to model bar |
| `harness` present | all 2172 blocks | dropped by generate |
| Distinct `harness` among real dual-cabs | 2 (`dual` present in only 24/55) | `harness.dual` does **not** track dual-cab → **cannot synthesize**, preserve verbatim |
| `favorite` values | `0` on all 2172 | generate can emit a constant `0` |

Already round-tripping (verified byte-identical on `data/Black Keys.hsp`), needing
**no change** — the Category 1/4 work already moved these reads to the `bNN` level:

* per-snapshot bypass array (`bNN.@enabled.snapshots`) via `_recover_snapshots`;
* bypass **footswitch** controller (`targetbypass`, FS1–FS10) via
  `_recover_footswitches`.

So item #1 is narrower than the parent spec implied: **only the base `value` is
misread.** The full round-trip diff of `Black Keys` @enabled dicts shows exactly
two residual classes — Class A (base `value` False→True, the audio bug) and
Class B (a redundant all-`True` snapshots array the source stores and generate
omits — semantically identical, not audio).

## Design

### Part 1 — base bypass at the `bNN` level

**Semantics (confirmed by data + the existing `test_generate.py:437` premise):**
the device reads `bNN.@enabled.value` as the live/on-load bypass. `bNN.@enabled`
carries three independent things: `value` (base bypass), `snapshots[i]`
(per-snapshot bypass), `controller` (footswitch bypass). The slot-level
`@enabled` is inert (always `True`).

**Decompile (`decompile._block_entry`):** read the base bypass from the *bNN*
`@enabled`, not the slot. `_block_entry` is called only from `_entry_for`, which
already holds the `bnn` dict; pass the bnn (or its `@enabled`) in. Emit
`enabled: false` when the base differs from the exemplar baseline (`True`).
`_unwrap_value` already returns the `"value"` key while ignoring
`snapshots`/`controller`, so `_unwrap_value(bnn["@enabled"])` is the base.

**Generate (`generate._to_hsp_bnn`):**
* Slot-level `@enabled` becomes always the exemplar value (`flat.get("@enabled",
  True)`, i.e. `True`) — it no longer carries `enabled_base`. Matches the source
  (slot always `True`).
* `bNN.@enabled.value` becomes `enabled_base` (currently hardcoded `True`).
* **Decouple the `value` from the snapshots-array fill.** The enabled snapshot
  array must keep its `None`→`True` fill (a snapshot with no explicit `disable`
  is *enabled*, independent of the base). This is the critical subtlety: base
  `value` can be `False` while snapshots 1–7 are `True` (`Black Keys b03`).
  Reusing `_wrap_value_with_snapshots(base, overrides)` — which fills `None` with
  `base` — would wrongly fill the array with `False`. Build the enabled wrapper
  explicitly instead:

  ```python
  base_enabled = enabled_base if enabled_base is not None else flat.get("@enabled", True)
  slot_inner["@enabled"] = {"value": flat.get("@enabled", True)}   # slot: inert True
  ...
  enabled_wrapped = {"value": base_enabled}                        # bNN.value: the real bypass
  if enabled_overrides and any(o is not None for o in enabled_overrides):
      enabled_wrapped["snapshots"] = [True if o is None else o for o in enabled_overrides]
  if fs_controller is not None:
      enabled_wrapped["controller"] = fs_controller
  ```

  `_wrap_value_with_snapshots` (param snapshots, `None`→base fill) is unchanged.

**Why this round-trips (verified by simulation over all 211):** with base read
from and written to `bNN.value`, and disables recovered for named snapshots where
`snapshots[i]` is explicitly `False`, every user block's **base value +
effective per-snapshot bypass over named snapshots** matches the source. The
`None`→`True` fill reproduces `Black Keys b03` (`value False`, `snapshots
[F,T,T,T,T,T,T,T]`) exactly, and every existing snapshot test (e.g.
`test_generate.py:675` expecting `[True, False, True, …]`) stays green because
the fill stays `True`.

**One structural caveat (empirical, not guaranteed).** A base-`False` block that
is *enabled* in some named snapshot round-trips only because such a block always
also carries an explicit `False` in some other snapshot, which triggers emission
of the snapshots array. The pure case — base `False`, at least one named
explicit `True`, and **zero** named `False` ("Case B") — would emit no snapshots
array and read base `False` in every snapshot (silently wrong). Case B is
**0/211 in the corpus** but is not structurally impossible (an author could write
it). The decompiler should emit a **warning** if it ever encounters a
base-`False` block whose only snapshot divergence is an enable (no `disable`),
flagging that it cannot fully round-trip until the snapshot enable-override lands.

### Part 2 — dual-cab + per-block verbatim state (`raw`)

`harness` is non-deterministic (`harness.dual` present on only 24/55 real
dual-cabs but 166/206 single WithPan cabs) and carries meaningful state, so it
**must be preserved verbatim, never synthesized** — the same principle as
`StructuralEntry`. This applies to extra slots and `favorite` too.

**Spec model (`spec.BlockEntry`):** add one optional field:

```python
@dataclass
class BlockEntry:
    ...
    raw: dict[str, Any] | None = None   # verbatim non-modeled bNN state
```

`raw` shape (all sub-keys optional):

```json
"raw": {
  "harness": { <verbatim bNN.harness dict> },
  "slots":   [ { <verbatim slot[1]> }, ... ]   // additional slots beyond slot[0]
}
```

`parse_spec` validation: `raw` must be a dict if present; `raw.harness` a dict if
present; `raw.slots` a list of dicts if present. `raw` is only meaningful on
`BlockEntry` (not split/join/structural).

**Decompile (`_block_entry`):** populate `raw` from the source bNN —
`raw.harness` verbatim whenever `bnn.get("harness")` is present (per the "all
blocks" decision), `raw.slots` = `copy.deepcopy(bnn["slot"][1:])` when the block
has more than one slot. Emit `raw` only when it has at least one sub-key (always,
since harness is universal). `_block_entry` needs the full `bnn`, not just
`slot[0]` — thread it through `_entry_for` (which already has it).

**Generate (`_to_hsp_bnn`):** after building the bNN from slot[0]:
* if `raw` and `raw.harness`: `bnn["harness"] = copy.deepcopy(raw["harness"])`;
* if `raw` and `raw.slots`: `bnn["slot"].extend(copy.deepcopy(raw["slots"]))`;
* always set `bnn["favorite"] = 0` (constant across the corpus; matches source).

`_to_hsp_bnn` gains a `raw` parameter; the `_compose_preset_hsp` placement loop
passes `block_entry.raw`. Threads through with the existing per-block kwargs.

**Interaction with item #1:** a second slot's `@enabled` (when present) is
carried verbatim inside `raw.slots`, untouched by the item-#1 logic, which only
manages slot[0]/bNN. The 1 anomalous slot-level `@enabled: False` in the corpus
is **Megadeth b05 — a *single*-slot IR cab** (`HX2_ImpulseResponseWithPan`,
`len(slot)==1`, carries an irhash), *not* a dual-cab. Its bNN base is `True`
(enabled). Item #1 rewrites slot[0]'s `@enabled` to the inert exemplar `True`,
so that lone slot-level `False` is **discarded and not asserted** by the
scoreboard. This is harmless **iff** the core premise holds — that the device
reads bypass at the bNN level and slot-level `@enabled` is inert. That premise is
strongly supported (210/211 blocks have slot-level `True`; the device-read-bNN
behavior is documented at `test_generate.py:437`) but the discard of this single
`False` should be **confirmed on the hardware-verify step** (an IR cab whose
slot-level bypass is dropped must still sound identical).

**IR / no_ir:** the primary slot's IR handling is unchanged. Second-slot IR
state (irhash) is carried verbatim in `raw.slots`, so no `_resolve_irhash` pass
runs on it (matches source; a dual-cab's second cab is a catalogued cab, not a
user IR, in the corpus).

## Scoreboard (new, honest measure)

The model bar (`test_decompile_acceptance.py`) is 211/211 and does not move. Add
a **new** acceptance test, `tests/test_decompile_sonic_fidelity.py`, that
ingests all `data/*.hsp` into one shared library (like the existing bar),
round-trips each, and asserts **per-user-block** equality of:

1. **base `bNN.@enabled.value`** (item #1) — the on-load/live bypass;
2. **effective per-snapshot bypass over the *named* snapshots**, with an explicit
   null-skip rule (below);
3. **every slot's `model`** across the full slot array (item #3 — catches the
   dropped second cab);
4. **every slot's param *values*** (unwrapped base values, per slot, all slots) —
   the actual sound (gain/tone/mix). Currently 0 diffs across the corpus, so it
   passes today and locks against a future param/coercion/stereo regression;
   without it a test named "sonic fidelity" would not assert the knob values;
5. **`harness`** dict equality (item #3);
6. **`favorite`**.

Target: **211/211**.

**The effective-per-snapshot-bypass comparator (precise, so two implementations
cannot disagree).** For a user block with named-snapshot count `N`
(`len(_snapshot_names(body))`), base value `v`, snapshots array `a` (absent →
all-`None`):

```
effective(side, i) = a[i]  if  (a exists and i < len(a) and a[i] is not None)
                     else v
```

Compare `effective(source, i) == effective(regen, i)` for `i in range(N)`
**only for cells where the SOURCE slot is present-and-non-null**. When the source
snapshot slot is `null`/absent the cell is a **wildcard — skipped**: `null` is
undefined device recall (the Category-4 "unreliable recall" state), so there is
no defined source value to hold regen to. Regen densifies such cells to `True`
(the `None`→`True` fill), a deliberate Category-4-consistent deviation on the
~30 presets carrying a `null` in a *named*, base-`False` slot. **Without this
explicit source-null skip the literal formula fails on those 30 presets —
181/211, not 211/211.** The skip is what makes the "does not assert null-recall"
exclusion below real rather than contradicted.

**What it deliberately does NOT assert (documented in the test):**
* **Redundant all-`True` snapshot arrays** (Class B) — source stores them on
  every block; regen omits them. Semantically identical (the effective-bypass
  compare normalizes this). Not audio.
* **`null`-at-*named*-snapshot recall (~30 presets)** — handled by the
  source-null skip above: the source stores `null` (undefined recall) in a
  *named* snapshot slot with base `False`; regen densifies to a `True` fill. This
  is the exact "unreliable recall" Category 4 set out to fix, it is not the
  on-load sound, and the device's `null` behavior is ambiguous. The
  `disable`-only snapshot model cannot express "enabled-in-snapshot" for a
  base-bypassed block; closing it needs a snapshot "enable"-override — a separate
  cycle. The scoreboard skips these cells (does not assert them) rather than
  silently passing them.
* **Unnamed trailing snapshot slots**, top-level unmodeled state (`sources`,
  `meta.info`, `xyctrl`, snapshot `valid`/`expsw`), and **non-FS bypass-assign
  controllers** (e.g. source `0x01010600`, which toggles bypass but is not an
  FS1–FS10 — control-surface only, not the loaded sound).

The scoreboard measures the **sonic state of every block** — bypass (base +
named-snapshot effective), all slot models + param values, harness, favorite —
not full-body byte-fidelity (top-level `sources`/`meta`/`xyctrl`/snapshot
metadata remain out of scope).

## Existing tests that MUST be updated (they assert the old level)

* **`tests/test_patch_cli.py::test_cli_disable_block` (line ~45)** — asserts
  `body[...]["b01"]["slot"][0]["@enabled"]["value"] is False`. After the fix the
  base bypass lives at the **bNN** level and the slot becomes the inert exemplar
  `True`, so this must be rewritten to
  `body[...]["b01"]["@enabled"]["value"] is False`. (This is a real behavior
  move, not a broken test — the assertion follows the bypass to its correct
  level.)
* **Stale comment `decompile.py:179-181`** in `_recover_snapshots` — "The base
  bNN-level `@enabled` is always True (generate never densifies it…)" becomes
  false once generate writes `bNN.value = False`. The disable-recovery logic is
  unaffected (it keys off explicit `snapshots[i] is False`, never the base), but
  the comment must be corrected.

## Existing tests to keep green (verified compatible by inspection)

* `test_decompile.py::test_decompile_roundtrip_stable` — `enabled: False` spec →
  self-round-trip; both sides use the new generate, so `p1 == p2` holds.
* `test_generate.py:437-457` — default blocks get `bNN @enabled == {"value":
  True}`; unchanged (base defaults `True`, no overrides).
* `test_generate.py:660-689` — disable emits `snapshots [True, False, …]` with a
  `True` fill; unchanged (fill stays `True`).
* `test_generate.py:476-477` — a default block's slot `@enabled == {"value":
  True}`; unchanged (slot stays exemplar `True`).

New/updated unit tests (TDD, alongside the corpus scoreboard):
* decompile emits `enabled: false` from a `bNN.value: False` block;
* generate places base bypass at `bNN.value` and keeps slot `@enabled` `True`;
* generate keeps the `None`→`True` enabled-snapshot fill when base is `False`;
* `raw` round-trips harness + second slot; `parse_spec` accepts/validates `raw`;
* generate emits `favorite: 0`;
* decompile warns on a Case-B block (base `False`, enabled-in-snapshot, no
  `disable`) — the un-round-trippable pure-enable case.

## Out of scope / deferred (record in the parent spec)

* #2 input-block params, #4 `preset.params`, top-level unmodeled state.
* Snapshot "enable"-override model (the `null`-recall / base-bypassed-then-
  enabled-in-snapshot case).
* Authoring dual-cabs from scratch (the `raw.slots` form is faithful for
  round-trip; a friendlier `cab2` authoring surface can come later).
* General harness *elision* for spec readability (we chose verbatim-on-all-blocks
  for max fidelity; elision is a future cosmetic optimization).
* **Harness/authoring consistency.** `harness` carries independent state (`dual`
  on 218 blocks, `Trails` on 382, harness-level `@enabled: false` on 34). Verbatim
  preservation is exact for a *round-trip*, but a **surgical edit** to a modeled
  field (e.g. re-enabling a base-bypassed block, or dropping a second cab via
  `raw.slots`) can leave `harness.dual` / `harness.@enabled` / `bypass` stale and
  internally inconsistent. Out of scope here (round-trip fidelity is the goal);
  worth a note in the patch/surgical-edit path later.
