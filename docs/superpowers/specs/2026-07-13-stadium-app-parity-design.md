# Stadium-app parity — full app-function coverage via helixgen

**Date:** 2026-07-13 · **Status:** draft, awaiting user review
**Branch:** `worktree-stadium-app-parity` (from `github/main` @ `cc6f35d`)

## Goal

Enumerate **every function of the Line 6 Helix Stadium desktop app**, map each
one to helixgen's CLI / MCP / skill surface, and drive the gaps to zero so the
user never needs to open the app. Deliverable of this phase: a **coverage
matrix** + a **ranked backlog**; implementation then proceeds in ranked order.

## Scope decisions (user-confirmed 2026-07-13)

**In scope:**

1. **Preset / setlist / IR management** — largely shipped (2.16–2.18) or
   in-flight: a separate agent is implementing the **tone-library model
   redesign** (`2026-07-13-tone-library-model-redesign.md`). This effort
   *verifies* coverage (rows in the matrix with evidence) and cross-references;
   it does **not** re-plan that work. IR management is believed done — verify
   explicitly (register, push, pull, list, on-device prune/rename/delete each
   get their own row).
2. **Global settings + I/O config** — everything in the app's global-settings
   surface (ins/outs, impedance, USB routing, footswitch/EXP global config,
   displays, tempo, tuner config …), read **and** write over the network.
3. **Live control / monitoring** — tuner readout, tempo, looper control,
   meters, live param dashboards (the 2001/2003 streams; `device watch` is the
   existing base).

**Out of scope** (listed in the matrix, verdict 🚫, so the coverage claim stays
honest): firmware update/rollback, whole-device factory restore, marketplace/
cloud-account features, and app-local UI affordances with no device effect
(drag-drop visuals, window management, theme).

**Priority:** not pre-committed — rank 🔴/🟡/🔍 rows by impact once the matrix
exists ("whatever the gap analysis says").

## Approach (chosen: A — inventory-first coverage matrix)

Rejected alternatives: **B** vertical slices per area (faster first ship, but
cannot honestly claim exhaustive coverage); **C** protocol-namespace-first
(wire-complete but inverts the goal — users think in functions, not commands).
A folds C's strongest element (the app's own resource/command defs) in as an
inventory source.

### Inventory: three parallel streams

| # | Stream | Method | Needs |
|---|--------|--------|-------|
| 1 | **Documentation** | Research agent fetches the official Stadium/Stadium XL owner's manual + app release notes; produces a user-facing function list organized by app screen/menu | web only |
| 2 | **App resources** | Dump the app bundle's definition files (`commanddefs/P35EditCommandDefs.json`, defs/resources per `docs/helix-protocol.md` §resources) — enumerate every assignable command, property, capability | local app bundle |
| 3 | **Frida capture sweep** | `tools/re_capture.py` instrumenting the running app while I drive it via computer-use through every menu/panel, logging which OSC commands each action emits | app + device + user access-approval |

Streams 1+2 run first (no device). Stream 3 is a **targeted checklist** built
from whatever 1+2 leave unresolved (expected: global-settings writes,
tuner/looper/tempo commands), not an open-ended wander. Capture ops: the
assistant drives both the instrumentation and the app (user-approved
computer-use); pcap on ports 2001–2003 is the fallback since the protocol is
cleartext.

### Coverage matrix

Lives at **`docs/stadium-app-parity.md`** — checked in, maintained as features
ship (same spirit as `BACKLOG.md`). One row per user-facing function:

| Column | Meaning |
|---|---|
| Function | What the user does, in user language |
| App location | Screen/menu where it lives |
| Protocol surface | Known command(s), or `needs-discovery` |
| CLI / MCP / skill | Status each: full · partial · none · n-a |
| Verdict | ✅ done · 🟡 partial · 🔴 missing · 🔍 needs-discovery · 🚫 out-of-scope |
| Notes | Evidence + cross-refs (backlog #, specs, in-flight library work) |

Rules: a ✅ requires **evidence** (shipped release, test, or HW validation
ref) — not memory. Rows owned by the in-flight library agent are marked as
such, never re-planned here. Every 🔴/🟡/🔍 row must end up with a backlog
entry — no silent gaps.

### Backlog + execution cadence

After the matrix: rank gaps by impact, present the ranking to the user, then
merge entries into **`docs/BACKLOG.md`** (one backlog, existing legend
`[local]` / `[device-write]` / `[discovery]`). Implementation proceeds in
ranked order: substantial features get their own brainstorm → spec → plan
cycle; small items batch. Discovery items funnel into additional capture
sessions (assistant-driven). Device-write validation follows the existing
gating rules (user-invoked `!` or granted Bash permission); reads are
unrestricted.

## Phases

0. **Inventory** — streams 1+2 in parallel; build stream-3 checklist from their
   holes; run the capture sweep.
1. **Matrix + gap ranking** — write `docs/stadium-app-parity.md`; verify
   claimed-done areas (IR management, library verbs) with evidence; present
   ranking to user.
2. **Backlog merge** — write ranked entries into `BACKLOG.md`.
3. **Implementation waves** — per ranked order, each wave through the normal
   skill flow (brainstorm/spec for big items, TDD, HW-validation gating,
   release process unchanged).

## Risks / error handling

- **Capture ambiguity** (action → several commands, or none observed): mark the
  row 🔍 needs-discovery with what was seen; never guess a protocol claim.
- **Firmware/protocol drift:** matrix notes the firmware version captured
  against; re-verify rows on device firmware updates.
- **Flaky Stadium network stack:** existing rule — re-run idempotent ops,
  reboot device if persistent.
- **Overlap with the library agent:** its rows are cross-referenced, and this
  branch touches only new docs (+ later, new feature code) to keep merges
  trivial.
- **Skills must stay tool-driven** (per project feedback): any new skill/MCP
  behavior contract lives in tool descriptions and CLI `--help`, not source
  references.

## Success criteria

1. Every function the Stadium app exposes has a matrix row with a verdict.
2. No 🔴/🟡/🔍 row lacks a backlog entry.
3. In-scope workflows (library management, global settings, live
   control/monitoring) are performable end-to-end via CLI/MCP/skills with the
   app closed — each validated on hardware as it ships.
