# Amp lens — XY pad (81v.1 Focus View, increment 1) — design

**Date:** 2026-11-22
**Status:** design-ready → implementing
**Context:** the `helixgen device live` editor renders every block's params as
knobs. The first "Focus View" deep editor is an **amp lens**: a 2D pad that
drives an amp's two defining params (Drive × Level) together, for fast
sweet-spot exploration — the one gesture a row of independent knobs can't give.

## Decision

A client-only addition to `page.html`. **No server, protocol, or engine
change** — it reuses the existing `/param` write path.

- **Scope:** amp blocks only (`roleOf(b) === "amp"`, i.e. category amp/preamp).
- **Axes, chosen by name (no model-specific table):**
  - X = the block's drive param — first non-internal param whose name matches
    `/\b(drive|gain)\b/i` but not `master`.
  - Y = the block's level param — first matching `/master|volume|ch.*vol|level/i`.
  - If either is missing, the lens is not shown (the knobs still are).
- **Reuse, not reinvent:** the two params already have registered controls in
  `P` (keyed `path/block/pid`). The pad looks them up and calls their `set()` +
  `queue("param", key)` on drag, so the knobs move in sync and remote/SSE
  updates repaint the dot through the same controls. One `record()` per axis at
  release = one undo step per axis per drag (matches the knob).
- **Geometry:** X maps left→right over `[min,max]`; Y maps bottom→top (up =
  more). `clampV`/`span`/`shown`/`fmt` are reused for mapping and the readout.
- **Guards:** inert when `down`; respects the global `dragging` lock (one
  control at a time); lives inside the flow pane, so the pane host hides it with
  the rest of the signal flow.

## Out of scope
Selectable axes, non-amp lenses (EQ curve, cab mic, comp meter) — each is its
own later increment. The lens is additive; the knobs stay.

## Test
Pure client UI. `node --check` + a stubbed runtime check of the two pure
helpers: axis pick (`pickAxis`) and the pad coordinate↔value mapping
(`padToVal`), including Y inversion and clamping. Repo convention: `page.html`
has no JS unit harness.
