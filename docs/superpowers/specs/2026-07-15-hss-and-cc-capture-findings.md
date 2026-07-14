# Capture findings — non-empty `.hss` framing + footswitch MIDI command layouts

**Date:** 2026-07-15 · **Method:** owner-driven Helix Stadium **Debug** app
(2.x) driving on hardware (Stadium XL, `192.168.4.84`), decoded offline via
`helixgen.device.hss` / `helixgen.device.content`. All device reads
non-activating (`/GetContentData`); all writes confined to `ZZCAP-`-prefixed
presets + the expendable `Throwaway` setlist, **all deleted afterward**
(device verified clean: 0 `ZZCAP-` pool presets, `Throwaway` empty).

Two capture targets:

- **TARGET B** — a real **non-empty `.hss` setlist export** to pin the
  filled-slot framing (backlog **#31**).
- **TARGET D** — footswitch **MIDI CC / Note / MMC** command slot layouts, the
  outstanding **hypothesis** in the Command Center design (backlog **#16**,
  `2026-07-14-command-center-design.md` §3).

---

## TARGET B — `.hss` filled-slot framing: **reader assumption is WRONG (corrected here)**

### How it was captured

All device user setlists were empty and both `USER`/`FACTORY` setlist **Export**
is disabled in the app (only user-created setlists — `Throwaway`, `helixgen`,
`Sarah`, `Mike` — are exportable, and all four were empty). To produce a real
non-empty export the current edit buffer was **Save-As-New**'d (app UI) into
`Throwaway` slot `1A` as `ZZCAP-HSS`, then `Throwaway` was exported via
**Librarian → Setlists → Export** (macOS save panel) to
`parity_nonempty_export.hss`. The pool preset + its `Throwaway` reference were
deleted afterward.

Local artifact (both `captures/` and `tests/fixtures/hss/` are gitignored — not
committed): `captures/parity_nonempty_export.hss` (also copied to
`tests/fixtures/hss/parity_nonempty_export.hss`). Compare with the earlier
**empty** sample `captures/parity_throwaway_export.hss`.

### What matched the existing reader (unchanged, re-confirmed)

- 24-byte header (`GGGY`\0\0\0\0 `LTES`\0\0\0\0 `u64=256`), gzip @ 0x18, POSIX
  ustar tar of `manifest.json` + `.1`..`.128`.
- `manifest.meta` = `{device_id: 2490368 (0x260000), device_version:
  318899516 (0x1302053C), name: "Throwaway"}`.
- Empty slots: manifest `contents[i].type == "<null>"`, `.N` member = 1-byte
  `0x00`. 128 slots total.

### What the reader got WRONG (corrected by this capture)

1. **Filled-slot manifest `type` token** — the previously-unknown replacement
   for `"<null>"` is:

   ```json
   {"path": ".1", "type": "application/stadium-preset"}
   ```

2. **Filled `.N` payload is the `.hsp` preset format, NOT `_sbepgsm`.** The
   `.hss` embeds each preset as an **`rpshnosj` + JSON** document (the `.hsp`
   family), **not** the device's `_sbepgsm` MessagePack content blob:

   | Source | Bytes | Magic |
   |---|---|---|
   | `.hss` filled `.1` member | 18315 | `rpshnosj` + **pretty JSON** (`{\n  "…`) |
   | device `/GetContentData` of the same preset | 12894 | `_sbepgsm` + MessagePack |

   The embedded JSON parses as a full Stadium preset: top-level `preset` with
   `flow` (2), `params`, `snapshots` (8), **`commands`**, **`sources`**,
   `xyctrl`, `clip`. Same JSON schema helixgen's `.hsp` writer emits (the app
   uses **pretty** JSON; helixgen's Stadium `.hsp` uses compact — both are the
   same `rpshnosj`+JSON family).

   **Consequence for the code (do NOT fix in this PR — file only):**
   `helixgen.device.hss` module docstring + `HssSlot.blob` handling assume the
   filled payload is `_sbepgsm` / `\xff\xff\xff\xffpgsm` and route it through
   `content.decode_any` / `install_into_pool`. On a **real** export that path
   **raises** ("not a recognised content blob"). The **read side already parses
   the container correctly** (header/gzip/tar/manifest/slots), and now the
   filled-slot `type` + payload format are known, so the **byte-faithful writer
   (#31) is unblocked** — but the import/install path must be updated to treat a
   filled `.N` as an `.hsp` (`rpshnosj`+JSON) document (parse JSON → recipe /
   `hsp` ingest), not as `_sbepgsm`. Writer emits `rpshnosj`+JSON per slot and
   sets `type: "application/stadium-preset"`.

3. Minor: the exported file happened to be **10264 bytes** — coincidentally the
   same on-disk size as the empty sample (gzip luck: the pretty-JSON preset is
   highly compressible and the tar is 512-byte-block aligned). File size is not
   a reliable "empty vs filled" signal; the manifest `type` / member size is.

---

## TARGET D — footswitch MIDI command slot layouts (#16): **hypothesis was WRONG; corrected here**

Captured by authoring footswitch commands in the app's Command Center on an
expendable preset (`ZZCAP-CC`, cid 1205), saving (overwrite), and
non-activating-pulling the `cg__.entt.cmnd` records. Distinct, non-constant
values were used so each slot is identifiable, and each subtype was **isolated**
(single command in the preset) to remove cross-command ambiguity.

`cmnd` records are keyed to a physical source via `trig → srcs.id__`; the
source's `(type, ctxt)` classifies the command:

- **Footswitch** = `srcs.type==1, ctxt==1`
- **Instant** = `srcs.type==4, ctxt==0`
- **EXP / continuous** = `srcs.type==1, ctxt==0`

### Footswitch MIDI — ground truth (saved-blob, distinct values)

`pvl` = `[pvla … pvll]` (12 int slots). Values I set in the app in **bold**.

| subtype | `func` | pvl (index 0…11) |
|---|---|---|
| **CC** | **1** | `[0, 1, `**`5`**`(ch), -1, -1, -1, `**`45`**`(CC#), `**`0`**`(val), 0, 100, 1, 0]` |
| **Note On** | **2** | `[0, 2, `**`7`**`(ch), -1, -1, -1, 0, 0, `**`40`**`(note E2), `**`77`**`(vel), 1, 0]` |
| **MMC** | **3** | `[0, 3, `**`1`**`(ch, dflt), -1, -1, -1, 0, 0, 0, 100, 1, `**`5`**`(msg=Record)]` |

**Unified footswitch 12-slot layout:**

| slot | field |
|---|---|
| pvl0 | `0` (reserved; PC program for the Bank/Program subtype — not isolated here) |
| **pvl1** | **subtype** (`0`=Bank/Program, `1`=CC, `2`=Note, `3`=MMC — mirrors `func`) |
| **pvl2** | **MIDI channel** |
| pvl3 | Bank **MSB** (`-1`=Off) |
| pvl4 | Bank **LSB** (`-1`=Off) |
| pvl5 | `-1` (reserved) |
| **pvl6** | **CC number** (CC) |
| **pvl7** | **CC value** (CC) |
| **pvl8** | **Note number** (Note) |
| **pvl9** | **Note velocity** (Note); `100` default for CC/MMC |
| pvl10 | `1` (constant) |
| **pvl11** | **MMC message** (`0`=Play … `5`=Record … `7`=Punch Out) |

**`func` enum (footswitch device value = the app's subtype-tab index):**
`0`=Bank/Program, `1`=CC, `2`=Note On, `3`=MMC. NOTE this **differs** from the
native `.hsp` `Command` param ordering documented earlier
(`0`=PC/`1`=CC/`2`=MMC/`3`=Note): **Note and MMC are swapped** between the two
encodings. (The 2026-07-14 `command_center.md` wire-capture tentatively read
Note=sub3/MMC=sub2 from `/setCommandParamVal idx1`; the **saved-blob evidence
here (Note=func2, MMC=func3) supersedes that** — it is the actual stored value
with distinct, isolated data.)

`behv` was observed `0` for CC and `1` for the Note (Momentary NoteOff) — the
Action row (Press/Release/Hold/Toggle) and Note's Latching/Momentary toggle
likely live in `behv`/`togl`, only partially characterized.

### Contrast — the other two source classes (already known, re-confirmed)

- **Instant** PC (`func=0`, srctype 4): `pvl=[`**`0`**`(program), `**`4`**`(ch),
  -1(MSB), -1(LSB), -1, 0, 0, 0, 100, 1, 0, 0]`. **ch is at pvl1 — the Instant
  layout has NO subtype slot** (channel occupies pvl1). This is a **different
  layout from footswitch** (which reserves pvl1 for the subtype and shifts
  channel to pvl2).
- **EXP / continuous** CC (`func=1`, srctype 1 ctxt 0): 5 meaningful **float**
  slots `pvl=[`**`2`**`(ch), `**`11`**`(CC#), `**`0`**`(min), `**`127`**`(max),
  0, …]` — ch@pvl0, CC#@pvl1, min@pvl2, max@pvl3.

### Transcoder bug report (do NOT fix in this PR — filed for a follow-up)

`src/helixgen/device/transcode.py :: _command_payload` currently emits (for the
MIDI family, all sources):

```python
pvl = [0, ch, msb, lsb, -1, 0, 0, 0, 100, 1, 0, 0]
if   func == 0: pvl[0] = PC        # PC
elif func == 1: pvl[5] = CC#; pvl[6] = Value
elif func == 3: pvl[8] = Velocity; pvl[10] = Note; pvl[11] = NoteOff  # "Note"
elif func == 2: pvl[7] = Message                                       # "MMC"
```

This matches the **Instant** layout (ch@pvl1, no subtype slot) but is **wrong
for footswitch commands** on two axes:

1. **`func` mapping swapped for Note/MMC.** Device footswitch uses Note=`2`,
   MMC=`3`; the code uses Note=`3`, MMC=`2`. Any `.hsp` `Command` value must be
   mapped to the device `func` (Note↔MMC swap) for footswitch/Instant sources.
2. **Slot layout for footswitch.** Footswitch reserves **pvl1 = subtype** and
   shifts data +1, and Note/MMC land in different slots than the code assumes:

   | field | code slot | **real footswitch slot** |
   |---|---|---|
   | subtype | (none) | **pvl1** |
   | channel | pvl1 | **pvl2** |
   | Bank MSB / LSB | pvl2 / pvl3 | **pvl3 / pvl4** |
   | CC# / CC value | pvl5 / pvl6 | **pvl6 / pvl7** |
   | Note number | pvl10 | **pvl8** |
   | Note velocity | pvl8 | **pvl9** |
   | MMC message | pvl7 | **pvl11** |

   (The `-1` reserved moves pvl4→pvl5; pvl10 stays the constant `1`.) The
   **Instant** path (ch@pvl1) is byte-correct as-is — so a fix must branch on
   the source class (footswitch/`ctxt==1` vs Instant), not use one layout for
   both. This is HW-validated only for **byte survival**, not audible/functional
   MIDI output (same caveat as #16/#33).

---

## Raw decoded records (for reference)

Footswitch (isolated captures, `ZZCAP-CC` cid 1205, `srcs.type==1 ctxt==1`):

```
CC   func=1 locl=27  pvl=[0,1,5,-1,-1,-1,45,0,0,100,1,0]
Note func=2 locl=28  pvl=[0,2,7,-1,-1,-1,0,0,40,77,1,0]
MMC  func=3 locl=33  pvl=[0,3,1,-1,-1,-1,0,0,0,100,1,5]
Instant PC func=0 locl=0  pvl=[0,4,-1,-1,-1,0,0,0,100,1,0,0]   (srctype 4)
EXP CC     func=1 locl=43 pvl=[2,11,0,127,0,0.0,...]           (srctype 1 ctxt 0)
```

`.hss` filled slot (`Throwaway`/`ZZCAP-HSS`):

```
manifest.contents[0] = {"path": ".1", "type": "application/stadium-preset"}
.1 member            = 18315 bytes, magic "rpshnosj" + pretty JSON preset
device get_content   = 12894 bytes, magic "_sbepgsm" + MessagePack (DIFFERENT format)
```
