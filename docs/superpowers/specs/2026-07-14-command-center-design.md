# Command Center authoring + synthesis (#16) — design + decoded ground truth

Status: implemented (EXPERIMENTAL). Route: **native `.hsp` encoding** (`preset.commands`).

## 1. Route decision (backlog item 1): NATIVE `.hsp`, corpus-proven

Unlike #33 MIDI-CC (whose `.hsp` `midisource` was 0 across the whole corpus,
forcing a `_helixgen_*` sidecar), Command Center **has a first-class `.hsp`
encoding** that real exports carry. Corpus reconnaissance over all 211
`data/*.hsp` exports found **`preset.commands`** in two presets:

- `Mandarin Fuzz.hsp` — one `PresetSnapshot` command on FS1 (`0x01010100`).
- `Epic Lots of EQ.hsp` — three `MIDI` commands on Instant 1/2
  (`0x04040100`/`0x04040101`): CC#85 (×2, merged) + PC 1.

So helixgen authors Command Center **natively** into `preset.commands`; no
sidecar. `view` lifts it back; the surgical edit verbs leave it untouched (it
is keyed by controller source, not block coordinate — see §4).

### `preset.commands` shape (corpus-exact)

`preset.commands` is a dict keyed by the **controller source-id string** (the
same ids `preset.sources` and `controllers.py` use), each mapping to an ordered
list of command records:

```json
"commands": {
  "16843008": [                         // 0x01010100 = FS1
    {"behavior": "latching", "curve": "linear", "delay": 0, "goid": 0,
     "ordinal": 0, "threshold": 0.0, "toggle": false, "type": "PresetSnapshot",
     "params": {"Action": {"value": 0}, "Command": {"value": 0},
                "Preset": {"value": 0}, "Setlist": {"value": 0},
                "Snapshot": {"value": 0}}}
  ]
}
```

- `type`: `"PresetSnapshot"` or `"MIDI"` (the two corpus families; HotKey/Utility
  — findings §5 family 4 — has no observed `.hsp` param vocab and is out of scope).
- `MIDI` param set: `CC#`, `Command`, `LSB`, `MIDI Ch`, `MSB`, `Message`, `Note`,
  `NoteOff`, `PC`, `Value`, `Velocity` (all 11 always present). The **`Command`
  param picks the message subtype**: `0`=PC, `1`=CC, `2`=MMC, `3`=Note
  (findings §5, confirmed by Epic: `Command:1`+`CC#:85` = CC, `Command:0`+`PC:1`
  = PC).
- `PresetSnapshot` param set: `Action`, `Command`, `Preset`, `Setlist`,
  `Snapshot`.
- Several records under one source key = a **merged switch** (`ordinal` 0,1,…).
- Each command source also gets a `preset.sources[key]` entry (FS: `bypass`
  + `fs_color`/`fs_label`/`fs_topidx`; Instant: just `{"bypass": false}`).

## 2. Recipe surface (top-level `commands` list)

```json
"commands": [
  {"switch": "FS1",      "command": "snapshot", "snapshot": 2},
  {"switch": "Instant1", "command": "midi_cc",  "cc": 85, "value": 127, "channel": 1, "toggle": true},
  {"switch": "Instant1", "command": "midi_cc",  "cc": 85, "value": 0},
  {"switch": "Instant2", "command": "midi_pc",  "program": 1, "channel": 1},
  {"switch": "FS3",      "command": "midi_note", "note": 60, "velocity": 100, "channel": 1}
]
```

### Adversarial-review outcome (2026-07-14, two rounds)

**Round 1** (pre-PR diff) found no crashes and confirmed robust handling of
corrupted input; the shipped families round-trip losslessly **in isolation**.
Fixed: **H1/H2** — dropped the unproven, device-ambiguous recall-`preset`
family (view now skips a device export's recall-preset command with a warning
rather than misprojecting it as a snapshot); **M1** — `nxtm` stays the
historical `1` for a command-free preset (only advanced past command entities
when commands exist), so the change is a no-op for every non-command tone;
**M2** — merged commands capped at 2 (the device slot count) at parse time;
**M3** — the `.hlx` compose warning now names `commands`/`midi`; **L3** — the
unknown-field error list is sorted. Left (LOW, matches existing patterns):
switch identifiers are validated at generate time (like `footswitches`), not
in `parse_spec` (which has no device_id).

**Round 2** (PR #50 delta) found **1 Critical — FIXED**: round 1's "lossless
round-trip of every shipped family" claim did NOT hold for the FS-collision
case. `Mandarin Fuzz.hsp` carries BOTH a block-bypass footswitch AND a
PresetSnapshot command on FS1 (device-legal); `view` projected both as
first-class entries and `parse_spec` hard-rejects the combination, so
`parse_spec(view(export))` failed — regressing the 211-export acceptance net
to 210/211. Fix (the codebase's established never-drop / never-emit-unparseable
idiom): `view` now emits a command whose switch also carries a footswitch
assignment under **`unknown_controllers`** (which `parse_spec` ignores) with a
stderr warning — NOT as a first-class `commands` entry. So **FS+command
composition round-trips via the `unknown_controllers` bucket, not as
first-class recipe output**; `parse_spec` still rejects the combination in a
hand-authored recipe (composing remains unimplemented and errors loudly at
authoring time). Acceptance + sonic nets re-verified 211/211 after the fix.
Round 2 also flagged for honesty (recorded in §3/§5): the footswitch command
`srcs.locl = 25 + NN` mapping for FS2–FS11 is EXTRAPOLATED from the single FS1
anchor (`locl 25`) plus the controller-srcs pattern — only FS1 was observed
carrying a command; and Instant 3–6 source ids follow the Instant 1/2 pattern
(already flagged).

- `switch`: `FS1`–`FS5`/`FS7`–`FS11` (`0x010101NN`) or `Instant1`–`Instant6`
  (`0x0404010N`). Reserved `FS6`/`FS12` rejected (tailored error). EXP continuous
  commands are out of scope (no `.hsp` source id anchored in the corpus).
- `command`: `midi_cc` / `midi_pc` / `midi_note` / `midi_mmc` (EXPERIMENTAL) /
  `snapshot`. A recall-`preset` family is **deferred** — it is unanchored (no
  corpus example) and, without a decoded Action/Command discriminator, a
  recall-preset to preset 0 is byte-indistinguishable from `snapshot 0` on the
  device (adversarial-review H1/H2).
- Optional common: `behavior` (`latching`/`momentary`), `toggle` (bool),
  `label`/`color` (FS scribble only).
- Same-switch entries merge in list order (ordinals auto-assigned), **max 2**
  (the device's observed `srcs.cmds` slot count).
- A switch used by BOTH `footswitches` (block bypass/param) AND `commands` is
  **rejected** with a clear error. The device *does* support it (Mandarin's FS1
  drives a block bypass ctrl AND a command on the same `srcs` entry), but
  helixgen does not compose the two stores yet — documented limitation.

## 3. Transcoder synthesis — decoded device ground truth (`cg__.entt`)

Pulled live 2026-07-14 (non-activating `GetContentData`): `Mandarin Fuzz`
(cid 403) + `ZZCAP-CC` (pool cid 1205, the parity-capture preset). This
**corrects findings §5**, which guessed `pvla..pvll` (12) uniformly.

A command is an **entity**. For each recipe command helixgen emits:

- a **`srcs`** entry `{id__, locl, ctxt, type, byps:false, cmds:[c1,c2], cnt1..3:0,
  mtms:0, mtyp:0}` — one per physical source, its `cmds` listing the command
  `cid_`s (≤2 observed). `(locl, ctxt, type)` by switch:
  - FS `0x010101NN` → `locl 25+NN, ctxt 1, type 1`. NB: for a **command** src
    only FS1 (`locl 25`) is anchored (Mandarin); FS2–FS11 are EXTRAPOLATED from
    that anchor + the same `25+NN` progression the (well-anchored) controller
    srcs use.
  - Instant `0x0404010N` → `locl N, ctxt 0, type 4` (Instant1 = locl 0
    anchored by ZZCAP; Instant 2–6 follow the pattern — 2 also anchored via
    the Epic `.hsp` source id).
- a **`cmnd`** entry `{cid_, type, func, behv, curv:5, dlay:0, goid:0, thrs:0.0,
  togl, trig, tid_, pvl*, psp*}`:
  - `cid_` = the command's **entity id** (allocated after block entities).
  - `type` = family: **1 = PresetSnapshot, 6 = MIDI**.
  - `func` = subtype. The native `.hsp` `Command` param orders MIDI subtypes
    `0`=PC/`1`=CC/`2`=MMC/`3`=Note, but the **device footswitch/Instant `func`
    swaps Note/MMC**: `0`=PC/`1`=CC/**`2`=Note/`3`=MMC** (HW capture 2026-07-15).
    `_command_payload` maps the `.hsp` `Command` value → device `func` via
    `_HSP_TO_DEVICE_MIDI_FUNC` before emitting.
  - `trig` = its `srcs.id__`; `tid_` = its target id.
  - **slot layout differs by source class** (findings §5 "different layout";
    footswitch vs Instant now HW-anchored):
    - **continuous/EXP** command → 5 int slots `pvla..pvle` + 5 bool `pspa..pspe`.
      Anchored (ZZCAP EXP-A CC, `func 1`): `pvla=channel, pvlb=CC#, pvlc=min,
      pvld=max`. Not authored (out of scope, #16 residual).
    - **footswitch** command → 12 int slots, layout **HW-captured 2026-07-15**
      (`../2026-07-15-hss-and-cc-capture-findings.md` §TARGET D): footswitch
      reserves `pvlb`=**subtype** (device `func` mirror) and shifts data +1 vs
      Instant — `pvla`=PC program (Bank/Program subtype), `pvlc`=channel,
      `pvld`/`pvle`=Bank MSB/LSB (`-1`=off), `pvlf`=reserved(`-1`), `pvlg`=CC#,
      `pvlh`=CC value, `pvli`=note#, `pvlj`=velocity (`100` default for
      CC/MMC/PC), `pvlk`=const `1`, `pvll`=MMC message. Captured (distinct,
      isolated) values, byte-for-byte:
      - CC (`func 1`): `[0,1,ch,-1,-1,-1,CC#,val,0,100,1,0]`
      - Note (`func 2`): `[0,2,ch,-1,-1,-1,0,0,note,vel,1,0]`
      - MMC (`func 3`): `[0,3,ch,-1,-1,-1,0,0,0,100,1,msg]`
      - PC (`func 0`): `[prog,0,ch,MSB,LSB,-1,0,0,0,100,1,0]` (PC not isolated
        on hardware but the +1 shift is fixed by the CC/Note/MMC captures).
    - **Instant** command → 12 int slots, `pvlb`=channel, **no subtype slot**.
      Anchored (ZZCAP Instant PC, `func 0`): `[0,ch,MSB,LSB,-1,0,0,0,100,1,0,0]`.
      Instant CC/Note/MMC slot placement is still inferred (only PC captured).
      The Note/MMC `func` swap is ALSO applied to Instant, but by **ASSUMPTION**,
      not capture: the `func` enum is treated as a property of the `cmnd` record
      schema (global), not the source class — only Instant PC was captured, and
      `0→0` is swap-invariant, so there is zero HW evidence either way. The
      assumed direction is pinned by golden tests
      (`test_transcode_instant_note_func_swapped` / `..._mmc_...`). Verifying it
      needs an app-authored Instant Note/MMC capture — currently **user-gated**
      (the capture rig needs the user to unlock the screen / grant
      Accessibility, same gate as the XY/set-edit-buffer captures). #16
      residual.
    - PresetSnapshot (Mandarin, `func 0`, all-zero) reproduces byte-for-byte.
- a **`trgs`** entry `{eID_: cid_, enty: 6, id__: tid_, pid_: 0, slot: 0,
  type: 4}` (a command-target entity; `sm__.scid` lists only controllers, NOT
  commands).

Integrated into `_synth_cg_from_recipe` **after** the controller graph so FS/EXP
`srcs`/`scid` synthesis is untouched; command `srcs` are appended with fresh ids.

## 4. Reconciliation through edit verbs

Commands are keyed by controller **source**, not block `(path, lane, pos)`, and
target the device / external gear / preset-snapshot state — not blocks. So
`add-block`/`remove-block`/`swap-model` renumbering does **not** touch them (no
`_remap_midi_positions`-style pass needed). A regression test pins that a
command survives a block insert/remove unchanged.

## 5. Honesty / residuals

- **Native `.hsp` round-trip** (author/view/mutate) is corpus-proven and fully
  offline-tested — the load-bearing deliverable.
- **Transcoder → device**: structure is anchored to real device records
  (Mandarin PresetSnapshot; ZZCAP EXP CC + Instant PC; and the 2026-07-15
  **footswitch CC/Note/MMC** isolated captures). The **footswitch** MIDI
  12-slot layout + the Note/MMC `func` swap are now **HW-anchored** (isolated,
  distinct-value captures — findings §TARGET D — pinned as golden assertions in
  `tests/test_commands.py`). Still inferred: **Instant** CC/Note/MMC slot
  placement (only Instant PC captured) and footswitch **PC/Bank** slots (PC not
  isolated on hardware — the +1 shift is fixed by the other three subtypes). HW
  validation asserts **byte-for-byte survival** (device accepts + preserves the
  synthesized `cmnd`, the #33 pattern), NOT audible/functional response — that
  stays uncharacterized, exactly like #33 (needs physical MIDI gear). The
  Note/MMC `func` swap on **Instant** sources is an ASSUMPTION (global-enum
  reasoning, zero HW evidence — see §3), pinned by golden tests; verifying it
  is user-gated (capture rig needs screen unlock / Accessibility).
- **No live authoring verb.** The wire path (`/attachCommandWithType` +
  `/setCommandParamVal`, 2-byte framing, handle allocation) is left
  unimplemented; commands are authored into the preset.
- **HotKey/Utility (family 4)** and **EXP continuous commands** are out of scope
  (no corpus/`.hsp` anchor).
- **A switch shared by `footswitches` + `commands`** is rejected at AUTHORING
  time (device allows it; helixgen doesn't compose the two stores yet). On
  READ (`view` of a device export that composes them, e.g. Mandarin Fuzz's
  FS1), the command is kept under `unknown_controllers` — parseable, labeled,
  never silently dropped (round-2 Critical fix; see the review section).
- **FS command-src `locl` extrapolation:** only FS1 (`locl 25`) is
  command-anchored; FS2–FS11 use the controller-srcs `25+NN` progression
  (round-2 Minor, honesty note in §3).
