# Helix Stadium network protocol (reverse-engineered)

Status: **proof-of-concept, working**. Reverse-engineered 2026-07-11 against a
Helix Stadium **XL** (firmware with `OpenSSH_9.6`) on the LAN, using the macOS
editor's debug build + Frida socket capture. Cleartext throughout — no TLS.

## Transport

The editor talks to the hardware over **ZeroMQ (ZMTP 3.0)**, three TCP sockets:

| Port | Device socket | Role                         | Client socket |
|------|---------------|------------------------------|---------------|
| 2002 | `ROUTER`      | RPC command/response         | `DEALER`      |
| 2001 | `PUB`         | property-change notifications | `SUB`        |
| 2003 | `PUB`         | DSP telemetry (meters, dspEvent) | `SUB`     |

Port 22 is a normal `sshd` (the editor bundles `libssh2`; used for some bulk /
file transfer paths, not required for preset CRUD).

ZMTP `NULL` security mechanism — no auth. A `DEALER` may connect to `:2002`
and issue commands immediately; the device supports multiple concurrent clients
(the editor and this client ran simultaneously, each seeing the other's writes
via the PUB streams).

## Message encoding

Each ZMQ frame is one **OSC** message: `address\0`(pad4) + `,tags\0`(pad4) +
args. Arg types seen: `i` int32-BE, `f` float32-BE, `b` blob (int32-BE length +
bytes, pad4), `s` string. **Blob payloads are msgpack** — usually a raw msgpack
value; container-listing blobs are a raw msgpack array; a few carry a 4-byte
length prefix before the msgpack (decoder tries both).

Convention: reads are `/XxxGet` → reply `/getXxx`; the first arg of a request is
a client-chosen **request id**, echoed in the reply. Writes reply `/status
[reqid, code, n]` with `code == 0` on success.

## Content model

Presets/setlists/IRs are "content" addressed by an integer **CID**. They live in
**containers**; setlists are virtual containers at fixed negative slots:

| Container | Meaning         |
|-----------|-----------------|
| `-1`      | FACTORY presets |
| `-2`      | USER presets    |
| `-5`      | Throwaway setlist |
| `-11`     | User IRs        |
| `-13`     | Block categories |

Item maps carry: `cid_` (CID), `name`, `cctp` (content type: **1000 preset,
1001 setlist, 1002 template/IR**), `posi` (0-based slot; Helix label = `posi//4
+ 1` bank + `"ABCD"[posi%4]`), `ccid` (parent), `blck` (block count).

## RPC vocabulary (port 2002)

| Op | Command | Args | Reply |
|----|---------|------|-------|
| **LIST**   | `/GetContainerContents` | `(reqid, containerCID)` | array of item maps |
| **READ**   | `/GetContentRef` | `(reqid, cid)` | item metadata map |
| **LOAD**   | `/LoadPresetWithCID` | `(reqid, cid)` | streams state on 2001; `/status` |
| **CREATE** | `/AddContentsToContainer` | `(reqid, container, msgpack[srcCIDs], pos, 0, 0)` | `/status` |
| **RENAME/ATTRS** | `/SetContentAttrs` | `(reqid, cid, msgpack{name:...})` | `/status` |
| **DELETE** | `/RemoveContent` | `(reqid, container, msgpack[cids])` | `/status` |
| **PARAM SET** | `/ParamValueSet` | `(reqid, dsp, block, ?, param, float, -1)` | (edit-buffer) |
| IR lookup | `/IrPathForHashGet` | `(reqid, blob16 hash)` | `/xxxIrxPathForHash1` |

Connect-time sync the editor performs (informational): `/ProductInfoGet`,
`/EditBufferStateGet`, `/getCloneLockState`, then a sweep of
`/GetContainerContents` + `/GetContentRef` + `/PropertyValueGet` over the roots.
A fresh client does **not** need to replay these to issue CRUD.

### CREATE returns the new CID via re-listing

`/AddContentsToContainer`'s `/status` does not carry the new CID. Re-list the
container and match by `posi` (or name) to obtain it. (`crud_demo.py` does this.)

## Notes / open questions

- `/ParamValueSet` address ints (dsp, block, ?, param, ?) are only partially
  decoded — enough to prove UPDATE; a full param map needs a diff sweep.
- Full preset *content* read (all blocks/params, not just metadata) arrives as a
  `/setEditBuffer` (~8 KB) + `/setPropertyValue` stream on 2001 after a LOAD, and
  likely also via an export path over SSH. Not needed for list/CRUD.
- Reqid is a per-client counter; values are arbitrary and only used for
  correlation.

## Files

- `helix_net.py` — reusable `HelixClient` (connect, list, read, load, create,
  rename, delete).
- `helix_probe.py` — standalone "list presets" POC.
- `crud_demo.py` — end-to-end create→rename→read→delete, verified by re-listing.
- `tools/osc.py` — OSC encode/decode + msgpack blob handling.
- `tools/hook_sockets.js`, `frida_run.py`, `frida_spawn.py` — the Frida capture
  harness used for discovery (attach or spawn the editor, dump device traffic).
