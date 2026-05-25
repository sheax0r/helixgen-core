# Footswitch + expression-pedal assignments — design

**Date:** 2026-05-25
**Status:** Approved (pending user review of this written spec)
**Source brief:** conversation 2026-05-25 ("set up footswitches", scope clarified
to FS with latching/momentary plus expression-pedal targets with min/max,
across multiple pedals)

## Goal

Add optional `footswitches` and `expression` sections to the helixgen spec so
generated `.hsp` presets carry the controller-block wrapping that a real
Helix Stadium XL writes when a user assigns a block to a footswitch or
assigns a parameter to an expression pedal.

Today the spec supports `snapshots` (scene-style per-snapshot bypass/value
overrides) but cannot express footswitch or expression-pedal assignments.
`generate.py` currently emits the "plain" `@enabled` form, with a code
comment explicitly noting the controller-wrapped form is the form that
carries footswitch info and that we do not emit it.

## Non-goals (this feature)

- Snapshot-mode footswitches (FSs that select a snapshot instead of toggling
  a block).
- MIDI controllers, threshold/curve/delay knobs on the controller block.
- Per-snapshot variation of EXP targets or FS assignments (the assignment is
  preset-level, not snapshot-level).
- Auto-discovery of FS/EXP labels for the LCD — the device does not store
  custom labels; it shows the assigned block's name.
- Devices other than Helix Stadium XL in v1. The architecture supports more
  device variants, but only the XL table is populated initially.

## Spec shape

Two new optional top-level keys on the existing spec object:

```json
{
  "name": "...",
  "paths": [...],
  "snapshots": [...],

  "footswitches": [
    {"switch": "FS3", "block": "Compulsive Drive"},
    {"switch": "FS4", "block": "Tape Echo Stereo", "behavior": "momentary"}
  ],

  "expression": [
    {
      "pedal": "EXP1",
      "targets": [
        {"block": "Teardrop 310", "param": "Position"}
      ]
    },
    {
      "pedal": "EXP2",
      "targets": [
        {"block": "Brit Plexi Brt", "param": "Master", "min": 0.0, "max": 0.7},
        {"block": "Tape Echo Stereo", "param": "Mix", "min": 0.0, "max": 0.4}
      ]
    }
  ]
}
```

### `footswitches` entry

| field      | type   | required | default     | notes                                                 |
|------------|--------|----------|-------------|-------------------------------------------------------|
| `switch`   | str    | yes      | —           | Logical name. Stadium XL: `FS1`..`FS10`.              |
| `block`    | str    | yes      | —           | Matches a placed block by `display_name` or `model_id`. Same resolution rules as `snapshots`. |
| `behavior` | str    | no       | `"latching"`| `"latching"` (toggle) or `"momentary"` (on while held).|

### `expression` entry

| field      | type    | required | default | notes                                                   |
|------------|---------|----------|---------|---------------------------------------------------------|
| `pedal`    | str     | yes      | —       | Logical name. Stadium XL: `EXP1`, `EXP2`, `EXPONBOARD`. |
| `targets`  | list    | yes      | —       | Non-empty list of `target` objects (see below).         |

### `target` object (inside `expression[i].targets`)

| field   | type    | required | default | notes                                                                       |
|---------|---------|----------|---------|-----------------------------------------------------------------------------|
| `block` | str     | yes      | —       | Resolves to a placed block, same as `footswitches[i].block`.                |
| `param` | str     | yes      | —       | Must exist on the target block's schema (same check the generator already does for `params` and snapshot overrides). |
| `min`   | float   | no       | `0.0`   | Heel-down value, normalized 0..1 (same convention as `params` for knob-style values). |
| `max`   | float   | no       | `1.0`   | Toe-down value, normalized 0..1. Must satisfy `min <= max`.                 |

For params that aren't in 0..1 space (Hz frequencies, integer counts,
booleans), v1 only supports EXP assignment to 0..1-style float params. If
the spec targets a non-float param with EXP, the generator raises
`GenerateError`. Hz/int/bool EXP targets are an out-of-scope follow-up.

### Interaction with `snapshots`

Unchanged in the spec. Snapshots continue to set per-snapshot base
enabled/param values. The controller block we wire for an FS-assigned block
applies on top at runtime — the device behaves as: snapshot load sets the
block's base bypass state, then the FS toggles from there. This matches the
hardware semantics and requires no special-case validation between the two
sections.

For EXP, current values for the swept param are still those produced by the
snapshot (snapshot 0 by default); the pedal sweeps from its `min` to `max`
mapped through the device's pedal position. We do not attempt to make the
EXP range vary per snapshot in v1.

## Architecture

A new module `src/helixgen/controllers.py` owns:

- `CONTROLLER_SOURCE_IDS: dict[str, dict[str, int]]` — outer key is the
  chassis's `meta.device_id` (or a logical alias like `"stadium_xl"`); inner
  key is the logical FS/EXP name; value is the `source` integer the
  controller block needs.
- `resolve_source_id(device_id: str, logical_name: str) -> int` — raises a
  `GenerateError` with the list of valid names for the device if the
  logical name is unknown.
- `valid_names_for(device_id: str) -> list[str]` — used in error messages
  and (later) by `helixgen list-controllers` if we add such a CLI command.

The Stadium XL table is **empirically derived** from the user's real
exports under `data/`. We write a one-time helper script (committed under
`scripts/derive_controller_table.py`, not invoked at runtime) that:

1. Takes a hand-built reference preset where the user has assigned known
   blocks to known FS/EXP slots.
2. Ingests it, walks `preset.flow` looking for `controller.source` values,
   walks `preset.sources` for additional declared source IDs.
3. Prints the derived `{logical_name: source_id}` mapping.

The output is reviewed and pasted into `controllers.py`. The script is for
table maintenance only; it never runs during `helixgen generate`.

## Spec parsing (`src/helixgen/spec.py`)

New dataclasses:

```python
@dataclass
class FootswitchAssignment:
    switch: str          # "FS1".."FS10" (validated at parse, resolved at generate)
    block: str
    behavior: str = "latching"

@dataclass
class ExpressionTarget:
    block: str
    param: str
    min: float = 0.0
    max: float = 1.0

@dataclass
class ExpressionAssignment:
    pedal: str           # "EXP1" / "EXP2" / "EXPONBOARD"
    targets: list[ExpressionTarget]

@dataclass
class Spec:
    # existing fields...
    footswitches: list[FootswitchAssignment] = field(default_factory=list)
    expression: list[ExpressionAssignment] = field(default_factory=list)
```

`parse_spec` gains `_parse_footswitches` and `_parse_expression` helpers
following the existing `_parse_snapshots` / `_parse_snapshot` pattern.

### Parse-time validation

- Each list must be a list if present (missing key → empty list).
- Each entry must be an object with required fields of the correct type.
- `behavior`, if present, must be `"latching"` or `"momentary"`.
- `min` and `max` must be floats in `[0.0, 1.0]` with `min <= max`.
- Within `footswitches`: no two entries may share a `switch` value; no two
  entries may share a `block`. (One FS per block, one block per FS.)
- Within `expression`: no two entries may share a `pedal` value. No two
  targets, anywhere in the spec, may share a `(block, param)` pair (so a
  single param cannot be driven by two pedals).
- Block-name and param-name resolution against the library is NOT done at
  parse time — same as snapshot params today. Those errors surface from
  `generate`.

Error messages follow the existing style: `Spec at <input> footswitches[1]:
"behavior" must be "latching" or "momentary".`

## Generation (`src/helixgen/generate.py`)

### Resolving against the chassis

`generate.generate_preset` extracts `device_id` from the chassis once,
near where it already reads `_helixgen_chassis_shape`. If the device_id is
missing or unknown to `CONTROLLER_SOURCE_IDS`, fall back to `"stadium_xl"`
and emit a one-line warning to stderr ("warning: chassis device_id
'<x>' not in controller table; assuming stadium_xl"). The warning is
suppressed when device_id is the XL alias.

### Building controller blocks

A new helper `_build_controller_block(source_id, behavior, type_, *,
min=None, max=None)` returns the dict that wraps `@enabled` (for FS) or
wraps the target param's `value` (for EXP). The shape, derived from real
exports, is:

```python
{
    "behavior": behavior,            # "latching" | "momentary"
    "bypassed": False,
    "curve": "linear",
    "delay": None,
    "goid": None,
    "max": max,                       # None for FS, float for EXP
    "midisource": 0,
    "min": min,                       # None for FS, float for EXP
    "source": source_id,
    "threshold": None,
    "type": type_,                    # "targetbypass" for FS, parameter form for EXP
}
```

The exact EXP `type` value (and whether EXP targets use the same wrapper
shape or a slightly different one — e.g. inside the param value dict vs.
wrapping it) is confirmed empirically against a real export with EXP
assignments during implementation. The reference-preset helper script
above produces the ground truth.

### Wiring into the slot dict

Today `_wrap_value_with_snapshots` produces `{"value": True}` or a snapshot-
wrapped variant for the `@enabled` field at the `bNN` level. For an
FS-assigned block, we instead emit `{"value": True, "controller": {...}}`
(or, if the block also has snapshot variation, `{"value": True, "snapshots":
[...], "controller": {...}}`). Snapshot wrapping and controller wrapping
compose; both can be present.

For EXP, the controller wraps the target param's inner value dict at the
`slot.params[<param>]` level. The same composition rule applies: snapshot
overrides on that param still get the `"snapshots": [...]` key alongside
the controller key.

### `preset.sources`

After all controller blocks are emitted, we collect every distinct
`source_id` used and ensure `preset.sources` contains an entry
`{"<source_id>": {"bypass": false}}` for each. The chassis may already
carry entries from the originating export; we preserve those and add any
new ones. We do not delete chassis entries even if they go unreferenced by
the new spec — pruning is out of scope for v1.

### Generation-time validation

Reuses `_resolve_snapshot_block` (and refactors it to a neutral name like
`_resolve_spec_block` if needed) for both FS and EXP block lookups —
unknown name or ambiguous match raises `GenerateError` with the same
phrasing snapshots already use.

For EXP: after resolving the block, check that the named `param` exists in
the block's library schema; otherwise raise `GenerateError` listing valid
param names — same pattern as the existing param check.

For switch/pedal names: `controllers.resolve_source_id` raises
`GenerateError` with valid names when the logical name isn't in the
chassis's table.

## Testing

New test files mirroring the existing snapshot test layout:

- `tests/test_spec_footswitches.py` — `parse_spec` happy path, shape errors,
  defaults, duplicate-detection cases.
- `tests/test_spec_expression.py` — same, for `expression`. Includes
  multi-target, min/max bounds, and `min > max` rejection.
- `tests/test_generate_footswitches.py` — round-trip: spec → generated
  `.hsp` payload (bytes after the 8-byte magic) → re-parsed dict →
  assertions on the controller block's `source`, `behavior`, `type` and
  on `preset.sources` membership. Both `"latching"` and `"momentary"`
  cases. Also: composition with `snapshots` (FS-assigned block that also
  has a per-snapshot disable).
- `tests/test_generate_expression.py` — same, for EXP. Single-target,
  multi-target across two blocks, custom min/max, EXP1+EXP2 in one spec.
- `tests/test_controllers.py` — table sanity: every entry in the Stadium
  XL table is unique within its inner dict; the table is non-empty; the
  expected logical names are all present.
- `tests/test_generate_footswitches_real.py` — fixture-gated integration
  test that takes a real `data/*.hsp` with known FS assignments, ingests
  it, asserts our extracted source IDs match what `controllers.py`
  declares. Guarded with the project's standard skip-if-not-present
  pattern so the suite stays green on a clean clone.

Manual on-hardware validation: load a generated `.hsp` carrying FS + EXP
assignments on the user's Stadium XL and confirm switches and pedal sweep
behave as specified. This is a separate step before declaring the feature
"device-validated," matching the v1 milestone pattern.

## Risks and open questions

1. **EXP wrapper shape uncertain.** The controller-block schema is well-
   documented for FS (`type: "targetbypass"`, `@enabled`-level wrap). For
   EXP it is inferred but not yet confirmed against a real export with EXP
   assignments. The reference-preset script will resolve this during
   implementation; if the actual shape differs from the table above, we
   update `_build_controller_block` accordingly. This is a known unknown
   and is not a blocker for the spec design.

2. **Source-ID table is empirically derived.** We are inferring that
   `0x01010100..0x01010109` are Stadium XL stomp-mode FS1..FS10 and that
   `0x01010400..` are expression pedals. If the inference is wrong, the
   generated preset will load with controllers wired to the wrong
   switches. The round-trip test against a reference preset catches this
   before shipping.

3. **`device_id` mapping.** Different Stadium XL units may emit different
   `device_id` values in exports (firmware version, hardware revision).
   We may need a small set of aliases that all map to `"stadium_xl"` in
   the table. Will be handled by the fallback warning until we see more
   variation in the data.

4. **`EXPONBOARD` existence.** The XL ships with an onboard expression
   pedal but it is unconfirmed whether it appears in `.hsp` exports under
   a distinct logical source ID or shares one of `EXP1`/`EXP2`. If the
   reference-preset script shows it shares an ID, `EXPONBOARD` becomes an
   alias rather than a separate entry. If it doesn't appear at all in
   `preset.flow` controllers (because routing is fixed in hardware), we
   drop `EXPONBOARD` from the table entirely. Resolved during
   implementation.

5. **Snapshot + controller composition is untested.** A block that has
   both per-snapshot variation and an FS assignment would emit
   `{"value": True, "snapshots": [...], "controller": {...}}`. We have
   not yet seen this exact combination in the user's exports; the
   reference-preset helper script should generate one to confirm the
   key ordering and shape are accepted by the device.

## Out-of-scope follow-ups

- Stadium standard (non-XL) and Helix Floor source-ID tables.
- Snapshot-mode footswitch assignment (FSs that select snapshots).
- MIDI controller sources (e.g. external MIDI footswitch controllers).
- Per-snapshot EXP target / range variation.
- Toe switch (the heel→toe pedal click that swaps EXP targets).
- A `helixgen list-controllers` CLI command listing the valid switch and
  pedal names for the active chassis.
