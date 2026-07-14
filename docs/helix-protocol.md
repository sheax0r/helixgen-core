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

#### Two content encodings — edit-buffer vs. stored preset

Preset content appears in **two encodings** that share the same msgpack body but
differ in magic and in one key:

| | Edit-buffer content | Stored preset content |
|--|--------------------|-----------------------|
| **Source / sink** | `/EditBufferStateGet` reply; `/setEditBuffer` stream | `/SetContentData` write; on-disk / imported presets |
| **8-byte magic** | `_sbepgsm` (= `msgpebs_` reversed) | `\xff\xff\xff\xff pgsm` (= `msgp` + `0xFFFFFFFF`, reversed — sibling of `_sbepgsm` / `ldompgsm`) |
| **Top-level keys** | `{cg__, hist, pm__, sfg_}` | `{cg__, pm__, sfg_}` — **same structure MINUS the volatile `hist`** |

**Converting one form to the other = swap the 8-byte magic and add/drop the
`hist` key.** To install an edit buffer as a stored preset (for
`/SetContentData`): strip the `_sbepgsm` magic, drop `hist` from the msgpack
map, re-encode, prepend the `\xff\xff\xff\xff pgsm` magic. To go the other way,
add `hist` back (any value; it is volatile) and swap to `_sbepgsm`. Everything
below about the `_sbepgsm` structure applies to both forms except the `hist`
row.

#### Preset content top-level structure (decoded)

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
| **CREATE (empty)** | `/CreateContent` | `(reqid:i, container:i, pos:i, ctype:i, msgpack{name:"…"})` | `/status [reqid, newCid, code]` | Creates a **new empty content entry** in `container` at slot `pos`. `ctype = 2` = a **preset**; **`ctype = 1003` under the setlists root `-5` creates a SETLIST** (live-verified 2026-07-14 — an item's `type` metadata field carries the ctype it was created with). **Its `/status` is special:** field 2 is the **new CID**, field 3 is the ok-code (`0`=ok) — unlike every other write. This is the first step of "Save As New" (§7.1), and the `helixgen device setlist create` path. |
| **CREATE (copy)** | `/AddContentsToContainer` | `(reqid:i, container:i, msgpack[srcCIDs], pos:i, 0:i, 0:i)` | `/status [reqid, code, n]` | Copies the listed source CIDs into `container` at slot `pos`. **The new CID is NOT in this `/status`** — re-list the container and match by `posi`/`name` to discover it. Trailing two ints observed as `0,0` (**partially decoded**). |
| **SAVE (persist buffer)** | `/SavePresetWithCID` | `(reqid:i, cid:i, 0:i, N:i)` | `/status [reqid, code, n]` | Persists the **current edit buffer** into an existing `cid`. The `0` third arg is fixed in captures. **`N` is an unknown 4th arg** — the editor sent `N=6` for a preset whose edit buffer had `bcnt=28` / 20 blocks, so **`N` is NOT the block count**; its meaning is unknown and **`N=0` works** (verified byte-faithful: after a `/SavePresetWithCID … 0` + reload, the `sfg_` and `pm__` sections are identical; only the volatile `hist`/`cg__` sections differ). |
| **WRITE content** | `/SetContentData` | `(reqid:i, cid:i, contentBlob:b)` | `/status [reqid, code, n]` | Writes preset **content** directly into an existing `cid`, replacing it. `contentBlob` is the **stored-preset** encoding (`\xff\xff\xff\xff pgsm` magic — see §4 "content encodings"), **not** the edit-buffer `_sbepgsm` form. This is how the editor installs an **imported** preset (it also sends `/SetContentAttrs` for name/colour and `/LoadPresetWithCID` after). Live-verified: used to restore a preset **byte-faithfully**. Combined with `/CreateContent` (§7.1) this is the full "author arbitrary content into a new slot" path. |
| **RENAME / set attrs** | `/SetContentAttrs` | `(reqid:i, cid:i, msgpack{name:"…"})` | `/status [reqid, code, n]` | Sets item attributes; `{name:"…"}` renames (works on presets, **setlists**, and **IRs** alike). The preset **colour** is `{colr: <int>}` — an **int enum index** (a string is accepted with status 0 but silently coerced to 0; live-verified 2026-07-14). Once non-default, `colr` appears in the item's container-listing/`GetContentRef` map (that is the read path). Preset **notes** are NOT an attr — see the `pm__` note below. |
| **DELETE** | `/RemoveContent` | `(reqid:i, container:i, msgpack[cids])` | `/status [reqid, code, n]` | Removes the listed CIDs from `container`. Deleting a **setlist** cid from `-5` kills its references but never the pool presets they point at. Deleting an **IR** cid from `-11` unregisters it immediately, but its backing `ir/*.wav` lingers until a **lazy device GC** (minutes) — during that window `/IrPathForHashGet` still resolves, so an immediate re-import is skipped as "already present" (and a delete → quick re-import of the same IR can wedge: file + path index present, no `-11` entry). helixgen removes the file over SFTP as part of its IR delete to close the window. |
| **IR path lookup** | `/IrPathForHashGet` | `(reqid:i, blob16:b)` | `/xxxIrxPathForHash1 [reqid, path:s]` | IRs are referenced by a **16-byte hash**; this resolves a hash to its on-device path. Reply address is literally `/xxxIrxPathForHash1`; the path is device-side, e.g. `"/data/stadium-family-fw/ir/<name>.wav"` (IR files live under `/data/stadium-family-fw/ir/`). |

### Live edit-buffer manipulation

| Op | Address | Typetags / args | Reply | Notes |
|----|---------|-----------------|-------|-------|
| **PARAM SET** | `/ParamValueSet` | `,iiiiifi` → `[reqid, path, block, 0, paramId, floatValue, -1]` | edit-buffer update (echoed on 2001) | **Layout confirmed live.** Sets one parameter. `path` = signal-path/DSP index; `block` = block index within the path (a reverb-block change was captured as `path=0, block=6`); the `0` (4th) and trailing `-1` are fixed in captures. `paramId` is the **numeric** param id from the model defs (§8). `floatValue` is `f`; int/bool params are passed as their float encoding. |
| **MODEL SET** | `/ModelSet` | `,iiiii` → `[cmd, dsp, block_id, subpos, modelId]` | edit-buffer update (echo `/setModelWithMID`) | Places/replaces a model. Args **decoded 2026-07-14** (e.g. `[117, 0, 4, 0, 70]` = cmd, dsp 0, block 4, subpos 0, MID 70); `modelId` is the **numeric** model id from the model defs (§8). A model swap **cascades**: `/setBlockEnable`, `/setBlockFavorite`, `/assignSnapshotBypass`, `/attachBlockBypassControllerWithBlob` (a `lrtcpgsm` blob), `/setControllerSource`+`/setSourceEnable`, and a batch of `/setPropertyValue` param defaults for the new model — replay these for a faithful live swap. |
| **SNAPSHOT NAME** | `/SetSnapshotName` | `,iis` → `[reqid, snapshotIndex, "Name"]` | `/status` | Renames snapshot `snapshotIndex` (0–7). |
| **EDIT BUFFER GET** | `/EditBufferStateGet` | `(reqid:i)` | `/getEditBufferState` → `[reqid, len:h, blob:b]` where blob = `_sbepgsm…` | Pulls the entire current edit buffer as the dialect-B blob (§4). `len` is an int64 (`h`) byte count. |

### Live device control (2026-07-14 parity capture)

Live-control commands pinned by the 2026-07-14 capture (full writeup:
`docs/superpowers/specs/2026-07-14-parity-capture-findings.md`). All on 2002;
`cmd` = the leading monotonic id; block addressing is `(dsp, block_id)`.

| Op | Address | Typetags / args | Notes |
|----|---------|-----------------|-------|
| **Recall snapshot (live)** | `/activateSnapshot` | `,ii` → `[cmd, snapshotIndex]` | Index **absolute, 0-based**. Followed by `/setBatchedParamVals` (the snapshot's param deltas). No atomic *copy*-snapshot opcode exists — the app duplicates via `/AddContentsToContainer` or a batch of property writes. |
| **Bypass/enable block (live)** | `/BlockEnableSet` | `,iiii` → `[cmd, dsp, block_id, enable]` | `enable` 0/1; echoed `/setBlockEnable` on 2001. |
| **Reorder container** | `/ReorderContainerContent` | `,iibi` → `[cmd, containerCID, msgpack[movedCIDs], newPos]` | Moves the listed CIDs to `newPos`. Works on both a **setlist's presets** and the **setlists** themselves (a setlist is a container under `-5`). `/updateContainerContent` returns the new order. |

`/LoadPresetWithCID` (above) is **load-by-CID** — the app's "make active" click
is this same command; there is no separate active-index (backlog #1 resolved).
`/ParamValueSet` and `/ModelSet` (above) are the other two live edit verbs.

### Global EQ properties (`dsp.globaleq.*`)

The three Global EQs (1/4"=`qtr`, XLR=`xlr`, Phones=`pho`) are **device
properties** written on the 2002 property channel — but with a **variant**
value, not a bare scalar:

```
/PropertyValueSet [cmd, 0, "lavppgsm"+msgpack{
    key_: "dsp.globaleq.<out>.<band>.<param>", type:"v",
    val_: { parm:<slot>, valu:<value> } }]
```

Bands `lowcut`(0) `lowshelf`(1) `low`(2) `mid`(3) `high`(4) `highshelf`(5)
`highcut`(6); param→slot `enable`=1 `freq`=2 `gain`=3 `q`=4 `slope`=5; output
level = key `dsp.globaleq.<out>.level` slot 3. A full-EQ snapshot is key
`globals.eq`. **Write-only over the network** — `/PropertyValueGet` returns an
empty blob for `dsp.globaleq.*` (the app reads EQ state from the connect-time
sync). Shipped as `device globaleq list|set` (+ MCP); codec
`src/helixgen/device/globaleq.py`, hardware-validated 2026-07-14.

### Telemetry: tuner & meters (2003 `/dspEvent`)

`/dspEvent` blobs are msgpack `{id__:{eid_,mid_}, vals:[…]}`. A **continuous
background pitch detector** streams on `{eid_:10, mid_:796}` as a **single float
= fractional MIDI note** (int = note, frac×100 = cents, `-1.0` = silence).
Grid **meters** stream on `{eid_:1, mid_:796/800}` as 128-float arrays. The
hardware tuner is engaged via FS12 (`volatile.press.taptempo` /
`volatile.held.taptempo`; exit `volatile.press.exittuner`) — but the pitch
stream is always live regardless, so a network tuner needs no engage.

### Command Center, MIDI/XY controllers

Decoded 2026-07-14 (see the findings spec §5–§7 for byte detail): Command Center
uses `/attachCommandWithType` (2-byte-length-prefixed framing) →
`/setCommandParamVal`; type families 1=Preset/Snapshot, 4=HotKey/Utility,
6=MIDI (MIDI subtype via param idx1: 0=PC,1=CC,3=Note,2=MMC). MIDI controller
assignment = `/attachParamController`/`/attachBlockBypassController` +
`/ControllerMIDISourceAdd` (CC# is a BE uint16 at blob offset 12; no channel on
the wire). XY zones activate via `/SetBatchedParamValues` (a 12-tuple
`[dsp,block,sub,paramId,valueF64]` batch = the block's whole param set; no
zone-index — the batch *is* the activation). Preset-side storage lives under
`cg__.entt` (`srcs`/`cmnd`/`trgs`, `ctrl`/`ctm_`).

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

This flow persists **whatever is currently in the edit buffer**. Build that
buffer first via `/ModelSet` + `/ParamValueSet` on the live buffer, then run the
sequence to persist it.

### 7.2 "Import Preset" — install arbitrary content into a CID (live-verified)

The editor's *Import Preset* installs a preset **blob** it already has (from a
file) directly into an existing CID via `/SetContentData` — it does **not** go
through the edit buffer, and it **overwrites the currently-selected preset's
CID** rather than auto-creating a new slot. Observed sequence on 2002:

1. **`/GetContainerContents (reqid, container)`** — locate the target CID.
2. **`/SetContentAttrs (reqid, cid, {name, colr, blck, flow})`** — set metadata
   (name, colour, block count, flow) to match the incoming preset.
3. **`/SetContentData (reqid, cid, storedBlob)`** — write the content. `storedBlob`
   is the **stored-preset encoding** (`\xff\xff\xff\xff pgsm` magic, no `hist`;
   §4 "content encodings").
4. **`/LoadPresetWithCID (reqid, cid)`** — load it into the edit buffer.
5. **`/IrPathForHashGet (reqid, blob16 hash)`** for each IR the preset references.

Used live to **restore a preset byte-faithfully**.

### 7.3 Full authoring path (create a new slot + install content)

Combine the two: `/CreateContent` mints a fresh CID, then `/SetContentData`
installs arbitrary content into it (no edit-buffer round-trip required):

```
/CreateContent (reqid, container, pos, ctype=2, {name})   -> new cid (from /status field 2)
/SetContentData (reqid, cid, storedBlob)                  -> install stored-preset content
/SetContentAttrs (reqid, cid, {name, colr, blck, flow})   -> metadata (optional; may precede data)
/LoadPresetWithCID (reqid, cid)                            -> make it live
```

`storedBlob` is produced from an edit buffer (or a helixgen-built preset) by the
magic-swap + drop-`hist` conversion in §4. This is the recommended path for
authoring a **new** preset from scratch without clobbering an existing slot.

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
  `parm` with numeric `id__`/`pid_`/`valu`, §4) are decoded. The `cg__.entt`
  controller-wiring maps are **now decoded 2026-07-14** for MIDI controllers and
  Command Center commands (`srcs`/`cmnd`/`trgs`, `ctrl`/`ctm_`; see the parity
  findings spec). Still open: the 8-key **`hrns` harness** dict per block, and
  **XY-zone storage** — `/SetBatchedParamValues` activates a zone on the wire but
  the inactive zones do **not** appear in the saved `_sbepgsm` (storage location
  unresolved).
- **Global EQ network read-back.** `dsp.globaleq.*` and `globals.eq` answer
  `/PropertyValueGet` with an **empty blob** — writes work (`device globaleq`),
  reads don't. The app sources EQ state from the connect-time sync; whether a
  bulk read exposes it is unexplored.
- **`.hss` filled-slot payload.** The container format (24-byte header + gzip +
  tar of `manifest.json` + 128 `.N` slots) is decoded and **readable**, but the
  filled-slot `type` token and `.N` payload framing are inferred from an *empty*
  export — a non-empty `.hss` is needed for a byte-faithful writer.
- **Time signature** is a **Song** property carried over SFTP (port 22,
  encrypted), not OSC — programmatic set needs song-file RE.
- **`_sbepgsm` ↔ `.hsp` converter.** The field-level mapping is known (blocks →
  model + named params; `defs.py` bridges numeric `id__`/`pid_` ↔ `.hsp`
  strings/names, applying helixgen's ingest model-id translation first). Writing
  the actual bidirectional converter — including the still-open `hrns` /
  controller leaves — remains the main lift for full round-trip.
- **`/SavePresetWithCID` 4th arg `N`.** Meaning unknown (not the block count;
  editor sent `6`, `0` works byte-faithfully). Harmless but uncharacterised.
- ~~**`/ModelSet` leading args.**~~ **Decoded 2026-07-14**:
  `[cmd, dsp, block_id, subpos, modelId]` (see §6 "Live device control").
  (`/ParamValueSet`'s `[reqid, path, block, 0, paramId, value, -1]` layout is
  also confirmed live.)
- **`/status` error codes.** Only `code == 0` (OK) is confirmed; the non-zero
  error taxonomy is uncatalogued.
- **2001 12-byte header field split** (§2) and **2003 framing** (assumed same as
  2002) are inferred, not verified.
- **`/CreateContent` `ctype`.** `ctype = 2` (preset) and `ctype = 1003`
  (setlist, under root `-5`) are live-verified; the IR-creation value is
  unknown (IRs are created by the watched-dir import, not `/CreateContent`).
- **Preset notes** live as the `preset.meta.info` entry (`{key_, type:"s",
  val_}`) in the content blob's **`pm__` property list** — not as a content
  attr. Read/write = `/GetContentData` → edit the entry → `/SetContentData`
  (non-activating; live-verified 2026-07-14). The rest of `pm__` is
  per-preset properties (`preset.tempo.bpm`, `preset.inst1.z`,
  `preset.floorboard.stomp.*`, …), kept sorted by key.
- **`/GetContentInfo` (and the other `*ContentInfo` addresses in the app
  binary) are NOT device commands** — the device replies `Msg dispatch
  failed: /GetContentInfo is NOT known!!!`; they are app-internal. On-device
  metadata reads are `/GetContentRef` / `/GetContainerContents`.
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
