# Stadium-app parity capture — protocol findings (2026-07-14)

Reverse-engineering writeup from the owner-driven Frida capture run on
2026-07-14 against a **Helix Stadium XL**, desktop app **v1.3.2.9805**
(`com.line6.p35edit`, debug build), over Wi-Fi. Harness:
`tools/re_capture_parity.py` (interactive) / a non-interactive control-file
variant; both stream every socket frame to a per-step-tagged JSONL. This
document is the authoritative record of the **argument shapes and value
encodings** pinned by that capture; `docs/helix-protocol.md` is the ongoing
reference and cites this file.

Capture artifacts (local only — `captures/` is gitignored): the tagged JSONL,
two preset content blobs (`ZZCAP-CC` = a Command-Center preset, `ZZCAP-CTRL` = a
controller/XY preset), and an exported `.hss` setlist.

## 0. Transport recap (unchanged, confirmed)

OSC-over-TCP, cleartext, big-endian. Three ports:
- **2002** — DEALER→ROUTER RPC: **every app→device write/command**, plus
  `/success` / `/status` replies. A **client-chosen monotonic request/command
  id is the first arg** of every command; the device echoes it.
- **2001** — device→app: property/state echoes (`/setPropertyValue`,
  `/setEditBuffer`, `/setBlockEnable`, …) and lower-rate telemetry.
- **2003** — device→app: high-rate telemetry (`/dspEvent`, `/trigger`,
  `/heartbeat`, `/stomp`, `/meter`).
- **22** — SSH/SFTP (encrypted): song files, and some content transfer. Opaque
  to the socket hook. **Time signature and per-song tempo travel here, not OSC.**

Two msgpack blob dialects recur (4-char field names encoded as msgpack
`uint32` of their ASCII bytes):
- **Property value** blob: magic `lavppgsm`, map `{key_, type, val_}`.
- **Controller def** blob: magic `lrtcpgsm`, map `{cid_, type, tid_, curv, min_,
  max_, …}` (see §7).

Block/param addressing throughout the live-edit commands is
**`(dsp, block_id[, param_id])`**: `dsp` = signal-path/DSP index (0 or 1),
`block_id` = block index within the path, `param_id` = numeric param id from the
model defs. The pool container is `-2`; the setlists root is `-5`.

---

## 1. Live device-control commands (port 2002)

All confirmed with concrete captured args. `cmd` = the leading monotonic id.

| Op | Address | Typetags | Args (decoded) | Echo / effect |
|----|---------|----------|----------------|---------------|
| **Recall snapshot** | `/activateSnapshot` | `,ii` | `[cmd, snapshotIndex]` — index **absolute, 0-based** (recalls of 1/2/1/0 captured) | followed by `/setBatchedParamVals` (the snapshot's param deltas as an undo-history msgpack array) |
| **Bypass/enable block** | `/BlockEnableSet` | `,iiii` | `[cmd, dsp, block_id, enable]` — e.g. `[115,0,3,0]` bypass, `[116,0,3,1]` on | echoed as `/setBlockEnable [devId, seq, dsp, block, enable]` on 2001. **`block_id` = the `sfg_.flow[dsp].blks` position key.** The toggle is **volatile** — audible immediately but NOT written to the content blob's `enbl` (base enable) until the preset is saved; so `/EditBufferStateGet` read-back does not reflect a live bypass. helixgen confirms the write via the 2001 echo, not a read-back. |
| **Set model** | `/ModelSet` | `,iiiii` | `[cmd, dsp, block_id, subpos, modelId]` — e.g. `[117,0,4,0,70]` | echoed `/setModelWithMID`; **cascade**: `/setBlockEnable`, `/setBlockFavorite`, `/assignSnapshotBypass`, `/attachBlockBypassControllerWithBlob` (a `lrtcpgsm` blob), `/setControllerSource`+`/setSourceEnable`, and a batch of `lavppgsm` `/setPropertyValue` param **defaults** for the new model |
| **Set param** | `/ParamValueSet` | `,iiiiifi` | `[cmd, path, block, 0, paramId, floatValue, -1]` (already in §6 of the protocol ref) | edit-buffer update |
| **Load preset (make active)** | `/LoadPresetWithCID` | `,ii` | `[cmd, cid]` — **load-by-CID confirmed** (`/LoadPresetAtContainerPosition` never appears) | `/setEditBuffer` + `/loadContentRef`. **Resolves backlog #1**: a preset has ONE load = recall-by-CID; there is no separate active-index. Single-click *select* only reads metadata (`/GetContentRef`); the double-click *load* is this command. |
| **Reorder within a container** | `/ReorderContainerContent` | `,iibi` | `[cmd, containerCID, msgpack[movedCIDs], newPos]` — e.g. `[306, -2, [1206], 5]` | `/updateContainerContent` returns the full re-ordered listing. **Same command reorders setlists** (a setlist is a container under `-5`). |

Notes:
- **"Copy snapshot" has no atomic opcode.** What looks like it in the app is
  either (a) a librarian preset **duplication** (`/AddContentsToContainer
  [cmd, -2, [srcCID], pos, 0, 0]` → new cid), or (b) a batch of property writes.
  To replicate a snapshot copy, read the source snapshot's deltas and write them
  onto the target.

---

## 2. Global EQ — `dsp.globaleq.*` (SHIPPED: `device globaleq`)

**Property-based** (this corrects the earlier "separate non-property screen"
belief). Writes go over the same 2002 property channel as global settings, but
with a **variant** value:

```
/PropertyValueSet [cmd, ctx=0, blob]
blob = "lavppgsm" + msgpack{ key_: "dsp.globaleq.<out>.<band>.<param>",
                             type: "v",
                             val_: { parm: <slot>, valu: <value> } }
```

- **Outputs** `<out>`: `qtr` (1/4"), `xlr`, `pho` (Phones) — three independent EQs.
- **Bands** `<band>` (numeric index in the snapshot form): `lowcut` (0),
  `lowshelf` (1), `low` (2), `mid` (3), `high` (4), `highshelf` (5),
  `highcut` (6). **All seven names + all three outputs hardware-confirmed**
  (each write returns `/success` code 0).
- **Param `<param>` → `parm` slot**: `enable`=1 (bool), `freq`=2 (Hz float),
  `gain`=3 (dB float), `q`=4 (float), `slope`=5 (int). Per-band validity:
  cut filters (lowcut/highcut) have enable/freq/slope; shelves (lowshelf/
  highshelf) have enable/freq/gain; peaking (low/mid/high) have enable/freq/
  gain/q.
- **Output level**: key `dsp.globaleq.<out>.level`, slot 3 (dB). (The full
  snapshot stores it as `olvl:{parm:3,valu}`; a single-key write of a bare
  float was also observed — helixgen sends the `{parm:3,valu}` form.)
- **Full-snapshot form**: key `globals.eq`, `type:"v"`, `val_ = {_qtr, _xlr,
  _pho, anyl:bool, post:bool}`; each output = `{drty, enab, eqbs:[{band, para:
  [{parm,valu}…]}…], olvl, out_:null}`. The app also emits this after edits; the
  single-key form is sufficient for helixgen.

**Read caveat:** `dsp.globaleq.*` and `globals.eq` return an **empty blob** to
`/PropertyValueGet` (the app gets EQ state from the connect-time sync, and never
issued a per-key read in the capture). So Global EQ is **write-only** over the
network; helixgen ships `set` + offline `list`, no `get`. (Open: whether a bulk
connect-time read exposes it — see BACKLOG.)

Byte-exact golden blobs: `tests/fixtures/globaleq/`. Codec:
`src/helixgen/device/globaleq.py`.

---

## 3. Tempo & time signature

- **Tempo BPM is a property** — already reachable via `device settings set
  global.tempo.bpm <n>` (also `preset.tempo.bpm`, `volatile.tempo.bpm`,
  `server.snapshot.tempo.bpm`). Confirmed readable (`global.tempo.bpm` → 120.0).
  The `/SetTempo` OSC verb the matrix guessed is unnecessary — the property path
  works.
- **Time signature is NOT on OSC.** It is a **Song** property; song data is
  pushed over the encrypted SFTP channel (port 22). An exhaustive cleartext
  token scan (`timesig`/`numerator`/`denominator`/`meter`/`beats`) found
  nothing. Setting it programmatically needs song-file RE (deferred). The
  6/4→6/8 change in the capture went entirely over SSH.

---

## 4. Tuner & meters — `/dspEvent` telemetry (port 2003)

The Stadium runs a **continuous background pitch detector**; its readout is
always on the wire (not tuner-mode-gated). The desktop app has **no** tuner view
(FS12 hardware screen only), yet the data streams — so helixgen can expose a
**network tuner and level meters purely by subscribing to 2003**.

- **Pitch**: `/dspEvent` with event id `{eid_:10, mid_:796}`, payload
  `vals=[<single float>]`. Encoding = **fractional MIDI note**: integer part =
  MIDI note number, fractional part × 100 = **cents** offset; `-1.0` = no-pitch/
  silence sentinel; idle baseline ≈ 14.97. Verified: a slightly-flat low-E read
  `39.90` → E2 (MIDI 40) −10 cents = 81.93 Hz; harmonics locked at the same −10
  cents (51.90 = E3, 58.90 = B3), proving the linear-semitone scale.
- **Meters**: same burst carries `/dspEvent` `{eid_:1, mid_:796}` and
  `{eid_:1, mid_:800}` = **128-float arrays** (grid level metering, ~0.0–0.08).
- **Engage/exit** the hardware tuner is **not** a tuner property — it is the
  TAP/Tuner footswitch (FS12): `volatile.press.taptempo` (val 11) +
  `volatile.held.taptempo` to enter; `volatile.press.exittuner` (val 11) to
  exit. helixgen does not need to engage it — the pitch stream is always live.

`/dspEvent` blobs are msgpack maps `{id__:{eid_,mid_}, vals:[…]}`.

---

## 5. Command Center — footswitch/EXP commands (port 2002)

**Distinct framing:** Command Center RPCs use a **2-byte big-endian length
prefix** (not the property frames' 4-byte). Every request leads with an `int
seq`.

Verbs:
- `/attachCommandWithType` `,iiiiiiib` → creates a command; device replies with
  a **sequentially-allocated handle**.
- `/setCommandParamVal (seq, handle, paramIdx, val)` → writes one command param.
- `/SelectedCommandSet`, `/CommandTypeSet`, `/removeCommand`.

**Type is two-level.** The attach `family` arg: **1 = Preset/Snapshot,
4 = HotKey/Utility, 6 = MIDI**. For MIDI, the subtype is then chosen via
`setCommandParamVal idx 1`: **0 = PC (Bank/Program), 1 = CC, 3 = Note,
2 = MMC** (MMC tentative — value cleared before a confirming write). MMC **is**
present in the app UI (the bundle-string survey missed it; the owner confirmed a
"Forward" MMC command).

**Param slots** (matched to the owner's entered values): CC → idx2 = channel
(5), idx6 = CC# (76), idx7 = value (65); PC → idx5 = program (44); Note →
idx8 = note-class (11 = B); Instant → idx1 = channel (4); EXP CC → idx0 =
channel (2), idx1 = CC# (11). `paramIdx` maps 1:1 onto the stored `pvla`…`pvll`
slots. EXP/continuous commands use a different slot layout than footswitch
commands.

**Slot → controller** ("Stomp A1" etc.): a small `locl` int (footswitches
observed 25/26/27; **Instant 1 = 0**; **EXP A = 43**), distinguished by
`srcType` (1 = FS/EXP, 4 = Instant) plus a continuous flag.

**Serialized in the preset** (`ZZCAP-CC.sbe`): under `cg__.entt`, a relational
store — `srcs` (source slots) → `cmnd` (payload: `type` family + `func` subtype
+ `pvla`…`pvll` param slots) → `trgs` (MIDI/param destinations), with `nxt*` id
allocators. The two surviving commands (Instant MIDI ch4, EXP A CC#11 ch2)
confirm the wire→storage mapping byte-for-byte (EXP stored `pvla=2, pvlb=11,
pvlc=0`(min)`, pvld=127`(max)).

Tentative (cleared/overwritten before a confirming value): MMC "Forward" enum
value, HotKey keycode/modifier, Utility "Mute All" id, Note octave slot.

---

## 6. MIDI controller assignment — block bypass / param (port 2002)

A two-step dance (echoed on 2001):

1. **Bind** a controller to a target:
   `/attachParamController ,iiiii = [goid, dsp, block, sub, param]` (for a param)
   or `/attachBlockBypassController` (for a bypass). The `…WithBlob` echo carries
   a `lrtcpgsm` controller def: `type:1` = bypass, `type:3` = param, `tid_` =
   target id.
2. **Create the incoming CC source**: `/ControllerMIDISourceAdd ,iib`. The
   **CC# is a BE uint16 at byte offset 12** of a 20-byte blob: `00 4f` = CC#79
   (bypass), `00 3c` = CC#60 (Triangle Fuzz "Tone" param). Fired **twice** (a
   MIDI-learn placeholder, then the committed CC). **No channel / min / max on
   the wire** — the MIDI channel is the device's global base channel.

Removal: `/removeControllerMIDISource` (per source) then `/removeController`
(whole binding).

**Serialized** (`ZZCAP-CTRL.sbe`, CC#61 → amp Drive): three linked places —
`cg__/entt/ctrl[]` (`cid_:4, cnt2:61, midi:0xB03D` = packed CC ch1+#61, `tid_:7`),
`ctm_/ptid[]` mapping `(block<<16 | param) → tid_` (`0x00020001 → tid_7` = amp
Drive), and the amp param's own `cid_:4 / tid_:7`.

This unblocks backlog **#33** (`midisource` was 0 in the whole corpus; now the
live encoding is known).

---

## 7. XY controller — zone morph (port 2002)

Selecting an XY "zone" (the amp's 4 corners + centre, e.g. Dom/Noonish/Thrill/
Free/Smooth) emits **one** `/SetBatchedParamValues ,ib` whose msgpack blob is a
**12-element array of `[dsp, block, sub, paramId, valueF64]`** — the block's
**entire** param set. There is **no zone-index field**; the batch of param
values *is* the activation. So XY zones are effectively **block-level
mini-snapshots**.

**Persistence caveat:** the `ZZCAP-CTRL.sbe` blob did **not** contain the
inactive zones — the amp block carried one model whose 12 params equal only the
active ("Smooth") zone; the other zones' float values appear nowhere, and no
zone container/label strings exist in the blob. **XY-zone storage location is
unresolved** — do not assume a `.sbe` round-trip preserves inactive zones. This
is the main open item for backlog **#34** (the wire *activation* is known; the
*storage* is not).

---

## 8. `.hss` setlist bundle format (partially SHIPPED: readable)

Not JSON/msgpack/zip. Structure: a **24-byte Line 6 header + a gzip stream**
whose decompressed content is a **POSIX (ustar) tar archive** — a `.tar.gz` with
a wrapper.

- Header (0x00–0x17): tag `GGGY` + u32 0, tag `LTES` (byte-reversed = `SETL`) +
  u32 0, u64 = 256 (version); gzip magic `1f8b` at 0x18.
- Tar members: `manifest.json` + **128** slot files `.1`…`.128`. An **empty**
  slot is a 1-byte `0x00` sentinel.
- `manifest.json`: `meta = {name, device_id:0x260000, device_version:0x1302053C}`
  and an ordered `contents[]` of 128 `{path:".N", type:"<null>"}` entries
  (`"<null>"` = empty slot). Filled slots embed the preset's `_sbepgsm` content
  as the `.N` payload.

**Reading** an `.hss` is trivial today (`gzip` + `tarfile` + `json` + the
existing `_sbepgsm` decoder). **Writing** is feasible but the filled-slot `type`
token and exact filled-`.N` payload framing are **inferred** — a **non-empty**
`.hss` export is needed before a byte-faithful writer (the captured export was of
an empty setlist). Unblocks most of backlog **#31**.

---

## 9. What this capture resolved (matrix/backlog deltas)

Resolved to a known argument shape / value encoding:
- Active-preset select (**#1**) — `/LoadPresetWithCID`, load-by-CID.
- Reorder presets **and** setlists — `/ReorderContainerContent [cmd, container,
  [cids], newPos]`.
- Live block bypass — `/BlockEnableSet [cmd, dsp, block, enable]`.
- Live model set — `/ModelSet [cmd, dsp, block, sub, modelId]`.
- Live snapshot recall — `/activateSnapshot [cmd, index]` (absolute).
- **Global EQ** — full property spec; **shipped** as `device globaleq`.
- **Tempo** — property; already shippable via `device settings`.
- **Tuner + meters** — `/dspEvent` schema; implementable via 2003 subscribe.
- **Command Center** (#16/#33-adjacent) — verb set, type families, param slots,
  `cg__.entt` storage.
- **MIDI controller assign** (#33) — `/ControllerMIDISourceAdd` CC# encoding +
  `.sbe` `ctrl`/`ctm_` storage.
- **XY** (#34) — `/SetBatchedParamValues` activation (storage still open).
- **`.hss`** (#31) — container format decoded; reading unblocked.

Device-only (confirmed 🚫, not app features): **Matrix Mixer** (the app has no
mixer view — only the Output block's Pan+Level) and the **Tuner UI**.

Still open: time signature (SFTP song file), XY-zone *storage*, `.hss` filled-slot
payload framing, and the Global EQ network *read-back* path.
