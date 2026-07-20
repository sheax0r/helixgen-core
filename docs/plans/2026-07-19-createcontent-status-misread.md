# Plan: /CreateContent status misread — stop destroying writes that succeeded

## Context

Implements `docs/BACKLOG.md` #38 (finally root-caused) and #30 (partial —
loud-fail only). Supersedes the "transient / load-correlated / power-cycle"
theory recorded in `docs/BACKLOG.md:1097-1121` and
`docs/superpowers/specs/2026-07-15-createcontent-status1-findings.md`; that
spec's "Remaining open question" is now answered and both should be updated
to point here.

**Root cause, established by live A/B against a Stadium XL (fw 1.3.2/1340)
on 2026-07-19.** Field 3 of the `/status` reply to `/CreateContent` is NOT
an error code. It tracks the device's edit-buffer dirty flag (`hist` in
`/EditBufferStateGet`). Evidence:

- With `hist=1` (active preset has unsaved edits), `/CreateContent` returns
  `/status [rid, newCid, 1]` — but the content **is created, at the exact
  requested `posi`**. Verified by the `/addContent` frame on the 2001 PUB
  stream and by the row surviving in `list_container(-2)`.
- With `hist=0` (any preset freshly loaded/saved), three consecutive creates
  returned `/status [rid, newCid, 0]`. Same code path, same payload, only
  the tone changed.
- The user's original report: 9 consecutive "failures" over ~40 minutes
  parked on an edited tone, then immediate success after loading an empty
  tone. A power-cycle did NOT help (the same dirty state was reloaded).
- Ruled out with evidence: not capacity (pool 65/512 and setlists 4/4 both
  refused), not `/getCloneLockState` (byte-identical in both states), not
  slot occupancy (a genuinely free `posi` still returned 1), not blob
  chunking (the IR listing is one 2298-byte frame).

Current behaviour is therefore **data-destroying**: `client.py:1126` treats
non-zero as failure, `_create_status_error` (`client.py:1205`) calls
`_delete_created_stub` (`client.py:1128`) which deletes the content that was
just correctly written, then raises. Whether the user is left with an orphan
or a silently-deleted preset depends only on whether the container listing
happened to be fresh enough for the cleanup to match.

Do NOT "fix" this by treating `1` as success. The non-zero taxonomy is still
uncatalogued (`docs/helix-protocol.md:765-767`) and a blanket ignore just
moves the bug. Verify the write by re-listing — which the install path
already does anyway to recover the true cid.

The same stale-container-index mechanism is the cause of the reported
`device list-irs` under-report (Task 4).

Repo rules apply: TDD (failing test first), stdlib + click only,
agent-facing surfaces updated in the same change.

**No live-device work in this plan.** The root cause was established by
direct live A/B before this plan was written (see Context) and the user
considers it sufficiently validated. Everything below is offline-testable
against the fake/injected socket. No device lock lease is needed; do not run
`tests/live/` as part of this work.

### Task 1: Verify `/CreateContent` by re-list instead of trusting field 3

- [x] Write failing tests first, against the fake/injected socket used by the
      existing `tests/test_device_client*.py` patterns:
  - `/status [rid, cid, 1]` **plus** the content present in the follow-up
    container listing → treated as SUCCESS, returns the cid, deletes nothing
  - `/status [rid, cid, 0]` with content present → SUCCESS (unchanged)
  - `/status [rid, cid, 1]` with content genuinely ABSENT from the listing →
    still an error, and only then may cleanup run
  - the re-list is what determines the cid (the create-reply cid stays
    documented-unreliable)
- [x] Change `_create_content_status` / `_create_content`
      (`src/helixgen/device/client.py:1085-1126`) and its three callers
      (`install_into_pool` :1340, `_save_edit_buffer_to` :1172,
      `create_setlist` :1400) to confirm by re-list before declaring failure
- [x] Keep a bounded settle/retry around the confirming re-list — the
      container index is known to lag (Task 5 covers the same lag)

### Task 2: Stop the destructive cleanup on a write that landed

- [x] Failing test: `_delete_created_stub` must NOT be reachable when the
      confirming re-list found the content
- [x] Restrict `_delete_created_stub` (`client.py:1128`) to the genuine
      not-created case
- [x] Failing test for the silent-no-op hazard: when cleanup runs but matches
      nothing (stale listing), that must be reported, not swallowed — the
      current silence is why orphan accounting looked clean

### Task 3: Fix the error message (the power-cycle advice is wrong)

- [x] Failing test pinning the new text (see `tests/test_cli_parity.py` for
      the message-contract pattern)
- [x] Rewrite `_create_status_error` (`client.py:1231-1234`). Drop
      "power-cycle the Helix and retry" — it demonstrably does not help.
      Point at the real precondition: the active preset has unsaved edits;
      save or reload it in the Helix app (or on the unit) and retry
- [x] Update `docs/BACKLOG.md` #38 to root-caused, and correct the recurrence
      section at :1097-1121 plus the 2026-07-15 findings spec

### Task 4: `device list-irs` — stale container index

- [x] Failing test: a container listing that lags a just-completed write must
      not be reported as authoritative
- [x] Reported symptom: `list-irs` returned exactly 24 entries for minutes
      after an IR upload, omitting a genuinely-present IR, while
      `ir_path_for_hash` (`client.py:528-546`) correctly resolved it.
      Confirmed as index lag, NOT truncation — the reply is a single
      2298-byte frame and later read back 25 entries
- [x] Make `list_irs` (`client.py:507`) either subscribe/settle before
      reading (the `mutating()` mechanism at :1237 exists precisely because
      the device only propagates promptly to a subscribed client) or
      cross-check against the authoritative point lookup
- [x] `device list-irs` (`cli_device.py:1786`) currently passes
      `strict=False`, so a partial read is silent. Make partial/stale reads
      loud

### Task 5: `device add --slot` — fail loudly (core #30)

- [ ] Failing test: `device add --slot 20A` raises, `--slot auto` and the
      bare form still work
- [ ] `mark_on_device` (`manifest.py:458-467`) persists the label but sync
      never converts it to a device address — `install_into_pool` is called
      with no `pos` (`setlist_sync.py:519`), so `_lowest_empty_posi` wins.
      The flag reports success and changes nothing
- [ ] Reject explicit labels in `device add` (`cli_device.py:2568-2582`) with
      a message naming #30. Do NOT implement real placement here — that
      needs the real-occupancy fetch (`setlist_sync.py:446` passes
      `occupied=set()`) and the pool-posi-vs-setlist-address decision that
      #30 reserves for the user
- [ ] Update the `--slot` help text and the `docs/CLI.md` entry in the same
      commit (`tests/test_cli_parity.py` pins this)

### Task 6: Correct the protocol reference

- [ ] `docs/helix-protocol.md` lists command spellings the device rejects.
      Verified live: `/ProductInfoGet` and `/EditBufferStateGet` work;
      `/getProductInfo` and `/getEditBufferState` return
      `Msg dispatch failed: ... is NOT known!!!`. `/getCloneLockState` is
      correct as written (`/GetCloneLockState` is rejected). Note that
      `/ProductInfoGet` *replies* on address `/getProductInfo`
- [ ] Document field 3 of the `/CreateContent` `/status` reply as an
      edit-buffer-dirty indicator, not an error code, and mark the rest of
      the non-zero taxonomy still uncatalogued (:765-767)

### Task 7: Unmask the live suite, correct CLAUDE.md, release

**Do not run the live suite in this plan** — edit the markers only. The live
run is deferred to the next hardware session (file it as a backlog entry).

- [ ] The live suite papers over this bug with cooldown-retry + `xfail`
      (`tests/live/conftest.py:541,547,591`,
      `tests/live/test_device_write.py:129`,
      `tests/live/test_device_ir.py:62`). Remove those xfails — they were
      masking a real, reproducible, data-destroying bug. Leaving them in
      would hide the regression this plan is meant to prevent
- [ ] Add a live-suite setup note: the device_write/device_ir cases should be
      exercised with the active preset **deliberately left dirty**, the exact
      condition that used to fail
- [ ] `CLAUDE.md` "Device-write awareness" currently tells agents to "expect
      #38 /CreateContent flakiness (re-run)". That advice is now wrong —
      re-running was never the fix and the writes were landing. Correct it
- [ ] Bump version, tag `vX.Y.Z` — **minor**, not patch: this changes write
      semantics
- [ ] File a backlog entry for the deferred live validation
- [ ] Move this plan to `docs/plans/completed/`

## Validation Commands

Run from the repo root:

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (includes the
  golden-output contract, the 211-export round-trip acceptance test, and
  `tests/test_cli_parity.py`, which pins the agent-facing `--help`
  contract). Live tests under `tests/live/` auto-skip without
  `HELIXGEN_LIVE=1`.

There is no separate lint/format/type-check step configured in this repo.

**No live-device validation in this plan.** Do not set `HELIXGEN_LIVE=1`.
The root cause was already confirmed by direct live A/B on 2026-07-19 and the
user considers it sufficiently validated; the full live run is deferred.
