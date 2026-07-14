# Helix Stadium app — coverage matrix

Every user-facing function of the **Helix Stadium desktop app** (v1.3.2.9805,
internal `p35edit`; `P35` = Stadium, `P37`/`P36` = Stadium XL) mapped to
helixgen's CLI / MCP / skill surface. Goal: drive the gaps to zero so the app is
never needed. Maintained ongoing (like `BACKLOG.md`).

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
| Set / batch preset color | Rename / Batch Color | `/SetContentAttrs` `{colr:int}` | full | full | n-a | ✅ | `device set-info <cid>... --color` (batch) / MCP `device_set_info`; int enum, HW-validated 2026-07-14 (#20) |
| Reorder presets | drag | `/ReorderContainerContent` | partial | none | partial | 🟡 | `slots reorder`+sync _(library-agent)_; not HW-validated; arg 🔍 |
| Move preset between setlists | drag | reference add/remove | full | full | full | ✅ | `device setlist add/remove` _(library-agent)_ |
| Delete / clear-from-setlist | Delete / Clear | `/RemoveContent` | full | full | full | ✅ | `device delete`; clear = drop reference |
| Export preset (.hsp) | drag out / Export | `/GetContentData` | full | n-a | full | ✅ | `device pull` (non-activating, 2.18) |
| Import preset | drag in / Import | `/CreateContent`+`/SetContentData` | full | full | full | ✅ | `device push`/`install` |
| Preset Info / Notes / Clips | Preset Info panel | notes = `pm__` `preset.meta.info` via `/GetContentData`+`/SetContentData`; clip = audio content | full | full | n-a | ✅ | notes ✅ `device set-info --notes` (non-activating RW, HW-validated 2026-07-14, #20); audio clips 🚫 |
| MIDI Recall display | sidebar | client-side calc | none | none | none | 🟡 | derivable from setlist/preset/snapshot index; could compute offline |

## 2. Setlist management _(library-agent)_

| Function | App location | Protocol | CLI | MCP | Skill | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| List setlists | sidebar | `/GetContainerContents(-5)` | full | full | full | ✅ | `device setlists` |
| Create setlist | sidebar ▸ + | `/CreateContent(-5, pos, ctype=1003, {name})` | full | full | full | ✅ | **#8 SHIPPED** `device setlist create` / `device_setlist_create` (HW-validated 2026-07-14); `create-local` = manifest only |
| Rename setlist | dbl-click | `/SetContentAttrs` `{name}` | full | full | n-a | ✅ | `device setlist rename` / `device_setlist_rename` (also renames local manifest record); HW-validated 2026-07-14 (#20) |
| Duplicate setlist | Duplicate | copy references (rcid) into a fresh setlist | full | full | n-a | ✅ | `device setlist duplicate` / `device_setlist_duplicate` (auto-creates target; pool presets shared, not copied); HW-validated 2026-07-14 (#20) |
| Reorder setlists | drag | reorder cmd | none | none | none | 🔍 | arg 🔍 |
| Delete / clear setlist | Delete / Clear | `/RemoveContent(-5,[cid])` | full | full | partial | ✅ | `device setlist delete` / `device_setlist_delete` — references die, pool presets never (never-orphan, HW-validated 2026-07-14, #20); clear = `unsync`/mirror-to-empty |
| Sync setlist(s) | (app is live) | pool+reference reconcile | full | full | full | ✅ | `device sync <setlist>`/`--all --gc` _(library-agent)_ |
| Import / export setlist (.hss) | File menu | bulk content | partial | none | partial | 🟡 | per-tone push/pull exists; single-file `.hss` bundle = **backlog #31 — needs a sample .hss** (format not guessed) |

## 3. Signal-flow editor

| Function | App location | Protocol | CLI | MCP | Skill | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| Add / remove / move / clear blocks | home_edit grid | authored `.hsp` → transcode | full | full | full | ✅ | `add-block`/`remove-block` + full-graph transcode (2.18) |
| Replace / swap model | Model List | swap in `.hsp` | full | full | full | ✅ | `swap-model` (same-category) |
| Copy / paste block | Action Panel | `.hsp` edit | partial | partial | partial | 🟡 | achievable via authoring; no one-shot copy verb |
| Parallel split (create) | drag down | `sfg_.flow` grid synth | full | full | full | ✅ | intra-flow split/join, HW-validated (2.18) |
| Split TYPE (Y / A-B / Crossover / Dynamic) | Split Inspector | split block params | full | full | full | ✅ | recipe `split.type` + validated per-type params; transcode-pinned (parity #18, 2026-07-14 spec) |
| Merge mixer (levels/pan/polarity) | Merge Inspector | merge block params | full | full | full | ✅ | recipe `join.params` (A/B Level/Pan, B Polarity, Level) validated; `set-param join`; transcode-pinned (#18) |
| Dual DSP / dual amp | two paths | dual-flow synth | full | full | full | ✅ | dual-amp synth, HW-validated (2.18) |
| Input block (source/Z/pad/trim/gate) | Input Inspector | per-path input + params | full | full | full | ✅ | input object form: impedance (device-self-described enum) + pad/trim/gate(+stereo per-channel); `set-param input`; transcodes (#18) |
| Output block level/pan | Output Inspector | per-path output params | full | full | full | ✅ | `output: {level, pan}` + `set-param output` (#18) |
| Output block destination (Matrix/XLR/1-4"/Path-2) | Output Inspector | output endpoint model | partial | partial | partial | 🟡 | not authorable; round-trips verbatim via `structural` entries — deliberate scope in the #18 design spec |
| FX Loop / Send / Return | block Inspector | loop block + Trails | full | full | full | ✅ | Send/Return/Mix/DryThru are ordinary block params; `trails` now covers `HD2_FXLoop*` (#18). Caveat: authoring an FX-Loop block needs an `HD2_FXLoop*` exemplar in the block library — no corpus export carries one, so ingest a preset containing an FX Loop first |
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
| Min/max range | Parameter Panel | `/ControllerBoundsSet` | full | full | full | ✅ | EXP min/max + FS **param toggles** with raw-unit min/max (#21; corpus 77/211, HW-persisted) |
| Curve / reverse / threshold | assign | `/ControllerCurveSet`/`ThresholdSet` | full | full | full | 🟡 | `curve`/`threshold` authored + round-tripped (#21); vocabulary from app-binary enum table, `curv` index anchored (linear=5); non-linear values EXPERIMENTAL (persistence HW-validated, response not characterized). Reverse = `min>max` (corpus-real) |
| Merge switch (multi-block per FS) | Assign to Switch | multi-target | full | full | full | ✅ | #21: N entries share one `switch`; one `srcs` + `scid → [cids]` (fixture + live-persisted) |
| FS label / color | Label/Color | `preset.sources` → `pm__` scribble | full | full | full | ✅ | #21: `label`/`color` per switch; color-int palette anchored by live pulls (red=2, dkorange=3, ltorange=4, purple=9, white=11; rest order-inferred EXPERIMENTAL) |
| Clear controllers / assignments | Action Panel | remove src/trg | partial | partial | partial | 🟡 | via re-authoring |
| MIDI CC / Note assignment | midiassign | `/ControllerMIDISourceAdd` | none | none | none | 🔴 | **backlog #33** — `midisource` is 0 in all 1553 corpus controllers; encoding underivable without mutating the live edit buffer |
| XY controller | XY screen | `/SnapshotSourceSet` XY / ctrl | none | none | none | 🔴 | **backlog #34** — all 84 corpus `xyctrl` dicts are defaults; no XY-sourced controller observed |
| **Command Center** (Preset/Snap, Song, Looper, Utility, ExtAmp, MIDI CC/PC/Note/MMC, HotKey) | view_command_center | `commanddefs` + `/ExecuteCommand`/`/CommandTypeSet` | none | none | none | 🔴 | **whole subsystem missing**; 2 cmds/switch, 16 instant, EXP MIDI, per-cmd channel |

## 7. IR (impulse response) management

| Function | App location | Protocol | CLI | MCP | Skill | Verdict | Notes |
|---|---|---|---|---|---|---|---|
| Import IR onto device | Cab IRs ▸ Import | SFTP + `HASH` + 2001 | full | n-a | full | ✅ | `device push-ir` instant (2.9) |
| Auto-upload preset IRs | on install | diff + push-ir | full | partial | full | 🟡 | `install --auto-irs` ✅; MCP `device_install_preset` skips IRs (#6) |
| List device IRs | Cab IRs | `/GetContainerContents(-11)` | full | full | n-a | ✅ | `device list-irs` |
| Export / download IR | Export | SFTP get | full | n-a | n-a | ✅ | `device pull-ir` (EXPERIMENTAL) |
| Delete device IR (prune) | Delete | `/RemoveContent(-11)` + SFTP file removal | full | full | full | ✅ | **#11 SHIPPED** `device delete-ir` / `device ir-prune` (dry-run default, `--force` for locally-referenced, `--only`); MCP `device_delete_ir`/`device_ir_prune`; HW-validated 2026-07-14 |
| Rename device IR | Rename | `/SetContentAttrs` `{name}` | full | full | n-a | ✅ | `device rename-ir` / MCP `device_rename_ir` (name-or-hash; hash untouched so presets keep resolving); HW-validated 2026-07-14 |
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
| Product / device info (fw, model) | Help ▸ About | `/ProductInfoGet` | ✅ | `helixgen device info` / MCP `device_info` (#21, HW-validated live: fw/serial/model/storage) |
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
- Live device ops — snapshot recall/copy, model set, block bypass.
- IR folders (§7), controller MIDI/XY sources (§6 — #33/#34).
  (IR prune/rename, setlist create/rename/delete/duplicate, preset
  color/notes all ✅ shipped 2026-07-14 — #20/#11/#8; device info (§12) and
  controller curve/label/merge/min-max depth (§6) ✅ — #21.)

**🔍 needs-capture (command known, arg shape only):** `/SetTempo`,
`/SetTimeSignature`, global-EQ write, tuner engage+readout,
looper/transport, active-preset select (#1), reorder args, live snapshot
recall. (`/PropertyValueSet` captured — §8 shipped 2.20.0; create-setlist
cracked without capture — #8 shipped 2026-07-14.)

**🚫 out-of-scope:** firmware, factory reset, SD format, full-device microSD
backup, Showcase multitrack, clones, favorites, templates, cloud/Remote-Access,
LEDs, focus-view/UI cosmetics.

Ranking + backlog entries: `docs/BACKLOG.md` (§ Stadium-app parity).
