# Global settings protocol — RE findings (2026-07-13 capture)

Captured via `tools/frida_run.py` (Frida on the running Helix Stadium app,
v1.3.2.9805) while changing the tempo, cross-checked by driving each command
directly against the device from `HelixClient`. Hardware-confirmed on Stadium XL
(`p35x1.local.`, 192.168.4.84).

## The property system

Global settings — and many live values — are exposed as **properties** in a
dotted namespace (`global.*`, `dsp.globaleq.*`, `preset.*`, `volatile.*`).
251 `global.*` keys enumerated from the app binary (see
`scratchpad/inventory/bundle-functions.md`). Every value is read/written over
the 2002 RPC channel.

**Dispatch is by address + OSC typetag signature.** The device registers a
handler per `(address, typetag)` pair — so the *same* address with the wrong arg
types returns `Msg dispatch failed: /X is NOT known!!!`. This is why earlier
guesses (`/PropertyValueGet [id:i]`, `/PropertyValueSet [id:i, val:f]`) were all
rejected: the verb name was right, the signature was wrong.

### Value blob format (`lavppgsm`)

A property value travels as a **blob**: 8-byte magic `lavppgsm` followed by a
msgpack map. **The 4-char field names are encoded as msgpack `uint32`** (their
ASCII big-endian value), NOT as strings — the device's dialect. Decode with
`msgpack.unpackb(blob[8:], raw=False, strict_map_key=False)` and map the int
keys back:

| uint32 key | ASCII | meaning |
|---|---|---|
| `1801812319` | `key_` | property key string |
| `1954115685` | `type` | `'f'` (float) or `'i'` (int) |
| `1986096223` | `val_` | the value (float64 or int) |

To **build** a value blob byte-for-byte like the app:
```python
import msgpack, struct
u32 = lambda s: struct.unpack(">I", s.encode())[0]
blob = b"lavppgsm" + msgpack.packb({u32("key_"): key, u32("type"): "f", u32("val_"): 132.0})
```

### Commands (all on 2002; `HelixClient._rpc` prepends the reqid)

| Purpose | Request | Reply | Notes |
|---|---|---|---|
| **Read current value** | `/PropertyValueGet [reqid:i, key:s]` | `/getPropertyValue [reqid, key, valueblob:b]` | `val_` in the blob is the **live** value |
| **Read definition** | `/PropertyDefWithKeyGet [reqid:i, key:s]` (or `/PropertyDefByIDGet [reqid, id:i]`) | `/keyPropertyDefinition [reqid, key, defblob:b]` | def blob carries `name`, `type`, `vmin`, `vmax`, `dval` (default), `vnme` (enum labels), `unts` |
| **Write value** | `/PropertyValueSet [reqid:i, ctx:i=0, valueblob:b]` | `/success [reqid, 0]` | also broadcasts `/setPropertyValue [65535,-1, blob]` on 2001 (live key, e.g. `volatile.tempo.bpm`) |
| **Key → numeric id** | `/IDForPropertyKeyGet [reqid:i, key:s]` | `/getIDForPropertyKey [reqid, key, id:i, 0]` | id only needed for `/PropertyDefByIDGet`; get/set/def-by-key work off the string key |

**Reply correlation:** the reply's first int is the echoed reqid (matches
`_rpc`). Success is `/success` (NOT `/status`) — the write reply is
`/success [reqid, 0]`, `0` = OK.

### Definition blob fields (`fedppgsm` magic, uint32 keys)

| uint32 | ASCII | meaning |
|---|---|---|
| `1851878757` | `name` | display name (may contain `\n`) |
| `1936224884` | `shrt` | short name |
| `1954115685` | `type` | `0`/`1`… a UI-type enum (separate from the value `type`) |
| `1685479788` | `dval` | default-value sub-map `{key_, type, val_}` |
| `1986879864` | `vmax` | max |
| `1986881902` | `vmin` | min |
| `1986948453` | `vnme` | enum value labels, e.g. `['Needle','Strobe']` (empty for continuous) |
| `1970173043` | `unts` | units enum |
| `1768185695` | `id__` | numeric property id |

The def read gives the whole catalog **live from the device** — no hardcoded
key table needed (though a curated page→key grouping is still useful for UX).
`dval.val_` is the **default**, not the current value — use `/PropertyValueGet`
for current.

## Worked example (hardware-validated round trip)

```python
# set preset.tempo.bpm to 120.0
c._rpc("/PropertyValueSet", [("i", 0), ("b", value_blob("preset.tempo.bpm","f",120.0))])
#   -> [('/success', [<reqid>, 0])]  ; app UI updated 132.00 -> 120.00
# read it back
c._rpc("/PropertyValueGet", [("s", "preset.tempo.bpm")])
#   -> [('/getPropertyValue', [<reqid>, 'preset.tempo.bpm', b'lavppgsm...val_=120.0'])]
```

## Implementation notes for the settings module

- Value `type` in the blob is `'f'` or `'i'`; pack `val_` as float64 for `'f'`,
  as int for `'i'`. Enum properties are `'i'` with `vnme` labels.
- Clamp/validate writes against `vmin`/`vmax` (and `vnme` length for enums)
  from the def before sending.
- `ctx` (2nd int of `/PropertyValueSet`) was `0` for global/preset scalar
  settings; keep `0` until a case needs otherwise.
- Changing the header tempo hit `preset.tempo.bpm` (this preset's Tempo Select =
  Per Preset) and echoed as `volatile.tempo.bpm` on 2001 — the `global.tempo.*`
  keys are the same shape.
