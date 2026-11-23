# Live controller assignment — RE findings (81v.3)

**Date:** 2026-11-22. **Device:** Stadium XL, fw 1.4.1 (`192.168.4.84`).
**Method:** send/observe from `HelixClient` on the 2002 socket — send an opcode,
read the edit buffer (`cg__.entt`) back, diff. All probes ran on a scratch load
of the edit buffer and restored the player's active preset afterwards. This
pins the **send recipe** the 2026-07-14 parity capture (`2026-07-14-parity-capture-findings.md`
§6) only observed; `/attachParamController`'s `goid` and the removal op were
unproven there.

## MIDI CC → parameter — PINNED, HW-validated

A parameter controller driven by an incoming MIDI CC is two granular writes;
both land on the live buffer and round-trip through `device to-hsp` (`untranscode`
→ `view` recovers the native `cg__.entt.ctrl` as recipe `midi`).

1. **Attach the controller** —
   `/attachParamController [reqid, dsp, grid_slot, sub, paramId]`.
   - The first int is the request id the client prepends (the capture's `goid`
     slot — NOT a separate handle to pass).
   - `grid_slot` is the same block coordinate as `/ModelSet` / `/ParamValueSet`;
     the device records the block's own entity id (`id__`) on the target leaf,
     so a reordered buffer still resolves. `sub` = 0 for the primary model.
   - Creates a `ctrl` leaf (`type:3` = param) + a `trgs` leaf
     (`{eID_=block id__, pid_, slot=dsp, type:2, mmid=modelId}`) + a
     `ctm_.ptid` entry, and **replies `/attachParamController [reqid, cid_, 0]`**
     — `cid_` is the device-allocated controller id (the handle for steps 2/3).
   - The new `ctrl` has no source yet (`max_`/`min_` default to the param's own
     range; no `trig`).

2. **Bind the MIDI CC source** —
   `/ControllerMIDISourceAdd [reqid, cid_, blob]`.
   - `blob` is 20 bytes; the CC# is a **big-endian uint16 at byte offset 12**
     (matches parity §6). The rest may be zero.
   - Sets the `ctrl`'s `midi` field (`0x80 | CC` packed) + `cnt2 = CC#` and adds
     a `srcs` entry (`type:3`, `locl` = the packed midi value). Channel is the
     device global base channel (not on the wire).
   - Replies `/status [reqid, cid_, 0]`. **The status code is the LAST arg**
     (here `0` = ok), not `args[1]` — `args[1]` is the controller id. (`client._status0`
     reads the last arg; `_ok` would mis-read it as failure.)

**Clear** — `/removeController [reqid, cid_]` removes the whole binding (`ctrl`
leaf + its source). Replies `/status` (last arg 0 = ok). Resolve `cid_` for a
`(path, grid_slot, pid)` target via the `trgs`/`ctrl`/`id__` linkage
(`liveedit.session._controller_cids_for`).

Client methods: `HelixClient.attach_param_controller` / `add_midi_cc_source` /
`remove_controller`. Live verbs: `LiveSession.assign_midi_cc` /
`clear_param_controllers`.

## Also confirmed live-known (not yet wired)

- `/attachBlockBypassController` — bypass-controller attach (non-blob form); the
  blob form `/attachBlockBypassControllerWithBlob` carries the pin-first
  `lrtcpgsm` blob.
- `/SourceEnableSet` (echoes `/setSourceEnable`) — enables a source by its
  existing `srcs` id; a bare `locl` returns status `-3` (no such source).

## Deferred — needs a frida app-MITM capture (pin-first)

- **Expression / footswitch SOURCE create.** The cascade's `/setControllerSource`
  is an echo address, not a command (`/ControllerSourceSet` / `/SetControllerSource`
  / `/ControllerSourceAdd` / `/ControllerSwitchSourceAdd` all return "NOT
  known"). A non-MIDI source (EXP pedal, footswitch) must be created before
  `/SourceEnableSet` can link it, and that create command is not discoverable by
  black-box send/observe — it needs capturing the Stadium Edit app's own 2002
  traffic (the same pin-first spike as the `lrtcpgsm` blob). Until then, EXP and
  footswitch controller *assignment* is deferred; `wire_expression` /
  `wire_footswitch` remain the offline `.hsp` authoring path.
- **Footswitch-bypass blob** (`/attachBlockBypassControllerWithBlob`'s `lrtcpgsm`)
  — same capture.
