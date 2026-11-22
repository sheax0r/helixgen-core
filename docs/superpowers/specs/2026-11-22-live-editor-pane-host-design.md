# Live editor pane-host shell (81v.1) — design

**Date:** 2026-11-22
**Status:** design-ready → implementing
**Context:** the `helixgen device live` web editor (`src/helixgen/liveedit/`) is
the keystone every 81v.x panel builds on (snapshots 81v.2, controllers,
command-center, global-settings 81v.5, IR/librarian). Those panels serialize
onto the shared `page.html` + `server.py` surface. This spec defines the *seam*
they mount on so a follow-on panel can be added without rewriting the editor or
colliding with the signal-flow editor.

## Problem

`page.html` is a single view: a sticky header, snapshot row, adjective-search
bar, and `<main id="rig">` holding the signal-flow editor. There is no place to
put a second top-level panel (e.g. the 81v.5 device-global settings pane), and
no route convention to keep a new panel's server endpoints from clashing with
the flat `/state`, `/param`, `/swap`, `/add-*`, `/move-block`, … routes.

## Decision

A **pane host**: the editor hosts named panes; "Signal flow" (`#rig`) is the
built-in one. A tab strip switches between panes and **appears only once a
second pane is registered**, so nothing dead ships before 81v.5 exists.

### Client contract (`page.html`)
- `PANES` — registry, seeded with the built-in `{ id:"flow", label:"Signal flow",
  el:() => #rig }`.
- `addPane({ id, label, el, onShow? })` — a follow-on panel calls this once, and
  appends its own `<section class="pane">` to the body. `el` returns that
  element; `onShow(active)` (optional) is called when the pane is shown/hidden
  so it can start/stop its own data stream.
- `showPane(id)` toggles `hidden` across panes and `.active` across tabs.
- The host is **visibility only**. A pane owns its data: it fetches from its own
  routes and manages its own refresh. The signal-flow state loop (`/events` →
  `onMsg` → `render`) is untouched, so panels cannot destabilise the editor.

### Device pane is independent of preset state
Per `2026-07-13-global-settings-re-findings.md`, device-global settings do **not**
reload on preset change. So the Device pane does not consume the signal-flow `S`;
it reads device-global state from its own namespace and refreshes on its own
cadence. This is why the host needs no state-fanout machinery.

### Server route namespace (`server.py`)
- Signal-flow routes stay flat (unprefixed) — unchanged.
- **Every follow-on panel namespaces its routes under `/<panel>/…`** (the
  library drawer already does this with `/library/*`). 81v.5 → `/device/*`
  (GET `/device/state`, POST `/device/<verb>`). Reserved + documented in
  `do_GET` and `_route`; the panel adds the actual handlers in its own PR.

## First tenant

81v.5 global-settings: a separate "Device" pane, lowest collision (no preset
coupling). It (1) appends `<section class="pane" id="devicepane">`, (2) calls
`addPane({id:"device", label:"Device", el:()=>#devicepane, onShow})`, (3) adds
`/device/*` routes in `server.py`. No change to the signal-flow editor.

## Out of scope

Per-pane deep linking, more than two panes' tab overflow, and any Device-pane
content — those are 81v.5+. This spec ships the host + the empty seam only.

## Test

Pure visibility UI; `page.html` is covered by `node --check` (repo convention —
no JS unit harness). The route-namespace reservation is a documented comment,
no logic. Manual: with only the flow pane, no tab strip renders; a stub second
pane makes the strip appear and switch.
