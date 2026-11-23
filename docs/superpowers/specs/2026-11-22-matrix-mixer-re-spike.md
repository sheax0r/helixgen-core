# Matrix Mixer protocol — RE spike + `device matrix` verb design (`helixgen-81v.8`)

> **SUPERSEDED (2026-11-23) for the READ side.** This spike concluded the matrix
> is "not a device property" and won't-do — that was WRONG. The whole monitor mix
> *is* a readable property, **`server.mixer.state`** (one `server.*` blob; the H1
> glob below probed per-strip dotted keys and missed it). It is now decoded and
> rendered read-only (`src/helixgen/device/mixer.py`, device-live Mixer pane,
> `helix-protocol.md` §6). The **write** path is still unproven — that part of the
> won't-do stands until a hardware-panel capture or a scratch write+read-back. The
> rest of this doc (the verb-shape design, the capture methods, the write-side
> escape hatches) is kept for provenance.

**Status:** RE spike — driven on hardware (Stadium XL fw 1.4.1, 2026-11-22),
incl. a validated **Frida** capture of the Helix Stadium **Debug** app. Three
findings now **close the question**: **(1) H1 ruled out** — the matrix
per-channel plane is **not a device property** (live catalog glob, 457 defs, zero
matrix keys). **(2) The desktop editor has no global Matrix Mixer screen** —
retail *and* the re-signed Debug build expose only Home / Librarian / Command
Center / Global EQ / Song (+ the per-preset output-block "Matrix" *destination*);
no monitor-mix faders anywhere (nav views + output endpoint + Window/Devices
menus all checked). **(3) The capture pipeline works** — Frida on the Debug app
cleanly captured `/BlockEnableSet` + `/ParamValueSet` (SEND on 2002) with 2001
echoes + 2002 acks, so (2) is a genuine *absence*, not a missed capture.
**Conclusion: the Matrix Mixer is a hardware-front-panel feature with no
editor-protocol surface — `device matrix` is a WON'T-DO via the desktop protocol**
(only a future app build that adds the screen, or hardware-panel RE, could change
that). **No device-write code ships from this spike.**
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

**Three hypotheses for the wire form** — **H1 ruled out; H2/H3 moot** once the app
was found to have no matrix screen (below). Kept for the record:

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

**And the desktop app does not expose the global matrix — CONFIRMED with Frida.**
Walking all five top-nav views (Home, Librarian, Command Center, Global EQ, Song)
plus the output endpoint and the Window/Devices menus found **no** global
Matrix-Mixer screen, in **both** the retail app and the Frida-hookable **Debug**
build. The only "Matrix" in the desktop editor is the **per-preset Output block's
destination selector** ("Matrix 1/4", XLR, Phones" — route this path into the
matrix; Pan/Level are the preset's, written via `/setParamValue`) — **not** the
device-global monitor-mix faders. The manual describes the Matrix Mixer with
touchscreen gestures ("swipe", "tap-hold the Matrix title", "Knob 1~8"): it is the
**hardware front-panel** screen.

**This is a real absence, not a missed capture.** A Frida hook on the Debug app
(§6.3) cleanly captured a known app write — toggling a block bypass produced
`SEND 2002 /BlockEnableSet`, a `/ParamValueSet` for a param edit, plus the 2001
echo and 2002 ack — so the pipeline works; there simply is no matrix UI to drive.

**Conclusion:** the Matrix Mixer has **no editor-protocol surface**. H2 cannot be
confirmed because the app never emits a matrix opcode (there is no screen for it),
and H3's only capture tool (Frida) has nothing to hook. `device matrix` is a
**won't-do via the desktop protocol** — see §7 for the (narrow, deferred) escape
hatches.

## 4. `device matrix` verb design (reference only — blocked; see §3/§7)

> This design is **not actionable today** — §3 found no editor-protocol surface to
> drive the matrix, so there is no opcode to implement against. It is kept as the
> target shape **if** a future app build exposes the Matrix Mixer (§7 hatch 1).

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

### 6.3 Frida outbound capture — RUN on the Debug app; **validated, no matrix UI**
Attached Frida (`tools/frida_run.py` + `hook_sockets.js`) to the re-signed
**Helix Stadium Debug** app (`~/Helix Stadium Debug.app`, pid-visible, Frida
17.15.4) over USB/IPv6. **The hook works**: toggling a block's bypass in the app
captured `SEND 2002 /BlockEnableSet` (+ the 2001 `/setBlockEnable` echo and the
2002 `/status` ack); param edits captured `/ParamValueSet`. This is the same
method that pinned `/doAgenda`, now re-verified end to end. **But the Debug build
has the same five views as retail and no Matrix Mixer screen** — so there is
nothing to drive, and no matrix opcode exists to capture. (Confirmed the device
link is IPv6/USB — `Devices` menu shows `Helix Stadium XL (fd0a:…::1)` — which is
why a LAN `device watch` subscriber, §6.2, saw none of this app traffic.) The
preset edit buffer was left as found (bypass toggled off→on, nothing saved;
verified via a LAN `device blocks` read).

---

## 7. Recommended scope for the follow-on epic (`device matrix`)

This spike **resolved the question**: every path to a `device matrix` over the
editor protocol is closed. The feasibility gate (does any Stadium build drive the
matrix?) was **run and failed** — retail and the Frida-hookable Debug build both
lack the screen, and the Frida capture is proven working (`/BlockEnableSet` +
`/ParamValueSet` captured), so this is a real absence, not a tooling gap.

**Recommendation: mark `device matrix` WON'T-DO via the desktop-editor protocol**,
and record the escape hatches so a future session doesn't re-run this spike:

1. **A future app build** that adds a Matrix Mixer screen → re-run the Frida
   capture (`tools/frida_run.py` + `hook_sockets.js` on the Debug app) to decode
   the opcode, then build `matrix.py` + `device matrix`. Nothing to do until then.
2. **Hardware-panel RE** — capture the device's own front-panel Matrix operation.
   Not host-scriptable (no app in the loop), needs on-device instrumentation;
   likely not worth it, and the matrix may not use an OSC opcode internally at all.
3. **Do NOT** re-attempt a property codec (H1 is out) or a LAN `device watch`
   capture (app traffic is USB, invisible to a LAN subscriber).

The UI parity doc already marks the Matrix Mixer the lowest-priority of the
device-global trio; this spike turns "lowest-priority, larger lift" into
"no viable editor-protocol capture path — defer indefinitely." The Device pane's
Matrix tab should ship as **"not supported — set it on the hardware"** rather than
gating on an engine epic that cannot be built today.

**Honest status line:** the matrix's *adjacent* output/routing/MIDI-channel plane
is **decoded and shipping** (`device settings`). The per-channel mixer plane is
**not a device property** (H1 ruled out) and **has no desktop-editor surface**
(retail + Debug both checked, Frida capture validated) — so it is **unreachable
via the editor protocol** (won't-do), barring a future app build.
