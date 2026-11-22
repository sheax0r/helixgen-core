# Matrix Mixer protocol — RE spike + `device matrix` verb design (`helixgen-81v.8`)

**Status:** RE spike — read-only. **H1 ruled out on hardware** (live catalog
glob, Stadium XL fw 1.4.1, 2026-11-22): the matrix per-channel plane is **not a
device property**. The remaining H2-vs-H3 split needs an app-drive capture, which
was **blocked** this session (computer-use grant for the Stadium app denied) — so
the per-channel wire opcode stays **capture-pending**. **No device-write code
ships from this spike** — the deliverable is this findings + design doc plus the
protocol section in `docs/helix-protocol.md`.
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

**To split H2 vs H3:** drive one matrix control in the app while running
`device watch` (2001). An echoed frame (any address) ⇒ **H2** and reads the
opcode; silence on 2001 ⇒ **H3**. This step was **blocked this session** — the
computer-use grant to control the Helix Stadium app was denied, so the app could
not be driven. `device watch` itself was verified working (idle device → only
`/dspEvent` telemetry, no property/edit-buffer frames).

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

## 6. Capture method (read-only) — how to finish the decode

A coordinated hardware window is needed (the Stadium XL is shared with the 81v.1
topology session — do not drive it concurrently). All steps below are read-only
on helixgen's side.

### 6.1 Live property-catalog glob (decisive for H1; app-independent)
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

### 6.2 Drive the app + `device watch` 2001 (reveals the opcode)
With the retail Stadium Edit app open, `helixgen device watch --filter
/setPropertyValue` (and unfiltered) while moving **one** matrix control at a time
(fader → pan → mute → solo → layer reset, per output). The subscriber decodes the
`lavppgsm` blob, so a `/setPropertyValue` echo reveals **H1**'s key+value; a
different echoed address reveals **H2**; silence on 2001 (write heard, no echo)
indicates **H3**.

### 6.3 Frida outbound capture (only if H3, env permitting)
The gold-standard outbound capture is `tools/hook_sockets.js` + `tools/re_capture.py`
against the **re-signed Helix Stadium Debug** app (`~/Helix Stadium Debug.app`)
over USB — the method that pinned `/doAgenda`. **Frida is not installed in this
spike's session**; if H3 holds, this is the follow-on epic's gating setup
(`pip install frida`, re-sign per `tools/resign-debug-app.sh`, hook, drive).

---

## 7. Recommended scope for the follow-on epic (`device matrix`)

1. **Run §6.1 + §6.2 in one coordinated read-only window** → resolve H1/H2/H3,
   capture one write per control type per layer + the reset.
2. **If H1/H2:** build `matrix.py` (offline codec, golden-blob tests) +
   `client.set_matrix` + `device matrix list/get/set/reset`, byte-exact to the
   app's writes — a ~`globaleq.py`-sized lift. Ship with the read-back caveat
   handled like `device globaleq`.
3. **If H3:** add Frida env, capture via §6.3, then same codec build.
4. **Read-back:** fold into the shared connect-time `globals.*` snapshot decode
   that Global EQ also wants (one decode serves both).
5. **Priority:** the UI parity doc marks the Matrix Mixer the **largest, newest,
   lowest-priority** of the device-global trio ("unless the owner uses multi-out
   routing"). The RE cost is one capture window + a globaleq-sized codec; worth it
   when multi-output monitor mixing is actually used.

**Honest status line:** the matrix's *adjacent* output/routing/MIDI-channel plane
is **decoded and shipping** (`device settings`); the per-channel mixer plane is
**not yet on the wire-decoded** — one coordinated read-only capture (§6.1+§6.2)
resolves it, and the verb build is small once the opcode is known.
