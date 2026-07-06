# Auto-wire controls (footswitches + wah/expression) — design

**Date:** 2026-07-05
**Status:** Draft (pending user review of this written spec)
**Source brief:** DESIGN task 2026-07-05 ("Auto-wire controls" backlog item) —
the `tone` skill should, by default, wire pedals to sensible footswitches and
route wah / sweep-type effects to an expression pedal, instead of shipping
presets with no live control.

## Goal

When the `tone` skill generates a preset, it should by default hand the player
usable on-stage control:

1. Every toggle-able effect block gets a **latching footswitch** (drive, fuzz,
   boost, modulation, delay, reverb, non-wah filter/pitch) so it can be stomped
   in and out live.
2. Any **wah / whammy / volume-pedal** block gets routed to an **expression
   pedal** so its sweep param actually moves.

Today the skill can emit `footswitches` and `expression` sections (the spec and
generator fully support them — shipped and hardware-validated, see
`2026-05-25-footswitches-design.md` and MEMORY "footswitches + EXP +
input-routing device-validated 2026-05-30"), but the `tone` SKILL.md workflow
never populates them. Presets therefore load with **no** live control: the wah's
`Pedal` param sits frozen at its stored value and no stomp toggles anything. This
feature closes that gap.

## Key finding — the wah sweep param is `Pedal`, not `Position`

The existing docs are wrong on the one detail that makes this feature work.
`CLAUDE.md` and `SKILL.md` both give the expression example as:

```json
{"block": "Teardrop 310", "param": "Position"}
```

`show_block` on every wah in the library says otherwise. The canonical
pedal-position param is **`Pedal`** (a `float`, default `0.5`, range `0..1`),
and the block's real display name is `Teardrop 310 Mono`. There is **no**
`Position` param anywhere in the library. A spec built from the doc example
fails generation with `Unknown param(s) ['Position']`.

Correcting these two doc examples (`Position` → `Pedal`, and verifying the block
name) is part of this feature and is **skill/doc-only** — no code change.

### Sweep-param survey (from the installed library)

| Block family              | Category  | model_id pattern            | Sweep param | Type          | Auto-route? |
|---------------------------|-----------|-----------------------------|-------------|---------------|-------------|
| Wahs (Teardrop, Chrome, Fassel, UKWah 846, Weeper, Colorful, Throaty, Vetta, Chrome Custom) | `filter` | `HD2_Wah*` | `Pedal` | float 0..1 | **yes → EXP1** |
| Pitch Wham                | `pitch`   | `HD2_PitchPitchWhamMono`    | `Pedal`     | float 0..1    | **yes → EXP1** |
| Volume pedal (Vol / Vol Stereo) | `volume` | `HD2_VolPanVol`, `HD2_VolPanVolStereo` | `Pedal` | float 0..1 | **yes → EXP2** |
| Poly Pitch / Downtune     | `pitch`   | `L6SPB_PolyPitch`, `L6SPB_PolyDowntune` | `Interval` | **int** | no (int; EXP v1 is 0..1 floats only) |
| Auto Filter / Mutant Filter / FM4 (Growler, Obi Wah, Voice Box, Synth OMatic) | `filter` | `HX2_Filter*`, `HD2_FM4*` | (envelope/LFO-driven; no `Pedal`) | — | no (not pedal-controlled) |

**Detection rule (canonical):** a block is "pedal-controllable" iff `show_block`
reports a `Pedal` param of type `float`. That single test covers every wah, the
Pitch Wham, and the volume pedal without hard-coding model IDs. The int-`Interval`
poly-whammy is deliberately excluded (matches the EXP v1 "0..1 floats only"
limit already stated in the footswitch spec).

## Default footswitch rules

### Which blocks get an FS (by category)

| Category      | Default            | Rationale                                                        |
|---------------|--------------------|-----------------------------------------------------------------|
| `drive`       | **latching FS**    | Dirt/boost/fuzz on-off is the #1 live gesture.                   |
| `delay`       | **latching FS**    | Kick delay in/out per section.                                   |
| `reverb`      | **latching FS**    | Same.                                                            |
| `modulation`  | **latching FS**    | Chorus/flanger/phaser/tremolo toggles.                          |
| `pitch` (non-Wham) | **latching FS** | Octaver/harmonizer toggle.                                      |
| `filter` (non-wah, non-pedal) | **latching FS** | Auto-filter/synth toggle.                            |
| `eq`          | skip by default    | EQ is usually always-on voicing, not a stomp. Assign an FS only when the block is clearly a "solo EQ boost" (user asked for a boost). |
| `dynamics`    | skip by default    | Comp/gate are set-and-forget always-on. (Opt-in only.)          |
| `send`        | skip               | Routing, not an effect.                                          |
| `amp`, `cab`, IR | **never**       | Core always-on tone. An amp/cab must never land on a footswitch.|
| wah / whammy  | **special** (see below) | Gets EXP; also gets a bypass FS so it can be kicked out.    |
| `volume`      | skip FS            | Gets EXP only; a bypass toggle is pointless.                     |

### Ordering, count, and behavior

- **Ordering:** assign in **signal-chain order** (front of chain → back),
  one block per switch, starting at **FS1** and incrementing. Because drives sit
  at the front of the chain, this naturally puts dirt on the low-numbered
  switches and time-based effects (delay/reverb) on the higher ones — the
  conventional "dirt near your foot, ambience up top" layout falls out for free.
- **Count:** assign one FS per toggle-able block, up to the **10-switch cap**
  (`FS1`..`FS10`). Real presets have 3–6 toggle-able effects, comfortably under
  10. If a preset somehow exceeds 10 toggle-able blocks, assign the first 10 in
  chain order and tell the user which blocks were left un-switched.
- **Behavior:** **`latching`** for everything by default (stomp on, stomp off).
  Reserve **`momentary`** for the case where the user explicitly asks for a
  "hold" gesture (e.g. a boost or Pitch Wham dive you only want while your foot
  is down). The skill does not pick `momentary` on its own.
- **Bijection guarantee:** the spec already enforces one-block-per-switch and
  one-switch-per-block (`_parse_footswitches` dedupes by block key). The skill
  must therefore emit each block at most once and never reuse an `FS` name — it
  builds the assignment list as a simple ordered walk, so this holds by
  construction.

## Wah / whammy → expression rules

### Detection

Use the canonical rule above: after `show_block`, treat any block that exposes a
`Pedal` float param as pedal-controllable. Classify by category for pedal
selection:

- **Wah** (`filter`, `HD2_Wah*`) → sweep `Pedal`.
- **Whammy** (`pitch`, `HD2_PitchPitchWhamMono`) → sweep `Pedal`.
- **Volume** (`volume`, `HD2_VolPanVol*`) → sweep `Pedal`.

### Pedal selection

- **Wah / whammy → EXP1** (the primary rocker; the sound a player expects under
  their toe).
- **Volume pedal → EXP2.**
- If only a volume pedal is present (no wah/whammy), put it on **EXP1** so the
  single expression pedal drives it.
- One pedal can carry multiple targets, but in the common case each pedal drives
  one block's `Pedal`. If both a wah and a whammy are present (unusual), they
  both want EXP1 — assign the wah to EXP1 and the whammy to EXP2, and tell the
  user, since two pedal-sweeps can't share one physical pedal meaningfully.

### Default sweep range

- **Full sweep `min: 0.0`, `max: 1.0`** for wah, whammy, and volume `Pedal`.
  Heel-down = 0.0 (wah closed / whammy at pitch floor / volume silent), toe-down
  = 1.0. This is the universal expectation; the stored `Pedal` default (0.5,
  center) is irrelevant once the pedal drives it.

### Wah bypass — the toe-switch analog

A real wah is inline but only colors the tone when engaged. Recommendation:

- Set the wah block **`enabled: false`** (bypassed) in the base path, and
- Also assign it a **latching FS** (from the FS budget) so the player stomps the
  wah in, rocks EXP1, then stomps it out.

The volume pedal, by contrast, is left **enabled** (always inline) with **no**
bypass FS. (Whether the wah should default off-vs-on is a taste call — see Open
Questions.)

### Conflict handling with user-requested EXP mappings

Auto-routing is a **default that fills only what the user didn't ask for**:

1. If the user explicitly asks for an EXP mapping ("EXP2 sweeps amp Master"),
   that request **wins** and is emitted first.
2. Auto-routing then fills the **remaining** pedal(s). If the user's request
   already occupies EXP1, route the wah to EXP2; if both pedals are taken,
   **skip** auto-routing the wah rather than clobber the user's intent, and tell
   the user the wah's `Pedal` was left un-swept (they can free a pedal or accept
   it manual).
3. The skill must respect the spec's hard constraint — **one `(block, param)`
   pair driven by at most one pedal** (`_parse_expression` raises on duplicates).
   So the skill de-dupes the union of user + auto targets on `(block, param,
   coordinate)` before building the `expression` list; the parser is the
   backstop.

## Integration with snapshots ("snapshots per part")

Footswitches and snapshots are **complementary controls**, not alternatives —
this feature wires FS/EXP *in addition to* whatever the sibling "snapshots per
part" item does, and neither owns the other:

- **Snapshots** = whole-scene recall (verse / chorus / solo): change many
  blocks' bypass + many params at once.
- **Footswitches** = per-effect stomp layered on top of the current snapshot:
  kick one delay in for a single phrase without leaving the scene.
- **Hardware semantics** (established in the footswitch spec): a snapshot load
  sets each block's *base* bypass state, then an FS toggles *from there*. The
  two compose cleanly; the spec needs no cross-section validation. A block may
  appear in both a snapshot `disable` list and a footswitch assignment — that is
  intended (scene sets the default, foot overrides).

**Decision:** auto-wire FS/EXP **even when the preset has snapshots.** A
multi-scene preset still benefits from live stomp control and a working wah.
This document does **not** design snapshot selection or "snapshots per part" —
it only records that the two features coexist without conflict.

## What's skill-only vs needs code

### Skill-only (this feature is almost entirely SKILL.md)

The spec and generator already fully support `footswitches` and `expression`
(dataclasses `FootswitchAssignment`, `ExpressionAssignment`, `ExpressionTarget`
in `spec.py`; source-ID tables in `controllers.py`; hardware-validated). So the
work is:

1. **New SKILL.md workflow step** — "Auto-wire controls" — that, after the chain
   and params are settled (after step 5, before generate):
   - walks the placed blocks, classifies each by category,
   - builds the `footswitches` list (chain order, FS1+, latching, skipping
     amp/cab/IR/eq/dynamics/send/volume),
   - detects pedal-controllable blocks via the `Pedal`-float test and builds the
     `expression` list (wah/whammy→EXP1, volume→EXP2, full 0..1 sweep),
   - sets `enabled: false` on the wah block and gives it a bypass FS,
   - merges with any user-requested FS/EXP (user intent wins; auto fills the
     rest; de-dupe on the spec's constraints).
2. **Doc corrections** — fix the wah EXP example param `Position` → `Pedal` in
   both `SKILL.md` and `CLAUDE.md`; verify the block display name via
   `show_block`. Add a Common-Mistakes row ("wah param is `Pedal`, not
   `Position` — always `show_block` the wah").
3. **Report changes** — the step-8 report and companion `<slug>.md` should list
   the FS map (`FS1 → Compulsive Drive`, …) and the EXP routing (`EXP1 → wah
   Pedal`, …) so the player knows what each control does.
4. **Opt-out** — the skill should honor "no footswitches" / "leave control alone"
   and skip auto-wiring; otherwise auto-wire is the default.

### Needs code — none required for v1

Nothing in `spec.py` / `generate.py` / `controllers.py` must change to ship
this. Optional, out-of-scope niceties that would make the skill's job easier:

- A library helper `is_pedal_controllable(block) -> (bool, sweep_param)` that
  encapsulates the `Pedal`-float test, so the skill doesn't reimplement it.
- Exposing each block's `category` in `show_block` / `list_blocks` output in a
  machine-friendly way (the skill currently infers category from which
  `list_blocks(category=…)` call returned the block).
- A `helixgen list-controllers` CLI/MCP verb (already noted as a follow-up in
  the footswitch spec).

These are conveniences, not blockers — the skill can do all of it today using
`show_block` + the category it already tracks.

## Worked example

Chain: `Compulsive Drive` → `Teardrop 310 Mono` (wah) → `Brit Plexi Brt` (amp) →
`Mic Ir_4x12 Greenback` (cab) → `Tape Echo Stereo` (delay) → `Plate Stereo`
(reverb). Auto-wiring emits:

```json
"footswitches": [
  {"switch": "FS1", "block": "Compulsive Drive"},
  {"switch": "FS2", "block": "Teardrop 310 Mono"},
  {"switch": "FS3", "block": "Tape Echo Stereo"},
  {"switch": "FS4", "block": "Plate Stereo"}
],
"expression": [
  {"pedal": "EXP1",
   "targets": [{"block": "Teardrop 310 Mono", "param": "Pedal", "min": 0.0, "max": 1.0}]}
]
```

with `enabled: false` set on the `Teardrop 310 Mono` block. The amp and cab get
**no** switches (always-on). The wah is bypassed until FS2, then swept by EXP1.

## Open questions for the user

1. **Wah default state.** Ship the wah **bypassed** (off until stomped, needs a
   dedicated FS — authentic pedal behavior) or **enabled** (always inline, sweeps
   as soon as you touch EXP1)? Draft assumes bypassed.
2. **Pedal assignment.** Is **wah/whammy → EXP1, volume → EXP2** the right
   default for your Stadium XL, given the onboard pedal? Or do you want volume on
   the onboard/EXP1 and wah on EXP2?
3. **FS ordering.** Plain **chain order from FS1** — or do you prefer an explicit
   **row split** (dirt/boost on FS1–FS5, time-based on FS6–FS10) that maps to the
   XL's physical top/bottom rows?
4. **EQ / comp toggles.** Keep EQ and comp/gate **always-on (no FS)** by default,
   or auto-assign a switch to a "solo EQ boost" when one is present?
5. **Momentary triggers.** Any block type you'd want as **momentary** by default
   (e.g. a boost or Pitch Wham you hold), or is latching-everywhere fine until you
   ask otherwise?
6. **Snapshot presets.** When a preset already has snapshots, should it **still**
   auto-wire per-effect footswitches (draft: yes), or do you consider snapshots
   sufficient control and want FS suppressed there?
7. **Budget overflow.** In the rare >10-toggle-able-blocks case, is "assign the
   first 10 in chain order, report the rest" acceptable, or should some category
   (e.g. reverb) yield its switch first?
```

