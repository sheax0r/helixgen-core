# Footswitch → block bypass assign — RE findings (2026-11-24)

The device RPC that CREATES a **bypass controller** on a block — the first op of
the app's right-click **Controller Assign** (block bypass → a Stomp
switch/expression pedal). Captured for the FS→bypass write path (bead
**hgc-nxm**). The verb *name* `/attachBlockBypassController` was already known
(it is echoed in a model-swap cascade as `/attachBlockBypassControllerWithBlob`,
§MODEL SET), but its wire **argument framing** and **reply shape** were never
captured, and dispatch is keyed on (address + argument signature) — so blind
arg-probing cannot find the shape. It needed a frida app-MITM capture.

## Method

Same rig as the controller-source and command-authoring captures:
`tools/frida_run.py` + `hook_sockets.js` on the re-signed Helix Stadium
Debug.app (over USB this session). Drove **right-click block → Assign to
Switch… → Stomp A → Stomp A1**, then the Latching/Momentary toggle, then
**None** (clear), with computer-use. Cross-checked the block coordinate against
`helixgen device blocks` (read over LAN while the app held USB). Stadium XL,
fw 1.4.1, scratch edit buffer (preset 25A / cid 1998), restored after
(`active --json` → `edited:false`).

## The decisive finding

**Only `/attachBlockBypassController` is new.** The rest of the
Controller-Assign cascade is byte-for-byte the LOCAL controller-source cascade
already shipped by hgc-r24 (PR #177): `/ControllerSourceSet`, `/CidBehaviorSet`,
`/removeControllerSource`, `/removeController` all appeared unchanged. So the
whole FS→bypass assignment is: **one new primitive + four existing ones.**

## The full lifecycle (deduped, in order)

```
# ASSIGN block bypass → Stomp A1 (FS1), Latching
SEND 2002 /attachBlockBypassController ,iiii [reqid, dsp, grid_slot, 1]   [112, 0, 1, 1]
RECV 2002 /attachBlockBypassController       [reqid, cid_, cid_]          [112, 1, 1]   # device allocates cid=1
SEND 2002 /ControllerSourceSet  ,iiiii [reqid, cid, source_type, locl, ctxt]  [113, 1, 1, 25, 1]
RECV 2002 /setControllerSource         [reqid, cid, srcId]                     [113, 1, 1]
SEND 2002 /CidBehaviorSet       ,iii   [reqid, cid, behv]                      [114, 1, 0]   # 0 = latching
RECV 2002 /setCidBehavior              [reqid, cid, behv, 0]                   [114, 1, 0, 0]

# Latching/Momentary toggle → Momentary
SEND 2002 /CidBehaviorSet              [reqid, cid, 1]                         [115, 1, 1]   # 1 = momentary
RECV 2002 /setCidBehavior              [reqid, cid, 1, 0]                      [115, 1, 1, 0]

# Controller Assign → None  (clear)
SEND 2002 /removeControllerSource ,ii  [reqid, cid]                           [116, 1]
RECV 2002 /status                      [reqid, 0, 0]                          [116, 0, 0]
SEND 2002 /removeController       ,ii  [reqid, cid]                           [117, 1]
RECV 2002 /status                      [reqid, 0, 0]                          [117, 0, 0]
```

(The `WithBlob`-named variant is only the 2001 *echo* of the attach; the command
sent on 2002 is the plain `/attachBlockBypassController` with no blob.)

### `/attachBlockBypassController [reqid, dsp, grid_slot, 1]`

- **The block sibling of `/attachParamController`** (`[reqid, dsp, grid_slot,
  sub, paramId]`) — it targets the block's *bypass*, so there is no param id.
- **`(dsp, grid_slot)` is the same block coordinate** as PARAM SET /
  `set_param`. Wire `[0, 1, 1]` matched `helixgen device blocks`: the selected
  block (Scream 808, `HD2_DistScream808Mono`) sits at **path 0, grid slot 1** —
  so `dsp=0, grid_slot=1`, and the gapped-row index (hgc-hhp) is irrelevant;
  it's the raw grid slot the device uses everywhere.
- **The trailing `1` is the fixed bypass sub-index.** A block has exactly one
  bypass, and the app sent `1` for a mono main-lane block; treated as a
  constant. (Only this one block was captured — a stereo / parallel-lane block
  was not, so if a future case needs a different sub it is uncaptured.)
- **The device ALLOCATES the cid** and replies `[reqid, cid_, cid_]` — the id at
  **both** positions after the reqid (like `/attachCommandWithType`'s `[reqid,
  slot, slot]`, NOT `/attachParamController`'s `[reqid, cid_, status]`). Read
  `args[1]`; the trailing field is the id, not a 0-is-ok status. Treat the cid
  as opaque.

### `behv` enum — wire-confirmed both values

The Latching/Momentary toggle writes `/CidBehaviorSet [reqid, cid, behv]`:
`0` = **latching** (the default), `1` = **momentary** — matching
`transcode._BEHV_INDEX` (`2` = continuous for a pedal, `3` = toedown, uncaptured
here). This is the first live confirmation of `behv=1`.

## Implemented

`client.attach_block_bypass_controller(path, block) -> cid`
(`src/helixgen/device/client.py`). Low-level: it takes the wire `(dsp,
grid_slot)` directly and returns the device cid. The caller finishes the
assignment with the existing `set_controller_source` (`(locl, ctxt)` from
`transcode._controller_locl_ctxt`) and `set_cid_behavior`, and clears it with
`remove_controller_source` + `remove_controller` — exactly the cascade above. No
UI wired here; the controllers / live-edit UI session builds on it.

## Notes for the next increment

- **Bypass sub-index for stereo / parallel-lane blocks** — only a mono
  main-lane block was captured (`1`). Confirm it stays `1` (or capture the
  variant) before wiring bypass-assign for a split lane.
- **Continuous (EXP pedal) → bypass** was not captured; the attach is almost
  certainly identical (`behv=2` via `set_cid_behavior`), but the source triple
  for an expression pedal (`locl,ctxt` = EXP1 `(42,0)`) is untested on this
  specific attach.
