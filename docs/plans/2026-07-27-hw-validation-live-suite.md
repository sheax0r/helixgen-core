# Plan: Hardware validation of the 0.30.0 `/CreateContent` fix (#90) + slots-reorder→sync (#7)

## Context

Implements `docs/BACKLOG.md #90` (live-hardware validation of the #38 fix) and
the residual of `docs/BACKLOG.md #7` (hardware-validate the `slots reorder` →
`sync` path).

0.30.0 root-caused #38: field 3 of the `/CreateContent` `/status` reply is the
device's **edit-buffer dirty flag** (`hist`), not an error code. The old client
read non-zero as failure and then *deleted the content it had just successfully
created*. The fix (confirm creates by re-list, never trust field 3, no
destructive cleanup) was developed and tested **entirely offline** against the
fake/injected socket. The live suite's masking (cooldown-retry + `xfail` in
`tests/live/conftest.py`, `test_device_write.py`, `test_device_ir.py`) was
REMOVED in the same change and **never re-run against hardware**. This plan
closes that gap and encodes the dirty-buffer condition as a permanent live
regression test instead of a one-off manual run.

For #7: `tests/live/test_sync.py::test_sync_lifecycle` already exercises
`device reorder` and `device slots reorder --setlist` → `sync`, but it only
asserts the verbs exit 0 — it never reads the device back to confirm the
setlist's **reference order** actually matches manifest membership order. That
read-back is exactly what #7's residual asks for.

Repo rules apply: TDD (failing test first), stdlib + click only, agent-facing
surfaces (verb `--help`, `CLAUDE.md`, `docs/CLI.md`, `docs/helix-protocol.md`)
updated in the same change, deferred work filed in `docs/BACKLOG.md` as a
numbered entry (never a TODO comment).

### Hardware/environment facts for this run

- The Stadium is reachable and persisted; `helixgen device info` works with no
  `--ip` (a Stadium XL, fw 1.3.2 build 1340). If it is NOT reachable, STOP:
  do not stub, fake, or skip your way to a green run — leave the task unchecked
  and report.
- Run live tests with the system interpreter, which has `click`, `pyzmq`,
  `pytest` and `pytest-xdist` available:
  `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python3 -m pytest -m "live and ..." tests/live`
  (the live suite forces itself serial regardless of `-n auto`).
- The live suite's own safety model (`tests/live/conftest.py`) redirects ALL
  local helixgen state to a scratch dir, takes an upfront `device backup`,
  prefixes every artifact `HGTEST`, and fails the session if device state or the
  real `~/.helixgen` files changed. Read that docstring before touching fixtures.
- **Device locks:** the live suite's `cli` fixture takes the REAL `all` lease
  itself. So do NOT hold a session lease while running `tests/live`, or the
  suite will block and fail. For manual/exploratory device steps (Task 3 only)
  take `helixgen device lock --scope all --label "ralphex:hw-validation"` and
  **release it with `helixgen device unlock` before any pytest run**.
- Device writes are preapproved for test runs, but keep to `HGTEST`-prefixed
  expendable artifacts and never leave the device in a broken state.

### Task 1: housekeeping — retire the completed #92 plan file

- [ ] `docs/plans/2026-07-18-sync-stale-hash.md` has all 11 checkboxes ticked and
      #92 shipped in core 0.29.0, but the file was never moved on `main`. Move it
      to `docs/plans/completed/` (plain `git mv`, no other change)

### Task 2: Baseline live run with a CLEAN edit buffer

- [ ] Confirm the device answers: `helixgen device info` (no `--ip`) and record
      model/firmware in the findings doc created in Task 5
- [ ] Run `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python3 -m pytest -m "live and (device_write or device_ir or setlists or sync)" tests/live -q`
      with the device's active preset freshly loaded (clean buffer, `hist=0`)
- [ ] Record the full result (pass/fail counts, any xfail/xpass, any error text)
      verbatim into the findings doc. Expected: everything passes, **no xfails**
- [ ] Any failure here is a real finding: diagnose it before moving on. Fix it
      under TDD (offline failing test first where the bug is offline-reachable),
      or — if it is device behavior rather than a code defect — file it in
      `docs/BACKLOG.md` as a numbered entry and note it in the findings doc

### Task 3: The #38 condition — creates against a DIRTY edit buffer

This is the condition that used to fail. A one-off manual run is not enough:
the suite's own `device load` calls reset the buffer to clean, so the dirty
state must be established *inside* the test that needs it.

- [ ] Write a failing-first live regression test in `tests/live/test_device_write.py`
      (marker `device_write`) that: loads an `HGTEST` preset, dirties the edit
      buffer via a live-ops mutation of the ACTIVE tone (e.g. `device set-param`
      or `device bypass` — do **not** save), asserts the buffer is dirty by
      whatever signal the client exposes, then performs a `device install` /
      `create` into an expendable slot and asserts the content is **present on
      the device afterwards** (confirm by re-list, the 0.30.0 contract), with
      full `HGTEST` teardown in a finalizer
- [ ] Add the equivalent dirty-buffer case for the IR path in
      `tests/live/test_device_ir.py` (marker `device_ir`) if the push-IR path can
      hit `/CreateContent` under a dirty buffer; if it structurally cannot, say so
      in the findings doc and skip rather than inventing coverage
- [ ] Run `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python3 -m pytest -m "live and (device_write or device_ir)" tests/live -q`
      and record the result verbatim
- [ ] Confirm the non-zero `/status` taxonomy beyond `1` is still uncatalogued
      (`docs/helix-protocol.md` ~765-767). If this run observes any value other
      than `0`/`1`, record it in the findings doc and update the protocol doc
- [ ] Leave the device tidy: teardown removed every `HGTEST` artifact, and the
      session state guard passed. Release any lock you took (`helixgen device unlock`)

### Task 4: #7 — reorder → sync read-back on an expendable setlist

- [ ] Strengthen `tests/live/test_sync.py::test_sync_lifecycle` (failing first
      against current behavior if the read-back is wrong): after the device-side
      `device reorder`, and again after `device slots reorder --to 0 --setlist
      HGTEST…` + `sync`, read the device back (`device list --setlist <HGTEST
      setlist> --json`, or the equivalent verb that exposes reference order) and
      assert the setlist's reference order matches the manifest's membership
      order for the two `HGTEST` tones — not merely that the verbs exited 0
- [ ] Run `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python3 -m pytest -m "live and sync" tests/live -q`
      and record the result verbatim
- [ ] If the device order does NOT follow manifest order, that is a real bug:
      diagnose, fix under TDD (offline test first where reachable), re-run live.
      If the mismatch turns out to be device-side semantics helixgen cannot
      control, document it in `docs/CLI.md` under `slots reorder` / `sync` and
      file a numbered `docs/BACKLOG.md` entry instead of forcing the assertion

### Task 5: Findings doc, backlog, and agent-facing surfaces

- [ ] Write `docs/superpowers/specs/2026-07-27-hw-validation-38-fix.md`: what was
      run, the exact commands, verbatim results, every observation about
      `/CreateContent` status semantics, and anything deferred
- [ ] `docs/BACKLOG.md`: close **#90** (replace with a one-line shipped note
      pointing at the findings doc) and close the **#7** residual — or, if either
      could not be fully validated, rewrite the entry to say precisely what
      remains and why. Do not mark anything validated that was not observed
- [ ] Update agent-facing surfaces only if behavior/contract changed:
      `docs/helix-protocol.md` (status taxonomy), `docs/CLI.md`, `CLAUDE.md`,
      affected verb `--help`
- [ ] Run the full offline suite one final time and confirm green

## Validation Commands

Run from the repo root:

- `PYTHONPATH=$PWD/src python3 -m pytest` — full offline suite (golden-output
  contract, the 211-export round-trip acceptance test, and
  `tests/test_cli_parity.py` which pins the agent-facing `--help` contract).
  Live tests auto-skip without `HELIXGEN_LIVE=1`.

There is no separate lint/format/type-check step configured in this repo.

Live (REQUIRED for this plan — it is a hardware-validation plan; a green offline
suite alone does NOT satisfy any task above):

- `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python3 -m pytest -m "live and (device_write or device_ir or setlists or sync)" tests/live -q`
