# Command Center command authoring — RE findings (2026-11-23)

The device RPCs that CREATE, edit, and clear a **Command Center** command on a
footswitch/Instant source. Captured for the 81v.4 write path (bead
**hgc-cc1**). The command verb *names* were decoded by name 2026-07-14, but the
wire **argument framing + handle allocation** were never captured and were
unimplemented in `client.py` — and dispatch is keyed on (address + argument
signature), so blind arg-probing of `/attachCommandWithType` cannot find the
shape. So it needed a frida app-MITM capture.

## Method

Same rig as the snapshot-param and controller-source captures:
`tools/frida_run.py` + `hook_sockets.js` on the re-signed Helix Stadium
Debug.app (over WiFi this session); drove **Command Center → Stomp A1 →
Command 1 → MIDI** with computer-use; cross-checked against the offline command
model (`transcode._command_payload` / `_command_locl_ctxt_type`). Stadium XL,
fw 1.4.1, scratch edit buffer (saved cid 1998), restored after.

## The three opcodes

```
# CREATE  (Command 1 = MIDI, default Bank/Program subtype, on Stomp A1 / FS1)
SEND 2002  /attachCommandWithType  ,iiiiiiib  [reqid, srcType, locl, ctxt, 0, family, subtype, blob=[]]
                                              [401,   1,       25,   1,    0, 6,      0,       <empty>]
RECV 2002  /attachCommandWithType            [reqid, slot, slot]          # device-allocated slot, e.g. [401, 1, 1]
RECV 2001  /attachCommand                    [cmdId, reqid, slot, locl, ctxt, 0, slot]   # [15163, 401, 1, 25, 1, 0, 1]
# (downstream the device applies it: /setSourceEnable [sess, sub, locl, ctxt, 1],
#  /setComponentContext, /setPropertyValue "volatile.floorboard.stomp.b.color.N")

# SET ONE PARAM  (switch subtype to CC → pvl index 1 = subtype)
SEND 2002  /setCommandParamVal      ,iiii     [reqid, slot, pvlIndex, value]   # [402, 1, 1, 1]
RECV 2002  /status                            [reqid, 0, 0]
#   CC#   → [404, 1, 6, 1]   (pvl index 6)
#   Value → [405, 1, 7, 1]   (pvl index 7)

# CLEAR  (command → None)
SEND 2002  /removeCommand           ,ii       [reqid, slot]                    # [406, 1]
RECV 2002  /removeCommand                     [reqid, slot, 0]                 # echo spelling + 0 code
```

### `/attachCommandWithType [reqid, srcType, locl, ctxt, 0, family, subtype, blob]`

- **`(srcType, locl, ctxt)` is the command source.** These are the SAME three
  ints `transcode._command_locl_ctxt_type(source_id)` returns — reordered to the
  wire's `(type, locl, ctxt)`. A footswitch **FS1** is `(1, 25, 1)` (type 1 =
  footswitch, locl 25+n−1, ctxt 1 for bank A); an **Instant1** is `(4, 0, 0)`.
  Confirmed on the wire: FS1 → `srcType 1, locl 25, ctxt 1`.
- **`family` is the device command class** — the `cg__.entt` `cmnd` `type`:
  **`1` = Preset/Snapshot, `6` = MIDI** (matches the offline model's
  `_command_payload` `type` and `untranscode` `ctype == 6`). The other Command
  Center families (Song/Looper/Utility/ExtAmp/HotKey) were not authored; their
  family numbers are **uncaptured**.
- **`subtype` is the family func.** MIDI: `0` = Bank/Program (the UI default),
  `1` = CC, `2` = Note, `3` = MMC — the DEVICE func enum (Note/MMC swapped vs
  the `.hsp` `Command` value; see `transcode._HSP_TO_DEVICE_MIDI_FUNC`).
- **The 5th int is a reserved field** — observed `0` on every attach (Command 1
  and Command 2, same value). Not the slot, not the family. Sent as `0`.
- **The blob is empty.** The app sends an empty command blob on attach.
- **The device ALLOCATES the slot.** The app does NOT pick it: `/attachCommand
  WithType` is identical for Command 1 and Command 2 of the same switch — the
  device appends to the source's command list and returns the new slot in its
  reply `[reqid, slot, slot]` (Command 1 → `[401,1,1]`, Command 2 → `[407,2,2]`)
  and in the 2001 echo's last arg. The slot is the handle the other two opcodes
  take; treat it as opaque (don't assume a per-switch 1-based numbering).

### `/setCommandParamVal [reqid, slot, pvlIndex, value]` → `/status [reqid, 0, 0]`

- Writes ONE int slot of the command's `pvl` array. **`pvlIndex` is EXACTLY
  `transcode._command_payload`'s footswitch pvl layout** — a clean live↔offline
  cross-check: `pvl = [_, subtype, channel, _, _, _, CC#, Value, …]`, and the
  captures landed on **subtype → index 1, CC# → index 6, Value → index 7**,
  matching those positions byte-for-byte. The 2002 reply is `/status [reqid, 0,
  0]` (code 0 = ok).

### `/removeCommand [reqid, slot]` → `/removeCommand [reqid, slot, 0]`

- Clears the command slot (the app's command → **None**). Answers with the echo
  spelling plus a **trailing** code (`0` = ok) — the same shape as the
  `/setControllerSource` / `/setCidBehavior` echo replies (last arg = code), NOT
  a plain `/status`.

## Implemented

`client.attach_command` / `set_command_param` / `remove_command`
(`src/helixgen/device/client.py`). Low-level: they take the wire ints directly;
the caller (a liveedit session / CLI verb) resolves a `.hsp` source id →
`(source_type, locl, ctxt)` via `transcode._command_locl_ctxt_type` and the pvl
layout via `transcode._command_payload`, exactly as the controller-source write
(`set_controller_source`) is driven. No UI wired here — the Command Center UI
session builds on these.

## Notes for the next increment

- **Action (Press/Release/Hold/Toggle)** was left at the UI default (Press) and
  not captured; it is likely another `/setCommandParamVal` slot or a `psp` bool
  slot — pin it before wiring the Action control.
- **Non-MIDI families** (Preset/Snapshot `type 1` is modeled offline; Song,
  Looper, Utility, ExtAmp, HotKey) need their own family numbers + pvl layouts
  captured before authoring.
