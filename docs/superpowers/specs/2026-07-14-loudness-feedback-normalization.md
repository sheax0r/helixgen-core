# Loudness feedback loop — measured volume normalization (2026-07-14)

Investigation + design spec for backlog **#58**. Question investigated: *can the
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
- Update rate ≈ **2–3 readings/sec per mid** (~15 per mid in a 6 s window).
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

### 2.4 Alternative path: USB audio capture (true LUFS)

The Stadium is a USB audio interface. Recording its USB return on the Mac and
computing K-weighted LUFS (ITU-R BS.1770) would be the *perceptually correct*
loudness measure — the grid meters are amplitude envelopes with no frequency
weighting, so a dark tone and a fizzy tone at equal meter level can differ a
couple dB in perceived loudness. Costs: cabling/config the network path doesn't
need, an audio-capture dependency (the repo is stdlib+click; K-weighting biquads
are implementable in pure Python but capture isn't), and it measures only the
send routed to USB. **Decision: network meters are the MVP; USB-LUFS is a
possible fidelity upgrade later, not phase 1.**

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
  i.e. ~10 s of actual playing at the observed 2–3 Hz rate).

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

### Skill integration

The tone skill's §5.7 static pass **stays** — it's the a-priori default at
authoring time. The measured loop becomes an optional finishing step offered by
the **device skill** after a sync ("want me to level-match the snapshots /
setlist while you play?"), since it needs the tones on the hardware and the
player in the room.

## 4. Risks / limitations

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
- **~2–3 Hz sampling** → each target needs ~10–20 s of playing; a full 8-snapshot
  pass is a couple of minutes of riffing. Fine, but set expectations.
- **Network flakiness** — standard rule applies: re-run; the loop is
  idempotent (measure → trim to absolute target, not cumulative).
- **Device-write gating** — phases 0 and 2 mutate the active tone; both are
  user-invoked with the consent flow, never autonomous.
