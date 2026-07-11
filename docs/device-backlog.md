# helixgen device — feature backlog

Future work for the network device-control feature (shipped in 2.0.0: preset
CRUD + content read/save + live param edits). Ordered loosely; not yet scheduled.

## In progress (this session)
- **Local backup library** — bulk-pull a setlist to local `.sbe` files + manifest;
  browse/restore offline. (`src/helixgen/device/backup.py`)
- **Live PUB mirror** — subscribe to the 2001/2003 streams for real-time
  param/meter/state events. (`src/helixgen/device/subscribe.py`)
- **`push`/restore** — capture the "set edit buffer" command so a pulled `.sbe`
  can be pushed back / cloned. (needs a live capture)
- **`.hsp` ↔ device authoring bridge** — install a helixgen-authored preset onto
  a device slot (author-by-replay via `/ModelSet` + `/ParamValueSet` +
  `/CreateContent` + `/SavePresetWithCID`, using the bundled model/param defs).

## Requested backlog (2026-07-11)

1. **Set the currently active tone** — select which preset is *active/playing* on
   the device. We already have `device load <cid>` (loads a preset into the edit
   buffer); confirm whether that fully covers "active tone" or whether there's a
   separate active-preset-index command (see `/PresetSnapshot` in the command
   defs). Expose cleanly (`device select <cid>` / MCP).

2. **Load IRs onto the device** — push a local `.wav` IR to the device's IR
   store. On-device IRs live under `/data/stadium-family-fw/ir/` (seen via
   `/xxxIrxPathForHash1`); the transfer is likely SSH/SFTP or a dedicated OSC
   command — needs a capture of the editor's IR-import. Reuse helixgen's existing
   `irhash` (48 kHz Stadium hash, already implemented) to name/identify.

3. **Pull IRs off the device** — download the device's IRs to local `.wav`
   files (inverse of #2); build/refresh the local IR library + `mapping.json`
   from what's actually on the hardware.

4. **Auto-load IRs referenced by a preset** — closing the loop: a wrapper/sidecar
   (e.g. JSON referencing an IR **file** + the tone file) so that when a preset
   uses a local IR, helixgen **checks whether the IR is already on the device**
   (by `irhash` — we can enumerate on-device IRs and compare hashes) and, if not,
   **loads it automatically** (feature #2) before/while installing the preset —
   instead of telling the user "remember to import the IR." This makes the
   `/tone` → playable-on-amp flow fully hands-off, IRs included.
   - Ties into: helixgen `register-irs`/`ir-scan`/`mapping.json`,
     `compute_irhash`, and the authoring bridge.

## Notes / dependencies
- #2/#3 hinge on how the editor transfers IR files (SSH/SFTP vs OSC) — one live
  capture of "Import Impulse Response" in the editor resolves it.
- #4 depends on #2 (upload), on-device IR enumeration (list IRs + their hashes),
  and the authoring bridge (#0 above) to reference the IR from the pushed preset.
- All device-write features should keep the **local-file-first** principle: work
  offline against local `.sbe`/`.hsp`/`.wav` copies, sync to hardware on demand.
