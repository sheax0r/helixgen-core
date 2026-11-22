# Cab mic lens — placement pad (81v.1 Focus View, increment 3) — design

**Date:** 2026-11-22
**Status:** design-ready → implementing
**Context:** third Focus-View deep editor. A cab's **Distance** and **Position**
mic-placement params are far more intuitive as a spatial pad over a speaker
cone than as two knobs. Verified offline (`paraminfo.load_param_ui`): every
`cab_ir_interp` model has the same continuous params — `Distance`, `Position`,
`Angle`, plus a `Mic` model select — so the lens is uniform across cabs.

## Decision

Reuse the amp lens. `lensEl` is already a generic 2-param XY pad (built and
**twice reviewed** for the amp lens: lensLock echo guard, set+fail wrap, grab
offset, one-control lock, no-click-write). Generalise it with one optional
`opts {cls, lab}` arg and call it for the cab:

- **Cab (category `cab_ir_interp`):** X = `Distance`, Y = `Position`, picked by
  exact name (`pickAxis(main, /^distance$/i)` / `/^position$/i`). Shown only
  when both exist. `lensEl(b, dist, pos, {cls:"cab", lab:"Mic placement"})`.
- **Backdrop:** a CSS concentric-ring gradient on `.lenspad.cab` suggesting the
  speaker cone (centre = cap, out = edge); purely cosmetic.
- **Mic model** (a select) and **Angle** stay as their knobs — the lens is
  additive, like the others.

No new drag/write/lock code: the cab lens is `lensEl` + a CSS class + a param
pick. No server/protocol/engine change.

## Out of scope
Angle as a rotation handle, a true mic-on-cone top-down render, per-mic
graphics. Additive — the knobs stay.

## Test
`node --check` + a stub check that `pickAxis` selects Distance/Position and
excludes the Mic select. The drag/lock/write path is the amp lens's, already
covered. Repo convention: `page.html` has no JS unit harness.
