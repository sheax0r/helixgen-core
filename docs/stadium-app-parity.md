# Helix Stadium app — coverage matrix

Every user-facing function of the **Helix Stadium desktop app** (v1.3.2.9805,
internal `p35edit`; `P35` = Stadium, `P37`/`P36` = Stadium XL) mapped to
helixgen's CLI / MCP / skill surface. Goal: drive the gaps to zero so the app is
never needed. Maintained ongoing (like `device-backlog.md`).

**Verdict legend:** ✅ done (with evidence) · 🟡 partial · 🔴 missing · 🔍 needs
protocol capture (arg shape) · 🚫 out-of-scope.
**Column values:** `full` / `partial` / `none` / `n-a`.

**Sources:**
- Stream 1 (manual): manuals.line6.com/en/helix-stadium/live/* + 1.3.x release
  notes → `scratchpad/inventory/manual-functions.md`.
- Stream 2 (bundle): app-binary OSC namespace + 251 `global.*` property keys +
  `commanddefs` → `scratchpad/inventory/bundle-functions.md`.
- `docs/helix-protocol.md`, `docs/superpowers/specs/2026-07-13-device-re-findings.md`.

A ✅ requires a shipped-release / test / hardware ref — never memory.

> **Capture note:** The full OSC *command namespace* and the full `global.*`
> settings namespace are already known from the app binary. Every 🔍 is only a
> command's **argument shape**, pinned by a targeted frida capture when that
> feature is implemented — not a blocker for this matrix.

> **In-flight (do not re-plan):** the tone-library-model-redesign agent
> (`docs/superpowers/specs/2026-07-13-tone-library-model-redesign.md`) is
> actively reworking the preset/setlist/library CLI (`register`, `device
> add/unsync/library`, setlist `sync-on/off`, `slots reorder`, managed-set
> mirror sync). Rows tagged _(library-agent)_ are owned there and cross-
> referenced here; their exact verb names are in flux.

---

## 1. Preset browser / library _(mostly library-agent)_

| Function | App location | Protocol | CLI | MCP | Skill | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| List / search / multi-select presets | Librarian | `/GetContainerContents` | full | full | full | ✅ | `device list`; search is client-side over listable data |
| Read metadata (no activate) | Librarian | `/GetContentInfo` | full | full | full | ✅ | non-activating read (2.18, #13) |
| Load into edit buffer | dbl-click | `/LoadPresetWithCID` | full | full | full | ✅ | `device load` |
| Make ACTIVE preset | click in setlist | `/LoadPresetAtContainerPosition` | none | none | none | 🔍 | backlog #1; distinct active-index vs load-to-buffer |
| New / duplicate / copy-to-setlist | Manage Presets | `/CreateContent`+`/SetContentData` | full | full | full | ✅ | `device create`/`install` + reference model _(library-agent)_ |
| Rename preset | Rename dialog | `/SetContentInfo` | full | full | full | ✅ | `device rename` |
| Set / batch preset color | Rename / Batch Color | `/SetContentInfo` (color) | none | none | none | 🔴 | color exists in `.hsp` meta; no device verb |
| Reorder presets | drag | `/ReorderContainerContent` | partial | none | partial | 🟡 | `slots reorder`+sync _(library-agent)_; not HW-validated; arg 🔍 |
| Move preset between setlists | drag | reference add/remove | full | full | full | ✅ | `device setlist add/remove` _(library-agent)_ |
| Delete / clear-from-setlist | Delete / Clear | `/RemoveContent` | full | full | full | ✅ | `device delete`; clear = drop reference |
| Export preset (.hsp) | drag out / Export | `/GetContentData` | full | n-a | full | ✅ | `device pull` (non-activating, 2.18) |
| Import preset | drag in / Import | `/CreateContent`+`/SetContentData` | full | full | full | ✅ | `device push`/`install` |
| Preset Info / Notes / Clips | Preset Info panel | `/SetContentInfo`; clip = audio content | none | none | none | 🔴 | notes text 🔴 (searchable meta); audio clips 🚫 |
| MIDI Recall display | sidebar | client-side calc | none | none | none | 🟡 | derivable from setlist/preset/snapshot index; could compute offline |

## 2. Setlist management _(library-agent)_

| Function | App location | Protocol | CLI | MCP | Skill | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| List setlists | sidebar | `/GetContainerContents(-5)` | full | full | full | ✅ | `device setlists` |
| Create setlist | sidebar ▸ + | `/CreateContent`(setlist) | none | none | partial | 🔍 | **backlog #8**; `create-local` = manifest only; device create arg 🔍 |
| Rename setlist | dbl-click | `/SetContentInfo` | none | none | none | 🔴 | device-side rename not exposed |
| Duplicate setlist | Duplicate | copy refs | none | none | none | 🔴 | not exposed |
| Reorder setlists | drag | reorder cmd | none | none | none | 🔍 | arg 🔍 |
| Delete / clear setlist | Delete / Clear | `/RemoveContent` | partial | partial | partial | 🟡 | `unsync`/managed-mirror handles member removal _(library-agent)_; whole-setlist delete 🔴 |
| Sync setlist(s) | (app is live) | pool+reference reconcile | full | full | full | ✅ | `device sync <setlist>`/`--all --gc` _(library-agent)_ |
| Import / export setlist (.hss) | File menu | bulk content | partial | none | partial | 🟡 | per-tone push/pull exists; single-file `.hss` bundle 🔴 |

## 3. Signal-flow editor

| Function | App location | Protocol | CLI | MCP | Skill | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| Add / remove / move / clear blocks | home_edit grid | authored `.hsp` → transcode | full | full | full | ✅ | `add-block`/`remove-block` + full-graph transcode (2.18) |
| Replace / swap model | Model List | swap in `.hsp` | full | full | full | ✅ | `swap-model` (same-category) |
| Copy / paste block | Action Panel | `.hsp` edit | partial | partial | partial | 🟡 | achievable via authoring; no one-shot copy verb |
| Parallel split (create) | drag down | `sfg_.flow` grid synth | full | full | full | ✅ | intra-flow split/join, HW-validated (2.18) |
| Split TYPE (Y / A-B / Crossover / Dynamic) | Split Inspector | split block params | partial | partial | partial | 🟡 | split synthesized; per-type params (freq/threshold/attack) not modeled as first-class |
| Merge mixer (levels/pan/polarity) | Merge Inspector | merge block params | partial | partial | partial | 🟡 | merge synthesized; mixer params not first-class authoring fields |
| Dual DSP / dual amp | two paths | dual-flow synth | full | full | full | ✅ | dual-amp synth, HW-validated (2.18) |
| Input block (source/Z/pad/trim/gate) | Input Inspector | per-path input + params | partial | partial | partial | 🟡 | per-path input source ✅; impedance/pad/trim/gate params 🔴 |
| Output block (dest/level/pan) | Output Inspector | per-path output + params | partial | partial | partial | 🟡 | routing partial; level/pan not first-class |
| FX Loop / Send / Return | block Inspector | loop block + Trails | partial | partial | partial | 🟡 | loop blocks placeable; send/return/mix/trails params partial |
| Live block bypass on device | click block | `/BlockEnableSet` | none | none | none | 🔍 | offline enable/disable ✅; live device toggle arg 🔍 |
| Live model set on device | Model List | `/ModelSet`+`/ModelEnableSet` | none | none | none | 🔴 | offline swap ✅; live device model-set not exposed |
| Matrix Mixer (per-output mix/mute/solo) | device Main Volume | `/MixerSave`, mixer params | none | none | none | 🔴 | 8 song tracks + paths + click + USB/BT/aux, fader/pan/mute/solo — whole subsystem missing |

## 4. Block & parameter editing (authoring)

| Function | App location | Protocol | CLI | MCP | Skill | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| Browse models / params / ranges | Model List | `defs` (bundled) | full | full | full | ✅ | `list-blocks`, `show-block` |
| Set params (slider/knob/precise) | Inspector | `.hsp` / `/ParamValueSet` | full | full | full | ✅ | `set-param`; tone skill authors |
| Reset param / factory-default | right-click | defaults | partial | partial | partial | 🟡 | can set to known default; no "reset to model default" verb |
| Save user defaults | Action Panel | `/BlockUMDSet` | none | none | none | 🚫 | app-local model-default store |
| Deep-edit / batched params | popup | `/SetBatchedParamValues` | none | none | none | 🟡 | per-param works; batched-set efficiency-only |
| Focus view | Inspector | UI-only | n-a | n-a | n-a | 🚫 | rendering affordance, no device state |
| Live param edit on device | knobs | `/ParamValueSet` | full | full | n-a | ✅ | `device set-param` (2.0) |

## 5. Snapshots

| Function | App location | Protocol | CLI | MCP | Skill | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| Create / name / color 8 snapshots | popup_snapshot | `snps` synth / `/SetSnapshotName` | full | full | full | ✅ | snapshot synth (2.18); color 🟡 (name yes, color field 🔴) |
| Per-snapshot bypass + param delta | snapshot edit | `cg__.entt` synth | full | full | full | ✅ | recipe `snapshots` |
| Recall snapshot live on device | switch | `/ActiveSnapshotIndexGet`/Set | none | none | none | 🔍 | live recall arg 🔍 |
| Copy / paste / swap snapshot | panel | `/CopySnapshot` | none | none | none | 🔴 | live ops; not exposed |
| Discard-edits / reselect behavior | panel + global | `global.snapshot.*` | none | none | none | 🔴 | via §8 property path |

## 6. Controller / footswitch / MIDI / Command Center

| Function | App location | Protocol | CLI | MCP | Skill | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| Assign footswitch → block bypass | ctrlassign | `srcs`/`trgs` synth | full | full | full | ✅ | footswitch synth, HW-validated (2.18) |
| Assign EXP pedal → param(s) | ctrlassign | `/ControllerSourceSet`+`/CidBehaviorSet` | full | full | full | ✅ | EXP synth incl. EXP1Toe wah |
| Momentary / latching | assign popup | `behv` | full | full | full | ✅ | recipe `behavior` |
| Min/max range | Parameter Panel | `/ControllerBoundsSet` | partial | partial | partial | 🟡 | EXP min/max authored |
| Curve / reverse / threshold | assign | `/ControllerCurveSet`/`ThresholdSet` | none | none | none | 🔴 | not modeled |
| Merge switch (multi-block per FS) | Assign to Switch | multi-target | none | none | none | 🔴 | not modeled |
| FS label / color | Label/Color | `/SetContentInfo` (scribble) | none | none | none | 🔴 | not modeled |
| Clear controllers / assignments | Action Panel | remove src/trg | partial | partial | partial | 🟡 | via re-authoring |
| MIDI CC / Note assignment | midiassign | `/ControllerMIDISourceAdd` | none | none | none | 🔴 | not modeled |
| XY controller | XY screen | `/SnapshotSourceSet` XY / ctrl | none | none | none | 🔴 | not modeled |
| **Command Center** (Preset/Snap, Song, Looper, Utility, ExtAmp, MIDI CC/PC/Note/MMC, HotKey) | view_command_center | `commanddefs` + `/ExecuteCommand`/`/CommandTypeSet` | none | none | none | 🔴 | **whole subsystem missing**; 2 cmds/switch, 16 instant, EXP MIDI, per-cmd channel |

## 7. IR (impulse response) management

| Function | App location | Protocol | CLI | MCP | Skill | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| Import IR onto device | Cab IRs ▸ Import | SFTP + `HASH` + 2001 | full | n-a | full | ✅ | `device push-ir` instant (2.9) |
| Auto-upload preset IRs | on install | diff + push-ir | full | partial | full | 🟡 | `install --auto-irs` ✅; MCP `device_install_preset` skips IRs (#6) |
| List device IRs | Cab IRs | `/GetContainerContents(-11)` | full | full | n-a | ✅ | `device list-irs` |
| Export / download IR | Export | SFTP get | full | n-a | n-a | ✅ | `device pull-ir` (EXPERIMENTAL) |
| Delete device IR (prune) | Delete | `/RemoveContent(-11)` | none | none | none | 🔴 | **backlog #11**; arg 🔍 |
| Rename device IR | Rename | `/SetContentInfo` | none | none | none | 🔴 | not exposed |
| IR folders / move to folder | New Folder | content path | none | none | none | 🔴 | folder org not modeled |
| Register / hash IRs locally | — | local | full | full | full | ✅ | `register-irs`/`ir-scan`/`ir-cache` |
| IR block params (hi/lo cut, mix) | IR block | `.hsp` params | full | full | full | ✅ | authored on the IR block |

## 8. Global settings — ✅ SHIPPED (2.20.0) via `device settings` (161 `global.*` keys)

The app exposes these as Global Settings pages; every value is a device
*property* read/written over `/PropertyValueGet` / `/PropertyValueSet` (protocol
RE'd + hardware-validated 2026-07-13, see
`docs/superpowers/specs/2026-07-13-global-settings-re-findings.md`). helixgen now
covers the whole property surface with `helixgen device settings list|get|set`
(+ MCP `device_settings_*`). The device self-describes each key (name/type/range/
enum) via `/PropertyDefWithKeyGet`, so the catalog is live, not hardcoded.

| Page / function | App location | Protocol | CLI | MCP | Verdict | Notes |
|---|---|---|---|---|---|---|
| Read/write ANY global setting | Global Settings | `/PropertyValueGet`/`Set [key,val]` | full | full | ✅ | `device settings get/set`; enum-by-label + range validation |
| Ins/Outs (levels, impedance, pad, trim, mic gain/phantom/lowcut, S/PDIF, USB, reamp) | Ins/Outs | `global.out.*`, `global.offset.input.*`, `global.in.mic.*` | full | full | ✅ | page `ins-outs` (49 keys) |
| Switches/Pedals (FS6 mode, up/down, combo, EXP, Control A-D, trigger, snapshot/preset return) | Switches/Pedals | `global.fs6.*`, `global.up.down.*`, `global.exp.*`, `global.polarity.control.*`, `global.trigger.*`, `global.snap.*` | full | full | ✅ | page `switches-pedals` (30 keys) |
| Displays (brightness, dim timeout, tap LED) | Displays | `global.brighness.*`, `global.timeout.screen.dim`, `global.tap.led` | full | full | ✅ | page `displays` |
| Preferences (numbering, tap-tempo pitch) | Preferences | `global.numbering.*`, `global.tap.tempo.pitch` | full | full | ✅ | page `preferences`; geolocation/remote-PIN excluded (cloud/privacy) |
| Songs (select song/marker, song play, looper-stops-with-song) | Songs | `global.song.*`, `global.looper.stops.with.song` | full | full | ✅ | page `songs` |
| Tempo/Click (bpm, follow, select, click sounds, MIDI clock) | Tempo/Click | `global.tempo.*`, `global.bpm.*`, `global.click*`, `global.midi.clock.*` | full | full | ✅ | page `tempo-click`; also `/SetTempo` (§10) |
| MIDI (USB-C, thru, channel, PC send/receive, snapshot CC) | MIDI | `global.midi.*` | full | full | ✅ | page `midi` |
| Date/Time (NTP, timezone, clock fields, format, hide) | Date/Time | `global.clock.*` | full | full | ✅ | page `date-time` |
| Tuner config (ref pitch, offsets, type, in/out, trails) | (device tuner) | `global.tuner.*` | full | full | ✅ | page `tuner` (19 keys) — also §9 |
| WiFi / Bluetooth enable | (device) | `global.wifi.*`, `global.bluetooth.*` | full | full | ✅ | page `wireless` |
| Global EQ (3 EQs: 1/4"/XLR/Phones, bands, bypass, copy/paste/reset) | Global EQ view | `dsp.globaleq.*` + `/GraphEnableSet` | none | none | 🔍 | **not property-based** — separate screen; param write path still to capture (follow-up) |

## 9. Tuner (device-only in the app — but network-addressable)

| Function | App location | Protocol | Verdict | Notes |
|---|---|---|---|---|
| Engage / exit tuner | device FS12 | activation cmd | 🔍 | no app view; engage command to capture |
| Read live pitch / cents | device screen | 2001/2003 stream | 🔍 | `device watch` sees streams; readout schema to capture |
| Reference pitch / offsets / type / in-out / trails | device tuner settings | `global.tuner.*` | 🔴 | ~15 keys via §8 |

## 10. Tempo

| Function | App location | Protocol | Verdict | Notes |
|---|---|---|---|---|
| Set BPM | tempo panel | `/SetTempo` | 🔍 | arg shape to capture |
| Time signature | tempo panel | `/SetTimeSignature` | 🔍 | arg shape to capture |
| Tap tempo | device FS12 | tap | 🔴 | |
| Tempo source / follow / MIDI clock | Tempo/Click | `global.tempo.*` | 🔴 | via §8 |

## 11. Looper / transport / Showcase

| Function | App location | Protocol | Verdict | Notes |
|---|---|---|---|---|
| Looper record/play/overdub/stop/undo | device FS / Command Center | `/ActivateLooper`+`/ExecuteCommand`(Looper) | 🔍 | command family known; args to capture |
| Transport (play/stop/cycle/markers) | Showcase | `/Transport*` (25 verbs) | 🔍 | multitrack player transport; arg shapes to capture |
| Song / Showcase multitrack + Playlists + Flags/Markers | Song view | `/SetCurrentSong`, Song content | 🚫 | large separate feature (audio player, cloud transfer); out of core scope unless requested |

## 12. Device maintenance / connectivity

| Function | App location | Protocol | Verdict | Notes |
|---|---|---|---|---|
| Backup a setlist to local files | Librarian export | `/GetContentData` | ✅ | `device backup` (non-activating, 2.18) |
| Restore preset content from file | Import | `/SetContentData` | ✅ | `device restore` |
| Full-device backup/restore (microSD) | device Maintenance | on-device only | 🚫 | microSD-side; app equivalent = librarian export (covered) |
| Product / device info (fw, model) | Help ▸ About | `/ProductInfoGet` | 🔴 | quick win: `device info` |
| Connect / auto-connect / manual IP | Connect dialog | discovery | ✅ | `--ip`/`$HELIXGEN_HELIX_IP` |
| Firmware update / factory reset / SD format | Update / Maintenance | cloud + flash | 🚫 | brick/destructive; out of scope |
| LED / scribble-strip control | — | `/LEDSet`/`/LEDSetBlink` | 🚫 | niche performance lighting |

## 13. Templates / Favorites / Clones / cloud

| Function | App location | Protocol | Verdict | Notes |
|---|---|---|---|---|
| Preset templates (save/select/import/export/folders) | Templates | content | 🚫 | app-local convenience over authoring helixgen already does |
| Block favorites (save/import/export) | Favorites | `/SaveBlockToFavorite` | 🚫 | app-local |
| Clones / Proxy captures (create/use/import/export) | Clones | `/IngestClone` + cloud training | 🚫 | cloud-trained capture; separate feature, out of core scope |
| Line 6 login / Remote Access / CustomTone | login / remoteaccess | cloud APIs | 🚫 | account/cloud |

---

## Summary (pre-ranking)

**✅ done:** preset CRUD, setlist reference-sync, full authoring/transcode
(graph, dual-amp, splits, snapshots, footswitch/EXP), IR upload/list/download,
non-activating read/backup, live param set.

**🔴 missing (in-scope), by size:**
- **Global settings** (§8) — 8 pages, ~150 relevant keys; the biggest gap.
- **Command Center** (§6) — whole footswitch-command subsystem.
- **Matrix Mixer** (§3) — per-output mixing/mute/solo.
- Signal-flow param depth (§3) — input/output/split/merge/loop params.
- Live device ops — snapshot recall/copy, model set, block bypass.
- IR prune/rename/folders (§7), setlist rename/delete/duplicate (§2),
  preset color/notes (§1), device info (§12), controller curve/MIDI/label (§6).

**🔍 needs-capture (command known, arg shape only):** `/PropertyValueSet`
(unlocks §8), `/SetTempo`, `/SetTimeSignature`, global-EQ write, tuner
engage+readout, looper/transport, active-preset select (#1), create-setlist
(#8), reorder args, live snapshot recall.

**🚫 out-of-scope:** firmware, factory reset, SD format, full-device microSD
backup, Showcase multitrack, clones, favorites, templates, cloud/Remote-Access,
LEDs, focus-view/UI cosmetics.

Ranking + backlog entries: `docs/device-backlog.md` (§ Stadium-app parity).
