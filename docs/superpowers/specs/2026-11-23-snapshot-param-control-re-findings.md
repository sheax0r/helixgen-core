# Per-param Snapshot Control — RE findings (2026-11-23)

The device RPCs that turn **Snapshot Control** on/off for one parameter — the
"camera" toggle in the Stadium app's per-param flyout, value renders white when
on. Captured for 81v.2 / bead **hgc-0g0.2**. The assign verb is not
wire-knowable (the app-internal names `/assignSnapshotParam`,
`/getSnapshotTargets`, `/getSnapshotCount` all answer `… is NOT known!!!` as
device commands), so it needed a frida app-MITM capture.

## Method

frida socket hook (`tools/frida_run.py` + `tools/hook_sockets.js`) attached to
the re-signed **Helix Stadium Debug.app** over USB; drove its Parameter-panel
flyout with computer-use; attributed frames by capture offset. The device
(Stadium XL, fw 1.4.1) was driven in a scratch edit buffer (saved cid 1998),
restored after. Every wire finding was cross-checked against the live edit
buffer read back with `HelixClient.read_edit_buffer` (`cg__.entt`).

## Assign — `/assignSnapParam`

```
SEND 2002  /assignSnapParam  ,iiiiif  [reqid, dsp, grid_slot, sub, paramId, value]
RECV 2002  /status                    [reqid, allocatedTargetId, 0]
RECV 2001  /assignSnapshotParam       [seq, reqid, dsp, grid_slot, sub, paramId, value, allocatedTargetId]
```

- `dsp`/`grid_slot`/`sub`/`paramId` are the **standard live-op param address**
  (same coordinate as `/ParamValueSet`); `value` is the param's current stored
  value (0..1-style units). Captured example: Gain of Scream 808 at
  path 0 / grid slot 1 / pid 1 → `[269, 0, 1, 0, 1, 0.12]`, with pid 1 and
  0.12 confirmed by `device params`.
- **The device allocates the snapshot target id and returns it in `/status`
  field 2** (here `12`). This overloads the usual `/status [reqid, code, dirty]`
  shape — field 2 is NOT a status code for this verb. Success is the presence
  of a `/status` reply (vs `/error`); field 3 was `0` (clean).
- The command **seeds `value` into every snapshot** — after the assign, the new
  target id appears in `cg__.entt.snps[i].tamv` of all 8 snapshots with that
  value. The 2001 echo verb is `/assignSnapshotParam` (note the longer
  spelling; the command is `/assignSnapParam`), with the allocated target id
  appended.

## Remove — `/removeSnapshotTarget`

```
SEND 2002  /removeSnapshotTarget  ,ii  [reqid, targetId]
RECV 2002  /status                     [reqid, 0, 0]
RECV 2001  /removeSnapshotTarget        [seq, reqid, targetId]
```

- `targetId` is the `cg__.entt.trgs[].id__` of the param's target (NOT the wire
  reqid, and NOT the dsp/slot/pid). `/status` is the ordinary success shape.
- Also seen standalone in the ModelSet cascade; here it is pinned as a
  first-class command.

## Resolving a param's target id (for remove)

`tamv` stores only `[targetId, value, …]`; the **address→target** map lives in
`cg__.entt.trgs`. Each entry: `{id__, eID_, pid_, slot, type, …}` where

- `eID_` is the block **device instance id** = `28*dsp + grid_slot` (the
  device's canonical id — see `untranscode`; confirmed: path 0 slot 1 → eID 1,
  path 1 slot 1 → eID 29, both matched live `trgs`),
- `pid_` is the param id, `slot` the model slot (0, or 1 for a dual-cab B
  model), `type` is **2 for a param target** (1 = block bypass).

A param is under snapshot control iff its `type 2` target is in the
snapshot-tracked set `cg__.entt.ctm_.stid`. `HelixClient.snapshot_param_target`
does exactly this lookup.

> A param can own ONE `trgs` target that serves BOTH snapshot control and a
> controller (the controller's `ctrl[].tid_` points at the same id). The
> snapshot-tracked set `ctm_.stid` is what distinguishes snapshot control, so
> removing the snapshot target does not disturb a controller on the same param.

## Implementation (shipped this PR)

- `HelixClient.assign_snap_param(path, block, pid, value, sub=0) -> int`
  (returns the allocated target id), `remove_snapshot_target(target_id)`,
  `snapshot_param_target(path, block, pid, sub=0, doc=None)`.
- `LiveSession.set_snapshot_control(path, block, pid, on, rev=…)` — ON seeds the
  param's current value and assigns; OFF resolves the target and removes;
  idempotent; endpoint/internal/stale-rev guards mirror `assign_midi_cc`.
- Route `POST /snapshot/param-control {path, block, pid, on, rev}`.
- UI: a clickable Snapshot-Control toggle in each param's label (Parameter
  panel) — filled = on, hollow = off.

HW-validated end-to-end on Stadium XL (assign → `snap` true + `tamv` seeded →
remove → gone; idempotent). Device left on clean saved cid 1998.
