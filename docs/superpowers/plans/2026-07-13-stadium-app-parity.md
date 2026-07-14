# Stadium-app parity — Phase 0–2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce an evidence-backed coverage matrix of every Helix Stadium app function mapped to helixgen CLI/MCP/skill status, and a ranked backlog of the gaps — so implementation of "never need the app" can proceed in priority order.

**Architecture:** Three parallel inventory streams (app owner's manual via web research; the app bundle's own resource/command defs; a frida capture sweep of the running app driven via computer-use) feed one checked-in matrix (`docs/stadium-app-parity.md`). Gaps are ranked and merged into the existing `docs/BACKLOG.md`. Feature implementation waves follow as separate brainstorm→spec→plan cycles keyed off the ranked backlog.

**Tech Stack:** Python stdlib + `click` (helixgen), `frida>=16` (capture, `tools/re_capture.py` + `tools/hook_sockets.js`), computer-use MCP (drive the app), pcap fallback (cleartext OSC on ports 2001–2003). Markdown deliverables.

## Global Constraints

- Spec of record: `docs/superpowers/specs/2026-07-13-stadium-app-parity-design.md`.
- Branch: `worktree-stadium-app-parity`, based on `github/main` @ `cc6f35d`. Remote is named **`github`**, not `origin`.
- App under test: **Helix Stadium.app v1.3.2.9805** at `/Applications/Line6/Helix Stadium.app`. Device at `192.168.4.84` (ports 22/2001/2002/2003; ignores ping).
- A ✅ verdict requires **evidence** (shipped release + test/HW ref), never memory.
- Rows owned by the in-flight tone-library-model-redesign agent (`docs/superpowers/specs/2026-07-13-tone-library-model-redesign.md`) are **cross-referenced, never re-planned** here.
- Every 🔴/🟡/🔍 row must produce a backlog entry — no silent gaps.
- Skills stay **tool-driven**: behavior contracts live in MCP tool descriptions + CLI `--help`, not source references (project feedback memory).
- Device-write validation is user-**pre-approved** this session (writes, frida, merge+ship); still prefer an empty/expendable slot for test writes.
- Run pytest as `PYTHONPATH=$PWD/src python -m pytest`.
- Matrix verdict legend: ✅ done · 🟡 partial · 🔴 missing · 🔍 needs-discovery · 🚫 out-of-scope. Backlog legend (existing): `[local]` / `[device-write]` / `[discovery]`.

---

## File Structure

- Create `docs/stadium-app-parity.md` — the coverage matrix (the primary deliverable; maintained ongoing).
- Create `scratchpad/inventory/` working files (not committed): `manual-functions.md`, `bundle-functions.md`, `capture-checklist.md`, capture analysis notes.
- Modify `docs/BACKLOG.md` — append the ranked parity gaps as new numbered items.
- Reuse `tools/re_capture.py`, `tools/hook_sockets.js` (extend the `STEPS` checklist only if needed; no rewrite).

---

## Task 1: Documentation inventory (stream 1)

**Files:**
- Create: `scratchpad/inventory/manual-functions.md` (working, uncommitted)

**Interfaces:**
- Produces: a flat, deduplicated list of user-facing Stadium app functions organized by app screen/menu, each with a one-line description. Consumed by Task 4 (matrix rows) and Task 3 (capture checklist).

- [ ] **Step 1: Dispatch a research agent** for the official Line 6 Helix Stadium / Stadium XL owner's manual + the HX Edit/Stadium app release notes. Prompt it to enumerate **every user-facing function** grouped by app area: preset browser, setlist management, signal-flow editor, block/param editing, snapshots, controller/footswitch assign, IR import/management, global settings (I/O, impedance, USB/routing, footswitch global, displays, tempo), tuner, looper, MIDI, device backup/restore, firmware. Ask for the function name, the app location, and whether it reads or writes the device.

- [ ] **Step 2: Write the result** to `scratchpad/inventory/manual-functions.md` as a checklist grouped by area.

- [ ] **Step 3: Verify coverage sanity** — confirm the list contains at least the areas named in Step 1 (grep the file for each area heading). If an area is missing, note it as "manual-silent" (candidate for capture discovery). No commit (scratch file).

---

## Task 2: App-bundle resource inventory (stream 2)

**Files:**
- Create: `scratchpad/inventory/bundle-functions.md` (working, uncommitted)
- Read-only: `/Applications/Line6/Helix Stadium.app/Contents/Resources/{commanddefs/*.json,xg_scripts/*.xml,P35*.json}`

**Interfaces:**
- Produces: (a) the full set of assignable commands/looper/MIDI/snapshot commands from `P35EditCommandDefs*.json`; (b) the UI-exposed menu/popup actions from `xg_scripts/*.xml`; (c) the device property namespace if present. Feeds Task 4 rows + Task 3 checklist.

- [ ] **Step 1: Enumerate command defs.** Parse the command-def JSON (note it may be *two concatenated JSON objects* per `docs/helix-protocol.md` §resources — split on the boundary before `json.loads`). List every command family and its params.

Run:
```bash
python3 - <<'PY'
import json, re, pathlib
p = pathlib.Path("/Applications/Line6/Helix Stadium.app/Contents/Resources/commanddefs/P35EditCommandDefs.json")
raw = p.read_text()
# file is two concatenated JSON objects; decode iteratively
dec = json.JSONDecoder(); i=0; objs=[]
while i < len(raw):
    while i < len(raw) and raw[i] in " \t\r\n": i+=1
    if i>=len(raw): break
    obj,end = dec.raw_decode(raw,i); objs.append(obj); i=end
for o in objs:
    print("--- object keys:", list(o.keys())[:20])
PY
```
Expected: prints the top-level command/definition keys (e.g. `PresetSnapshot`, `Looper`, `MIDI-*`, `Utility`, `ExtAmp`).

- [ ] **Step 2: Enumerate UI actions.** Grep `xg_scripts/*.xml` for menu items, buttons, and popup actions (the app's own UI surface).

Run:
```bash
grep -rhoiE '(label|text|action|command)="[^"]+"' "/Applications/Line6/Helix Stadium.app/Contents/Resources/xg_scripts/" | sort -u | head -200
```
Expected: a list of UI labels/actions (menus like Export, Rename, Set Color, global-settings toggles).

- [ ] **Step 3: Enumerate device properties.** Grep the bundle for property keys the app reads/writes (`PropertyValueGet`/`setPropertyValue` namespace, `preset.*`, global settings keys).

Run:
```bash
grep -rhoE '[a-z][a-z0-9]+\.[a-z0-9._]+' "/Applications/Line6/Helix Stadium.app/Contents/Resources/" 2>/dev/null | grep -iE 'global|input|output|impedance|tempo|tuner|footswitch|routing|usb|midi|display|preset\.' | sort -u | head -200
```
Expected: candidate device-property keys (global settings surface).

- [ ] **Step 4: Write** the three enumerations to `scratchpad/inventory/bundle-functions.md`, each under its own heading with the source file noted. No commit (scratch file).

---

## Task 3: Build the frida capture checklist (stream 3 prep)

**Files:**
- Create: `scratchpad/inventory/capture-checklist.md` (working, uncommitted)

**Interfaces:**
- Consumes: `manual-functions.md` (Task 1), `bundle-functions.md` (Task 2), and the already-known command surface in `docs/helix-protocol.md` + `docs/superpowers/specs/2026-07-13-device-re-findings.md`.
- Produces: an ordered list of app actions to perform during capture, restricted to functions whose **protocol surface is unknown** after streams 1+2 (expected: global-settings writes, tuner/looper/tempo/MIDI live commands, active-preset select, create-setlist).

- [ ] **Step 1: Diff** the union of Task 1 + Task 2 functions against the commands already documented in `docs/helix-protocol.md` (the OSC address list) and `device-re-findings.md`. Mark each function `known` / `needs-discovery`.

- [ ] **Step 2: Write** the `needs-discovery` set to `capture-checklist.md` as an ordered action script — each entry: a step id, the exact app clicks to perform, and what to watch for. Group by app area so one capture session covers a whole area. Keep the format compatible with `tools/re_capture.py`'s `STEPS` tuples so entries can be pasted in.

- [ ] **Step 3: Sanity-cap the list** — if it exceeds ~30 actions, split into multiple sessions by area and note the split. No commit (scratch file).

---

## Task 4: Capture sweep — run frida while driving the app

**Files:**
- Modify (if needed): `tools/re_capture.py` `STEPS` list — paste the Task 3 checklist entries.
- Output: `captures/re_capture_<epoch>.jsonl` (gitignored capture artifacts).
- Create: `scratchpad/inventory/capture-analysis.md` (working, uncommitted).

**Interfaces:**
- Consumes: `capture-checklist.md` (Task 3).
- Produces: for each `needs-discovery` function, the observed OSC command(s) + arg shape, or `no-command-observed`. Feeds Task 5 matrix rows.

- [ ] **Step 1: Preflight.** Confirm frida present and app + device reachable.

Run:
```bash
python3 -c "import frida; print('frida', frida.__version__)"
ping -c1 -t1 192.168.4.84 >/dev/null 2>&1; nc -z -G1 192.168.4.84 2002 && echo "2002 open"
```
Expected: frida version prints; `2002 open`. (Device ignores ping — the `nc` check is authoritative.)

- [ ] **Step 2: Request computer-use access** to `Helix Stadium` (and Terminal for running the capture, though the capture runs via Bash). Load computer-use tools via ToolSearch (`query: "computer-use", max_results: 30`).

- [ ] **Step 3: Launch the capture harness** in the background (it attaches frida to the running app and walks the checklist).

Run:
```bash
python3 tools/re_capture.py
```
The harness prints an instruction per step and waits for ENTER. For each step: I perform the app action via computer-use (screenshot → click/type), then advance the step. Prefer an **empty/expendable preset slot** for any write.
Expected: one `captures/re_capture_<epoch>.jsonl` written, frames tagged per step.

- [ ] **Step 4: Decode the capture.** For each step, extract the non-noise 2002 RPC commands and their decoded args.

Run:
```bash
python3 - <<'PY'
import json, base64, glob, os
f = sorted(glob.glob("captures/re_capture_*.jsonl"), key=os.path.getmtime)[-1]
from collections import defaultdict
by=defaultdict(set)
for line in open(f):
    o=json.loads(line); addr=o.get("addr","?")
    if addr in {"/dspEvent","/trigger","/heartbeat","/meter"}: continue
    by[o.get("step","?")].add(addr)
for step in sorted(by): print(step, "->", sorted(by[step]))
PY
```
Expected: each step maps to the OSC address(es) it emitted.

- [ ] **Step 5: Write** per-function findings (command + arg shape, or `no-command-observed`) to `scratchpad/inventory/capture-analysis.md`. Flag ambiguous actions (multiple/zero commands) explicitly. No commit of scratch; capture `.jsonl` stays gitignored.

- [ ] **Step 6: Re-run for split sessions** if Task 3 produced more than one. Repeat Steps 3–5 per session.

---

## Task 5: Write the coverage matrix

**Files:**
- Create: `docs/stadium-app-parity.md`

**Interfaces:**
- Consumes: Tasks 1, 2, 4 outputs; current helixgen surface (CLI `--help`, MCP tool list, skills).
- Produces: the committed matrix — one row per app function with verdict + evidence. Consumed by Task 6 (backlog) and by all later implementation waves.

- [ ] **Step 1: Establish current helixgen surface** so CLI/MCP/skill columns are evidence-based, not memory.

Run:
```bash
PYTHONPATH=$PWD/src python -m helixgen --help 2>&1 | sed -n '1,60p'
PYTHONPATH=$PWD/src python -m helixgen device --help 2>&1 | sed -n '1,80p'
grep -oE 'name="[^"]+"' .mcp.json 2>/dev/null | head
```
Expected: the live CLI verb tree + MCP tool names (the authoritative "what we have").

- [ ] **Step 2: Draft the matrix** with the header row and columns from the spec (Function · App location · Protocol surface · CLI · MCP · Skill · Verdict · Notes), grouped by app area. Populate one row per function from the merged inventory.

- [ ] **Step 3: Assign verdicts with evidence.** For each row: mark ✅ only with a concrete ref (release/test/HW); 🟡 partial with the missing piece named; 🔴 missing; 🔍 needs-discovery (command still unknown after Task 4); 🚫 for the out-of-scope set (firmware, factory restore, cloud, UI cosmetics). Cross-reference in-flight library-agent rows to their spec.

- [ ] **Step 4: Explicitly verify the "believed done" areas** the user called out — IR management (register / register-irs / ir-scan / push-ir / pull-ir / list-irs / on-device prune) and library verbs — giving each its own row with a real evidence ref, not a blanket ✅.

Run:
```bash
PYTHONPATH=$PWD/src python -m helixgen device --help 2>&1 | grep -iE 'ir|setlist|sync|install|slots'
```
Expected: confirms which IR/library verbs actually exist to back the ✅ rows.

- [ ] **Step 5: Coverage self-check** — every function from Tasks 1+2 appears as a row; every 🔴/🟡/🔍 row has a Notes entry naming the gap. Grep for stray `TBD`/`?` in verdict column and resolve.

- [ ] **Step 6: Commit.**

```bash
git add docs/stadium-app-parity.md
git commit -m "docs: Stadium app-function coverage matrix"
```

---

## Task 6: Rank gaps and merge into the backlog

**Files:**
- Modify: `docs/BACKLOG.md` (append new numbered items under a "Stadium-app parity" heading)

**Interfaces:**
- Consumes: `docs/stadium-app-parity.md` (Task 5).
- Produces: ranked, actionable backlog entries with the existing legend tags.

- [ ] **Step 1: Extract** all 🔴/🟡/🔍 rows from the matrix into a gap list.

- [ ] **Step 2: Rank** each gap by impact (how often the user must currently open the app for it) × effort, and tag `[local]` / `[device-write]` / `[discovery]`. Discovery items note "needs capture session".

- [ ] **Step 3: Present the ranking to the user** for confirmation before writing (the spec commits to this checkpoint). Adjust order per their feedback.

- [ ] **Step 4: Write** the ranked entries into `docs/BACKLOG.md` under a new `### Stadium-app parity (2026-07-13)` subsection, continuing the existing `#N` numbering, each linking back to its matrix row.

- [ ] **Step 5: Commit.**

```bash
git add docs/BACKLOG.md
git commit -m "docs(backlog): ranked Stadium-app parity gaps"
```

---

## Task 7: Open the top-ranked feature wave

**Files:** none directly — this task hands off to the normal skill flow.

**Interfaces:**
- Consumes: the ranked backlog (Task 6).

- [ ] **Step 1: Take the #1 ranked gap.** If it is a substantial feature (e.g. global-settings read/write), invoke `superpowers:brainstorming` for it → its own spec → its own plan. If it is a batch of small known items (e.g. backlog #1 active-preset, #8 create-setlist, #11 ir-prune), group them into a single small-items plan.

- [ ] **Step 2: Implement** via TDD (`superpowers:test-driven-development`), following helixgen conventions (stdlib + click, MCP mirrors CLI, tool-driven skill contracts). HW-validate device writes on an expendable slot. Release per the automated version-bump process (do not move `stable`/tags by hand).

- [ ] **Step 3: On green + HW validation, finish the branch** via `superpowers:finishing-a-development-branch` → PR → merge to `main` (user pre-approved merge+ship). Repeat Task 7 for the next ranked gap.

---

## Self-Review notes

- **Spec coverage:** streams 1–3 → Tasks 1–4; matrix → Task 5; verify-believed-done (IR/library) → Task 5 Step 4; backlog merge + ranking checkpoint → Task 6; implementation waves + gating/release → Task 7. Out-of-scope set handled as 🚫 rows in Task 5 Step 3.
- **Type/name consistency:** matrix file path `docs/stadium-app-parity.md` and verdict legend are identical across Tasks 5–7 and the spec. Capture output path `captures/re_capture_<epoch>.jsonl` matches `tools/re_capture.py`.
- **Placeholders:** discovery-phase tasks intentionally produce enumerations rather than code; each has a concrete command + expected output. No `TBD`/"handle later" left.
