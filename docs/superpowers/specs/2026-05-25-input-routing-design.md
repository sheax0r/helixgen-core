# Per-path input routing — design

**Date:** 2026-05-25
**Status:** Approved (pending user review of this written spec)
**Source brief:** conversation 2026-05-25 ("our presets are always set up just
to take input from only 1 input jack"), Helix Stadium XL has two front
instrument jacks and a hardware "both" mode that helixgen has been ignoring.

## Goal

Let the spec choose which physical input(s) feed each path. Default the main
path (`paths[0]`) to **both** instrument jacks so the user can plug into
either Inst 1 or Inst 2 and the same preset works.

Today helixgen carries the chassis's `b00` input-endpoint block forward
verbatim. Whatever input mode was set in the chassis-source preset is what
the generated preset gets. The user's chassis was captured from a preset
that used `P35_InputInst1` (Inst 1 only), so every generated preset
inherits that mono-Inst-1 routing. The fix is to let the spec override the
input endpoint on each path.

## Non-goals (this feature)

- Output-endpoint control. The chassis's `b13` output (Matrix / XLR / 1/4" /
  SPDIF / Path2A) stays whatever it was. The existing-but-unused
  `PathEntry.output` field stays parsed-but-unused.
- Per-snapshot input switching.
- Aux/Return/USB/Variax/MIDI input sources. Only the front Inst 1 / Inst 2
  jacks and their combined "both" mode are exposed in v1.
- Devices other than Helix Stadium XL. The architecture supports more
  device variants; only the XL table is populated initially.
- `.hlx` (legacy Helix) chassis output. The feature applies only when the
  active chassis is a Stadium `.hsp` chassis (`_helixgen_chassis_shape =
  "hsp"`). When the chassis is `.hlx`, spec-level `input` values are
  validated for shape (still must be one of the four legal strings) but
  ignored during generation, with a one-line stderr warning ("input
  routing is .hsp-only; ignored for .hlx chassis").

## Spec shape

The existing `PathEntry.input` field — currently parsed by `_parse_path`
but never consumed downstream — becomes meaningful:

```json
{
  "name": "...",
  "paths": [
    {"input": "both", "blocks": [...]},
    {"input": "none", "blocks": [...]}
  ]
}
```

| field   | type | required | default                                           | notes                                                |
|---------|------|----------|---------------------------------------------------|------------------------------------------------------|
| `input` | str  | no       | `"both"` for `paths[0]`, `"none"` for `paths[1]`  | One of `"inst1"`, `"inst2"`, `"both"`, `"none"`.     |

`output` stays declared on `PathEntry` but remains unused; we don't touch
it in this feature.

### Asymmetric defaults

`paths[0]` defaults to `"both"` because the goal is "plug into either jack
and it works on the main signal path." `paths[1]` defaults to `"none"`
because in 181 of the user's 206 real presets, path 1 is unused or a
parallel branch fed from path 0 via a split, not an independent input.
Defaulting path 1 to `"both"` would silently double up the input signal
in nearly every preset — undesirable.

When the user wants a 2-guitar setup (rare for this user but supported),
the spec sets `paths[1].input` explicitly.

## Architecture

A new `INPUT_MODELS` table lives in the same module that owns the FS/EXP
source-ID table (`src/helixgen/controllers.py`):

```python
INPUT_MODELS: dict[str, dict[str, str]] = {
    "stadium_xl": {
        "inst1": "P35_InputInst1",
        "inst2": "P35_InputInst2",
        "both":  "P35_InputInst1_2",
        "none":  "P35_InputNone",
    },
}
```

Empirically derived from the user's 206 real exports — these four model IDs
are the only `b00.slot[0].model` values that appear on stomp paths
across the dataset. No reverse engineering required.

`resolve_input_model(device_id, mode) -> str` raises `GenerateError`
listing the four valid modes if the mode is unknown, with the same
fallback-to-`stadium_xl`-with-warning behavior as the FS/EXP resolver.

## Spec parsing (`src/helixgen/spec.py`)

`_parse_path` already extracts `input` and validates it's a string. We
tighten the check:

```python
VALID_INPUT_MODES = {"inst1", "inst2", "both", "none"}

inp = data.get("input")
if inp is not None:
    if not isinstance(inp, str):
        raise _err(source, '"input" must be a string if provided.')
    if inp not in VALID_INPUT_MODES:
        raise _err(
            source,
            f'"input" must be one of "inst1", "inst2", "both", "none" '
            f'(got {inp!r}).',
        )
```

Defaults are applied at generate time (not parse time), so the parsed
`PathEntry.input` stays `None` when the spec omits it. Generate maps
`None` → the per-path-index default. This keeps the parsed `Spec` faithful
to user input — useful for diagnostics ("did the user say this, or was it
defaulted?").

## Generation (`src/helixgen/generate.py`)

After the chassis is loaded and user blocks are placed in `preset.flow`,
but before the dict is serialized, a new pass rewrites the input
endpoint on each path:

```python
DEFAULT_INPUT_MODES = ("both", "none")  # by path index

for path_index, path_entry in enumerate(spec.paths):
    mode = path_entry.input or DEFAULT_INPUT_MODES[path_index]
    target_model = controllers.resolve_input_model(device_id, mode)
    _rewrite_input_endpoint(flow[path_index], target_model)
```

Both `DEFAULT_INPUT_MODES` and `_rewrite_input_endpoint` live in
`generate.py` alongside the existing chassis-handling helpers.

`_rewrite_input_endpoint(path_dict, target_model)`:

1. Locate `path_dict["b00"]["slot"][0]`. If missing, raise `GenerateError`
   ("chassis has no b00 input slot on path N").
2. If the existing `model` already equals `target_model`, return (no-op).
3. Detect current stereo character: a param is stereo if its value is a
   dict with a `"1"` subkey containing a `value` field.
4. Detect target stereo character: `target_model` ends in `_2` (i.e.
   `P35_InputInst1_2`) → stereo; otherwise mono. This `_2` heuristic is a
   tight match against the Stadium XL data; if other devices use a
   different naming convention, we extend the rule when we add them.
5. Reshape params if the character changes; see helper below.
6. Set `slot[0]["model"] = target_model`.
7. Leave `@enabled`, `endpoint`, `favorite`, `harness`, `path`, `position`,
   `type`, and the slot's `@enabled`/`version` untouched. These are
   chassis-level fields the device wires up; we don't manipulate them.

### Mono ↔ stereo param reshape

`_reshape_input_params(params: dict, *, to_stereo: bool) -> dict` returns
a new params dict in the target shape:

- **mono → stereo:** for each scalar param `{"value": x}`, rewrite to
  `{"1": {"value": x}, "2": {"value": x}}`. After processing, add
  `StereoLink: {"value": False}` (matches real `P35_InputInst1_2`
  exports). If the source dict already had a `StereoLink` (it won't, in
  the mono case, but defensively), preserve it.
- **stereo → mono:** for each `{"1": {"value": x}, "2": {"value": y}}`,
  collapse to `{"value": x}` (channel 1 wins; channel 2 is discarded —
  consistent with `hsp._unwrap_value`'s existing stereo fallback). Drop
  `StereoLink` if present.
- Same character on both sides → return params unchanged.

Param values are preserved across the swap — the user's Pad, Trim,
noiseGate, decay, and threshold values from the chassis carry forward.
This is the whole point of mutating the existing block rather than
replacing it with a reference.

## Testing

- `tests/test_spec_input.py` — parse-level: all four valid modes accepted,
  invalid value rejected with the listed valid set, `input` omitted
  produces `PathEntry.input = None`.
- `tests/test_generate_input.py` — generate-level round-trip for each
  mode: spec → bytes → re-parsed dict → assert `b00.slot[0].model`,
  assert mono/stereo param shape, assert `StereoLink` exists iff stereo.
  Includes "no input specified" case verifying the asymmetric defaults
  (path 0 → `P35_InputInst1_2`, path 1 → `P35_InputNone`).
- `tests/test_input_reshape.py` — direct unit tests of
  `_reshape_input_params`:
    - mono → stereo wraps each value and adds `StereoLink: false`
    - stereo → mono takes channel 1 (verify channel-2 value is discarded
      cleanly — assert resulting value matches channel 1, not channel 2,
      using distinct test values)
    - identity (mono → mono, stereo → stereo) returns unchanged params
- Fixture-gated real-export test: ingest a `data/*.hsp` whose path 0 uses
  `P35_InputInst1_2` (113 candidates), extract its `b00` params shape,
  build a synthetic mono chassis with matching scalar values, run
  reshape mono → stereo, and assert the result's structural shape (set
  of keys, presence of `StereoLink`, dict-vs-scalar param topology)
  matches the real export's. Specific param values are not asserted
  (they're user-tuned and not interesting for shape correctness).

Manual on-hardware validation: generate three presets (one each
`input: "inst1"`, `"inst2"`, `"both"`), load on the Stadium XL, plug
into Inst 1 only / Inst 2 only / both, confirm signal flow matches the
mode.

## Risks and open questions

1. **Stereo character heuristic.** Detecting "this `b00` is stereo" from
   "params have a `1` subkey" is empirical. All 113 of the user's
   `P35_InputInst1_2` exports follow this shape, and none of the mono
   variants do. Low risk but worth a defensive assert (raise
   `GenerateError` if model is `P35_InputInst1_2` but params look mono,
   or vice versa) for early failure if the device firmware changes the
   wrapping in a future revision.

2. **Channel-1-wins on stereo → mono is lossy.** A user who has different
   gate thresholds on each channel of a stereo input and then switches
   the spec to mono silently loses the channel-2 values. This is fine
   for v1 (users specifying `inst1` are explicitly choosing a mono setup
   and shouldn't have stereo-mismatched gain staging), but worth a note
   in the user-facing CLI docs once we write them.

3. **`StereoLink` default.** Real exports show `StereoLink: false` for
   stereo `b00` blocks in this dataset. If a future export ever shows
   `StereoLink: true` we'll know — and can revisit whether that should
   be exposed in the spec. Out of scope for v1.

4. **Path 1 with explicit `inst1` and path 0 also `inst1`** — the device
   allows this (one signal feeding two parallel chains) and it round-
   trips fine through our model swap, so no special-case validation is
   needed. Just a reminder that no global "uniqueness across paths"
   constraint applies.

## Out-of-scope follow-ups

- Output-endpoint control (`PathEntry.output`).
- Aux / Return / USB / Variax inputs (the full P35 input model family).
- Per-snapshot input switching.
- Stadium standard (non-XL) and Helix Floor input model tables.
- Exposing per-channel input params for `P35_InputInst1_2` (separate
  Pad/Trim/threshold per jack).
