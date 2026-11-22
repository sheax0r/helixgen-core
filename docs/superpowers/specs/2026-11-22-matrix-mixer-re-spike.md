# Matrix Mixer protocol — RE spike + `device matrix` verb design (`helixgen-81v.8`)

**Status:** RE spike — read-only, driven on hardware (Stadium XL fw 1.4.1,
2026-11-22). Two findings close most of the question: **(1) H1 ruled out** — the
matrix per-channel plane is **not a device property** (live catalog glob, 457
defs, zero matrix keys). **(2) The app-capture-over-LAN path is a dead end** —
driving the desktop Stadium app for ~5 min produced **zero** LAN 2001 frames (all
11,680 were port-2003 telemetry): app↔device is USB and not mirrored on the LAN
PUB, and the desktop app does not appear to expose the global Matrix Mixer at all
(only the per-preset output-block "Matrix" destination). So the per-channel wire
opcode stays **undecoded**, and the realistic follow-on is Frida-on-app (iff a
build exposes it) or hardware-panel capture — or a won't-do. **No device-write
code ships from this spike.**
**Date:** 2026-11-22. **Firmware target:** Stadium XL, app v1.4 / fw 1.4.1.

Companion to the UI design (`sheax0r/helixgen` PR #94,
`2026-11-22-device-global-io-eq-mixer-ui-design.md`), whose Matrix tab (§7.3) is
explicitly **gated on this engine epic**. Mirrors the repo's `*-re-findings.md`
discipline: decoded facts are pinned to a capture or a bundled def; everything
else is labelled inference or capture-pending.

---

## 1. What the Matrix Mixer is (and is not)

The **Matrix Mixer** builds a **separate monitor mix per physical output** — one
layer each for **1/4"**, **XLR**, and **Phones**
([manual](https://manuals.line6.com/en/helix-stadium/live/matrix-mixer)). It is a
**device-global, live** surface: it mixes **non-preset** sources and **does not
save with the preset** and **does not round-trip through `.hsp`**.

Each layer's channel strips (manual order):

| Strip | Count | Notes |
|---|---|---|
| Home paths | 4 stereo | 1A, 1B, 2A, 2B (+ a "Home" summary strip) |
| Song tracks | 8 stereo | named, editable labels (+ a "Song" summary strip) |
| Click | 1 | |
| Count In | 1 | "reserved for future functionality" |
| USB In 1/2 | 1 stereo | DAW monitoring |
| Bluetooth | 1 | greyed unless a BT device is paired |
| Aux | 1 | greyed unless Return 1/2 (XL: 3/4) Type = Aux In |

Per channel: **Level** (fader / Knob 1–8, default **0.0 dB**), **Pan** (stereo
balance, default **Center**), **Mute**, **Solo** (solo-bus; tap-hold a solo to
clear all). Metering is **pre-fader**. **No phase/polarity control exists.** Each
layer also has a **Global EQ** (already shipped — `device globaleq`) and a
**Reset** (tap-hold the "Matrix" title → that layer's Level 0.0 dB / Pan Center /
Mute+Solo off; an opt-in checkbox also resets main output volume to 0.0 dB).
Outputs can be **linked** to share one layer.

### Not to be confused with the preset "Output block"

The signal-flow editor's **Output block** (`P35_OutputMatrix`, model 783, in
`transcode.py`; owned by 81v.1) is the **per-preset** gain/pan of the signal
*leaving the chain* — it saves with the preset and lives in `.hsp`. The **Matrix
Mixer** is the **device-global** monitor mix of everything hitting a physical
output. Same word "matrix", unrelated surfaces, **separate write paths** — the
`device matrix` verb must never touch preset content, and `set-param output …`
must never touch the matrix.

---

## 2. Engine readiness — what is already decoded

The per-channel mixer plane is **unmodeled** (confirmed below). But the
**adjacent output/routing plane** is already a decoded property surface and ships
today via `device settings` — these are real, pinned to the bundled catalog
(`src/helixgen/device/_settings_pages.json`) and the `global.*` property channel
(`settings.py`, `lavppgsm`/`fedppgsm` codec):

| Key | What it is |
|---|---|
| `global.out.level.qtr` / `.xlr` / `.spdif` | per-output **master** level (the Volume-knob targets; post-matrix output trim). **No `global.out.level.phones`** in the catalog — Phones level rides the Volume knob / Phones Global EQ level. |
| `global.out.spdif` / `global.out.spdif.type` / `global.out.samplerate.spdif` | S/PDIF out routing + format |
| `global.out.usb.12` | USB 1/2 out routing (manual options include *Direct Out / Matrix 1/4" / Matrix XLR / Matrix Phones*) |
| `global.out.groundlift.xlr` | XLR ground lift |
| `global.out.src.reamp.u7` / `.u8` (+ `.p37`) | reamp send source select |
| `global.midi.matrix.ch.qtr` / `.xlr` / `.phones` | the **MIDI channel** each output's Matrix layer listens on (the manual's "Matrix controls can be controlled remotely via MIDI CC"). |

These are the **output governors** around the mixer. They are not the per-channel
faders — but a `device matrix` surface should know they exist (routing *into* the
matrix, and the MIDI-CC remote path *out* of it).

## 3. What is NOT decoded — the per-channel plane

The per-channel **Level / Pan / Mute / Solo** for each of the ~16 strips × 3
output layers is **absent from the bundled property defs**
(`_defs_data.json` carries zero `matrix` audio props; the only `*matrix*` keys are
the three `global.midi.matrix.ch.*` above). So the mixer plane is **not** a
modeled `global.*` / `dsp.*` property today, and HX Edit sources matrix state
from the connect-time sync, not a per-key read (same as Global EQ).

**Three hypotheses for the wire form** — **H1 ruled out on hardware**, H2/H3 open:

- **(H1) Undocumented property keys. — RULED OUT (HW, Stadium XL fw 1.4.1,
  2026-11-22).** A live `/MatchingPropertyDefinitionsGet` glob of the **device's
  own** catalog (not the bundle) returned **457 defs total** and **zero** matrix
  per-channel keys: `dsp.matrix*` → 0, `dsp.out*` → 0, `*.pan` → 0, `*.solo` → 0,
  `*.fader` → 0, `*.mute` → only a global `hal.audio.mute`, `*matrix*` → only the
  three `global.midi.matrix.ch.*`. So the per-channel Level/Pan/Mute/Solo is **not
  a device property** at all, and a Global-EQ-style `/PropertyValueSet` codec is
  **not** the path. (The only matrix-adjacent reads are routing/monitor selectors,
  §2.)
- **(H2) A dedicated opcode family.** Like `/doAgenda` is dedicated for topology,
  the matrix has its own verb (e.g. `/setMatrix…` / `/MatrixChannelSet`) with a
  `(layer, channel, param, value)` tuple or a msgpack op. Now the **leading**
  hypothesis, precisely because it is not a property.
- **(H3) Not LAN-observable.** The matrix is driven only over the app's own
  transport and the device does **not** echo it on the 2001 PUB stream. Then the
  only decode path is a **Frida outbound capture** on the app socket (§6.3).

**The `device watch` + drive-app plan is a DEAD END — HW-shown 2026-11-22.** With
the Stadium app driven for ~5 min (every nav view, block select, output-endpoint
open) while `device watch` ran, the LAN streams carried **11,680 frames, ALL on
port 2003 (telemetry); zero on 2001, zero property/edit-buffer frames.** The
desktop app ↔ device link is **USB**, and USB-originated edits are **not
broadcast on the device's LAN 2001 PUB stream** — so a LAN subscriber (what
`helixgen device watch` is) cannot see the app's writes at all. This is exactly
why the `/doAgenda` capture used **Frida on the app's socket** (`hook_sockets.js`,
hooking USB/IPv6 `send`/`recv`), not `device watch`. The 2001-echo note in
`helix-protocol.md §6` ("the desktop editor" echoes on 2001) holds for a
LAN-connected or hardware change, **not** a USB-app one.

**And the desktop app may not even expose the global matrix.** Walking all five
top-nav views (Home, Librarian, Command Center, Global EQ, Song) plus the output
endpoint found **no** global Matrix-Mixer screen. The only "Matrix" in the
desktop editor is the **per-preset Output block's destination selector** ("Matrix
1/4", XLR, Phones" — route this path into the matrix; Pan/Level are the preset's,
written via `/setParamValue` on the edit buffer) — **not** the device-global
monitor-mix faders. The manual describes the Matrix Mixer with touchscreen
gestures ("swipe", "tap-hold the Matrix title", "Knob 1~8"), i.e. the **hardware
front-panel** screen. So it is plausible the Matrix Mixer has **no desktop-editor
surface at all**, only the hardware panel.

**Consequence:** H2 (dedicated opcode) can't be confirmed from the desktop app —
there may be nothing to drive, and even if there were, its writes wouldn't reach
a LAN subscriber. Two real paths remain for the follow-on epic (§7): Frida on the
app **iff** a build exposes the matrix, or capturing the **hardware panel**'s own
traffic. If neither surfaces an opcode, the matrix is **not reachable via the
editor protocol** — a won't-do, not a codec.

## 4. `device matrix` verb design (proposed — gated on §3 resolving)

Grounded in the manual's control set and the Global EQ verb shape. The exact
argument encoding follows from which hypothesis holds; the **surface** is the
same either way.

```
device matrix list                      # offline catalog: the 3 layers × their channels × params
device matrix get  [--output qtr|xlr|phones]       # current state (iff readable — see note)
device matrix set  <output> <channel> <param> <value>
device matrix reset <output>            # layer reset: Level 0.0 / Pan Center / Mute+Solo off
```

- `<output>` ∈ `qtr` / `xlr` / `phones` (same vocab as `device globaleq`).
- `<channel>` ∈ a fixed catalog: `home.1a home.1b home.2a home.2b` (+ `home`
  summary), `song.1 … song.8` (+ `song` summary), `click`, `countin`, `usb`,
  `bluetooth`, `aux`. (Exact wire indices are capture-derived.)
- `<param>` ∈ `level` (dB, default 0.0) / `pan` (balance, default center) /
  `mute` (bool) / `solo` (bool).
- **Codec** = a new `src/helixgen/device/matrix.py`, modelled on `globaleq.py`
  (offline, unit-tested against golden blobs from the capture), with the
  `HelixClient.set_matrix(...)` method in `client.py` — exactly the globaleq split.
- **`reset`** is a client-side convenience: write every channel's level→0 / pan→
  center / mute→off / solo→off for the layer (the app's title tap-hold). If a
  single device-side "reset layer" opcode is captured, use it instead.

**Read-back caveat (expected, pin on capture).** Global EQ and most `dsp.*`
properties are **write-only over the network** (`/PropertyValueGet` → empty blob;
state comes from the connect-time sync). The matrix is very likely the same, so
`device matrix get` may be **unavailable** (document it, like `device globaleq`
has no read). A reliable read then needs a connect-time `globals.*` bulk-snapshot
decode — a shared ask already noted for Global EQ (81v.6).

## 5. Won't-do (with reason)

- **Phase/polarity** — no such control exists on the Matrix Mixer (manual).
- **USB / Send / Digital as matrix *layers*** — only 1/4" / XLR / Phones are
  Matrix layers. USB/SPDIF are *destinations fed from* a chosen matrix
  (`global.out.usb.12` routing), not layers with their own faders. Not in scope.
- **MIDI-CC remote control of the matrix** — that is the hardware's own
  `global.midi.matrix.ch.*` path (CC in). `device matrix` writes the mix directly
  over OSC; it does not synthesize MIDI. The CC channels are readable/settable
  already via `device settings`.
- **Song-track matrix channels as first-class** — only meaningful with Song View
  (microSD-gated, 81v.12 won't-do). Surface the strips, but their audio is inert
  without a loaded song.
- **Persisting matrix state in `.hsp`** — device-global by nature; it must never
  save with the preset (would corrupt the `device to-hsp` model). Same decision as
  Global EQ.
- **Output *linking*** — a UI/ergonomics feature over the same per-layer writes
  (write both layers). Not a distinct engine primitive; defer to the UI.

## 6. Capture method — what was run, and what's left

Driven on hardware 2026-11-22 (Stadium XL fw 1.4.1). §6.1 and §6.2 were **run
this session** (read-only); §6.3 (Frida) was not (not installed).

### 6.1 Live property-catalog glob — RUN; **H1 ruled out**
`/MatchingPropertyDefinitionsGet [reqid, glob:s]` → `sfdppgsm {defs:[…]}`
enumerates the **device's** property defs (how `server.active.preset.id` and the
`global.*` namespace were found). It is a pure GET — no state change, app not
required. Reuse the shipped codec: send the command via `HelixClient._rpc(…,
raw_blobs=True)`, strip the 8-byte `sfdppgsm` magic, msgpack-decode to
`{_u32("defs"): [def-map, …]}`, and read each def-map with the same field keys as
`settings.decode_property_def` (`K_KEY/K_DVAL/K_VMIN/K_VMAX/K_VNME/…`). Globs to
try: `*matrix* *mix* *monitor* dsp.out* global.out* *.mute *.solo *.pan`. A hit
under `dsp.matrix.*` (or similar) with `vmin/vmax/default` confirms **H1** and
hands over the whole read-side. An empty result for the per-channel keys rules
out H1 and points at H2/H3.

### 6.2 Drive the app + `device watch` — RUN; **DEAD END**
Drove the retail Stadium app for ~5 min while `device watch` ran. Result:
**11,680 frames, all port-2003 telemetry; zero on 2001.** App↔device is USB and
not mirrored on the LAN PUB, so `device watch` (a LAN subscriber) never sees app
writes — this method cannot capture app traffic (see §3). Separately, no global
Matrix-Mixer screen was found in the desktop app (§3), so there may be nothing to
drive here regardless.

### 6.3 Frida outbound capture — the real remaining path (NOT run)
The only way to see app→device writes is a Frida hook on the app's socket:
`tools/hook_sockets.js` + `tools/re_capture.py` against the **re-signed Helix
Stadium Debug** app (`~/Helix Stadium Debug.app` is present) over USB/IPv6 — the
method that pinned `/doAgenda`. **Frida is not installed this session**
(`pip install frida`, re-sign per `tools/resign-debug-app.sh`, spawn via
`frida_spawn.py`, hook, drive). **But this only helps if a Stadium build exposes
the global Matrix Mixer as a drivable screen** — unconfirmed; the v1.4 desktop
app did not (§3). If no app build drives it, the remaining source is the
**hardware front panel** itself (not scriptable here), or the matrix has no
editor-protocol opcode at all.

---

## 7. Recommended scope for the follow-on epic (`device matrix`)

This spike **narrowed the epic sharply**: the easy paths (property codec, LAN
watch) are closed. Do these in order, and **stop early if a step fails** — each
is a go/no-go gate:

1. **Feasibility gate first (cheap).** Confirm whether *any* Stadium build (retail
   or Debug) exposes the global Matrix Mixer as a drivable screen. The v1.4
   desktop app did **not** (only the per-preset output block). If no build drives
   it, the only source is the hardware front panel, and `device matrix` is likely
   a **won't-do** (no editor-protocol opcode) — document and stop.
2. **If a build drives it:** `pip install frida`, re-sign the Debug app
   (`tools/resign-debug-app.sh`), spawn + hook (`tools/frida_spawn.py` +
   `hook_sockets.js`), and drive one matrix control at a time (fader/pan/mute/
   solo/reset per output). Decode the outbound opcode from the capture. (This is
   H2; there is no property path — H1 is out.)
3. **Then build** `matrix.py` (offline codec, golden-blob tests) +
   `client.set_matrix` + `device matrix list/get/set/reset`, byte-exact to the
   captured opcode. Read-back is likely write-only (like Global EQ) → fold into a
   shared connect-time `globals.*` snapshot decode.
4. **Priority:** the UI parity doc marks the Matrix Mixer the **largest, newest,
   lowest-priority** of the device-global trio ("unless the owner uses multi-out
   routing"). With the easy paths now ruled out, the cost is a Frida capture
   session **plus** the feasibility risk that it isn't editor-driveable at all —
   so this is the right one to defer until multi-output monitor mixing is needed.

**Honest status line:** the matrix's *adjacent* output/routing/MIDI-channel plane
is **decoded and shipping** (`device settings`). The per-channel mixer plane is
**not a device property** (H1 ruled out on HW) and is **not capturable via the
desktop app over LAN** (USB transport + no global-matrix screen found). Finishing
it needs a Frida capture of a build that exposes the matrix — or, if none does,
it is unreachable via the editor protocol (a won't-do).
