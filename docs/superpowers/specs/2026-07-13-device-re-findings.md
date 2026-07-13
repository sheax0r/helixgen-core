# Device RE findings (2026-07-13 capture session)

Captured via `tools/re_capture.py` (Frida on the running Helix Stadium app) while
assigning A5/A7/EXP1 and exporting a non-active preset. Source capture:
`captures/re_capture_1783918916.jsonl`; cross-checked against device preset
cid 1064 ("Time Running Out") and its HX Edit `.hsp` export.

## 1. Non-activating content read — `/GetContentData`

**`/GetContentData [reqid:i, cid:i]` → returns the preset's content blob, with NO
preceding `/LoadPresetWithCID`.** Confirmed: HX Edit's *export of a non-active
preset* issued exactly this, and the preset did not become active. It is the GET
counterpart to `/SetContentData [cid, blob]`.

- Response is the full content blob (~14 KB), delivered over multiple 8192-byte
  socket frames — the client's response-reader must reassemble (the existing
  `/EditBufferStateGet` path already handles a same-size blob, so reuse that
  transport). The returned blob is the **stored** content form.
- Implementation: add `client.get_content(cid) -> bytes` sending
  `/GetContentData [reqid, cid]`; repoint `backup`/`pull`/sync-Phase-A off
  `load_preset`+`get_edit_buffer`. No active-preset query needed at all — the
  read simply never activates.

## 2. Controller assignment — `/ControllerSourceSet` + `/CidBehaviorSet`

The assign commands HX Edit sent (args after `reqid`):

| action | `/ControllerSourceSet` args | `/CidBehaviorSet` args |
|---|---|---|
| assign **A5** → block bypass | `[tgt=2, 1, locl=29, ctxt=1]` | `[tgt=2, behv=0]` |
| assign **A7** → block bypass | `[tgt=3, 1, locl=31, ctxt=1]` | `[tgt=3, behv=0]` |
| assign **EXP1** → wah param  | `[tgt=5, 1, locl=42, ctxt=0]` | `[tgt=5, behv=2]` |

Arg shape: `/ControllerSourceSet [reqid, target_id, 1, locl, ctxt]`,
`/CidBehaviorSet [reqid, target_id, behavior]`. `behv 0` = latching bypass,
`behv 2` = continuous (param sweep).

## 3. The `locl`/`ctxt` mapping (the previously-missing piece)

Verified against cid 1064's saved `srcs`/`ctrl` (device side) vs its `.hsp`
export (`0x010101NN` side):

- **Stomp bank A, footswitch N (1..12): `locl = 24 + N`, `ctxt = 1`.**
  Evidence: A1→25, A4→28, A5→29, A7→31 (all `ctxt=1`); A5/A7 were the manual
  reassignments, A1/A4 the untouched defaults — every one fits `24+N`.
- **EXP1 (and EXP1Toe): `locl = 42`, `ctxt = 0`.** The wah's toe-bypass and pedal
  sweep both land on `(42, 0)`.
- **`.hsp` ↔ device:** `.hsp` source `0x010101NN` (A-bank, NN = FS#−1) ↔ device
  `(locl = 25 + NN, ctxt = 1)`; `.hsp` EXP source `0x0102010M` ↔ device
  `(locl = 42, ctxt = M==0 ? 0 : 1)` (EXP2 = `ctxt 1` is probable from earlier
  fixture evidence; EXP1 = `ctxt 0` confirmed).

### Not yet pinned (edge cases — out of scope unless needed)
- `ctxt = 9` seen on looper command-footswitches (`0x010104NN`) — a distinct
  command/looper context; not covered by A-bank/EXP synthesis.
- Stomp **bank B** (`0x010102NN`) `locl`/`ctxt` — no capture; defer.
- EXP2 `ctxt` unconfirmed by this capture (probable `1`).

## 3a. The signal-flow grid (`sfg_.flow`) — hardware-derived, the routing model

Reversing why synthesized presets drew no connecting lines / mis-routed:

- **Each flow is a FIXED 28-slot grid**, two rows of 14: **row 0 = grid positions
  0..13** (input at 0, output at 13), **row 1 = 14..27** (input at 14, output at
  27). `bcnt` is always **28** (NOT the block count).
- **`bmap[gridpos] = the global block id at that grid position`** — an identity
  map when block ids are assigned in grid order (`id = 28*flow + gridpos`).
  `blks` alternates `[gridpos, block, …]` for occupied positions only.
- **Serial / dual-DSP:** one populated flow per DSP path; user blocks in row 0
  (positions 1..12), `OutputMatrix` at 13.
- **Intra-flow split:** lane-0 blocks + the `type:3` split + `type:4` join sit in
  row 0 at `gridpos = .hsp pos`; lane-1 blocks in row 1 at `gridpos = 14 + pos`.
  **`split.bblk` = the first lane-1 grid slot** (`14 + min lane-1 pos`);
  **`join.bblk` = `14 + join.pos`** (the row-1 slot beneath the join); `bflw` =
  flow index. Row 0 terminates with a normal `OutputMatrix` (NOT `OutputPath2A`).
  Verified: a synthesized grudge dual-amp matches HX Edit's own import
  byte-for-byte (grid, models, bblk).

## 3b. Controller/target/source ids are 1-BASED; `scid` keys by SOURCE id

Two more hardware-confirmed rules for `cg__.entt`:

- **All `trgs`/`srcs`/`ctrl` ids are 1-based** — the device treats id 0 as
  null/unassigned, so a 0-based id silently kills the binding. The block instance
  id lives in the trg's `eID_`, not its `id__`.
- **`sm__.scid = [source_id, [controller_cids], …]` — keyed by SOURCE id**, not
  target id. (Keying by trg id makes only the FS whose trg id happens to equal
  its src id bind.)
- `nxtc`/`nxts`/`nxtt` are the next-free controller/source/target ids (not tied
  to block ids).

## 4. Synthesis implication

For controller synthesis (spec 2 Part B) we write the `srcs`/`trgs`/`ctrl`
graph directly into the `_sbepgsm` blob (no runtime commands): per assigned
control, emit a `src` with the derived `(locl, ctxt)`, a `trg` for the block
bypass/param (keyed by the block's device `eID_`), and a `ctrl` linking them
with `behv` (0 bypass / 2 param) — mirroring cid 1064's decoded graph.
