# Helix Stadium XL network protocol reference

Authoritative, reverse-engineered reference for talking to a **Line 6 Helix
Stadium XL** over the LAN *without* the HX Edit / Helix Stadium editor. This
document supersedes the summary in the repo-root `PROTOCOL.md` and is the
canonical protocol reference; keep terminology aligned with
`docs/superpowers/specs/2026-07-11-helix-device-v2-plan.md`.

**Status:** proof-of-concept, working. CRUD (list / read / load / create-copy /
rename / delete) and live `set-param` have been exercised against real hardware.
Some fields are only partially decoded — every uncertain claim below is marked
**UNVERIFIED** or **partially decoded**.

**Provenance:** reverse-engineered on 2026-07-11 against a Helix Stadium **XL**
(firmware reporting `OpenSSH_9.6`) on the local network, using the macOS **Helix
Stadium Debug** editor build plus a **Frida** socket-capture harness (attach or
spawn the editor, dump device traffic). Numeric model/param/command id maps are
read directly from the definition files **bundled inside the editor app**
(section 8). Corroborated by a public community write-up of the same protocol.

---

## 1. Overview and safety

The device exposes a small cluster of **ZeroMQ** TCP services that the editor
uses for everything: browsing content, loading presets, editing parameters, and
receiving live telemetry. The wire format is **OSC messages carrying msgpack
blobs**.

**Security note — read before you connect anything to a network you don't
control:**

- All ZeroMQ traffic is **cleartext** (no TLS).
- The ZMQ sockets use the ZMTP **`NULL`** security mechanism — **no
  authentication**. Any host that can reach the device's ports 2001–2003 can
  browse, load, edit, create, and delete content. A DEALER may connect to
  `:2002` and issue commands immediately.
- **Multiple concurrent clients are supported.** The editor and an independent
  client can run at the same time; each sees the other's writes via the PUB
  streams. Do not assume you are the only writer.
- Port **22** is a genuine OpenSSH 9.6 `sshd` (the editor bundles `libssh2`).
  It requires **publickey or password** auth — there is **no** anonymous SSH.
  SSH is used for some bulk/file-transfer paths and is *not* required for the
  preset CRUD and edit operations described here.
- The device is intended to be used on a trusted, isolated network (its own
  Wi-Fi AP or a wired LAN segment). Treat exposure to any wider network as a
  full compromise of the device's content.

Device addressing: fixed/observed IP e.g. `192.168.4.84`; mDNS/Bonjour hostname
`p35x1.local` (`p35` is the internal model code for the Stadium; `p37` is a
sibling model that shares the definition-file format).

---

## 2. Transport

ZeroMQ, **ZMTP 3.0**, cleartext, over TCP. Three sockets:

| Port | Device socket | Client socket | Role |
|------|---------------|---------------|------|
| 2002 | `ROUTER` | `DEALER` | Request/response **RPC** (commands + `/status` acks) |
| 2001 | `PUB` | `SUB` | **Property-change notifications** (`setPropertyValue` / `setEditBuffer` stream) |
| 2003 | `PUB` | `SUB` | **DSP telemetry** (`/dspEvent`, `/trigger`, `/meter`, `/heartbeat`) |

- **2002** is the only socket you *send* on. As a `DEALER`↔`ROUTER` pair,
  request/response correlation is by an application-level **request id**
  (section 6), not by ZMQ identity framing.
- **2001** carries the device's outbound property firehose: after a
  `/LoadPresetWithCID` the device streams `/setEditBuffer` (the full preset
  content blob, ~8 KB) followed by a burst of `/setPropertyValue` messages. It
  also carries live echoes of parameter changes (so a second client sees the
  editor's edits).
- **2003** carries continuous DSP telemetry: per-block/meter events
  (`/dspEvent`, `/meter`), a **1 Hz `/trigger`**, and a periodic
  **`/heartbeat`**. Subscribe if you want live signal/meter data; ignore it for
  CRUD.

### ZMTP handshake

Standard ZMTP 3.0: exchange the 10-byte signature + version, negotiate the
**`NULL`** security mechanism (READY command with the socket-type property),
then frames flow. A `pyzmq` `DEALER`/`SUB` performs this automatically; you do
not implement it by hand. No credentials are exchanged.

### Frame → message mapping

Each ZMQ **frame** carries exactly **one OSC message** (section 3). Multi-part
ZMQ messages are used where noted (large list replies chunk their blob across
frames — see `/GetContainerContents`).

### The 2001 PUB header (12 bytes)

Device→editor frames on **port 2001** are prefixed with a **12-byte binary
header** *before* the OSC packet. Observed layout (big-endian fields):

```
offset 0  : version   (uint32-BE)   — protocol/stream version tag
offset 4  : sequence  (uint32-BE)   — monotonically increasing per-stream counter
offset 8  : length    (uint32-BE)   — byte length of the OSC packet that follows
offset 12 : OSC packet (address + typetags + args), `length` bytes
```

To parse a 2001 frame: read the 12-byte header, take `length` bytes, and decode
that as OSC. **UNVERIFIED:** the exact split/meaning of the three uint32 fields
(version vs. sequence vs. length) is inferred from position and monotonic
behaviour; treat field 1 as a version tag and field 2 as a sequence counter
until confirmed by a wider capture. Frames on **2002** and **2003** have **no**
such prefix — the frame *is* the OSC packet. (**2003**'s framing is assumed to
match 2002; **partially decoded**.)

---

## 3. OSC encoding

Every message is a standard OSC 1.0 message with three parts laid out back to
back, each independently padded to a 4-byte boundary with NUL bytes:

1. **Address** — an ASCII string like `/GetContainerContents`, NUL-terminated
   and then **NUL-padded up to a multiple of 4 bytes**.
2. **Type-tag string** — a comma `,` followed by one type character per
   argument (e.g. `,ibi`), NUL-terminated and **NUL-padded to a multiple of 4**.
   **The leading comma counts toward the length for padding purposes** — e.g.
   `,iiiiifi` is 8 bytes of content (comma + 7 tags) → padded to 8; `,ib` is 3
   bytes → padded to 4.
3. **Arguments** — packed in order, each per its type tag.

### Argument types observed

| Tag | Type | Wire format |
|-----|------|-------------|
| `i` | int32 | 4 bytes, **big-endian**, signed |
| `f` | float32 | 4 bytes, **big-endian** (IEEE-754) |
| `h` | int64 | 8 bytes, **big-endian**, signed |
| `s` | string | ASCII, NUL-terminated, **NUL-padded to a multiple of 4** |
| `b` | blob | int32-BE **length** prefix, then that many bytes, then **NUL-padded to a multiple of 4** |

All multi-byte integers and floats are **big-endian** (OSC network byte order).
Blob *payloads* are msgpack (section 4) — the blob's own 4-byte length prefix is
the OSC framing, distinct from any msgpack-internal length.

---

## 4. msgpack blob conventions

Blob (`b`) argument payloads are **msgpack**. Two distinct dialects appear:

### Dialect A — container / metadata maps (fixstr 4-char-code keys)

Used for content listings and item metadata. Plain msgpack maps whose keys are
**msgpack `fixstr` 4-character ASCII codes**. Keys seen: `blck`, `ccid`, `cid_`,
`cctp`, `posi`, `name` (content model, section 5), and — in the bundled command
defs — the same shape. These decode cleanly with any msgpack library
(`strict_map_key=False` is recommended since some maps mix key types).

### Dialect B — preset content ("edit buffer"), the `_sbepgsm` blob

The full preset/edit-buffer content is a **custom blob**:

```
8-byte ASCII magic  "_sbepgsm"   +   msgpack document
```

The magic is the string `msgpebs_` stored **byte-reversed** (`_sbepgsm`). This
is a general Line 6 convention: a msgpack document is prefixed by `msgp` + a
4-char tag, stored reversed. (The bundled model-def files use the same trick:
`ldompgsm` = reverse of `msgpmodl`; see section 8.)

The msgpack document in dialect B uses map keys that are **uint32-packed
4-char-codes** (a 4-byte code packed as a msgpack integer), **not** fixstr.
Decoded, the codes render as 4-char tags such as `cg__`, `asnp`, `entt`,
`cmnd`. To read them, decode the msgpack normally, then for each integer key
unpack it to its 4 ASCII bytes (big-endian) to recover the tag.

#### Preset content (`_sbepgsm`) top-level structure (decoded)

The decoded `_sbepgsm` document is a map with these top-level 4CC keys:

| Key | Type | Meaning |
|-----|------|---------|
| `cg__` | map | **Config.** Holds `asnp` (active-snapshot index) and `entt` (see below), plus `nxt*` "next-id" counters. **Volatile** across a save (differs after save+reload). |
| `hist` | int | Edit/undo **history** marker. **Volatile** (differs after save+reload). |
| `pm__` | list | **Global/preset params** — a list of `{key_, type, val_}` entries, e.g. `key_ = "preset.clip.end"`. Stable across a byte-faithful save. |
| `sfg_` | map | **Signal-flow graph** (the actual tone). Stable across a byte-faithful save. See below. |

`cg__.entt` (config "entities") contains: `cmnd`, `ctm_`, `ctrl`, `sm_`,
`snps` (an **8-element** snapshot array), `srcs`, `trgs` (controller sources /
targets).

`sfg_` (signal-flow graph) contains: `enbl` (enabled), `fcnt` (flow count), and
`flow` — a **2-element list, one per DSP path**. Each flow entry is a map with:

| `flow[]` key | Meaning |
|--------------|---------|
| `bcnt` | **block count** for this path (e.g. `28`) |
| `blks` | **list of blocks** (the models + their params) |
| `bmap` | block map / layout |
| `cid_` | content id of this flow |
| `enbl` | path enabled |
| `snap` | per-path snapshot data |
| `tid_` | path/topology id |

**Save fidelity:** a `/SavePresetWithCID` (§6) reproduces `sfg_` and `pm__`
**byte-for-byte**; only the volatile `hist` and `cg__` sections differ after a
save + reload. So for tone content, compare/round-trip on `sfg_` + `pm__` and
ignore `hist`/`cg__`.

#### Block / param layout inside `sfg_.flow[dsp].blks` (the `.hsp`↔device Rosetta layer)

This is the level at which a device preset and a helixgen `.hsp` describe the
**same thing** (blocks → a model + named params) — they differ only in encoding.

**`blks` is a FLAT alternating list** `[int, dict, int, dict, …]`. Each
`(int, dict)` **pair is one block**: the `int` is the block's index/key and the
`dict` is the block. `bcnt` = number of blocks; `bmap` = the index map
`[0 .. bcnt-1]`. Iterate the list two elements at a time (or use `bmap`) to
recover blocks.

**Block dict** keys:

| Key | Meaning |
|-----|---------|
| `cid_` | content id of this block instance |
| `enbl` | enabled (`0`/`1`) |
| `favo` | favorite flag |
| `hasb` | bool |
| `hrns` | **harness** dict (8 keys — routing/DSP wiring, the device analogue of helixgen's `raw.harness`) |
| `id__` | block instance id |
| `mdls` | **models list** (usually length 1 — the block's model instance) |
| `snap` | bool (per-snapshot presence) |
| `tid_` | topology/type id |
| `type` | **block category int** (e.g. `8` = Reverb — matches the §8 category ids) |

**`block['mdls'][0]`** is the **model instance**:

| Key | Meaning |
|-----|---------|
| `cid_` | content id |
| `enbl` | enabled |
| `id__` | **the numeric model id** — resolves via the bundled defs: `defs.model_name_for(id__)`. Examples: `769` → `P35_InputInst1_2`, `310` → `HD2_DistScream808Mono`, `387` → `HD2_DistBallisticFuzzMono`. |
| `lbid` | label/bank id |
| `parm` | **param list** (see below) |
| `snap` | bool |
| `tid_` | topology id |
| `vers` | model version |

**Each entry in `mdls[0]['parm']`** is one parameter:

| Key | Meaning |
|-----|---------|
| `accs` | access/flags |
| `cid_` | content id |
| `mid_` | model id (echoes the parent's `id__`) |
| `pid_` | **param id** — resolves via `defs.param_meta(model_id, name)` / the model-params table. |
| `snap` | per-snapshot marker |
| `tid_` | topology id |
| `valu` | **the value — a normalized float** |

Worked example: for model `310` (`HD2_DistScream808Mono`), a `parm` with
`pid_ = 1, valu = 0.18` — the defs say `pid 1 = "Gain"` (also `pid 2 = "Tone"`,
`pid 3 = "Level"`). So that block is a Screamer 808 with Gain ≈ 0.18.

**Key takeaway — same semantic model, two encodings.** The device edit buffer
and helixgen's `.hsp` both model a preset as **blocks → (a model + named
params)**. They differ only in how a model and a param are named:

| Concept | `.hsp` (helixgen) | Device `_sbepgsm` |
|---------|-------------------|-------------------|
| Model | model-id **string** (e.g. `HD2_DistScream808Mono`) | numeric `id__` |
| Param | param **name** (e.g. `Gain`) | numeric `pid_` |
| Value | normalized float | normalized float `valu` (same scale) |

The **bundled modeldefs (`defs.py`, §8) is the translation table** in both
directions: `defs.model_id_for(name)` / `defs.model_name_for(id__)` and
`defs.param_meta(model_id, name)` ↔ `pid_`.

> **Caveat — apply helixgen's model-id translation first.** helixgen renames a
> handful of model ids on ingest (e.g. `HD2_DrvScream808` ↔ the device's
> `HD2_DistScream808Mono`; see the project's ingest translation table). Convert
> a helixgen model-id string back to its **device-native** name **before**
> calling `defs.model_id_for`, or the lookup will miss.

The remaining open items at this level (exact `hrns` sub-fields, controller
`srcs`/`trgs` wiring) are tracked in §9.

### Leading 4-byte length prefix (some blobs)

Some blobs carry a **leading 4-byte big-endian length** *before* the msgpack
(count of msgpack bytes). A robust decoder tries both: attempt to msgpack-decode
from offset 0; if that fails, skip 4 bytes and retry. The `_sbepgsm` variant has
its 8-byte magic first; container-listing blobs are frequently a bare msgpack
array with no prefix.

### Relationship to `.hsp` (helixgen's on-disk format)

The two formats differ in **encoding** but share the **same semantic model**
(blocks → a model + named params). `.hsp` is 8-byte magic `rpshnosj` + JSON with
model-id **strings** and param **names**; `_sbepgsm` is 8-byte magic + msgpack
with numeric model ids (`id__`) and param ids (`pid_`). The **bundled modeldefs
(`defs.py`, §8) is the translation table** between them — see the block/param
layout subsection above for the field-level mapping. A complete converter still
has open leaf-level items (`hrns`, controller wiring); tracked in §9.

---

## 5. Content model

All browsable content — presets, setlists, IRs, and the block/model catalog —
is **"content" addressed by an integer CID** (content id). Content lives inside
**containers**; **setlists and other top-level groupings are virtual containers
at fixed negative CIDs**:

| Container CID | Meaning |
|---------------|---------|
| `-1` | FACTORY presets |
| `-2` | USER presets |
| `-5` | Throwaway setlist (scratch) |
| `-11` | User IRs |
| `-13` | Block categories (the model catalog surface) |

`/GetContentRef` on these roots returns friendly display names — e.g. "Factory
Presets", "User Presets", "User IRs".

### Item metadata map (dialect A keys)

Each content item is a msgpack map with (at least) these 4-char-code keys:

| Key | Meaning |
|-----|---------|
| `cid_` | the item's own **CID** (integer) |
| `name` | display name (string) |
| `cctp` | **content type**: `1000` = preset, `1001` = setlist, `1002` = template / IR |
| `posi` | **0-based slot index** within the container |
| `ccid` | **parent container CID** |
| `blck` | block count (number of DSP blocks in the preset) |

### `posi` → Helix bank/letter label

Presets on the device are labelled *bank + letter* (four presets per bank,
lettered A–D). Convert a 0-based `posi`:

```
bank_number = posi // 4 + 1      # 1-based bank
letter      = "ABCD"[posi % 4]   # A, B, C, or D
label       = f"{bank_number}{letter}"   # e.g. posi=5 -> "2B"
```

---

## 6. Command reference (port 2002 RPC)

All commands below are sent **from the DEALER to the device ROUTER on port
2002**. **Convention:** the **first argument is a client-chosen request id**
(any int32; a per-client counter is typical) that the device **echoes** in its
reply, so you can correlate response to request. Reads follow a `/XxxGet` →
`/getXxx` naming pattern. **Most writes reply with `/status`**
`[reqid, code, n]` where **`code == 0` means OK** (`n` is a count/detail field).

> **`/status` shape differs per command — read carefully.** The **majority** of
> writes use `[reqid, code, n]` (code in the **second** field). But
> **`/CreateContent` is the exception**: its `/status` is
> `[reqid, newCid, code]` — the **second** field is the **new CID** and the
> **third** field is the ok-code. When parsing a `/status`, key off which
> command you sent; do not assume field 2 is always the code.

Notation: typetags are shown OSC-style (`,iib` etc.). "msgpack[…]" = a `b` blob
whose payload is a msgpack array; "msgpack{…}" = a `b` blob whose payload is a
msgpack map.

### Content browsing / CRUD

| Op | Address | Typetags / args | Reply | Notes |
|----|---------|-----------------|-------|-------|
| **LIST** | `/GetContainerContents` | `(reqid:i, containerCID:i)` | `,ibi` → `[reqid, msgpack-array-of-item-maps, trailing:i]` | Lists a container's items (dialect A maps). **Large replies chunk the blob** across multiple frames — reassemble before msgpack-decoding. |
| **READ meta** | `/GetContentRef` | `(reqid:i, cid:i)` | item metadata map (dialect A) | Single item's metadata. On a root CID returns the container's friendly name. |
| **LOAD** | `/LoadPresetWithCID` | `(reqid:i, cid:i)` | `/status`; then device streams `/setEditBuffer` + `/setPropertyValue` on **2001** | Loads a preset into the edit buffer. Full content arrives on the PUB stream, not in the 2002 reply. |
| **CREATE (empty)** | `/CreateContent` | `(reqid:i, container:i, pos:i, ctype:i, msgpack{name:"…"})` | `/status [reqid, newCid, code]` | Creates a **new empty content entry** (e.g. an empty preset) in `container` at slot `pos`. `ctype = 2` observed for a **preset**. **Its `/status` is special:** field 2 is the **new CID**, field 3 is the ok-code (`0`=ok) — unlike every other write. This is the first step of "Save As New" (§7.1). |
| **CREATE (copy)** | `/AddContentsToContainer` | `(reqid:i, container:i, msgpack[srcCIDs], pos:i, 0:i, 0:i)` | `/status [reqid, code, n]` | Copies the listed source CIDs into `container` at slot `pos`. **The new CID is NOT in this `/status`** — re-list the container and match by `posi`/`name` to discover it. Trailing two ints observed as `0,0` (**partially decoded**). |
| **SAVE (persist buffer)** | `/SavePresetWithCID` | `(reqid:i, cid:i, 0:i, N:i)` | `/status [reqid, code, n]` | Persists the **current edit buffer** into an existing `cid`. The `0` third arg is fixed in captures. **`N` is an unknown 4th arg** — the editor sent `N=6` for a preset whose edit buffer had `bcnt=28` / 20 blocks, so **`N` is NOT the block count**; its meaning is unknown and **`N=0` works** (verified byte-faithful: after a `/SavePresetWithCID … 0` + reload, the `sfg_` and `pm__` sections are identical; only the volatile `hist`/`cg__` sections differ). |
| **RENAME / set attrs** | `/SetContentAttrs` | `(reqid:i, cid:i, msgpack{name:"…"})` | `/status [reqid, code, n]` | Sets item attributes; `{name:"…"}` renames. Also used to set the preset **colour** via a `colr` key (`{colr:…}`) as the 3rd step of "Save As New" (§7.1). Other attr keys **partially decoded**. |
| **DELETE** | `/RemoveContent` | `(reqid:i, container:i, msgpack[cids])` | `/status [reqid, code, n]` | Removes the listed CIDs from `container`. |
| **IR path lookup** | `/IrPathForHashGet` | `(reqid:i, blob16:b)` | `/xxxIrxPathForHash1 [reqid, path:s]` | IRs are referenced by a **16-byte hash**; this resolves a hash to its on-device path. Reply address is literally `/xxxIrxPathForHash1`; the path is device-side, e.g. `"/data/stadium-family-fw/ir/<name>.wav"` (IR files live under `/data/stadium-family-fw/ir/`). |

### Live edit-buffer manipulation

| Op | Address | Typetags / args | Reply | Notes |
|----|---------|-----------------|-------|-------|
| **PARAM SET** | `/ParamValueSet` | `,iiiiifi` → `[reqid, path, block, 0, paramId, floatValue, -1]` | edit-buffer update (echoed on 2001) | **Layout confirmed live.** Sets one parameter. `path` = signal-path/DSP index; `block` = block index within the path (a reverb-block change was captured as `path=0, block=6`); the `0` (4th) and trailing `-1` are fixed in captures. `paramId` is the **numeric** param id from the model defs (§8). `floatValue` is `f`; int/bool params are passed as their float encoding. |
| **MODEL SET** | `/ModelSet` | `,iiiii` → `[127, 0, 1, 0, modelId]` | edit-buffer update | Places/replaces a model. The leading ints in captures are literally `127, 0, 1, 0` (their exact roles — reqid? path? block? — are **partially decoded**); `modelId` is the **numeric** model id from the model defs (§8). |
| **SNAPSHOT NAME** | `/SetSnapshotName` | `,iis` → `[reqid, snapshotIndex, "Name"]` | `/status` | Renames snapshot `snapshotIndex` (0–7). |
| **EDIT BUFFER GET** | `/EditBufferStateGet` | `(reqid:i)` | `/getEditBufferState` → `[reqid, len:h, blob:b]` where blob = `_sbepgsm…` | Pulls the entire current edit buffer as the dialect-B blob (§4). `len` is an int64 (`h`) byte count. |

### Connect-time / info commands

| Op | Address | Args | Reply |
|----|---------|------|-------|
| Product info | `/ProductInfoGet` | `(reqid:i)` | `/getProductInfo` |
| Clone-lock state | `/getCloneLockState` | (see §7) | (state) |
| Property value | `/PropertyValueGet` | `(reqid:i, …)` | `/getPropertyValue` |

**UNVERIFIED / naming:** reply address names marked "-style" are inferred from
the `/XxxGet`→`/getXxx` convention and may differ in exact casing (the IR-path
reply `/xxxIrxPathForHash1` is confirmed exact). `/status` `code` values other
than `0` (error taxonomy) are not yet catalogued.

---

## 7. Connect-time sync sequence

When the editor attaches, it performs this handshake before it will let the user
interact. A fresh CRUD client does **not** need to replay all of it, but it is
the reference "known-good" bring-up:

1. Open the three ZMQ sockets (DEALER→2002, SUB→2001, SUB→2003); complete the
   ZMTP `NULL` handshake; SUB sockets subscribe to all topics.
2. `/ProductInfoGet` → `/getProductInfo` (model, firmware, capabilities).
3. `/EditBufferStateGet` → `/getEditBufferState` `[reqid, len, _sbepgsm blob]`
   (pull the current edit buffer).
4. `/getCloneLockState` (whether the device is busy/locked, e.g. mid-clone).
5. `/PropertyValueGet` → `/getPropertyValue` (assorted device properties).
6. `/IrPathForHashGet (reqid, blob16 hash)` for each IR hash referenced by the
   current buffer (resolve hashes → paths).
7. A sweep of `/GetContainerContents` + `/GetContentRef` over the root
   containers (§5) to populate the browser.

After this the device streams live updates on 2001/2003. To just list/CRUD, you
can skip straight to `/GetContainerContents` on the container you care about.

### 7.1 "Save Preset As → Save As New" write sequence (live-verified)

The editor's *Save Preset As → Save As New* action — the full **write path** for
persisting the current edit buffer to a new slot — is this exact ordered sequence
of four RPCs on 2002:

1. **`/CreateContent (reqid, container, pos, ctype=2, {name})`** — create the
   empty preset entry. **Grab the new CID from its special `/status [reqid,
   newCid, code]`** (§6).
2. **`/SavePresetWithCID (reqid, newCid, 0, N)`** — persist the current edit
   buffer into that CID. `N=0` works (§6).
3. **`/SetContentAttrs (reqid, newCid, {colr: …})`** — set the preset colour.
4. **`/LoadPresetWithCID (reqid, newCid)`** — load the freshly-saved preset back
   into the edit buffer.

To author over the wire: build the desired edit buffer (via `/ModelSet` +
`/ParamValueSet` on the current buffer), then run this sequence to persist it.

---

## 8. Bundled definition files (the "Rosetta Stone")

The numeric ids that the wire protocol uses (`modelId` in `/ModelSet`,
`paramId` in `/ParamValueSet`) are **not** on the wire in human-readable form —
they are defined in data files **bundled inside the editor app**, which you may
read directly to build name↔id maps:

```
/Users/michael.shea/Helix Stadium Debug.app/Contents/Resources/
```

| File | Format | What it gives you |
|------|--------|-------------------|
| `P35ModelCatalog.json` | JSON | Category → list of model **string ids** (the browsing/UI grouping). **No numeric ids.** |
| `modeldefs/p35md-1_3_0_0.bin` | JSON header + `\0` + `ldompgsm` magic + **msgpack** | **The main map:** model-string → `{numeric id, category, params:{name→{id,type,def,min,max}}}`. This is where the numeric `modelId` and `paramId` values come from. |
| `commanddefs/P35EditCommandDefs.json` | **two concatenated JSON objects** | Footswitch/command definitions (`/PresetSnapshot`, `/Looper`, `/MIDI-*`, …) with per-command param id/type/range. |
| `P35Controls.json` | JSON | Physical control (footswitch / expression / knob) definitions. |
| `P35ModelUIDefs.json` | JSON | UI layout / knob-rendering metadata per model. |
| `ModelMetadataStore.sqlite3` | SQLite | Editor's metadata cache (secondary). |
| `cab_mic.imb`, `main.imb` | binary bundles | Cab/mic + main asset bundles (out of scope). |

### `P35ModelCatalog.json` structure

```json
{ "categories": [
    { "id": 2, "name": "Amp", "shortName": "Amp", "color": "0xFF3C3C",
      "classes": [ {"name":"Guitar"}, {"name":"Bass"}, {"name":"Clone"} ],
      "models": [ "Agoura_AmpWhoWatt103", "Agoura_AmpUSTweedman", … ] },
    … ] }
```

- 20 categories. Category `id` is the block-category number (e.g. `2` Amp, `3`
  Preamp, `5` Cab, `6` Distortion, `7` Delay, `8` Reverb, `9` Modulation,
  `10` Dynamics, `11` EQ, `12` Pitch/Synth, `13` Wah/Filter, `14` Volume/Pan,
  `15` FX Loop, `16` Looper, `17` Input, `18` Output, `19` Split, `20` Merge;
  `0` None, `1` Favorites). Note the id sequence skips `4` and lists `16` before
  `15`.
- `models` is a list of **model string ids** (e.g. `Agoura_AmpBrit2203MV`,
  `HD2_AmpLine6Litigator`). Prefixes seen: `Agoura_`, `HD2_`, `HX2_`. These are
  the same names helixgen uses (e.g. `HD2_AmpBritPlexiBrt`).
- The catalog gives you **grouping and display**, not numeric ids — cross to the
  model defs for the number.

### `modeldefs/p35md-1_3_0_0.bin` structure (numeric id source)

Layout: a small JSON header `{"id":["0x00260000"],"ver":"0x13000000","pbn":0 }`,
a single `\0`, the 8-byte magic **`ldompgsm`** (reverse of `msgpmodl`), then a
**msgpack map** keyed by model-string id (**801 models** in the `1_3_0_0`
Stadium/`p35` build; 615 of them `HD2_*`), across **~7065 params** in total.
helixgen's `device/defs.py` extracts these into the `modelId` / `paramId`
name↔id maps (801 models / 7065 params). Each value:

```jsonc
{
  "id": 758,               // <-- numeric modelId for /ModelSet
  "category": "amp",
  "harness": 760,          // harness/DSP wiring id (partially decoded)
  "stereo": …, "usage": …, "cap_edge": …,   // capability flags
  "cablink": [ … ],        // linked default cab(s) for amp models
  "meters": { … },
  "params": {
    "Drive": { "id": 1,  "type": "f", "def": 0.4,  "min": 0, "max": 1 },
    "Bass":  { "id": 2,  "type": "f", "def": 0.58, "min": 0, "max": 1 },
    "Deep":  { "id": 5,  "type": "b", "def": true, "min": false, "max": true },
    "Level": { "id": 8,  "type": "f", "def": -10,  "min": -10, "max": 10 },
    "AmpCabZFIR": { "id": 101, "type": "i", "def": 0, "min": 0, "max": 1 }
    // …
  }
}
```

- **Param `type`** is one of `f` (float), `i` (int), or `b` (bool). Match this
  when encoding `/ParamValueSet` (all go on the wire as the `f` slot; encode
  int/bool into that float per type).
- **`params[name].id`** is the **`paramId`** for `/ParamValueSet`.
- Build the two maps you need directly from this file:
  - `modelId`  ← `modeldefs[model_string]["id"]`
  - `paramId`  ← `modeldefs[model_string]["params"][param_name]["id"]`
  - plus per-param `type`/`min`/`max`/`def` for validation.
- Other builds/models present in the same dir: `p35md-1_1…` / `1_2…` (older
  Stadium firmware — pick the one matching the device's reported version) and
  `p37md-*` / `c63*` (sibling models; **not** Stadium). Prefer the highest
  `p35md` version the device supports.

### `commanddefs/P35EditCommandDefs.json` structure (**two concatenated JSON
objects**)

This file is **not** a single JSON document — it is a **header object
immediately followed by a defs object**, concatenated with no separator. A
standard `json.load` reads only the first. Parse with a streaming/`raw_decode`
loop:

1. **Object 1 — header:** `{ "id": ["0x00260000"], "ver": "0x00000000" }`.
2. **Object 2 — command defs:** a map keyed by command/footswitch-function name.
   Keys seen: `Undefined`, `PresetSnapshot`, `PresetSnapshot-Instant`,
   `PresetSnapshot-Drum`, `Song`, `Song-Instant`, `Song-Drum`, `Looper`,
   `Looper-Instant`, `Looper-Drum`, `Utility…`, `ExtAmp…`, `MIDI-Instant`,
   `MIDI-FS`, `MIDI-Exp`, `MIDI-Drum`.

Each command def:

```jsonc
"PresetSnapshot": {
  "id": 1,
  "params": {
    "Action":   { "id": 0, "type": "i", "def": 0, "min": 0, "max": 3 },
    "Command":  { "id": 1, "type": "i", "def": 0, "min": 0, "max": 7 },
    "Setlist":  { "id": 2, "type": "i", "def": 0, "min": 0, "max": 1024 },
    "Preset":   { "id": 3, "type": "i", "def": 0, "min": 0, "max": 1024 },
    "Snapshot": { "id": 4, "type": "i", "def": 0, "min": 0, "max": 7 }
  }
}
```

Same `{id, type, def, min, max}` param schema as the model defs — this is how
footswitch/controller assignment commands are parameterised on the device.

---

## 9. Known-unknowns / TODO

- **Remaining `_sbepgsm` leaf fields.** The top-level (`cg__`/`hist`/`pm__`/
  `sfg_`) **and** the block/param layer (`blks` → block dict → `mdls[0]` →
  `parm` with numeric `id__`/`pid_`/`valu`, §4) are now decoded. Still open: the
  8-key **`hrns` harness** dict per block, and the controller-wiring maps in
  `cg__.entt` (`cmnd`/`ctm_`/`ctrl`/`sm_`/`snps`/`srcs`/`trgs`).
- **`_sbepgsm` ↔ `.hsp` converter.** The field-level mapping is known (blocks →
  model + named params; `defs.py` bridges numeric `id__`/`pid_` ↔ `.hsp`
  strings/names, applying helixgen's ingest model-id translation first). Writing
  the actual bidirectional converter — including the still-open `hrns` /
  controller leaves — remains the main lift for full round-trip.
- **`/SavePresetWithCID` 4th arg `N`.** Meaning unknown (not the block count;
  editor sent `6`, `0` works byte-faithfully). Harmless but uncharacterised.
- **`/ModelSet` leading args.** `127, 0, 1, 0` — which are reqid/path/block/flag
  is unconfirmed; verify before relying on it for placement. (`/ParamValueSet`'s
  `[reqid, path, block, 0, paramId, value, -1]` layout is now **confirmed live**.)
- **`/status` error codes.** Only `code == 0` (OK) is confirmed; the non-zero
  error taxonomy is uncatalogued.
- **2001 12-byte header field split** (§2) and **2003 framing** (assumed same as
  2002) are inferred, not verified.
- **`/CreateContent` `ctype`.** Only `ctype = 2` (preset) is observed; values
  for setlist/IR creation are unknown.
- **PIN / remote-access auth.** The device has a menu concept of remote-access /
  pairing (and a real `sshd` on 22 with publickey/password). Whether/how a PIN
  gates the ZMQ ports on some firmware, and how SSH keys are provisioned
  (`sshKeys/` bundle in the app), is unexplored — assume **no ZMQ auth** on the
  tested firmware.
- **`/AddContentsToContainer` trailing ints** (`0, 0`) and `/SetContentAttrs`
  attr keys beyond `name` are partially decoded.

---

## 10. Provenance and cross-references

- Reverse-engineered against a Helix Stadium **XL** using the **Helix Stadium
  Debug** editor build + a **Frida** socket-capture harness (`tools/` in this
  repo: `hook_sockets.js`, `frida_run.py`, `frida_spawn.py`, plus `tools/osc.py`
  for OSC encode/decode + msgpack blob handling). CRUD verified end to end
  (create → rename → read → delete, confirmed by re-listing).
- Numeric id maps read from the editor's bundled definition files (§8).
- Corroborated by a public community write-up of the same protocol.
- Related docs in this repo: root `PROTOCOL.md` (earlier short summary — this
  file supersedes it), `docs/superpowers/specs/2026-07-11-helix-device-v2-plan.md`
  (the helixgen `device …` integration plan), `docs/helix-format-reference.md`
  and `docs/ir-hash-algorithm.md` (the on-disk `.hsp` / IR-hash side, distinct
  from the wire format here).
