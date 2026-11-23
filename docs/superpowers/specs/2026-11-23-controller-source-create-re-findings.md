# EXP / footswitch controller source-create — RE findings (2026-11-23)

The device RPC that CREATES a non-MIDI controller source — an expression pedal
or footswitch — and binds it to a parameter. Captured for 81v.3 / bead
**hgc-r24** (the EXP/FS piece deferred from the MIDI-CC increment hgc-86j).
The MIDI-CC path was already pinned live (`/attachParamController` →
`/ControllerMIDISourceAdd`); the local-source command was not wire-knowable —
`/setControllerSource` appears only as a cascade *echo*, and its command form
answers `… is NOT known!!!`, while `/SourceEnableSet` needs an already-created
source id. So it needed a frida app-MITM capture.

## Method

Same rig as the snapshot-param capture: `tools/frida_run.py` + `hook_sockets.js`
on the re-signed Helix Stadium Debug.app over USB; drove the Parameter-panel
flyout → **Assign Controller…** with computer-use; cross-checked against the
live edit buffer (`cg__.entt.srcs`/`ctrl`). Stadium XL, fw 1.4.1, scratch edit
buffer (saved cid 1998), restored after.

## The cascade (Controller Assign → Exp Pedal 1)

```
SEND 2002  /attachParamController  ,iiiii  [reqid, dsp, grid_slot, sub, paramId]
RECV 2002  /attachParamController          [reqid, cid, 0]                 # device allocates cid
RECV 2001  /attachParamControllerWithBlob  [seq, reqid, dsp, slot, sub, pid, cid, targetId, <lrtcpgsm blob>]

SEND 2002  /ControllerSourceSet    ,iiiii  [reqid, cid, source_type, locl, ctxt]
RECV 2002  /setControllerSource            [reqid, cid, srcId]             # NOTE: reply uses the echo spelling
RECV 2001  /setControllerSource            [seq, reqid, cid, source_type, locl, ctxt, srcId]

SEND 2002  /CidBehaviorSet         ,iii    [reqid, cid, behv]
RECV 2002  /setCidBehavior                 [reqid, cid, behv, 0]
```

- **`/attachParamController`** is the SAME verb the MIDI path uses — it creates a
  sourceless param controller and returns the device-allocated `cid`. On a param
  that already has a controller, the app does NOT re-attach; it only re-sources
  (so reassigning a source reuses the cid — no orphan controller piles up).
- **`/ControllerSourceSet [reqid, cid, source_type, locl, ctxt]` is the missing
  command.** `source_type` = 1 for a local controller. `(locl, ctxt)` is the
  device source code — the SAME code `cg__.entt.srcs[].locl/ctxt` carry, and the
  SAME mapping the preset transcoder already owns (`transcode._controller_locl_
  ctxt`): EXP1 → `(42, 0)`, EXP2 → `(42, 1)`, EXP1Toe → `(37, 0)`, footswitch
  bank A `FS{n}` → `(25 + (n-1), 1)`, bank B → ctxt 2. **Captured and
  cross-checked: EXP1 → `[271, 7, 1, 42, 0]`; FS1 → `[273, 7, 1, 25, 1]`.** The
  command's own reply on 2002 is `/setControllerSource` (the echo spelling) —
  this family does NOT answer with `/status`.
- **`/CidBehaviorSet [reqid, cid, behv]`** sets the controller behaviour
  (`ctrl.behv`, `transcode._BEHV_INDEX`): **2 (continuous) for an expression
  pedal, 0 (latching) for a footswitch / toe** — captured EXP1 → `behv 2`,
  FS1 → `behv 0`. The reply is `/setCidBehavior` (echo spelling).

The device, after the FS source bind, also auto-emits on 2001 a
`/setSourceEnable [seq, srcId, locl, ctxt, …]` and a stomp-label
`/setPropertyValue` — these are device-side cascade, NOT client sends.

## Clear (Controller Assign → None)

```
SEND 2002  /removeControllerSource  ,ii  [reqid, cid]  -> /status
SEND 2002  /removeController        ,ii  [reqid, cid]  -> /status
```

The app unbinds the source then removes the controller. (helixgen's shipped
per-param clear uses `/removeController`; dangling local `srcs` are transient —
the device GCs them on save/reload, same as the MIDI path.)

## Implementation (shipped this PR)

- `HelixClient.set_controller_source(cid, locl, ctxt, source_type=1)`,
  `set_cid_behavior(cid, behv)`, `remove_controller_source(cid)` (raw wire).
- `LiveSession.assign_local_source(path, block, pid, source, rev=…)` — resolves
  the identifier (`FS1`/`EXP1`/`EXP1Toe`/…) → `(locl, ctxt, behv)` via
  `controllers.controller_mapping` + `transcode._controller_locl_ctxt`, REUSES an
  existing cid or attaches, binds the source, sets the behaviour; endpoint/
  internal/stale-rev guards + fresh-attach rollback mirror `assign_midi_cc`.
  `source_catalog()` lists the assignable sources for the picker.
- Routes `POST /controllers/assign-source` and `GET /controllers/sources`.
- UI: a source dropdown + "Assign source" button in the Controllers pane form,
  beside the existing MIDI-CC assign.

HW-validated end-to-end on Stadium XL: EXP1 assign → `behv 2` + EXP source
bound; FS1 reassign on the same param reused the cid (`behv 0`); clear emptied
it. Device left on clean saved cid 1998.
