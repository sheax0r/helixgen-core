# Controller Identifier ↔ English Mapping (Helix Stadium XL)

**Status:** Design approved 2026-07-08. **Implemented in 1.0.1** (branch
`feature/controller-identifier-mapping`).
**Implementation gate:** SATISFIED. `redesign-hsp-canonical` landed as **1.0.0**;
this spec was reconciled against the redesigned controller representation on
**2026-07-09** and implemented. The redesign moved the decode surface from
`decompile.py` to `view.py` and the authoring/validation surface from
`generate.py`/`spec.py` hooks to `mutate.wire_*` + `recipe.apply_recipe`; the
mapping tables still live in `controllers.py` as this spec intended. See the
per-section reconciliation notes below.
**Owner device:** Helix **Stadium XL** (`device_id` → `stadium_xl`).

---

## 1. Problem

helixgen's spec vocabulary exposes raw device identifiers (`FS1`, `EXP1Toe`, …) to humans. Two
concrete problems:

1. **Leakage:** tone descriptions surface strings like `FS5 → Compulsive Drive` that a guitarist
   should not have to mentally map to a physical switch. Humans should be able to *read* what a
   tone does in plain language, and *describe* controls in plain language, without knowing the
   identifier scheme.
2. **The vocabulary is wrong.** helixgen accepts `FS1..FS10` (contiguous). The hardware exposes
   **12** footswitches (`FS1..FS12`); only **FS1–FS5** and **FS7–FS11** are assignable, with
   `FS6` = MODE and `FS12` = TAP/Tuner reserved. So helixgen's `FS6` actually addresses the MODE
   switch, and the real assignable `FS11` cannot be expressed at all.

## 2. Evidence (why the vocabulary is wrong)

Device layout (Line 6 official manual + Command Center + XL Floor cheat-sheet): 12 capacitive
footswitches in **2 rows × 6 columns**, numbered left-to-right, **top row FS1–FS6, bottom row
FS7–FS12**. Command Center lists the assignable set literally as *"Footswitch 1-5, 7-11, or Exp
Toe."* Reserved: `FS6` = MODE (top-right), `FS12` = TAP/Tuner (bottom-right); `FS1`/`FS7` are also
Bank Up/Down in Preset mode but remain assignable as stomps.

Source-id corroboration (211 real `.hsp` exports parsed; source index = FS# − 1, i.e. source
`0x010101NN`, `NN = FS# − 1`):

Counts below are **files (of 211) containing at least one controller bound to that source**,
re-verified in the worktree against `data/*.hsp` on 2026-07-09.

| Device switch | source id | in real presets | helixgen ≤1.0.0 | helixgen 1.0.1 |
|---|---|---|---|---|
| FS1–FS5 | `0x01010100`–`04` | yes (FS1 55, FS5 61 files) | ✓ correct | ✓ correct |
| **FS6 (MODE)** | `0x01010105` | **0 files** | ✗ exposed as assignable "FS6" (bug) | ✓ reserved → tailored MODE error |
| FS7–FS10 | `0x01010106`–`09` | yes (FS10 87 files) | ✓ correct | ✓ correct |
| **FS11** | `0x0101010a` | **90 files** | ✗ missing from table | ✓ added, assignable |
| FS12 (TAP) | `0x0101010b` | **0 files** (reserved) | n/a (never in table) | ✓ reserved → tailored TAP/Tuner error |

`FS6` scoring **0** files across 211 presets is the tell: nobody assigns to the MODE switch.
`FS11` scoring **90** files is the real 5th assignable switch on the bottom row. (`EXP1Toe`,
the wah toe switch, appears in 142 files.)

Sources: manuals.line6.com/en/helix-stadium/live/{top-panel-and-footswitches,command-center,midi};
Helix Stadium XL Floor cheat-sheet (doc 40-00-0572 Rev B).

**Open verification (needs one device round-trip before hard-coding):** assign a block to the
bottom-row, 5th-from-left switch (device FS11, immediately left of TAP/Tuner) and export; confirm
`source == 0x0101010a`.

## 3. Goals / Non-goals

**Goals**
- A single, device-accurate canonical vocabulary for assignable controls.
- Bidirectional mapping: identifier → English (name **and** physical position); English → identifier.
- The `tone`/`setup` skills render controls to humans in English, and accept English from humans.
- `decompile` stops silently dropping controls it doesn't recognise.

**Non-goals (v1)**
- Standard (non-XL) Stadium (identical FS grid, no onboard pedal) — future extension; the design
  stays device-keyed so it drops in later.
- EXP3, external Control A/B/C/D pedals, the unidentified `0x010104NN` source bank, and the
  `0x0101020N` (00–03) targetbypass bank newly found during implementation — these are
  **decode-labeled as "unknown control"** (not silently dropped) but not authorable in v1.
- Any change to how blocks/snapshots are encoded — controls only.

## 4. Decisions (from brainstorming)

1. **Device-accurate, no legacy.** Canonical set is exactly the hardware's assignable controls.
   Old/invalid identifiers are **rejected with a specific error**, not silently accepted. (Low
   blast radius: the only currently-accepted-but-wrong identifier is `FS6`; `FS1–FS5, FS7–FS10`
   keep identical meaning and source ids, so existing valid specs are unaffected. `FS11` is new.)
2. **English shows both name + position.** hsp→human always renders e.g.
   `Footswitch 5 (top row, 5th from left)`. Humans may *say* `FS5`, a position, or both.
3. **English→identifier via a dedicated small-model sub-agent** whose only job is this translation,
   fed our structured mapping data. No brittle Python NL parser; no reliance on the primary agent's
   attention. The skill validates the returned identifier against the canonical set.

## 5. Canonical vocabulary

| Identifier | source id | kind | row,col | canonical name | position phrase |
|---|---|---|---|---|---|
| `FS1`  | `0x01010100` | footswitch | top,1    | Footswitch 1 | top row, 1st from left (top-left) |
| `FS2`  | `0x01010101` | footswitch | top,2    | Footswitch 2 | top row, 2nd from left |
| `FS3`  | `0x01010102` | footswitch | top,3    | Footswitch 3 | top row, 3rd from left |
| `FS4`  | `0x01010103` | footswitch | top,4    | Footswitch 4 | top row, 4th from left |
| `FS5`  | `0x01010104` | footswitch | top,5    | Footswitch 5 | top row, 5th from left (2nd from right) |
| `FS7`  | `0x01010106` | footswitch | bottom,1 | Footswitch 7 | bottom row, 1st from left (bottom-left) |
| `FS8`  | `0x01010107` | footswitch | bottom,2 | Footswitch 8 | bottom row, 2nd from left |
| `FS9`  | `0x01010108` | footswitch | bottom,3 | Footswitch 9 | bottom row, 3rd from left |
| `FS10` | `0x01010109` | footswitch | bottom,4 | Footswitch 10 | bottom row, 4th from left |
| `FS11` | `0x0101010a` | footswitch | bottom,5 | Footswitch 11 | bottom row, 5th from left (2nd from right) |
| `EXP1` | `0x01020100` | expression | —        | Expression Pedal 1 | onboard pedal, EXP 1 (violet LED) |
| `EXP2` | `0x01020101` | expression | —        | Expression Pedal 2 | onboard pedal, EXP 2 (teal LED) |
| `EXP1Toe` | `0x01010500` | toe switch | —     | Expression pedal toe switch | the toe switch under the expression pedal (push the pedal fully forward to click it); standard wah auto-engage |

**Reserved (rejected with tailored errors):** `FS6` (`0x01010105`, MODE, top-right);
`FS12` (`0x0101010b`, TAP/Tuner, bottom-right).

**Naming note:** `EXP1Toe` is retained as the identifier (already hardware-validated on live wah
setups; renaming would break existing presets). Its English label uses the device's term "Exp Toe".

**Aliases** (seed for the translation sub-agent; non-exhaustive, extended in implementation):
positional phrases per the table above, plus common synonyms — "top-left"/"top left switch" → FS1,
"bottom right stomp / second from right on the bottom" → FS11, "toe switch"/"wah engage"/"pedal
toe" → EXP1Toe, "the expression pedal"/"wah pedal sweep" → EXP1.

## 6. Architecture

Everything routes through one module, **`src/helixgen/controllers.py`**, which already owns the
per-device source-id table and the forward/reverse resolvers. Extend it; do not scatter tables.

### 6.1 Data model
**Implemented (1.0.1):** added `CONTROLLER_META["stadium_xl"]` keyed by identifier, each record
carrying `source_id`, `kind`, `row`, `col`, `canonical_name`, `position_phrase`, `aliases`; the
flat `CONTROLLER_SOURCE_IDS[device]` name→int table is now **derived from** `CONTROLLER_META`
(single source of truth), so the existing forward/reverse resolvers keep working unchanged. The
reserved switches live in a separate `RESERVED["stadium_xl"] = {"FS6": (0x01010105, "MODE"),
"FS12": (0x0101010b, "TAP/Tuner")}` table rather than a per-record flag.

**Reconciliation note (position_phrase):** the §5 table's parenthetical secondary hints
("(2nd from right)", "(top-left)") are stored as `aliases`, and `position_phrase` holds the clean
directional phrase ("top row, 5th from left"), so `english_for_controller` renders
`"Footswitch 5 (top row, 5th from left)"` without nested parentheses (matching the stated contract
example) while the hints still feed the translation sub-agent's alias vocabulary.

The authoring/validation surface moved in the 1.0.0 redesign: controllers are wired by
`mutate.wire_footswitch` / `wire_expression` / `wire_wah_toe` (which wrap `ControllerError` →
`MutateError`) and driven from `recipe.apply_recipe`. `resolve_controller_source` now checks
`RESERVED` first and raises a tailored "not assignable" error for `FS6`/`FS12`; the reverse
`controller_name_for_source` is deliberately left untouched — it still returns `None` for
un-tabled sources and never raises (decode must stay tolerant).

New functions (pure stdlib):
- `english_for_controller(device, identifier) -> str` — e.g. `"Footswitch 5 (top row, 5th from left)"`.
- `controller_mapping(device) -> list[dict]` — the full structured table, JSON-serialisable, for
  the translation sub-agent (served by `helixgen controllers --json`).
- `resolve_controller_source(device, identifier)` — unchanged contract, but now raises a tailored
  error for reserved (`FS6`/`FS12`) vs unknown identifiers, listing the valid canonical set.
- `controller_name_for_source(device, source_id)` — unchanged (reverse lookup).

### 6.2 Decode direction (hsp → human)
**Reconciliation:** the 1.0.0 redesign renamed the decode entry point from `decompile.py` to
`view.py` (a read-only projection off the canonical `.hsp`; no sidecar). The behaviour change
landed there:
- `view.py::_recover_footswitches` / `_recover_expression`: unchanged recovery, but a source id
  not in the table (or a param-driven footswitch out of v1 scope) is **kept and labeled**
  `"unknown control (source 0xNNNNNNNN)"` rather than dropped/warned-away. These labeled entries are
  collected into a **new, separate top-level `unknown_controllers` list** on the projection — kept
  distinct from `footswitches`/`expression` so that `spec.parse_spec` (which reads only known keys
  via `.get()`) ignores it and round-trip stays safe. (This is where EXP3 / the `0x010104NN` bank /
  the `0x0101020N` targetbypass bank surface instead of vanishing.)
- The `tone`/`setup` skill report renders each assigned control with `english_for_controller`
  (name + position), never the bare identifier.

### 6.3 Encode direction (human → hsp) — translation sub-agent
- The skill, when a human describes a control in free text, spawns a **dedicated small-model
  sub-agent** (e.g. Haiku) whose entire job is: given (a) the human phrase and (b)
  `controller_mapping(stadium_xl)`, return **exactly one** canonical identifier, or `AMBIGUOUS`
  (list candidates) or `NONE`.
- Contract: strict output (identifier string / `AMBIGUOUS:<ids>` / `NONE`); the skill **validates**
  the result is in the canonical set before writing it into the spec; on `AMBIGUOUS`/`NONE` the
  skill asks the human to clarify.
- This sub-agent is reusable within a session (same device mapping every call).

### 6.4 Consumers
- **Encoding (authoring):** no change to the on-device encoding. In the redesigned architecture the
  encode path is `recipe.apply_recipe` → `mutate.wire_footswitch`/`wire_expression`/`wire_wah_toe`
  (which resolve via `controllers.resolve_controller_source` and surface the tailored reserved
  errors). The deleted `generate.py`/`spec.py` controller hooks from the pre-1.0.0 design are
  **not** reintroduced.
- `helixgen controllers [--json]` returns the full JSON table so the skill and
  translation sub-agent get the data without a second hard-coded table. (Was
  also an MCP `controller_mapping` tool until the 0.20.0 MCP removal.)
- `CLAUDE.md`: correct the footswitch vocabulary (FS1–5, FS7–11, Exp Toe; FS6/FS12 reserved),
  document the English rendering and the translation-sub-agent flow.
- `tone`/`setup` SKILL.md: auto-wire uses the corrected set (skip FS6; FS1→FS5 then FS7→FS11);
  reports render English; human control descriptions go through the translation sub-agent.

## 7. Backward compatibility & migration
- `FS1–FS5`, `FS7–FS10`, `EXP1`, `EXP2`, `EXP1Toe`: identical meaning and source ids — unaffected.
- `FS6`: now **rejected** ("FS6 is the MODE switch and is not assignable; assignable switches are
  FS1–FS5, FS7–FS11"). It never produced a working assignment anyway.
- `FS11`: newly valid.
- No spec migration tooling needed; the change is additive + one removal.

## 8. Testing
- Table integrity: every canonical identifier has a unique source id; reserved ids excluded from
  the assignable set; every entry has name + position + ≥1 alias.
- `resolve_controller_source`: valid ids resolve; `FS6`/`FS12` raise the tailored reserved error;
  unknown raises the "valid set" error.
- Round-trip: identifier → source → `controller_name_for_source` → identifier; identifier →
  `english_for_controller` is stable and contains both name and position.
- `decompile`: a preset containing an un-tabled source id yields an `"unknown control (…)"` entry,
  not a dropped one; a preset using `0x0101010a` decodes to `FS11`.
- Translation sub-agent: a fixed **phrase → expected-identifier** eval set exercises the
  data/prompt contract (e.g. "top left" → FS1, "second from right bottom row" → FS11, "wah toe" →
  EXP1Toe). The live model call stays out of the deterministic pytest suite; the eval runs as a
  separate check.
- Regenerate/decompile round-trip stays green on real-export fixtures.

## 9. Rollout
1. (Gate) redesign lands → sub-agent re-reviews this spec vs new controller code.
2. TDD implementation in a worktree off main.
3. Device round-trip to confirm `FS11 = 0x0101010a` before finalising.
4. Full suite green + hardware sanity check.
5. Version bump per repo release process; ship.

## 10. Open items
- [ ] Confirm `FS11 = 0x0101010a` via device round-trip. (Coordinated with the 1.0.1 hardware
  confirmation before merge; source is present in 90/211 real exports.)
- [ ] (Stretch) Identify the `0x010104NN` bank, the `0x0101020N` (00–03) targetbypass bank found
  during implementation, and EXP3 (`0x01020102`) for a future authorable pass. All three are
  currently decode-labeled as "unknown control".
- [ ] Decide small-model choice for the translation sub-agent (default: Haiku).
