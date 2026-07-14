# Controller depth + `device info` — design (parity #21, 2026-07-14)

Closes matrix **§12 "Product / device info"** and the **§6 controller rows**:
curve / reverse / threshold, min/max, merge switch (multi-block per FS),
FS label + color, and FS→param toggles. MIDI-CC/Note sources and the XY
controller are **deferred with evidence documented** (see §6 below).

Every field shape here is EVIDENCE-derived. Sources used:

- **E1 — the 211 real `.hsp` exports** in `data/` (device-written JSON).
- **E2 — device `_sbepgsm` fixtures** `tests/fixtures/device_content/preset_{151,152,157}.sbepgsm`.
- **E3 — live non-activating `GetContentData` pulls** (2026-07-14, Stadium XL fw 1.3.2)
  of factory presets that pair with their E1 exports: `2 Guitar Rig` (179),
  `Nash Sesh` (169), `Unlikely Pair` (171), `Deluxe Gig` (187),
  `BAS:4 PRO 4 Pros` (204), `Deconstructed Bliss` (188).
- **E4 — the Stadium app binary string table** (`Helix Stadium.app` 1.3.2,
  `strings` of the serializer's enum-vocabulary region; the same region carries
  the exact strings E1 uses: `targetbypass`, `latching`, `continuous`, …).

## 1. `.hsp` controller model (E1)

Every controller dict in the corpus (1553 instances) has exactly these keys:
`behavior, bypassed, curve, delay, goid, max, midisource, min, source,
threshold, type`.

- `type`: `targetbypass` (951) | `param` (602). (`localbypass` exists in E4,
  0 corpus uses — out of scope.)
- `behavior`: `latching` (1325) | `continuous` (224) | `momentary` (4).
  E4 adds `toedown` (0 corpus uses — not authored).
- `curve`: only `"linear"` in the corpus. E4 vocabulary (contiguous string
  table, exactly where the other controller enums live):
  `slow5 slow4 slow3 slow2 slow1 linear fast1 fast2 fast3 fast4 fast5`.
  Cross-check: the device-side `ctrl.curv` int is **5** on every E2/E3
  controller — the 0-based index of `linear` in that 11-entry table.
- `threshold`: `0.0` (or null on digital-FS bypass dicts). Field shape float.
- `midisource`: **0 in all 1553** — no MIDI-sourced controller anywhere (§6).
- `min`/`max`: EXP param sweeps 0..1 (inverted min>max = reverse, corpus-real);
  **FS→param toggles carry RAW param values** (`min:-7.0, max:-5.2` dB — and
  raw INTS on int params: Interval 2→4, Transport 0→1, so authoring must not
  float-coerce them); bypass dicts carry `False/True` (566), `null/null`
  (360) or `0.0/1.0` (25) of the 951 across the whole corpus.

**FS→param toggles are real and common (77 corpus instances):** a stomp source
(`0x010101NN`) on a `param`-type controller with `behavior: latching|momentary`
toggles that param between `min` and `max`. E3 pairing (`2 Guitar Rig`):
`.hsp {type: param, behavior: latching, source FS5}` ⇔ device
`ctrl {type:3, behv:0} ← srcs(locl 29, ctxt 1)`, raw min/max preserved.

**Merge switch is real (87 merged stomp/toe switches across 66 of the 211
exports, 2–5 targets per switch):** the same
source id appears on multiple blocks' `@enabled` controllers (and may also
drive params — `Deconstructed Bliss` FS7 drives a bypass AND a momentary
`Transport` param). Device side (E2/E3): **one `srcs` entry per physical
source**, `sm__.scid` maps it to a **list of ctrl ids** (`1, [1, 3]`).

**Scribble strips (`preset.sources`)**: keyed by decimal source id; value
`{bypass: bool, fs_label: str, fs_color: str, fs_topidx: 0}` (partial key sets
occur). Corpus colors: `auto red purple white ltorange dkorange green blue
yellow pink turquoise`. `.hsp` labels may exceed 12 chars; the device stores
max 12 (E3: `.8th VintDigi` → `.8th VintDig`).

## 2. Device `_sbepgsm` controller graph (E2 + E3)

- `srcs` entry: `{byps, cmds:[-1,-1], cnt1..3:0, ctxt, id__, locl, mtms:0,
  mtyp:0, type:1}` — one per physical source; ids 1-based.
- `ctrl` entry: `{behv, cid_, curv, dlay, goid, max_, min_, thrs, tid_, togl,
  trig, type}` — `trig` = src id, `tid_` = trg id.
  - `type`: 1 = bypass (trg enty 2, pid 0), 3 = param (trg enty 3, pid N).
  - **`behv` = behavior enum index**: `latching=0, momentary=1, continuous=2`
    (`toedown=3` presumed from the E4 table order). E3 anchor:
    `Deconstructed Bliss`'s single corpus `momentary` param controller is
    `behv=1`. The pre-existing synthesis encoded momentary as `behv=0 +
    togl=True` — **wrong**; `togl` varies freely on latching controllers
    across E3 (volatile latch state, no `.hsp` counterpart) and is now always
    emitted `False`.
  - `curv` = 0-based index into the E4 curve table (linear = 5).
  - `min_`/`max_`: `False/True` for bypass; raw param values for type 3.
- **source `locl`/`ctxt` map** (E3-paired, `.hsp` source id → `(locl, ctxt)`):
  - stomp bank A `0x010101NN` → `(25+NN, 1)` (pre-existing, confirmed)
  - stomp bank B `0x010102NN` → `(25+NN, 2)` (E3 `2 Guitar Rig` bank-B srcs)
  - looper-function bank `0x010104NN` → `(25+NN, 9)` (E3 `Nash Sesh`: 7
    looper param ctrls ⇔ 7 ctxt-9 srcs, locl 25,26,27,28,32,33,34 = NN
    0,1,2,3,7,8,9 exactly)
  - **toe switch `0x01010500` → `(37, 0)`** — E3 `Deconstructed Bliss`: its
    only `(37,0)` src is the wah-toe bypass. The pre-existing `(42, 0)`
    mapping collided with EXP1 (locl 42 = Exp Pedal, ctxt = pedal index) and
    is fixed.
  - EXP pedals `0x0102010M` → `(42, M)` **for M ∈ {0, 1} only**
    (pre-existing, confirmed). `0x01020102` (likely EXP3 — 4 corpus
    uses, e.g. `Marshall and vh4`'s wah) has no anchored encoding and
    is SKIPPED on transcode, never collapsed onto EXP2; `view` keeps it
    in `unknown_controllers`.
- `pm__` scribble strips: `preset.floorboard.stomp.<row>.<n>.{color,label,topidx}`,
  row `a`/`b` = bank A/B, `n` = NN+1. **Color int palette** (E3 pairings
  red=2, dkorange=3, ltorange=4, purple=9, white=11; auto=1 pervasive):

  | int | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
  |---|---|---|---|---|---|---|---|---|---|---|---|---|
  | name | none | auto | red | dkorange | ltorange | yellow | green | turquoise | blue | purple | pink | white |

  Anchored: auto, red, dkorange, ltorange, purple, white. Inferred from the
  E4 table order (which lists exactly this sequence minus the two
  chunk-elided names): none, yellow, green, turquoise, blue, pink —
  **EXPERIMENTAL**.

## 3. Recipe surface (authoring)

`footswitches` entries gain optional fields; multiple entries may now share
one `switch` (**merge switch**). The duplicate guard keys on
`(block, param, path, lane, pos)` — the same block may appear once for bypass
and once per param.

```json
"footswitches": [
  {"switch": "FS3", "block": "Compulsive Drive", "label": "DRIVE", "color": "red"},
  {"switch": "FS3", "block": "Tape Echo Stereo"},
  {"switch": "FS4", "block": "Brit Plexi Brt", "param": "Drive",
   "min": 0.4, "max": 0.65, "behavior": "momentary", "curve": "fast1"},
  {"switch": "EXP1Toe", "block": "Teardrop 310 Mono", "threshold": 0.65}
]
```

- `param` (str) — FS→param toggle; **requires numeric `min` and `max`** (raw
  param values). Omitted → bypass assignment (min/max rejected there).
- `label` (str) / `color` (palette name) — scribble strip, per SWITCH;
  conflicting values for one switch are a SpecError. Labels >12 chars warn
  (device truncates at 12).
- `curve` — one of the 11 curve names; non-`linear` values are EXPERIMENTAL.
- `threshold` (float) — flip point; EXPERIMENTAL semantics, evidenced shape.

`expression` targets gain `curve` (same vocabulary). **Reverse** = `min > max`
(corpus-real for EXP; documented). No boolean `reverse` sugar — one spelling.

## 4. Round-trip (`view`)

- `_recover_footswitches` also emits non-default `curve`/`threshold`, plus
  `label`/`color` looked up from `preset.sources` (attached to the switch's
  first recovered entry).
- New: FS/toe-sourced `param`-type controllers are recovered as footswitch
  param entries (`param`/`min`/`max`/`behavior`/`curve`). Numeric min/max only.
- Bank-B (`0x010102NN`), looper-bank (`0x010104NN`) and any other un-tabled
  sources stay in `unknown_controllers` (kept + labeled, never dropped).

## 5. Transcode (`bridge.py` / `transcode.py`)

- `hsp_to_paths` lifts per-controller `behavior`/`curve`/`threshold` for both
  `fs_bypass` and param controllers (now under `ctl_params`, superseding
  `exp_params`), keyed by device param name.
- `_synth_cg_from_recipe`: srcs **deduped by `(locl, ctxt)`**; `scid` groups
  all of a source's ctrl ids into one `[sid, [cid, …]]` entry; `behv` =
  behavior index; `curv` = curve index; `thrs` = threshold; `togl` always
  False; param ctrls emit raw min/max; `byps` passes through
  `preset.sources[sid].bypass` when present.
- `_synth_pm`: `fs_color` name → palette int (table §2), labels truncated to
  12 (device-canonical).

## 6. Deferred (numbered backlog entries)

- **MIDI CC/Note controller source** — `midisource` is 0 in all 1553 corpus
  controllers; no on-device preset carries one; the int encoding of
  channel/CC cannot be derived from strings; probing requires mutating the
  live edit buffer (`/ControllerMIDISourceAdd`), which the active-tone rule
  forbids. Backlog entry documents the attempts.
- **XY controller assignment** — all 84 corpus `preset.xyctrl` dicts are
  factory defaults; no controller source in any export maps to the XY axes;
  same probing constraint. Backlog entry.
- **Bank-B stomp authoring** (`0x010102NN`) — encoding fully evidenced (§2),
  but the physical layer (second stomp page) has no English-layout mapping
  yet; view keeps them labeled in `unknown_controllers`. Folded into the same
  backlog item as XY? No — noted in #21 close-out.

## 7. `device info`

`/ProductInfoGet (reqid)` → `/getProductInfo [reqid, map]` (live-derived
2026-07-14, fw 1.3.2 build 1340): 4CC-keyed map `{clid, host: {ctyp, hoid,
id__, name, res_, sdas, sdcs, sdts, snum, vers: {buld, date, majo, mino, patc,
targ}}, nexs, stpw}`. `id__` = numeric device id (2490368 = Stadium XL),
`snum` = serial, `sdas`/`sdts` = storage available/total bytes, `vers` =
firmware (`majo.mino.patc`, build, epoch date).

- `client.product_info()` → curated dict `{model, device_id, helixgen_model,
  serial, firmware, firmware_build, firmware_date, sd_total_bytes,
  sd_available_bytes, raw}` (raw = full 4CC-decoded reply).
- CLI `helixgen device info [--json]`; MCP `device_info(model, ip)`.

## 8. Validation

- Offline: full suite incl. the 211-export round-trip + sonic-fidelity bars
  and the `_sbepgsm` byte-fidelity gate.
- Hardware (authorized, `ZZB-` prefix only, non-activating install/pull):
  author a preset exercising merge + FS-param + label/color + curve +
  threshold, install via pool `CreateContent`+`SetContentData`, `get_content`
  back, assert the synthesized `srcs`/`ctrl`/`scid`/`pm__` fields persist
  unchanged; delete afterwards. `device info` validated live.
