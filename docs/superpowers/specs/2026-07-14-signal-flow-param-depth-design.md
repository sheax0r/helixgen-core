# Signal-flow param depth — design (parity #18, matrix §3)

**Date:** 2026-07-14 · **Status:** implemented this PR · **Scope:** pure
`.hsp` authoring + `view` lift + surgical edits + transcode fidelity for the
five 🟡 signal-flow rows in `docs/stadium-app-parity.md` §3: Input block
params, Output block params, Split TYPE + params, Merge mixer params, FX
Loop / Send / Return params.

All field shapes below were derived from the 211-export real-device corpus in
`data/` (grep evidence quoted inline) and the bundled device defs
(`src/helixgen/device/_defs_data.json`), never invented.

---

## 1. Evidence — what the wire format actually stores

### 1.1 Input endpoint (`b00`, `type: "input"`)

Corpus models: `P35_InputInst1` ×120, `P35_InputInst1_2` ×115 (stereo/both),
`P35_InputNone` ×190, `P35_InputInst2` ×5, `P35_InputMic` ×1.

Slot params (mono shape `{"value": x}`):

| hsp param | type | range (defs) | default (defs) | notes |
|---|---|---|---|---|
| `Pad` | int | 1..2 | 1 | 1 = off, 2 = on (corpus: 346×1, 9×2); inst jacks only |
| `Trim` | float dB | −24..6 | 0.0 | |
| `noiseGate` | bool | | False | corpus: 362 False / 184 True |
| `threshold` | float dB | −96..0 | −48.0 | gate threshold |
| `decay` | float | 0.01..1 | 0.1 | gate decay |
| `StereoLink` | bool | | False | stereo (`_1_2`) model only |

Stereo model (`P35_InputInst1_2`) wraps each param per-channel:
`{"1": {"value": x}, "2": {"value": y}}` plus a scalar `StereoLink`.
`P35_InputMic` (adds `LowCut`, no `Pad`) is **out of v1 scope** — it is not in
the `controllers` input-mode table, so its `b00` passes through untouched
exactly as today.

**Impedance is NOT on the input block.** It lives at the preset level:
`preset.params.inst1Z` / `inst2Z`, a **string** enum. Corpus values:
`"FirstBlock"` ×120, `"FirstEnabled"` ×76, `"1M"` ×13, `"230K"` ×2.
The device **self-describes** the full enum (read live from the Stadium XL via
`/PropertyDefWithKeyGet "preset.inst1.z"`, 2026-07-14):

> `PropertyDef(key='preset.inst1.z', name='Inst 1 In-Z', type='i', vmin=0,
> vmax=9, default=1, enum=['First Block', 'First Enabled', '10k Ohm',
> '22k Ohm', '32k Ohm', '70k Ohm', '90k Ohm', '136k Ohm', '230k Ohm',
> '1M Ohm'])`

so the ladder is exactly 10 values (there is **no 3.5M on Stadium**), the
device-side int is the enum index 0..9, and the factory default is
**index 1 = First Enabled**. `.hsp` strings for the four corpus-observed
values pair with indices 0/1/8/9; the six middle strings
(`10K`..`136K`) follow the observed compact convention (`"230K"`, `"1M"` —
uppercase, no "Ohm") and are marked *inferred* in
`flowparams.IMPEDANCE_VALUES`.

### 1.2 Output endpoint (`b13`, `type: "output"`)

Corpus models: `P35_OutputMatrix` ×311 (the normal destination — both DSPs sum
at the matrix), `P35_OutputPath2A` ×114 / `Path2B` ×5 (path-1→path-2 feeds),
`P35_OutputXLR` ×3, `SPDIF`/`QtrInch`/`None` ×1 each. Every output model
carries exactly two slot params:

| hsp param | type | range (defs) | default |
|---|---|---|---|
| `gain` | float dB | −120..20 | 0.0 |
| `pan` | float | 0..1 | 0.5 |

**Destination routing stays verbatim** (the endpoint's *model*), carried by
`view`'s existing `structural` entries — this feature models only
level/pan, per the parity row.

### 1.3 Split types + params (`type: "split"`)

| recipe `type` | model | params (defs ranges) | corpus |
|---|---|---|---|
| `y` | `P35_AppDSPSplitY` | `BalanceA` f 0..1 (0.5), `BalanceB` f 0..1 (0.5), `enable` b (True) | ×64 |
| `ab` | `P35_AppDSPSplitAB` | `RouteTo` f 0..1 (0.5), `enable` b | ×11 |
| `crossover` | `P35_AppDSPSplitXOver` | `Frequency` f 25..15000 (500), `Reverse` b (False), `enable` b | ×1 |
| `dynamic` | `P35_AppDSPSplitDyn` | `Threshold` f −60..0 (−15), `Attack` f 0.05..5 (0.86), `Decay` f 0.05..5 (0.86), `Reverse` b (False), `enable` b | ×1 |

### 1.4 Merge mixer (`type: "join"`, model `P35_AppDSPJoin` ×72)

`A Level` f −60..12 (0), `A Pan` f 0..1 (0.5), `B Level` f −60..12 (0),
`B Pan` f 0..1 (0.5), `B Polarity` b (False), `Level` f −60..12 (defs def 3,
corpus exemplar 0). Param names contain spaces — they are the literal wire
names.

### 1.5 FX Loop / Send / Return

These are ordinary **library user blocks** (`HD2_SendMono1/2`,
`HD2_ReturnMono1`, `HD2_FXLoopMono1..4`, `HD2_FXLoopStereo*`), so their slot
params (`Send`, `Return`, `Mix`, `DryThru`) were **already first-class** —
authored/validated/lifted like any block param (corpus: `HD2_SendMono1` ×5
with `Send`/`DryThru`; `HD2_ReturnMono1` ×3 with `Return`/`Mix`). The only
gap was **trails**: the device manual documents a Trails param on FX-Loop
blocks (harness-level, like delay/reverb), but helixgen's `trails` field
hard-errored outside delay/reverb.

---

## 2. Recipe shapes (authoring surface)

### 2.1 Input — string (unchanged) OR object

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

- `source` — the existing `inst1|inst2|both|none` vocabulary; optional in the
  object (defaults per path exactly like the string form).
- `impedance` — one of `flowparams.IMPEDANCE_VALUES`, or a per-jack object
  `{"inst1": "...", "inst2": "..."}` (needed when `source: "both"` and the
  jacks differ). Applies to the jack(s) the source uses; `none` + impedance is
  an error. An omitted impedance is non-binding (a path merely using a jack is
  not a "FirstEnabled" request — another path's explicit value wins); two
  paths giving the same jack **different explicit** values is an error.
- `pad` — bool → hsp `Pad` 2/1. Instrument sources only (`none` rejects it).
- `trim` — float dB, −24..6 → hsp `Trim`.
- `gate` — bool shorthand (`true` ≡ `{"enabled": true}`) or object:
  `enabled` (default true when the object is given) → `noiseGate`,
  `threshold` (−96..0) → `threshold`, `decay` (0.01..1) → `decay`.
- `link` — bool → `StereoLink`; `both` (stereo) source only.
- Per-channel values (stereo source only): `pad`/`trim`/`gate.*` accept
  `{"1": x, "2": y}` in place of a scalar (a scalar writes both channels).

### 2.2 Output — object per path

```json
"output": {"level": -3.0, "pan": 0.4}
```

`level` (float −120..20) → hsp `gain`; `pan` (float 0..1) → hsp `pan`.
Applies to the path's **primary** output endpoint (the lane-0 `b13`).
Destination (the endpoint *model*) is untouched. The pre-existing
parsed-but-ignored string form of `"output"` now raises an actionable
SpecError (it never did anything).

### 2.3 Split — friendly `type` + validated params

```json
{"split": {"type": "crossover", "params": {"Frequency": 800.0, "Reverse": true}}}
```

- `type` ∈ `y|ab|crossover|dynamic` (table §1.3). `model` remains accepted
  (back-compat / forward-compat for unlisted models); when **both** are given
  they must agree. One of the two is required.
- For the four known models, `params` are validated (names, types, numeric
  ranges) against `flowparams.SPLIT_PARAM_SCHEMAS`; unknown names error and
  list the valid set. An unknown `model` string skips param validation
  (verbatim pass-through, as today).

### 2.4 Merge (join) — validated params

```json
{"join": {"params": {"A Level": 0.0, "B Level": -2.0, "B Pan": 0.1,
                     "B Polarity": true, "Level": 0.0}}}
```

Same validation policy against `flowparams.JOIN_PARAM_SCHEMA` for the default
`P35_AppDSPJoin`; a custom `model` skips validation.

### 2.5 FX-loop trails

`trails` is now legal on **delay, reverb, and FX-Loop** blocks. Decision:
FX-loop trails joins the first-class `trails` field (not `raw.harness`)
because the device treats it identically to delay/reverb trails (harness-level
spillover flag) and the manual documents it as the same user-facing feature.
The gate is `category in (delay, reverb) or model_id.startswith("HD2_FXLoop")`
— Send-only / Return-only blocks have no trails semantics and still error.
`view` lifts `Trails` symmetrically for the same set.

---

## 3. Semantics decisions

### 3.1 Deterministic input-endpoint normalization (behavior change)

`generate` previously copied `b00` slot params verbatim from the **chassis**
(an arbitrary user export), so authored presets silently inherited the
chassis's gate/trim/pad state, and `preset.params.instNZ` leaked the chassis
impedance. All 211 real exports carry **complete** `b00` param sets — an
empty/partial `b00` is not device-shaped. New rule:

- `generate` always writes the **full modeled param set** for each path's
  `b00`: schema defaults (§1.1, from device defs) overlaid with the recipe's
  input-object fields. String-form / omitted `input` gets pure defaults.
- `generate` always writes `instNZ` for every jack a live source uses:
  recipe `impedance` if given, else `IMPEDANCE_DEFAULT = "FirstEnabled"`
  (the device-declared factory default — `PropertyDef.default == 1 ==
  'First Enabled'`, §1.1). Unused jacks keep the chassis value.

Consequences: the golden corpus is **regenerated** (the goldens previously
pinned empty-`b00` output, which no real export exhibits — the new output is
strictly more device-shaped), and the `view→generate` loop over the corpus
becomes exact for input params and impedance.

Scope (confirmed by adversarial review): chassis flows *beyond* the spec's
paths keep their chassis input **model** (only their endpoint params are
normalized), and an **unused** jack's `instNZ` keeps the chassis value —
"no chassis leak" covers spec-path `b00` params + used-jack impedance.

### 3.2 `view` lift rules

- **Input**: emit the object form iff any modeled `b00` param differs from
  the schema default, or a used jack's `instNZ ≠ "FirstEnabled"`; else keep the
  readable bare string. Stereo channels that agree lift as a scalar; differing
  channels lift as `{"1": x, "2": y}`.
- **Output**: emit `"output": {"level", "pan"}` iff the lane-0 output
  endpoint's `gain`/`pan` differ from (0.0, 0.5). The endpoint itself still
  round-trips verbatim as a `structural` entry (never dropped, per the
  `unknown_controllers` precedent); on regenerate the `output` field is
  applied **after** structural emission, so an edited `output` wins over the
  stale structural copy.
- **Split**: emit `type` alongside `model` for the four known models.
- **Join / FX-loop**: emission unchanged (params were already lifted);
  FX-loop blocks now lift `trails` instead of leaving it in `raw.harness`.

### 3.3 Surgical edits — pseudo-block routing in `set_param`

`mutate.set_param` intercepts the exact block names `input`, `output`,
`split`, `join`, `merge` (alias of `join`) and routes to the new
`mutate.set_flow_param(body, kind, param, value, path=…, pos=…)`:

- `input` params use the **recipe vocabulary**: `impedance`, `pad`, `trim`,
  `gate`, `threshold`, `decay`, `link` (stereo shape handled; scalar writes
  both channels). `impedance` writes the jack(s) of that path's current input
  model.
- `output` params: `level`, `pan` (lane-0 `b13`).
- `split`/`join`/`merge` params: the literal wire names (§1.3/1.4), validated
  against the placed model's schema. `--pos` disambiguates when a path has
  two split regions.

CLI `set-param` and MCP `patch_preset`'s `set_param` op inherit this for free
(both call `mutate.set_param`). These names cannot collide with real library
blocks (display names are humanized model titles; the corpus has none named
`input`/`output`/`split`/`join`/`merge`), and the pseudo-names win by design —
documented in CLAUDE.md.

### 3.4 Transcode (`.hsp → _sbepgsm`)

Split/join params **already** survive (bridge lifts `structural` params;
`_build_structural_block` re-synthesizes the parm list via defs — now pinned
by test). New:

- `bridge.hsp_to_paths` lifts `b00` slot params → `path["input_params"]`
  (device names; stereo `{"1","2"}` wrappers become the device's `.1`/`.2`
  param names) and the lane-0 `b13` output `gain`/`pan` →
  `path["output_params"]`.
- `transcode._make_input_endpoint` / the output-matrix emission apply those
  params over the scaffold defaults via `_synth_parm`.
- `hsp_to_sbepgsm` lifts `preset.params.instNZ` → `recipe["inst_z"]`;
  `_synth_pm` maps the string to the device's `preset.instN.z` int via
  `flowparams` (mapping pinned/validated on hardware; see §4).

### 3.5 Out of scope (documented, deliberate)

- `P35_InputMic` / return-jack input sources (b00 passes through untouched,
  as today).
- Output **destination** model authoring (stays verbatim structural).
- Per-snapshot overrides on endpoint/split/join params (corpus shows e.g.
  snapshotted `RouteTo`; carried verbatim via structural today — split/join
  entries keep whatever `snapshots` arrays the wire dict has only in
  structural form; a modeled split emitted by `view` drops them — same as
  before this change).
- Matrix Mixer (§3 row) — separate subsystem, backlog #17.

## 4. Hardware validation plan (ZZA- prefix, non-activating)

1. **Impedance int mapping (read-only):** ✅ resolved without correlation —
   the device self-describes `preset.inst1.z` (`/PropertyDefWithKeyGet`),
   giving the exact 10-value enum + index order + factory default (§1.1).
   Device int = enum index; `.hsp` string = compact form of the label.
2. **Round-trip:** author `ZZA-FlowParams.hsp` (input gate/pad/trim/Z, output
   level/pan, crossover split params, join mixer params), `device install`
   into the pool, `get_content` back, decode, assert every param value.
   Delete the ZZA- preset afterwards.

### 4.1 Findings (hardware-validated 2026-07-14, Stadium XL @192.168.4.84)

- **Impedance enum + int mapping:** resolved via the device's own
  `/PropertyDefWithKeyGet "preset.inst1.z"` self-description (§1.1) — 10
  values, int = enum index 0..9, factory default 1 = *First Enabled*. No
  content correlation needed. The 4 corpus-observed `.hsp` strings anchor
  indices 0/1/8/9; the middle strings' spelling remains the only inferred
  part (`flowparams.IMPEDANCE_VALUES` comment).
- **ZZA- round-trip: ALL 18 assertions PASS.** Authored
  `ZZA-FlowParams.hsp` via `helixgen generate` (input: 1M impedance, pad on,
  trim −3 dB, gate on @ −55 dB / 0.2 decay; output: level −4.5 dB, pan 0.25;
  crossover split: Frequency 800 Hz, Reverse on; join: A Level −2 dB,
  B Pan 0.1, B Polarity on), transcoded (`hsp_to_sbepgsm`, 11081 bytes),
  installed non-activating into the pool (`install_into_pool`, cid 1199),
  read back with the non-activating `get_content`, decoded, and byte-checked
  every value: input model 770 with `Pad=2 / Trim=-3.0 / noiseGate=True /
  threshold=-55.0 / decay=0.2`; OutputMatrix 783 with `gain=-4.5 / pan=0.25`;
  split model 476 with `Frequency=800.0 / Reverse=True`; join 478 with
  `A Level=-2.0 / B Pan=0.1 / B Polarity=True`; `pm__ preset.inst1.z == 9`
  (1M) and `preset.inst2.z == 1` (default). The ZZA- preset was deleted
  afterwards (pool re-listed to confirm; no ZZA- artifacts remain). The
  device's active tone was never touched (non-activating install + read).
