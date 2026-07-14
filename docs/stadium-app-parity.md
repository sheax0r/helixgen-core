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

> **2026-07-14 parity capture:** an owner-driven Frida capture pinned the
> argument shapes / value encodings for the remaining 🔍 rows. Full writeup:
> `docs/superpowers/specs/2026-07-14-parity-capture-findings.md`. Resolved: Global
> EQ (now **shipped** — `device globaleq`), active-select (#1), reorder args,
> live bypass/model/snapshot ops, Command Center (#16), MIDI controller (#33) &
> XY (#34) wire encoding, tuner/meter telemetry schema, tempo (property), `.hss`
> container format (readable). Confirmed device-only (🚫): Matrix Mixer & Tuner
> UI. Still open: time signature (SFTP song), XY-zone storage, `.hss` filled-slot
> payload, Global EQ network read-back.

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
| Make ACTIVE preset | click in setlist | `/LoadPresetWithCID` | full | full | full | ✅ | **#1 RESOLVED 2026-07-14**: a preset has ONE load = recall-by-CID (`/LoadPresetAtContainerPosition` never appears; there is no separate active-index). = `device load`. Single-click select is just a metadata read |
| New / duplicate / copy-to-setlist | Manage Presets | `/CreateContent`+`/SetContentData` | full | full | full | ✅ | `device create`/`install` + reference model _(library-agent)_ |
| Rename preset | Rename dialog | `/SetContentInfo` | full | full | full | ✅ | `device rename` |
| Set / batch preset color | Rename / Batch Color | `/SetContentAttrs` `{colr:int}` | full | full | n-a | ✅ | `device set-info <cid>... --color` (batch) / MCP `device_set_info`; int enum, HW-validated 2026-07-14 (#20) |
| Reorder presets | drag | `/ReorderContainerContent` `[cmd,container,[cids],pos]` | partial | none | partial | 🟡 | `slots reorder`+sync _(library-agent)_; **arg decoded 2026-07-14** (moved-cids + dest index); live reorder verb still to wire + HW-validate |
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
| Reorder setlists | drag | `/ReorderContainerContent` (setlists are containers under -5) | none | none | none | 🔍 | **arg decoded 2026-07-14** (same command as preset reorder); verb not yet wired |
| Delete / clear setlist | Delete / Clear | `/RemoveContent(-5,[cid])` | full | full | partial | ✅ | `device setlist delete` / `device_setlist_delete` — references die, pool presets never (never-orphan, HW-validated 2026-07-14, #20); clear = `unsync`/mirror-to-empty |
| Sync setlist(s) | (app is live) | pool+reference reconcile | full | full | full | ✅ | `device sync <setlist>`/`--all --gc` _(library-agent)_ |
| Import / export setlist (.hss) | File menu | 24-byte header + gzip + tar (`manifest.json` + 128 `.N` slots) | partial | none | partial | 🟡 | **#31: format decoded 2026-07-14, reading unblocked** (stdlib gzip+tarfile+json + `_sbepgsm` decoder). Sample `.hss` captured (empty setlist); a non-empty export still needed for a byte-faithful *writer* |

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
| Live block bypass on device | click block | `/BlockEnableSet` `[cmd,dsp,block,enable]` | full | full | n-a | ✅ | **SHIPPED 2026-07-14** `device bypass PATH BLOCK on\|off` (+ MCP `device_bypass`, `device_blocks` lister). HW-confirmed via the `/setBlockEnable` echo; live toggle is volatile until save |
| Live model set on device | Model List | `/ModelSet` `[cmd,dsp,block,sub,modelId]` | full | full | n-a | ✅ | **SHIPPED 2026-07-14** `device model PATH BLOCK <model>` (+ MCP `device_model`); numeric id or model-id string. Device rejects cross-category. (Controller re-attach + default push from the app cascade not replayed) |
| Matrix Mixer (per-output mix/mute/solo) | **device screen only** | device-hardware UI | n-a | n-a | n-a | 🚫 | **NOT an app feature** (confirmed 2026-07-14 by manual + app-bundle survey: the desktop app has no mixer view — only the Output block's Pan+Level). Device-screen-only, out of app-parity scope |

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
| Recall snapshot live on device | switch | `/activateSnapshot` `[cmd, index]` | full | full | n-a | ✅ | **SHIPPED 2026-07-14** `device snapshot <index>` (0-based, +MCP `device_snapshot`); absolute index |
| Copy / paste / swap snapshot | panel | (no atomic opcode) | none | none | none | 🔴 | **2026-07-14: no `/CopySnapshot` exists** — the app copies via preset duplication or a batch of property writes. Replicate by reading source deltas → writing onto target |
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
| MIDI CC / Note assignment | midiassign | `/attachParamController`/`/attachBlockBypassController` + `/ControllerMIDISourceAdd` | none | none | recipe `midi` → transcode | 🔴→🟡 | **#33 MOSTLY SHIPPED 2026-07-14 (EXPERIMENTAL, CC-only)**: recipe `midi` list → `spec`/`mutate.wire_midi` (namespaced `preset._helixgen_midi`) → `view` round-trip → transcoder synthesizes `cg__.entt/ctrl`(`cnt2`/`midi`/`type`/`tid_`)+`ctm_.ptid` per §6. STORAGE HW-validated (install→SetContentData→GetContentData round-trip persisted both ctrl records byte-for-byte on Stadium XL 2026-07-14). Residuals: `.hsp`-native encoding not invented (transcode-only route); audible CC response uncharacterized (no external CC sent); MIDI Note out of scope; no live verb |
| XY controller | XY screen | `/SetBatchedParamValues` (zone = block-level param batch) | none | none | none | 🔴→🔍 | **#34 activation decoded 2026-07-14**: selecting a zone pushes the block's whole param set (no zone index). ⚠️ inactive-zone **storage** still unresolved (not in `.sbe`) |
| **Command Center** (Preset/Snap, MIDI CC/PC/Note/MMC, Instant) | view_command_center | `/attachCommandWithType`+`/setCommandParamVal` (2-byte-len framing) | recipe `commands` → `preset.commands` | `preset.commands` (native) | recipe `commands` → transcode | 🔴→🟡 | **#16 SHIPPED 2026-07-14 (EXPERIMENTAL)**: NATIVE `.hsp` route — `preset.commands` (corpus-proven: Mandarin Fuzz + Epic Lots of EQ). Recipe `commands` list (midi_cc/pc/note/mmc + snapshot on FS1-5/FS7-11 + Instant1-6, ≤2/switch) → `spec`/`mutate.wire_command` → `view` round-trip → transcoder synthesizes `cg__.entt` srcs→cmnd→trgs. Live pulls (Mandarin + ZZCAP-CC) CORRECTED findings §5: `cmnd` slots are type-dependent (PresetSnapshot 5+5, MIDI FS/Instant 12+12); command = entity (`cid_`==trg `eID_`, enty 6/type 4). STORAGE HW-validated (snapshot+PC round-trip byte-for-byte; create-path hit #38, restore worked). Residuals: no live verb; recall-preset family deferred (unanchored/ambiguous); HotKey/Utility + EXP-continuous out of scope; FS CC/Note/MMC slots a hypothesis (PC anchored); audible response uncharacterized; FS shared with `footswitches` rejected |

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
| Global EQ (3 EQs: 1/4"/XLR/Phones, bands, bypass) | Global EQ view | `dsp.globaleq.<out>.<band>.<param>` via `/PropertyValueSet` (variant `{parm,valu}`) | full | full | ✅ | **SHIPPED** `device globaleq list/set` (+ MCP `device_globaleq_*`); **IS property-based** (corrects earlier note); byte-exact codec + HW-validated 2026-07-14 (all 3 outputs, 7 bands). **Write-only** over the network (no `/PropertyValueGet` read-back) → no `get`; copy/paste/reset are app-side conveniences |

## 9. Tuner (device-only in the app — but network-addressable)

| Function | App location | Protocol | Verdict | Notes |
|---|---|---|---|---|
| Engage / exit tuner | device FS12 | `volatile.press.taptempo`/`held.taptempo`; exit `volatile.press.exittuner` | 🚫→🔍 | no app view (device-only UI). **Engage decoded 2026-07-14** — but not needed: the pitch stream is always live |
| Read live pitch / cents | device screen | 2003 `/dspEvent` `{eid_:10,mid_:796}` | 🔍 | **schema decoded 2026-07-14**: single float = fractional MIDI note (int=note, frac×100=cents, −1=silence). Continuous background detector — implementable as `device tuner` via 2003 subscribe (no engage needed) |
| Reference pitch / offsets / type / in-out / trails | device tuner settings | `global.tuner.*` | 🔴 | ~15 keys via §8 |

## 10. Tempo

| Function | App location | Protocol | Verdict | Notes |
|---|---|---|---|---|
| Set BPM | tempo panel | property `global.tempo.bpm` | ✅ | **2026-07-14: no `/SetTempo` needed** — tempo is a property, already settable via `device settings set global.tempo.bpm <n>` (also `preset.tempo.bpm`) |
| Time signature | tempo panel | **Song property over SFTP** (not OSC) | 🔴 | **2026-07-14: not on OSC** — carried in the song file over the encrypted SFTP channel; programmatic set needs song-file RE (deferred) |
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
