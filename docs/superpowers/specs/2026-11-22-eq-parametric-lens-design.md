# Parametric-EQ lens — Freq × Gain points (81v.1 Focus View, increment 4) — design

**Date:** 2026-11-22
**Status:** design-ready → implementing
**Context:** completes the EQ story. The graphic-EQ lens (shipped) handles
fixed-frequency band EQs; a **parametric** EQ instead has per-band Frequency +
Gain + Q, plus shelves and cuts. Verified offline (`paraminfo.load_param_ui`,
`HX2_EQParametricMono`): bands are named `"Low Freq"/"Low Gain"`,
`"Mid Freq"/"Mid Gain"`, `"High Shelf Freq"/"High Shelf Gain"`, each with an
`"<X> Enable"` toggle; `Q`, `Cut`s and `Slope`s are separate.

## Decision

Client-only in `page.html`. **No server/protocol/engine change.**

- **Pair bands by name:** `eqBandsParam()` matches each `"<prefix> Gain"` param
  to its `"<prefix> Freq"` sibling (and its optional `"<prefix> Enable"`),
  sorted by current frequency. `Level` (no Freq sibling) and `Q` (not a Gain)
  fall out. No model table.
- **Gate:** category `"eq"`; graphic lens wins when ≥3 frequency-named bands
  exist, else the parametric lens shows when ≥2 Freq+Gain pairs exist.
- **Render:** an SVG curve, one draggable point per band at (X = **log**
  frequency over `[min,max]`, Y = gain). A disabled band (its Enable off) is
  dimmed but still draggable. A 0 dB guide line.
- **Edit:** drag a point to set BOTH its Freq (X, log-mapped) and Gain (Y) via
  their `P` controls; the point is a 2-param drag held with `lensLock {x,y}` (so
  the existing `paramLocked` echo guard covers both), with grab offsets (no
  jump), content-box coordinate mapping, and no write on a bare click. Knob,
  remote and `fail` updates on freq/gain/enable repaint the point. One
  `record()` per axis that changed = one undo step per axis.
- **Q, cuts, slopes, Level** stay as their knobs — the lens is additive.

## Out of scope
A true summed biquad response curve (points only, no filled curve), dragging Q,
cut handles as vertical lines. Later polish.

## Test
`node --check` + a stub check of `eqBandsParam` (pairing, sort, Level/Q
exclusion, Enable linkage) and the log-frequency mapping (`freqFrac`/
`freqOfFrac` round-trip, clamp, monotonic, geometric-mean midpoint). The 2-param
drag/lock path is the amp pad's, already reviewed. Repo convention: no JS unit
harness.
