# Helix Stadium format & hardware reference (helixgen local notes)

Synthesized in our own words from Line 6's official Helix Stadium Owner's Manual
(manuals.line6.com/en/helix-stadium). Provenance URLs are cited per section so
the skill never needs to live-query Line 6. Numbers here are what the **device**
does; where helixgen deliberately narrows scope (v1 limitations) that is called
out. Line 6 / Helix / HX are Yamaha Guitar Group trademarks; this is descriptive
interop documentation, not copied text.

## Devices: Stadium vs Stadium XL

Two hardware variants share the same `.hsp` preset format.

- **Helix Stadium (non-XL):** no onboard expression pedal; supports up to two
  *external* expression pedals via the Control A/B jacks.
- **Helix Stadium XL:** adds an **onboard expression pedal with a hidden toe
  switch** (push the pedal fully forward, firmly, to click it). All EXP-pedal /
  toe-switch behavior below that references the onboard pedal is **XL-only**.

Both variants expose the same footswitch panel (12 switches — see below).
Source: https://manuals.line6.com/en/helix-stadium/live/top-panel-and-footswitches

## Footswitches — 12 total, FS1–FS12, two RESERVED

The panel has **12 footswitches labeled FS1 through FS12** (two rows of six),
each with its own OLED scribble strip and capacitive touch.

- **FS6 = MODE** (toggles Stomp A / Stomp B and other footswitch modes) —
  **RESERVED, not user-assignable to a block.**
- **FS12 = TAP / Tuner** (tap tempo; hold for tuner) — **RESERVED.**
- **Assignable to blocks: FS1–FS5 and FS7–FS11 — ten switches.** Note the gap:
  the ten assignable switches are NOT FS1–FS10; they skip FS6 and include FS11.

Footswitch modes: Stomp A, Stomp B, Preset, Snapshot, Combo, Transport
(selected/cycled via the MODE switch).

> **helixgen mapping caveat:** helixgen's controller table (`controllers.py`)
> assigns source IDs from `0x01010100` up. These map to the ten *physical
> assignable* switches (FS1–FS5, FS7–FS11) in panel order — source index =
> FS# − 1. The logical names are therefore **FS1, FS2, FS3, FS4, FS5, FS7, FS8,
> FS9, FS10, FS11** — NOT "FS1..FS10". Source id `0x0101010a` is the 11th
> physical switch (FS11), corroborated by 211 real exports (FS11 appears 109×;
> the reserved FS6 = `0x01010105` appears 0×). Labeling any assignable slot "FS6"
> is wrong — FS6 is the reserved MODE switch and takes no block assignment.
Source: https://manuals.line6.com/en/helix-stadium/live/top-panel-and-footswitches

## Expression pedals & toe switch

- Two expression controllers: **EXP 1** and **EXP 2**.
- On the **XL**, the single onboard pedal controls either EXP 1 or EXP 2; the
  **toe switch toggles which one is active** (LED: violet = EXP 1, teal = EXP 2).
  That toggle is the toe switch's **default** job, but it is **custom-assignable**
  to other functions (e.g. a block bypass).
- Auto-assign defaults: adding a **Wah** or **Pitch Wham** block auto-assigns it
  to **EXP 1**; a **Volume Pedal** block auto-assigns to **EXP 2**.
- **Wah engage:** helixgen binds a wah's bypass to the toe switch (source
  `0x01010500`, observed on ~all real wah exports) so pushing toe-down engages
  the wah while EXP 1 sweeps it. This is corpus-validated from real exports and
  matches the long-standing Helix wah workflow; the manual documents the toe
  switch as EXP1/EXP2 toggle-by-default + custom-assignable, which is consistent.
Sources: https://manuals.line6.com/en/helix-stadium/live/top-panel-and-footswitches ,
https://manuals.line6.com/en/helix-stadium/live/bypass-and-controller-assign

## Controllers & assignment model

Controllers that can drive a parameter or a block's bypass: Stomp footswitches,
EXP 1 / EXP 2, the EXP toe switch, external footswitches/pedals (Control A/B),
MIDI CC/notes, the XY touch controller, and **snapshots** (a parameter set to
"snapshot control" changes value per snapshot). Adding an effect block
auto-assigns it to the first free Stomp A (then Stomp B) switch.
Source: https://manuals.line6.com/en/helix-stadium/live/bypass-and-controller-assign

## Signal paths & DSP

- **Two primary paths: Path 1 and Path 2**, each running on its **own
  independent DSP**. (helixgen `dsp0` / `dsp1` = Path 1 / Path 2.)
- Each path can split into **two parallel lanes, A and B** — so the device
  addresses **Path 1A, 1B, 2A, 2B**. helixgen's `lane` coordinate = A/B.
- **Up to 12 block locations per path.** helixgen models 12 slots per path
  across the 2 paths = **24 addressable positions**; the device's A/B lane
  splits (which helixgen v1 does not author) hold additional blocks per lane.
- Stereo models cost roughly 2× the DSP of the mono version; amps/reverb/pitch
  ("Poly")/Clone are the most DSP-hungry — a path can run out of DSP before 12
  blocks. This is "Dynamic DSP".
- **helixgen v1 limitation:** one serial chain per path; parallel A/B splits
  inside a path are not authored (recipe `paths` = 1–2 entries → Path 1 / Path 2).
Sources: https://manuals.line6.com/en/helix-stadium/live/signal-path-routing ,
https://manuals.line6.com/en/helix-stadium/live/dynamic-dsp

## Snapshots — 8 per preset

- **Exactly 8 snapshots per preset.** A snapshot is a saved scene stored *inside*
  the preset.
- A snapshot captures/overrides: block **bypass** state (any block except the
  Looper), the values of any **snapshot-controlled parameters**, snapshot-enabled
  **Command Center** values, and (optionally) **system tempo**.
- The snapshot active when you **save** the preset is the one recalled on load.
  (helixgen writes `activesnapshot = 0`, so snapshot slot 0 must carry the
  intended on-load state.)
- helixgen recipe model: each snapshot is a delta from path base values;
  `disable: [...]` bypasses blocks, `params: {...}` overrides values. The recipe
  author path only *disables* in a snapshot, so layered presets place every
  needed block base-enabled and disable the complement. To toggle a block **on**
  in a specific snapshot after the fact, edit the `.hsp` with
  `helixgen enable <preset> <block> --snapshot <name>`. (The device itself
  captures per-snapshot bypass in both directions.)
Source: https://manuals.line6.com/en/helix-stadium/live/snapshots

## Block categories

Line 6's official categories (left) vs helixgen's internal taxonomy (right):

| Line 6 (manual)        | helixgen category |
|------------------------|-------------------|
| Distortion             | `drive`           |
| Dynamics               | `dynamics`        |
| EQ                     | `eq`              |
| Modulation             | `modulation`      |
| Delay                  | `delay`           |
| Reverb                 | `reverb`          |
| Pitch and Synth        | `pitch`           |
| Wah and Filter         | `filter`          |
| Volume and Pan         | `volume`          |
| Amp / Preamp           | `amp`             |
| Cab / Cab IR           | `cab`             |
| FX Loop / Send-Return  | `send`            |
| Clone (amp/pedal capture) | *(not modeled)* |
| Looper                 | *(not modeled)*   |

The mapping is a rename, not a factual conflict. Note **Clone** (Stadium's
NAM/capture-style block) and **Looper** have no helixgen category.
Source: https://manuals.line6.com/en/helix-stadium/live/effect-blocks

## Cab IRs (impulse responses)

- Cab blocks hold one or two Cabs / Cab IRs. User IRs are imported as `.wav` via
  the Stadium app's Librarian (Cab IR list); they can live in nested folders.
- **The device normalizes every imported IR** to **2048-sample, 48 kHz, 32-bit
  float, mono** regardless of the source WAV's rate/length/bit-depth/channels.
  (So the device itself accepts non-48 kHz sources — it resamples.)
- **helixgen identifies IRs by a content-derived hash** (32 hex chars) rather
  than filename or slot, reproduced bit-identically off-device. This hash scheme
  is **reverse-engineered and field-validated (loads on real hardware)** — Line 6
  does not publish it, so it is not verifiable against official docs, only
  empirically. helixgen's "48 kHz-only" input rule is a helixgen limitation (it
  doesn't resample), *not* a device limitation.
- The WAV must also be imported onto the device (Librarian → Cab IRs → Import)
  for a preset's hash to resolve; otherwise the block shows "No Model".
Source: https://manuals.line6.com/en/helix-stadium/live/cab-blocks

## Trails (delay / reverb / FX-loop spillover)

- Delay, Reverb, **and FX Loop** blocks have a **Trails** parameter. Trails On =
  the wet tail keeps ringing when the block is bypassed, and also spills over
  **across snapshot changes** within the same preset.
- There is **no** delay/reverb spillover across a **preset change** on Helix —
  trails only bridge bypass and snapshot switches inside one preset.
- helixgen models `trails` on delay/reverb blocks only and frames it primarily as
  block-bypass spillover; the snapshot-switch spillover use and FX-loop
  applicability are real but not modeled/documented by helixgen (minor).
Sources: https://manuals.line6.com/en/helix-stadium/live/snapshots ,
https://line6.com/support/topic/24631-delayverb-trails-and-snapshots/

## Loading presets

Presets are loaded/saved via Line 6's desktop app (HX Edit for legacy Helix; the
**Helix Stadium** app for Stadium) over USB — helixgen writes files only, it
never talks to hardware.
Source: https://manuals.line6.com/en/helix-stadium/live/helix-stadium-edit-application

## Not verifiable against official docs (reverse-engineered / empirical)

Line 6 does not publish these; helixgen derived them from real exports and they
are field-validated by loading on a real Stadium XL. Treat as empirical, not
manual-backed:
- Internal model IDs (`HD2_AmpBritPlexiBrt`, etc.) and the ingest/generate
  model-id translations (e.g. `HD2_DistScream808Mono` ↔ `HD2_DrvScream808`).
- Controller source-ID integers (`0x010101NN` FS bank, `0x010201NN` EXP bank,
  `0x01010500` toe switch).
- `.hsp` container layout (8-byte magic `rpshnosj` + compact JSON) and the
  `bNN` flow / `@enabled` / harness structure.
- The Cab IR content-hash algorithm.
