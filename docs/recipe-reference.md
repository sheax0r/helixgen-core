# helixgen recipe reference

The **recipe** is the JSON author-input to `helixgen generate`. It is
**input-only** — it is not written to disk
and is never read back as truth; the `.hsp` it produces is the canonical
artifact (see [`CLAUDE.md`](../CLAUDE.md) "Architecture: `.hsp` is the source of
truth"). `helixgen view <preset.hsp>` projects a `.hsp` back into this recipe
shape for inspection or hand-authoring a similar preset.

This file is the **full field reference**. CLAUDE.md carries the base shape and
a one-line index of every optional section; the exhaustive per-field detail
lives here.

**Run `helixgen show-block "<block>"` before writing a recipe** — param names
are case-sensitive and `generate` rejects unknown ones.

## Base recipe shape

```json
{
  "name": "Preset Display Name",
  "author": "you",
  "paths": [
    {
      "blocks": [
        {"block": "Compulsive Drive", "params": {"Gain": 0.45, "Tone": 0.55}},
        {"block": "Brit Plexi Brt",   "params": {"Drive": 0.7, "Master": 0.5}},
        {"block": "Mic Ir_4x12 Greenback 25 With Pan"},
        {"block": "Tape Echo Stereo", "params": {"Mix": 0.18}},
        {"block": "Plate Stereo",     "params": {"Mix": 0.12}}
      ]
    }
  ]
}
```

- `paths` is 1–2 entries (each maps to one DSP). Parallel splits inside a path use `split`/`join` entries (see "parallel splits" below).
- `block` matches the display_name from `list-blocks` (e.g. "Brit Plexi Brt") — case-sensitive. If ambiguous, use the model_id in brackets (e.g. "HD2_AmpBritPlexiBrt").
- `params` values are floats 0.0–1.0 for most knobs; some are ints/bools/Hz. Verify ranges with `show-block`.

## Optional: per-path input routing + input block params

Each path entry may carry an optional `"input"` field. The simple form is a
mode string:
- `"inst1"` — Instrument 1 jack only
- `"inst2"` — Instrument 2 jack only
- `"both"` — both jacks (stereo) — **default on paths[0]**
- `"none"` — input disabled — **default on paths[1]**

The object form adds the Input-block params (impedance / pad / trim / gate):

```json
"input": {
  "source": "inst1",
  "impedance": "1M",
  "pad": true,
  "trim": -6.0,
  "gate": {"enabled": true, "threshold": -55.0, "decay": 0.2},
  "link": false
}
```

- `source` — same vocabulary as the string form; optional (same defaults).
- `impedance` — `"FirstBlock"` / `"FirstEnabled"` (the auto modes), `"10K"`,
  `"22K"`, `"32K"`, `"70K"`, `"90K"`, `"136K"`, `"230K"`, `"1M"` (the device's
  full self-described ladder — no 3.5M on Stadium). Preset-level, per jack:
  applies to the jack(s) the source uses (with `"both"`, a per-jack object
  `{"inst1": ..., "inst2": ...}` is accepted). Omitted → the device default
  `"FirstEnabled"`; an omission never conflicts with another path's explicit
  value (explicit wins). Two paths giving the same jack **different explicit**
  values is an error.
- `pad` — bool (instrument sources only).
- `trim` — float dB, −24..6.
- `gate` — `true`/`false` shorthand, or `{"enabled", "threshold" (−96..0 dB),
  "decay" (0.01..1)}`. Giving the gate **object** implies `enabled: true`
  unless you set `"enabled": false` explicitly.
- `link` — StereoLink; `"both"` source only.
- With `"both"`, `pad`/`trim`/`gate.*` also accept per-channel values
  `{"1": x, "2": y}` (a scalar writes both channels).

`generate` always writes the **full** input-endpoint param set (defaults +
your overrides) and the used jacks' impedance — the chassis's gate/trim/pad
state and used-jack impedance never leak into an authored preset. (Scope:
an **unused** jack's impedance and an unused chassis flow's input *model*
keep their chassis values — only their endpoint params are normalized.)
`view` lifts non-default input params back into this object form
(all-default inputs stay the readable string).

Stadium-only; ignored with a warning for `.hlx` (legacy Helix) chassis.

## Optional: per-path output level/pan

```json
"output": {"level": -3.0, "pan": 0.4}
```

- `level` — float dB, −120..20 (the output block's `gain`).
- `pan` — float 0..1 (0.5 = center).
- Applies to the path's primary (lane-0 `b13`) output block. The output
  **destination** (Matrix/XLR/1/4"/Path-2 feed…) is not authored here — it
  round-trips verbatim via `structural` entries; an explicit `output` wins
  over a stale structural copy.

## Optional: parallel splits — split TYPE + merge mixer

A path's `blocks` may carry one or two `split`…`join` regions (lane-1 entries
between them form the B branch). The split takes a friendly `type` and
per-type params; the join is the merge mixer:

```json
{"split": {"type": "crossover", "params": {"Frequency": 800.0, "Reverse": false}}},
{"block": "Tape Echo Stereo", "lane": 1},
{"join": {"params": {"A Level": 0.0, "B Level": -2.0, "B Pan": 0.5,
                     "B Polarity": false, "Level": 0.0}}}
```

- Split types → params (validated; unknown names error and list the valid set):
  - `"y"` — `BalanceA`, `BalanceB` (0..1), `enable`
  - `"ab"` — `RouteTo` (0..1), `enable`
  - `"crossover"` — `Frequency` (25..15000 Hz), `Reverse`, `enable`
  - `"dynamic"` — `Threshold` (−60..0 dB), `Attack`/`Decay` (0.05..5 s),
    `Reverse`, `enable`
- A raw `model` string is still accepted (must agree with `type` if both are
  given); unknown models pass params through unvalidated.
- Join (merge-mixer) params — literal wire names **with spaces**: `"A Level"`,
  `"A Pan"`, `"B Level"`, `"B Pan"` (0..1), `"B Polarity"` (bool), `"Level"`
  (−60..12 dB). The device default for the master `"Level"` is **+3 dB** —
  omit it and the merged signal comes out 3 dB hot; write `"Level": 0.0`
  for unity.
- FX Loop / Send / Return block params (`Send`, `Return`, `Mix`, `DryThru`)
  are ordinary block params — author them like any other block.

## Optional: snapshots (Stadium scenes)

Add a top-level `snapshots` array (up to 8 entries) to define named scenes that override block bypass and param values within one preset:

```json
"snapshots": [
  {"name": "Rhythm"},
  {"name": "Lead",  "params": {"Brit Plexi Brt": {"Drive": 0.85}, "Tape Echo Stereo": {"Mix": 0.30}}},
  {"name": "Clean", "disable": ["Compulsive Drive"], "params": {"Brit Plexi Brt": {"Drive": 0.30}}}
]
```

- Each snapshot is a delta from path-level base values. Snapshot 0 (the first) is active on load.
- `disable: [...]` bypasses those blocks in that snapshot; `params` overrides values.
- Block references must resolve to a block already placed in `paths`.
- Omit `snapshots` entirely to use the device's defaults (8 unnamed slots, no variation).

When a snapshot references a block whose display name is ambiguous (multiple
placed blocks humanize to the same name, e.g. two "Stereo" blocks across a
split), carry a `(lane, pos)` coordinate:

- `disable` entries may be objects instead of bare strings:
  `"disable": [{"block": "Stereo", "lane": 1, "pos": 2}]`
- `params` may be a list instead of a name-keyed object:
  `"params": [{"block": "Stereo", "lane": 1, "pos": 2, "params": {"Mix": 0.3}}]`

Coordinates are only needed to disambiguate; the bare string / name-keyed object
forms remain valid for uniquely-named blocks. `path` (0 or 1) is added only when
the same name is ambiguous across both DSP paths.

## Optional: footswitches

Assign blocks to physical footswitches on the device. The Stadium XL has 12
capacitive footswitches in **2 rows × 6 columns** (top row FS1–FS6, bottom row
FS7–FS12), but only **10 are assignable**: `FS1`–`FS5` (top row) and
`FS7`–`FS11` (bottom row). `FS6` (**MODE**) and `FS12` (**TAP/Tuner**) are
reserved and rejected with a tailored error if you try to assign them. There is
also `EXP1Toe` — the toe switch under the onboard expression pedal (push the
pedal fully forward to click it).

```json
"footswitches": [
  {"switch": "FS3", "block": "Compulsive Drive", "label": "DRIVE", "color": "red"},
  {"switch": "FS3", "block": "Tape Echo Stereo"},
  {"switch": "FS4", "block": "Brit Plexi Brt", "param": "Drive",
   "min": 0.45, "max": 0.7, "behavior": "momentary"},
  {"switch": "EXP1Toe", "block": "Teardrop 310 Mono"}
]
```

- `switch` — an assignable footswitch `"FS1"`–`"FS5"` or `"FS7"`–`"FS11"`, or
  `"EXP1Toe"` (expression-pedal toe switch). `"FS6"`/`"FS12"` are reserved
  (MODE / TAP-Tuner) and not assignable.
- `block` — must reference a block placed in `paths`.
- `behavior` — `"latching"` (default; toggle) or `"momentary"` (on while held).
- **Merge switch**: several entries may share one `switch` — the switch then
  toggles all of its targets at once (blocks and/or params). Each target
  (block, or block+param) may appear only once across all entries.
- **Param toggle**: add `param` plus **required numeric `min`/`max`** (raw
  param units — a Level is in dB, a knob 0..1) and the switch toggles that
  param between the two values instead of the block's bypass. A single-knob
  stomp is a param toggle; a multi-param change is a snapshot.
- **Scribble strip**: `label` (device shows ≤12 chars; longer warns) and
  `color` — one of `none auto red dkorange ltorange yellow green turquoise
  blue purple pink white`. Per switch: on a merged switch set label/color on
  one entry (or identically on all); conflicting values are a spec error.
  Only `FS1`–`FS5`/`FS7`–`FS11` have strips — label/color on `EXP1Toe` (or a
  pedal) warns and is not written.
- `curve` — controller response curve: `"linear"` (default) or `slow5`…`slow1`
  / `fast1`…`fast5`. Non-linear values are EXPERIMENTAL (vocabulary from the
  device's own enum table; persistence hardware-validated, audible response
  not yet characterized).
- `threshold` — flip point (float) for position switches like `EXP1Toe`;
  EXPERIMENTAL. Forces the explicit-bounds controller encoding.
- **Wah/expression auto-engage:** assign the wah's bypass to `EXP1Toe` (with
  `EXP1` sweeping its `Pedal` param) so pressing the pedal toe-down engages the
  wah — the standard Helix wah behavior. A regular `FS` works too but requires a
  separate stomp.

**Controller vocabulary & English rendering.** `helixgen controllers`
(add `--json` for the machine-readable table) lists every assignable
controller with its English name + physical position, e.g.
`Footswitch 5 (top row, 5th from left)`. When reporting a tone to a human,
render controllers in this English form (via `helixgen controllers` /
`controllers.english_for_controller`), never a bare `FS#`. When a human *describes* a control in plain language
("the top-left switch", "second from right on the bottom", "the wah toe"),
translate it to a canonical identifier with a dedicated small-model
translation sub-agent fed the `helixgen controllers --json` table — it returns
exactly one identifier (or `AMBIGUOUS`/`NONE`); validate the result against
the canonical set before writing it into a recipe. `view` never drops controls it
can't map: an un-tabled/out-of-v1-scope source is kept and labeled under a
separate top-level `unknown_controllers` list (ignored by `parse_spec`, so it
stays round-trip safe).

## Optional: expression pedal

Sweep one or more parameters with the expression pedal(s). Stadium XL
exposes `EXP1` and `EXP2`.

```json
"expression": [
  {
    "pedal": "EXP1",
    "targets": [{"block": "Teardrop 310 Mono", "param": "Pedal"}]
  },
  {
    "pedal": "EXP2",
    "targets": [
      {"block": "Brit Plexi Brt",   "param": "Master", "min": 0.0, "max": 0.7},
      {"block": "Tape Echo Stereo", "param": "Mix",    "min": 0.0, "max": 0.4}
    ]
  }
]
```

- `pedal` — `"EXP1"` or `"EXP2"`.
- `targets` — non-empty list. Each target sweeps one param on one block.
- `min`/`max` — normalized 0..1 floats; default `0.0`/`1.0`. **Reverse sweep**
  = `min > max` (heel = max effect, toe = min) — corpus-real and supported.
- `curve` — per-target response curve, same vocabulary as footswitches
  (default `"linear"`; non-linear EXPERIMENTAL).
- One pedal may have many targets. One `(block, param)` pair may be driven by
  at most one controller (pedal OR footswitch param-toggle) across the spec.
- v1 only sweeps 0..1-style float params (knob values). Hz/int/bool params are out of scope.

## Optional: MIDI CC control (EXPERIMENTAL — #33)

Bind incoming **MIDI Control Change** messages to param sweeps and block-bypass
toggles. Add a top-level `midi` list — shape analogous to `expression`, keyed by
CC# instead of pedal:

```json
"midi": [
  {"cc": 61, "targets": [{"block": "Brit Plexi Brt", "param": "Drive",
                          "min": 0.0, "max": 1.0}]},
  {"cc": 79, "targets": [{"block": "Tape Echo Stereo", "bypass": true}]}
]
```

- `cc` — the CC number, integer `0`–`127`. Each CC appears once (list several
  `targets` under it). The MIDI channel is the device's **global base channel**
  (`global.midi.channel`) — not authored per-preset (the parity capture found no
  channel on the wire).
- `targets` — non-empty list. Each target is either a **param sweep**
  (`{"block", "param", "min", "max"}`, normalized 0..1 like `expression`;
  `min`/`max` default `0.0`/`1.0`) or a **bypass toggle**
  (`{"block", "bypass": true}`). A target is one or the other, not both.
  `path`/`lane`/`pos` disambiguate a duplicate block name.
- **One controller per param:** a `(block, param)` is driven by at most one of
  footswitch-param / expression / MIDI across the whole spec. (A block's
  **bypass** may be driven by several sources — e.g. an FS *and* a MIDI CC — the
  device supports multi-source bypass.)
- **CC-only.** MIDI Note controller sources are out of scope (the parity capture
  pinned only the CC source encoding; a `note` field errors).
- **How it's realized:** the binding is NOT written as a device-native `.hsp`
  controller (the `.hsp` `midisource` encoding is 0 across the whole corpus and
  the parity capture pinned only the *device* `.sbe`/wire encoding, so inventing
  an `.hsp` shape is out of scope). It is recorded in a helixgen-namespaced
  `preset._helixgen_midi` list that the **transcoder** turns into the device
  `cg__.entt` `ctrl`/`ctm_` records on `device install`/`sync`. `view` lifts it
  back into this `midi` recipe shape. The surgical edit verbs keep the records
  reconciled: `add-block`/`remove-block` remap their coordinates on renumbering
  (removing a MIDI-bound block drops its binding with a warning), and
  `swap-model` drops a binding whose param the new model lacks (warning).
- **EXPERIMENTAL** until hardware-validated. There is no live `device` verb for
  MIDI assignment yet (author it into the preset). Stadium-only; ignored for
  `.hlx` (legacy Helix) chassis output.

## Optional: Command Center commands (EXPERIMENTAL — #16)

Bind a **footswitch or Instant slot** to a Command Center command — a MIDI
message (PC/CC/Note/MMC) or a Preset/Snapshot action — sent when the switch is
pressed. Unlike `footswitches` (which toggle a block's bypass/param), a command
targets the **device / external MIDI gear / preset-snapshot state**, not a
block. Add a top-level `commands` list:

```json
"commands": [
  {"switch": "FS1",      "command": "snapshot", "snapshot": 2, "label": "SNAP", "color": "red"},
  {"switch": "Instant1", "command": "midi_cc",  "cc": 85, "value": 127, "channel": 2, "toggle": true},
  {"switch": "Instant2", "command": "midi_pc",  "program": 44, "channel": 4},
  {"switch": "FS3",      "command": "midi_note", "note": 60, "velocity": 100, "channel": 1}
]
```

- `switch` — `FS1`–`FS5`/`FS7`–`FS11` or `Instant1`–`Instant6`. Reserved
  `FS6`/`FS12` rejected; EXP continuous commands are out of scope.
- `command` + its fields:
  - `midi_cc` — `cc` (0–127, required), `value` (0–127), `channel` (1–16).
  - `midi_pc` — `program` (0–127, required), `channel`, `bank_msb`/`bank_lsb` (−1=off).
  - `midi_note` — `note` (0–127, required), `velocity`, `channel`, `note_off` (bool).
  - `midi_mmc` — `message` (0–127, required), `channel`. **EXPERIMENTAL.**
  - `snapshot` — `snapshot` (0–7, required).
  - (A recall-`preset` family is **not** offered — it is unanchored and, without
    a decoded Action discriminator, byte-indistinguishable from `snapshot 0` on
    the device. Deferred — see BACKLOG #16.)
- At most **2 commands per switch** (a merged switch — the device's cap).
- `behavior` (`latching`/`momentary`), `toggle` (bool). `label`/`color` set the
  FS scribble strip (Instant slots have no strip — a warning is emitted).
- Several entries may share one `switch` (a **merged switch** — ordinals assigned
  in list order).
- A switch used by BOTH `footswitches` (block bypass/param) AND `commands` is
  rejected (the device allows it; helixgen doesn't compose the two stores yet).
  On read, `view` keeps a device export's command-on-a-footswitch-switch under
  `unknown_controllers` (labeled, ignored by the parser) so the projection
  stays round-trip safe.
- **How it's realized:** authored NATIVELY into `preset.commands` — the encoding
  real exports carry (corpus-proven), NOT a sidecar. The transcoder synthesizes
  the device `cg__.entt` `srcs`/`cmnd`/`trgs` on `device install`/`sync`;
  `view` lifts it back. Commands are switch-keyed, so surgical block edits
  (`add-block`/`remove-block`/`swap-model`) leave them untouched.
- **EXPERIMENTAL.** STORAGE hardware-validated on Stadium XL: snapshot + MIDI
  PC and the **footswitch CC/Note/MMC** slot layouts all round-trip
  byte-for-byte (the footswitch layouts were HW-captured 2026-07-15). Still
  inferred: Instant CC/Note/MMC slots + footswitch PC/Bank slots. The
  audible/functional MIDI response is uncharacterized (needs physical MIDI
  gear). No live `device` verb. Stadium-only; ignored for `.hlx` chassis output.

## Optional: per-block IR reference

For IR blocks (`"block": "With Pan"` and other `HX2_ImpulseResponse*` variants),
add an optional `ir` field to load a registered user IR:

```json
{"block": "With Pan", "ir": "YA DXVB 112 Mix 01.wav",
 "params": {"HighCut": 6500.0, "LowCut": 90.0, "Mix": 1.0}}
```

- `ir` accepts a wav basename (looked up in `mapping.json` values) or a
  32-char hex hash (looked up in keys).
- If `ir` is omitted, the block uses the canonical `irhash` recorded during
  ingest of an IR-bearing preset.
- Register IRs first with `helixgen register-irs`; see `list-irs` for what's
  available.

Stadium-only; ignored without warning for `.hlx` (legacy Helix) chassis output.

## Optional: delay/reverb/FX-loop trails (`trails`)

Delay, reverb, and FX-Loop blocks may carry an optional `"trails"` boolean that controls
harness spillover — whether the block's echoes / reverb tail keep ringing when
the block is **bypassed** (manually or via a footswitch):

```json
{"block": "Tape Echo Stereo", "params": {"Mix": 0.25}, "trails": true},
{"block": "Plate Stereo",     "params": {"Mix": 0.15}, "trails": true}
```

- `trails: true` / `false` sets the block's bNN `harness.params.Trails`.
  - `true` → tail rings out and fades when you bypass the block.
  - `false` → tail cuts off abruptly the instant you bypass the block.
- Trails governs tail spillover on **block bypass** (footswitch or manual) —
  and also across **snapshot switches** within the same preset (the tail rings
  through a scene change instead of cutting). It never bridges a **preset**
  change. To hear the bypass case, bypass the block — ideally while palm-muting
  so the guitar's natural sustain doesn't mask the wet tail. (Footswitch/
  manual-bypass behavior is hardware-validated on Stadium XL.)
- Omitting `trails` leaves the device default (or whatever a decompiled
  `raw.harness` carried) untouched.
- **Delay, reverb, and FX-Loop blocks only** (FX-Loop = `HD2_FXLoop*`; the
  device manual documents Trails there too). Setting `trails` on any other
  block — including Send-/Return-only blocks — is a generate error.
- `view` lifts an existing `Trails` out of `raw.harness` into this clean
  `trails` field (same delay/reverb/FX-loop scope), so it round-trips as a
  first-class setting. If both `trails` and a `raw.harness` are present,
  `trails` wins.
- Stadium-only; ignored for `.hlx` (legacy Helix) chassis (no harness emitted).
- Editing an existing `.hsp` never needs `trails`: `set-param`/edit verbs
  preserve the block's `harness` (and its `Trails`) verbatim in place.

## Optional: per-block verbatim state (`raw`)

A recipe block may carry an optional `"raw"` object holding verbatim Stadium bNN
state that helixgen does not model, so that *authoring* a preset from a recipe
can reproduce it:

- `"harness"` — the bNN-level `harness` dict (carries structural fields like
  `dual`, `upper`, `bypass`, `EvtIdx`, and its own `@enabled`). Non-deterministic;
  preserved verbatim. The one author-facing harness field, `Trails`
  (delay/reverb spillover), is modeled separately as the block-level `trails`
  field above and is lifted out of `raw.harness` by `view`.
- `"slots"` — additional slots beyond the first (`slot[1:]`), i.e. the second
  cab of a dual-cab block.

`raw` is emitted by `view` and consumed by `generate`. **Editing an existing
`.hsp` never needs `raw`** — in-place mutation leaves every unmodeled field
untouched by construction; `raw` matters only for authoring a fresh preset that
carries such state. Stadium-only.
