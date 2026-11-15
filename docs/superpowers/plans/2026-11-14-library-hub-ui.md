# Library Hub UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local browser hub to browse/search the authored tone + IR library, see its relationship to the connected Stadium, and reconcile the two with one click.

**Architecture:** A new `src/helixgen/libraryui/` module mirroring `liveedit/`: a stdlib `ThreadingHTTPServer` (localhost-only, the same Host/Origin/Content-Type guards) serving one self-contained `page.html`, backed by a `LibrarySession` that gathers computer-side state (tone metadata, guitars, IRs) and device-side state (pool, setlists, device IRs) and runs the reconcile actions by calling the same functions the `device` CLI verbs call. Computer side works offline; the device side degrades to a "no Stadium" state. No DB — in-memory scan + filter.

**Tech Stack:** Python 3.11 stdlib (http.server), vanilla HTML/JS single page, `pytest`. Device control via the existing `helixgen[device]` client. No new dependencies, no build step.

**Spec:** `/Users/michael.shea/git/helixgen/docs/superpowers/specs/2026-11-13-library-hub-ui-design.md` (plugin repo; the UI design spec travels with this plan).

## Global Constraints

- helixgen-core only; base branch `library-hub-ui` off fresh `origin/main` (0.62.0).
- Stdlib HTTP server, localhost bind only, single `page.html`, no new deps, no build step. Reuse `liveedit/server.py` guards verbatim.
- The server calls Python functions directly (as `liveedit` does); it never shells out to the `helixgen` CLI.
- Identity: tones match device presets by `meta.name`; IRs by irhash. Ambiguous device names are surfaced as a conflict, never auto-matched.
- Every device-mutating action acquires the correct advisory lock (the `@_locked` scope the matching CLI verb uses); the hub holds one `pid`-bound session lease and one persistent `HelixClient`.
- Row model: tone-first, one flat ordered list of variants per tone (guitar × venue), labelled guitar + venue.
- Phase 2 content-diff is gated on a hardware round-trip spike (Task P2-0) before the badge is built.

## Review Focus

- **No device reachable at launch** → the hub still opens and the whole computer side is live; device column shows a clear "no Stadium" state, device actions disabled. (Task 2 test.)
- **Ambiguous device preset name** (two presets same name) → reconcile refuses and shows the competing cids rather than copying/removing the wrong one. (Task 3 test.)
- **A variant `.hsp` whose file is missing** on disk (metadata points nowhere) → the row renders with the variant flagged unavailable, not a crash. (Task 1 test.)
- **Device drops/flaky mid-action** → the action reports the error to the page and leaves the hub usable; no half-written local state. (Task 3 test.)
- **A tone/variant name with characters that need escaping** (quotes, slashes) in search/filter and in the page → matched and rendered safely, no injection into the DOM. (Task 2 test.)

---

## Phase 1 — the usable hub

### Task 1: `LibrarySession` read model + presence diff

**Files:**
- Create: `src/helixgen/libraryui/__init__.py`, `src/helixgen/libraryui/session.py`
- Test: `tests/test_libraryui_session.py`

**Interfaces:**
- Consumes: `tone_meta.load_all_tone_metas()`, `tone_meta.split_variant_key()`, `guitars.load_all_profiles()`, `ir.IrMapping`, `ir_meta.load_all_ir_metas()`; a device client exposing `list_presets()`, `list_setlists()`, `list_container(cid)`, device-IR listing (injected, so tests pass a fake and offline passes `None`).
- Produces: `LibrarySession(client_factory=None)`; `public_state() -> dict` with shape `{"connected": bool, "tones": [ToneRow], "irs": {...}, "setlists": [...], "facets": {...}}` where a `ToneRow` is `{"slug","name","artist","song","descriptor","tags","variants":[VariantRow],"presence":"computer|device|both"}` and a `VariantRow` is `{"key","guitar","venue","treatment","preset_name","hsp","available":bool,"presence":"computer|device|both","device_cid":int|None}`.

- [ ] **Step 1: Write failing tests** in `tests/test_libraryui_session.py`:
  - `test_offline_lists_computer_tones`: with `client_factory=None`, `public_state()["connected"] is False` and every authored tone appears with its variants; `presence` is `"computer"`.
  - `test_presence_both_when_name_on_device`: a fake client whose pool contains a preset named exactly a variant's `preset_name` → that variant's `presence == "both"` and `device_cid` set; a device-only preset appears as a `presence=="device"` row.
  - `test_variant_keyed_guitar_at_venue`: a `<guitar>@<venue>` variant key splits into `guitar` + `venue` in its VariantRow.
  - `test_missing_hsp_flagged_unavailable`: a variant whose `.hsp` path doesn't exist → `available is False`, no exception.
  - `test_facets_collected`: `facets` includes the distinct guitars, venues, treatments (`venue_detail.treatment`), and tags present.
- [ ] **Step 2: Run, verify they fail** — `pytest tests/test_libraryui_session.py -x` → fail (module missing).
- [ ] **Step 3: Implement** `session.py`: a `LibrarySession` that loads the computer model once per `refresh()`, lazily builds the device model when a client is available (guarded so any device error → `connected=False`, computer side intact), matches by name/irhash, and derives facets. `LibraryError(Exception)` with `status` for action errors (mirror `LiveError`).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** `feat(libraryui): read model + computer↔device presence diff`.

### Task 2: Server + page (offline-first browse/search/filter)

**Files:**
- Create: `src/helixgen/libraryui/server.py`, `src/helixgen/libraryui/page.html`
- Test: `tests/test_libraryui_server.py`

**Interfaces:**
- Consumes: `LibrarySession` (Task 1); `liveedit/server.py` guard/handler pattern.
- Produces: `make_server(session, host, port)`; routes `GET /` (page), `GET /state` (`public_state()`), `GET /refresh` (re-scan), `POST` action routes (stubs until Tasks 3–5). `serve(session, host, port, on_ready)`.

- [ ] **Step 1: Write failing tests**: `test_bad_host_rejected` (403), `test_state_served_offline` (GET /state returns connected False + tones), `test_name_with_quotes_escaped` (a tone named `a"b/c` round-trips through /state as data and the page template escapes it — assert the JSON value is exact and the page uses `textContent`, not HTML interpolation, for names).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** `server.py` adapted from `liveedit/server.py` (same `_guard`, `_bad_host`, `_refuse`, security headers); `page.html` as a two-pane layout: left = computer library (tone-first rows, expandable variants), right = device (pool + setlists), a facet filter bar + free-text search on top, presence badges. Device pane shows a "no Stadium connected" state + a Refresh/Reconnect control when `connected` is false. Names/text rendered via `textContent`.
- [ ] **Step 4: Run, verify pass**; manual smoke via `python -c` serving once.
- [ ] **Step 5: Commit** `feat(libraryui): localhost server + two-pane browse/search page`.

### Task 3: Reconcile actions — copy → and pull ←

**Files:**
- Modify: `src/helixgen/libraryui/session.py`, `server.py`, `page.html`
- Test: `tests/test_libraryui_actions.py`

**Interfaces:**
- Consumes: the function behind `device copy` (upsert by `meta.name`, IRs by default) and `device to-hsp` (device content → `.hsp`) + `library import` registration; the matching `@_locked` scopes.
- Produces: `session.copy_to_device(variant_key, to=None, pos=None)`, `session.pull_from_device(cid)`; `POST /copy`, `POST /pull`.

- [ ] **Step 1: Write failing tests**: `test_copy_calls_device_copy_with_hsp` (fake records the `.hsp` pushed), `test_pull_writes_hsp_and_registers` (to-hsp output lands in `library/tones/` and a tone meta is created), `test_ambiguous_device_name_refused` (two device presets same name → `LibraryError` naming the cids, nothing pushed), `test_device_error_surfaced_not_crashing` (client raises → action returns error dict, session still serves state).
- [ ] **Step 2–4:** fail → implement (acquire lease/lock, call the functions, re-derive presence) → pass.
- [ ] **Step 5: Commit** `feat(libraryui): one-click copy→/pull← reconcile`.

### Task 4: Setlist management

**Files:** Modify `session.py`, `server.py`, `page.html`; Test: `tests/test_libraryui_setlists.py`

**Interfaces:** the functions behind `device setlist create/rename/delete/duplicate`, `device copy --to` / `device create --from` (reference a pool-only preset into a setlist), `device rm --from`, `device move`. Produces `session.setlist_*` methods + `POST /setlist/*`.

- [ ] **Step 1: Failing tests**: create/rename/delete/duplicate call through; add-reference uses `device create --from <cid>` for a pool-only (no-`.hsp`) preset and `device copy --to` for an authored tone; remove/move call through; an occupied `--pos` is reported, not stacked.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat(libraryui): setlist create/duplicate/reorder + membership`.

### Task 5: IR view + push/delete/rename

**Files:** Modify `session.py`, `server.py`, `page.html`; Test: `tests/test_libraryui_irs.py`

**Interfaces:** `ir.IrMapping` + `ir_meta` (computer), device list-irs (device), functions behind `device push-ir` / `delete-ir` / `rename-ir`. Produces an IR view in `public_state()` (computer vs device, matched by hash) + `POST /ir/*`.

- [ ] **Step 1: Failing tests**: IR presence diff by hash (computer-only / device-only / both); push/delete/rename call through; the 2048 IR-cap refusal surfaces as an error, not a crash.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5: Commit** `feat(libraryui): IR browser + device push/delete/rename`.

### Task 6: `helixgen library ui` CLI verb

**Files:** Modify `src/helixgen/cli_library.py`; Test: `tests/test_cli_parity.py` (help contract), `tests/test_libraryui_cli.py`

**Interfaces:** mirrors `device live` wiring in `cli_device.py` — build a `LibrarySession` with a device-client factory (addressing via `--ip`/`--port` fallthrough), acquire the `pid`-bound session lease, `serve(...)`, print the URL, open a browser unless `--no-open`.

- [ ] **Step 1: Failing tests**: `library ui --help` advertises `--port-http`/`--no-open`; the verb starts the server on a free port and `GET /` returns the page (threaded test, then shut down).
- [ ] **Step 2–4:** fail → implement (default `--port-http 8766`) → pass.
- [ ] **Step 5: Commit** `feat(cli): helixgen library ui — the library hub verb`.

### Task 7: Docs + release prep (core)

- [ ] Update `docs/CLI.md` Library-commands section with `library ui`; add a short `docs/` note. Run `pytest tests/test_cli_parity.py`.
- [ ] Full suite green (`pytest`); build the wheel and run `library ui --help` from it (per the "verify the published wheel" rule) before any version bump.
- [ ] Bump `pyproject.toml` version; PR to core `main`. (Release to PyPI follows core's own gated flow; the plugin pin bump + plugin release follow after.)
- [ ] **Commit** per step; PR body cross-references the spec + the plugin companion.

## Phase 2 — sync smarts + edit (spike-gated)

### Task P2-0: content-diff spike (GATE — needs the Stadium)

- [ ] On hardware: copy a helixgen tone to the device, Save it on the device, pull it back, and compare the library `.hsp` to the device content on the `to-hsp` **semantic-equivalence projection** (not bytes). Confirm an untouched tone reads "same" and a real edit reads "changed". If the projection is noisy, record what canonicalization is needed. **No badge ships until this passes.**

### Task P2-1: "changed" badge
- [ ] Add a per-variant content-diff to the presence model using the projection validated in P2-0; surface a "changed" badge on on-both rows; ← pulls the device version back. Tests use a fixture pair (same projection / differing projection).

### Task P2-2: edit handoff
- [ ] "Edit" on a tone copies it to the device (if needed) and opens a `liveedit` server **in-process** (second `ThreadingHTTPServer` thread) sharing the hub's one client + session lease, on its own port. No subprocess, no second lock owner.
