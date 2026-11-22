# EQ graphic lens — curve editor (81v.1 Focus View, increment 2) — design

**Date:** 2026-11-22
**Status:** design-ready → implementing
**Context:** second Focus-View deep editor. A graphic EQ's bands are far easier
to read and dial as a **frequency-response curve** than as a row of identical
knobs. Verified offline (`paraminfo.load_param_ui`): graphic-EQ params are
**named by frequency** — `"125 Hz"`, `"1 kHz"`, `"16 kHz"`, `"31.25 Hz"` — and
the value is that band's gain. So the curve is derivable from the param names at
runtime, no model-specific table — the same trick the amp lens used.

## Decision

Client-only in `page.html`. **No server/protocol/engine change** — reuses the
`/param` write path and each band param's control already registered in `P`.

- **Scope:** graphic EQ only — a block whose params include **≥3 frequency-named
  gain params**. Parametric EQ (Low/Mid/High × Freq/Gain/Q + shelves/cuts) and
  tilt EQ are their own later increments; their knobs stay as-is.
- **Detect** at runtime: `eqBands(params)` = non-internal continuous params whose
  name parses as a frequency (`parseHz`), sorted ascending by Hz. The lens shows
  only when `category === "eq"` and `bands.length >= 3`.
- **Render:** an SVG curve, one handle per band. X = band index, evenly spaced
  (graphic bands are octave-spaced, so even spacing *is* log-frequency). Y =
  the band's gain over its `[min,max]` (up = boost), with a zero-dB guide line.
  A polyline connects the handles.
- **Edit:** drag a handle vertically to set that band's gain. Reuses the band's
  `P` control (`set` + `queue("param", key)`), so the knob and the handle stay
  in sync and remote/SSE/`fail` updates repaint the curve (wrap `set` **and**
  `fail`, per the amp-lens review). One `record()` at release = one undo step.
- **Locks/guards:** each handle is a single-param drag, so it sets
  `dragging = key` exactly like a knob — the existing `paramLocked` echo guard
  already covers it (no multi-key lens lock needed). Inert when `down`; no write
  on a bare click; respects the one-control `dragging` lock; lives in the flow
  pane so the pane host hides it.

## Out of scope
Parametric/shelf/tilt EQ curves, dragging X (frequency) or Q, a global output
level handle. Additive — the knobs stay.

## Test
`node --check` + a stubbed runtime check of the pure helpers: `parseHz`
(Hz/kHz/decimal/reject), `eqBands` (filter + sort + the ≥3 gate, internal/toggle
exclusion), and the gain↔Y mapping (`gainFrac`/`gainOfY` round-trip, clamp, Y
inversion). Repo convention: `page.html` has no JS unit harness.
