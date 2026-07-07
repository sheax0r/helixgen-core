# Volume-normalization pass (tone skill)

**Date:** 2026-07-06
**Status:** Design (brainstormed + approved).
**Branch:** `feature/volume-normalization`
**Scope:** the `tone` skill only (`.claude/skills/tone/SKILL.md`). No code, no
tests — prose guidance + a heuristic. Backlog item 6 from
[[project_tone_skill_backlog]].

## Goal

After a tone is designed, do a final **level pass** so the preset's loudness is
sane and — especially when replicating a pre-existing tone — so the **relative**
loudness between parts/snapshots tracks the source material (how loud each part
is *relative to* the others, not identical absolute level). Two targets, weighted
to the first:

1. **Relative (primary):** the level differences between snapshots/parts within
   one preset match the source's part-to-part dynamics.
2. **Absolute (secondary):** each preset sits at a consistent baseline loudness,
   so presets don't jump wildly in level when the player switches between them.

## Constraint that shapes everything

helixgen **generates presets from specs; it never renders audio**, so it cannot
*measure* loudness offline. The pass is therefore a **skill heuristic + research
+ the user's ear**: it sets *starting* level deltas from rules of thumb and known
conventions, states the *intended* relative balance in the report, and the user
fine-tunes by ear on the device. The design is honest about this — it does not
claim measured accuracy.

## The knob

`show_block` the amp and use its **channel-volume param** (`ChVol`, or the amp's
`Level` — the exact name varies by amp, so confirm with `show_block`; do NOT use
`Master`, which also changes power-amp sag/feel). This is tone-neutral-ish and
costs **no slot**. Only when the amp exposes no channel-volume param, add **one
end-of-chain volume block** (post-cab, from the `volume` category) and automate
that. In a layered two-amp preset, each snapshot levels whichever amp is active
via that amp's own channel volume.

## The heuristic — three forces, applied in order

1. **Anchor (absolute, consistent across presets).** Set the *reference part*
   (usually the rhythm) to a standard channel-volume anchor — default
   `ChVol ≈ 0.5` (a value that leaves headroom and doesn't clip; adjust to the
   amp's taper if `show_block` shows an unusual range). Because every preset
   anchors its main part to the same value, presets sit at a consistent baseline.
   If research indicates the source tone should sit notably hotter or softer
   relative to its material, offset the anchor accordingly.

2. **Gain compensation (the non-obvious force).** More gain → more compression →
   louder/denser *perceived* level at the same knob. So **lower-gain parts get a
   level bump** to sit even: a clean or edge-of-breakup part typically needs its
   channel volume pushed **up** to match a high-gain rhythm's perceived loudness,
   not left equal. A very hot, highly-compressed high-gain rhythm may need a
   small trim. Never leave a clean part at the same knob as a high-gain part and
   call it balanced.

3. **Intended dynamics (relative deltas, research-overridable).** Default
   conventions, expressed relative to the rhythm anchor:
   - **Lead / solo:** ~**+2–3 dB** over rhythm — leads should cut through.
   - **Crunch:** ~match rhythm.
   - **Clean:** perceptually **matched** to rhythm (achieved via force #2's
     upward bump, since clean is less compressed).
   When step-1b research reveals the **source's** actual part-to-part dynamics
   (e.g. "the solo is barely louder", "the clean verse drops right back"), those
   **override** these conventions.

### dB → param translation (rough, by design)

The knobs are 0–1 (0–10 on display) and we can't measure, so use a rule of thumb
— *a small channel-volume nudge (~0.05–0.10) ≈ a couple dB* — to turn the
intended dB deltas into starting param values. Treat these as starting points,
not calibrated values; the user's ear is the final arbiter.

## Integration into the tone workflow

- **New step 5.7 "Volume-normalization pass"**, after 5.5 (snapshots) and 5.6
  (auto-wiring), before step 7 (generate). It runs for every preset — a
  single-snapshot preset still gets the anchor (force 1); multi-snapshot presets
  get all three forces.
- **Per-snapshot level moves** are `params` overrides on the channel-volume param
  (or the volume block), added alongside the existing per-snapshot gain / EQ /
  effect deltas in the `snapshots` array. For a base (non-snapshot) preset the
  anchor is set on the base amp params.
- **Report (step 8) + companion `.md` (step 7a):** add a one-line **"Levels"**
  summary of the *intended* relative balance, e.g.
  `Levels: rhythm anchor; lead +~2 dB; clean bumped to match (fine-tune by ear)`.
- **Common Mistakes:** add a row — *leaving a clean/low-gain part at the same
  level knob as a high-gain part and calling it balanced* → apply gain
  compensation (force 2).

## Out of scope (deferred / non-goals)

- A **code loudness-estimator** (scoring perceived loudness from params) — amp-sim
  loudness isn't reliably predictable from knob values; rejected in favor of the
  skill heuristic.
- A **device-measured loop** (capturing real levels off the Stadium / a DAW meter
  and computing exact offsets) — most accurate but manual and outside the offline
  generate flow; a possible future enhancement, not this pass.
- Using `Master` for leveling (changes power-amp sag/feel — explicitly avoided).
- Loudness matching across *different source recordings* in absolute LUFS — the
  pass targets a consistent internal baseline + source-relative dynamics, not a
  mastering-grade absolute target.
