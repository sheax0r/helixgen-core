# Loudness feedback loop — measured volume normalization (2026-07-14)

Investigation + design spec for backlog **#62**. Question investigated: *can the
Helix give us signals that tell us how loud a tone actually is when played, so
normalization can be driven by measurement instead of heuristics?*

**Answer: yes.** The always-on port-2003 telemetry stream the tuner rides
carries **per-node signal-chain level metering** — including the instrument
input level and the output-stage levels **in the same burst** — and helixgen
already ships the transport + raw decoder (`device meters`,
`src/helixgen/device/meters.py`). What's missing is (a) characterizing which of
the 128 grid cells is which node, and (b) a measurement + closed-loop
normalization layer on top. Both are specified below.

## 1. Why a feedback loop at all

The tone skill's volume-normalization pass (SKILL.md §5.7) is *a priori*: anchor
the rhythm part's channel volume, then apply gain-compensation heuristics
("more gain reads louder"). That's the right static default, but it can't
predict the interaction of pickup output × drive stacking × amp compression ×
IR sensitivity — two presets "normalized" this way can still land 6 dB apart.
Real normalization needs to *measure* the level coming out of the chain while
the player plays, compare, and trim. The device turns out to broadcast exactly
the signal needed for that loop, continuously, with zero setup.

## 2. Signals available from the device (investigated 2026-07-14)

### 2.1 Grid meters on 2003 — the primary signal (live-probed today)

`/dspEvent` `{eid_:1, mid_:796}` and `{eid_:1, mid_:800}`: two 128-float
arrays, already decoded raw by `helixgen.device.meters` and surfaced as
`device meters` / MCP `device_meters`. The 2026-07-14 capture findings noted
only "~0.0–0.08" (idle noise floor) and left semantics uncharacterized. A live
probe today (`device meters --seconds 6 --json` against the Stadium XL, active
preset `Input Inst 1_2 → Noise Gate Mono → Vermin Dist Mono → Mandarin Rocker
Mk 3 → With Pan (IR cab) → Output Matrix`, signal present) shows much more
structure:

- **Cells come in adjacent pairs** carrying the identical value (stereo /
  dual-mono L+R per meter node).
- **mid 796** populated clusters (all else exactly 0.0):
  - cells 0–1: small, fluctuating 0.0005–0.067 with playing — consistent with
    **instrument input level**;
  - cells 14–17 (one value ×4): tracks cells 0–1 closely — post-gate;
  - cells 18–21 (×4): 0.006–0.031 — post-drive into amp;
  - cells 22–25 (×4): **0.83–1.06**, the hot spot — post-amp (a cranked
    Mandarin Rocker into the cab);
  - cells 8–9 and 26–27: **identical to each other** in every reading
    (0.30–0.62) — the **post-chain / path output level**, reported twice.
- **mid 800** populated cluster: cells 108–119, six pairs patterned
  `a,a,b,b,a,a,b,b,c,c,d,d`, where `a` **exactly equals mid 796's cells 8–9**
  in the same burst — the **output-stage / hardware-send meters** (main L/R,
  and sibling sends at slightly different trims) fed by the path output.
- Values **exceed 1.0** (max observed 1.0563) → linear float amplitude
  envelope, not a clamped 0–1 UI value. dB math is `20·log10(v)`.
- Update rate ≈ **2–3 readings/sec per mid** as first sampled; phase 0 measured the true rate at **~10 Hz per mid** (the first probe's CLI path under-sampled).
- Stream is **always on**: no app, no tuner engage, no device mutation —
  read-only subscribe (same as the network tuner).

**The killer property:** input cells and output cells arrive in the *same
burst*, so we can compute the chain's **gain transfer** (output ÷ input) per
reading — a loudness measure largely invariant to how hard the user happens to
be picking. That makes cross-preset comparison far more robust than measuring
output level alone.

### 2.2 Pitch stream — a free "is the player playing" gate

`/dspEvent` `{eid_:10, mid_:796}` (the network-tuner scalar; `-1.0` = silence)
rides the same stream. A measurement pass can gate its samples on
`pitch != -1.0` **or** input-cell level above the idle floor, so silence and
noise between riffs never pollutes the statistics.

### 2.3 Undecoded `/meter` address — check during characterization

The 2003 port also carries a distinct **`/meter`** OSC address
(`docs/helix-protocol.md` §2 transport table) that the parity capture never
decoded — only `/dspEvent` was. Phase 0 should dump a few `/meter` frames; if
it's a labeled per-output meter feed, it may hand us the mid-800 mapping for
free.

### 2.4 Tier 2: USB audio capture — the full-fidelity signal

The Stadium is a USB audio interface, so the Mac can record the actual
processed audio. This is a categorically richer signal than the meters: the
grid cells are **scalar amplitude envelopes at ~10 Hz** — they can say *how
loud*, never *what it sounds like*. Everything spectral (§4) requires this
tier. Costs: cabling/routing config the network path doesn't need, an
audio-capture dependency (core is stdlib+click; capture needs `ffmpeg`
-avfoundation / `sox` / PortAudio, and analysis wants `numpy` — precedented in
this project by the IR catalog's 5-band FFT pass), and it measures only the
send routed to USB. **Decision: network meters are the phase-1 MVP for level
normalization; USB capture is the phase-3 upgrade that unlocks quality
analysis, not just loudness.**

## 3. Design

### Phase 0 — characterize the grid (no user playing required)

Map cell index → signal-chain node by **actuating known changes and watching
which cells respond**, using live-ops verbs (all already shipped):

1. **Unit calibration:** with any signal present (even hum / noise floor),
   `device set-param` the path **output block `level`** (dB-native) down 6 dB →
   the cells downstream of the output node must scale by exactly 0.501×. Proves
   linear amplitude and pins the post-output cells in one move.
2. **Bisect the chain:** `device bypass` each block in turn / trim amp ChVol /
   toggle the gate — the first cell cluster that responds sits downstream of
   that node.
3. **Vary the layout:** load presets with different block counts, split paths,
   and both DSPs occupied; watch how populated-cell indices move to derive the
   index formula (stride per path, 2 cells per node, why the ×4 clusters and
   the duplicated 8–9/26–27 pair exist).
4. **Dump `/meter` frames** (§2.3) while at it.

Deliverables: a mapping table in `docs/helix-protocol.md` §telemetry (+ the
findings appended to this spec), and `meters.py` gaining labeled decoding —
e.g. `label_cells(reading, layout) → {node: level}` with `input`, per-block,
`output` roles. This phase **mutates the active tone** (bypass/level pokes), so
run it on an expendable preset with the user's consent, per device-write
gating.

### Phase 1 — measurement verb

`helixgen device measure [--seconds N] [--json]` + MCP `device_measure`:
subscribe, gate on playing (§2.2), and accumulate per gated reading:

- `input` envelope (cells 0–1), `output` envelope (path-output cells), and
  `gain = output / input`;
- report robust statistics in dB: median and p75 of `output`, median `gain`,
  sample count, and % of the window that was gated silent (so the caller knows
  the measurement is trustworthy — reject windows with < ~20 playing samples,
  i.e. ~4 s of actual playing at the phase-0-measured ~10 Hz rate).

Read-only; safe under device-write gating.

### Phase 2 — closed-loop normalization

`helixgen device normalize` (+ MCP), user-invoked (it **mutates the device and
the local `.hsp`s**). Two scopes, same loop:

- **Snapshots within a preset:** for each snapshot: `device snapshot <i>`,
  prompt "play the same riff", `measure`, compute the dB delta of median
  `gain` vs the anchor snapshot.
- **Presets across a setlist:** for each managed tone: `device load`, same
  measure, delta vs an anchor preset.

Actuation per target: prefer the **path output block `level`** — it's already
denominated in dB, so the correction is exact in one move (`set-param output
level -- <dB>`); fall back to amp ChVol (knob-value ↔ dB is model-dependent,
so ChVol needs a re-measure iteration; output level shouldn't). Apply live via
`device set-param` for instant feedback, re-measure once to confirm within a
±1 dB tolerance band (don't chase meter noise below that), then **write the
final trim into the local `.hsp`** via the mutate verbs (the `.hsp` stays the
source of truth — for snapshots, as per-snapshot overrides) and `device sync`
so the device copy is rebuilt from it.

Interaction contract: the skill/CLI tells the user *when to play and when to
stop* per target, shows each measured level and applied trim in dB, and skips
(with a warning) any target whose measurement window had too little playing.

### Phase 3 — signal *quality* analysis (USB audio tier)

`helixgen analyze-audio <capture.wav>` (or capture built in): record N seconds
of playing through the active tone via the Stadium's USB return, compute the
metric set in §4, and return a structured report the agent can reason over.
This turns the loop from "match levels" into "does the tone measure the way
the intent says it should" — see §4.3 for how the intelligence layer closes
that loop.

### Skill integration

The tone skill's §5.7 static pass **stays** — it's the a-priori default and the
only option offline. On top of it, two measured entry points:

- **At creation time (tone skill), when the Helix is online.** After authoring
  the `.hsp`, the tone skill offers a refinement loop: sync the candidate to an
  expendable scratch slot, prompt the user to play, `device measure` (phase 1)
  for level trims — and with USB capture available, `analyze-audio` (phase 3)
  for tonal-balance moves. Each iteration applies deltas to the local `.hsp`
  via `patch_preset` and re-syncs, so the `.hsp` remains the source of truth
  throughout. Device online-ness is cheap to detect (`device info` with a
  short timeout); offline authoring is unchanged.
- **Iterating later (device skill).** The same loop offered on demand against
  tones already on the hardware — "level-match this setlist / these snapshots
  while you play", or "this patch sounds harsh, measure it and fix it" —
  without re-authoring anything.

Both are user-invoked (device-write gating) and both converge on the same
primitives: `measure` / `analyze-audio` → agent decides deltas → `patch_preset`
→ `device sync`.

## 4. Signal-quality mathematics — what's out there

The user's instinct ("FFT stuff plus some intelligence") is right, in two
layers: **deterministic DSP metrics** (well-established, cheap to compute) and
an **interpretation layer** mapping metrics ↔ tone vocabulary ↔ param moves.

### 4.1 What each signal tier can support

| Metric family | Network meters (2–3 Hz scalars) | USB audio (full-rate) |
|---|---|---|
| Level / loudness envelope | ✅ (phase 1) | ✅ |
| Per-node gain staging, clip/headroom checks | ✅ (cells >1.0 = hot node) | only at the output |
| K-weighted LUFS, true peak | ❌ | ✅ |
| Anything spectral (brightness, mud, fizz) | ❌ | ✅ |
| Dynamics (crest factor, transients) | ❌ (envelope too slow) | ✅ |
| Noise / hum diagnosis | partial (idle floor level) | ✅ (identifies *what* the noise is) |

### 4.2 The standard metric set (all classical DSP, no ML required)

- **Loudness:** ITU-R BS.1770 **LUFS** (K-weighting = one shelf + one high-pass
  biquad, then gated mean-square — implementable in pure Python/numpy;
  `pyloudnorm` is the reference library), RMS, **true peak**.
- **Dynamics:** **crest factor** (peak/RMS, in dB) — directly measures how
  compressed/saturated the tone is (a metal rhythm sits ~6–10 dB, a clean
  strum ~15–20 dB); attack-transient energy ratio for "tight vs mushy".
- **Spectral (FFT / Welch PSD):** **band energies** over guitar-relevant bands
  — the IR catalog already does exactly this (stdlib `wave` + numpy, 5 bands)
  to derive its bright/dark/beefy/tight tags, so the vocabulary and code
  precedent exist in-repo. Plus **spectral centroid** (single-number
  brightness), **spectral tilt** (dB/octave slope), and targeted trouble
  bands: mud ≈ 200–400 Hz, boxiness ≈ 400–800 Hz, harshness ≈ 2.5–4 kHz,
  fizz ≳ 6 kHz.
- **Noise:** FFT peaks at 50/60 Hz + harmonics = hum (ground/single-coil);
  broadband floor in silent gaps = hiss/gain-staging noise; both measured
  against the playing level for a usable SNR figure.
- Heavier tooling exists (`librosa`, `essentia`, `aubio` for onset/MFCC-level
  features) but is overkill: the set above is a few hundred lines of
  numpy and covers everything a tone-refinement loop can act on.

### 4.3 The intelligence layer

The metrics only become useful mapped to *intent* — and that mapping is the
agent's job, not a formula's. The skill already expresses intent in the IR
catalog's controlled vocabulary (bright/dark/beefy/tight/…); the analysis
report should speak the same language: compute the descriptors, let the agent
compare them against what the tone *should* be ("tight modern-metal rhythm"
measuring crest 16 dB and centroid 900 Hz is neither tight nor bright), and
translate the gap into concrete moves (raise gate threshold, cut 300 Hz,
presence up, swap IR for a brighter mix) applied via `patch_preset` and
re-measured. Deterministic guardrails stay deterministic (clipping, hum, SNR);
*taste* stays with the agent + player. A useful design target: the analyzer
emits `{measured_tags, trouble_bands, dynamics, noise}` in catalog vocabulary,
so the same grep-first tags used to *choose* an IR also *verify* the result.

## 5. Risks / limitations

- **Meter ≠ perceived loudness.** Amplitude envelope, no K-weighting; expect
  ±1–2 dB perceptual error across very different voicings. Acceptable for
  "stop the lead patch from being 6 dB quieter"; USB-LUFS (§2.4) if ever not.
- **Playing consistency.** The gain-ratio metric + median statistics + the
  "same riff" prompt mitigate but don't eliminate it. Distorted chains are
  heavily self-compressed, which actually helps (output level is insensitive
  to input level there).
- **Unknown tap points.** Whether mid-800 output meters sit pre or post Global
  EQ / volume knob is unknown until phase 0; normalization compares like with
  like either way, but the docs should state it.
- **Layout generality.** Cell mapping was probed on one serial preset; splits,
  dual-amp, and DSP-1 layouts must be pinned in phase 0 before `label_cells`
  can be trusted.
- **~10 Hz sampling** (phase-0 corrected) → each target needs a few seconds
  of steady playing; a full 8-snapshot pass is well under a minute of riffing.
- **Network flakiness** — standard rule applies: re-run; the loop is
  idempotent (measure → trim to absolute target, not cumulative).
- **Device-write gating** — phases 0 and 2 mutate the active tone; both are
  user-invoked with the consent flow, never autonomous.

## 6. Phase-0 findings (2026-07-14, Stadium XL — hardware-verified)

Phase 0 ran the day the spec landed. Hypothesis scoreboard:

- **CONFIRMED: the grids are live per-node audio envelopes.** Bypassing the
  amp collapsed the downstream cells −33 dB and restored cleanly. Linear
  amplitude, values >1.0 legal, **~10 Hz per mid** (the spec's initial
  "2–3 Hz" was a CLI sampling artifact).
- **CONFIRMED: cell roles** (serial path-0 preset): pairs, L+R duplicated —
  cells 0–1 instrument input; 8–9 == 26–27 chain out; 22–25 post-amp;
  mid 800's populated cells = output-send pairs carrying the chain-out level.
  ×4 clusters and the full per-layout index formula remain uncharacterized.
- **CORRECTED: all meter taps sit UPSTREAM of the output block's `gain`** — a
  landed −60 dB output-gain write moves **no** cell. So §3's phase-0 "output
  level −6 dB" calibration idea can't work on the output block (an in-chain
  actuator like a bypass does the job, and did); and phase 2's output-gain
  trims are *applied* exactly (dB-native) but *not verifiable* via the grid —
  verify via an in-chain actuator or accept the dB math.
- **NEW: the live-ops wire addresses blocks by `(blks_key − 1) / 2`.** The
  parity capture's "`block_id` = the blks position key" was wrong (erratum
  filed): at a raw key the device **echoes success while toggling the wrong
  block**, and `/ParamValueSet` **silently drops the write** (no ack, no
  echo) — which is why `device set-param` had never worked and `device
  bypass`/`device model` targeted the wrong block. Fixed in
  `client._wire_block` (public coordinates unchanged). Echoes are NOT
  landing-proof; the meters are.
- **CONFIRMED: `/ParamValueSet` values ride in raw units** (dB floats
  verbatim — no normalization).
- **CONFIRMED: pitch-gating beats level-gating.** Single-coil hum
  (0.01–0.07) overlaps playing input levels but reads `-1.0` on the pitch
  stream; `measure` gates on real-pitch + input floor.
- **OPEN: the `/meter` OSC address never appeared** in any 2003/2001 window
  observed today (only `/dspEvent`, `/trigger`, `/heartbeat`) — likely
  conditional (looper? app-attached?); still worth a dump if it ever shows.

Phase 1 shipped alongside (`device measure` + MCP `device_measure`); phase 2
(`device normalize`) and the full cell-index map remain backlog #62.

## 7. Phase-2 notes (2026-07-16 — shipped offline, hardware rig-gated)

Phase 2 shipped 2026-07-16 (backlog #62; hardware validation deferred to
backlog #73): snapshot-aware `set-param --snapshot`, per-snapshot
output-level overrides through the transcoder (a param snapshot target keyed
by the OutputMatrix endpoint's instance id — synthesized by analogy with the
HW-proven user-block targets, not yet device-verified), and `helixgen device
normalize` (snapshot + setlist scopes, dry-run by default, trims written to
the local `.hsp` only). Per §6's meter-tap finding, the loop **trusts the dB
math** for output-gain trims and never re-measures to confirm them — §3's
"re-measure once to confirm within ±1 dB" step is dropped for the output
actuator (it would always read "no change"); the tolerance band survives as
the *planning* dead-band (deltas ≤ 1 dB are not trimmed).

**Erratum — hardware-validation signal source.** §3's interaction contract
assumed a human "plays the same riff" per target. Validation instead uses a
**looped pitched test signal** (audio interface / phone output → inst1): it
passes `measure`'s playing gate — a real sustained pitch (the tuner stream
must report a note, so use a pitched riff/tone loop, not broadband noise,
which gates out exactly like hum) above `INPUT_FLOOR`, held for ≥ ~4 s of
gated samples per window — and makes runs repeatable in a way human picking
is not. The "play the same riff" prompt remains the human-facing contract;
the rig is the validation substitute. No rig was connected for the phase-2
implementation session, hence #73.
