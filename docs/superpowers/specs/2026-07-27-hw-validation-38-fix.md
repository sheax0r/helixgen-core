# Hardware validation: 0.30.0 `/CreateContent` fix (#90) + slots-reorder→sync (#7)

Live-hardware validation run, 2026-07-27. Plan:
`docs/plans/2026-07-27-hw-validation-live-suite.md`.

## Device under test

```
model:     stadium (stadium_xl)
device id: 2490368
serial:    47292244582131381
firmware:  1.3.2  (build 1340 2026-04-13)
```

Reached with no `--ip` (persisted discover record). Before the baseline run a
stale `all` lease (`live-test-suite`, dead pid 39810 — an earlier live run
killed mid-flight) was auto-reclaimed after the 120 s dead-pid grace, and the
active preset (cid 1407, `Stoner Blues Board - Ibanez`, slot 16C) was reloaded
to guarantee a clean edit buffer (`hist=0`).

## Task 2: baseline live run, clean edit buffer

Command (all runs):

```
HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python3 -m pytest -m "live and (device_write or device_ir or setlists or sync)" tests/live -q
```

### Run 1 (pre-fix) — verbatim result

```
1 failed, 14 passed, 1 skipped, 58 deselected in 325.86s (0:05:25)
FAILED tests/live/test_device_ir.py::test_push_rename_pull_delete_ir
AssertionError: push-ir saw the /addContent broadcast but the -11 registry
listing never gained the entry within 20s. This is the backlog-#38 index-lag
regression: list-irs must settle/cross-check rather than report a stale
listing.
SKIPPED [1] tests/live/test_global_settings.py: global-settings writes are
extra-gated: set HELIXGEN_LIVE_GLOBAL=1 (in addition to HELIXGEN_LIVE=1)
```

A second identical run (after a first-pass fix attempt built on a wrong
hypothesis, see below) failed the same way:
`1 failed, 14 passed, 1 skipped ... in 323.89s`.

### Root cause: the `-11` listing cache ignores watched-dir imports

Manual reproduction with the suite's own deterministic HGTEST wav
(irhash `898c17bd7b550fb8ba2cacfde6b70361`) established, on this hardware:

- The push itself works perfectly: SFTP upload + 2001 subscription →
  `/observeWatchedDirChange` then `/addContent` broadcast in ~2 s, carrying
  the registered row (`cid_` 1464, later 1465, `posi` 26, correct hash).
- The registration is real and authoritative: `/GetContentRef <cid>` returns
  the full row, `/IrPathForHashGet` resolves the hash to
  `/data/stadium-family-fw/ir/HGTEST-ir.wav`.
- But `/GetContainerContents -11` kept returning the pre-import 26-row
  listing **indefinitely** — polled for 11+ minutes total (172 s continuous,
  then 60 s-interval polling), fresh connections and 2001-subscribed
  (`mutating()`) reads alike. The 0.30.0 "settle under a 2001 subscription"
  fix does NOT make the listing converge for watched-dir imports; that claim
  was developed offline and is falsified on this firmware.
- **An RPC content write invalidates the cache instantly.** A
  `/SetContentAttrs` rename of the new row — even to its **same name** —
  made the very next `/GetContainerContents -11` read complete (27 rows,
  hash present). A delete (`/RemoveContent`) likewise refreshed it. This is
  presumably why the editor never sees the problem: it maintains its model
  from `/addContent` deltas instead of re-listing.

So the historical "index lag" (2026-07-15) and, likely, part of the #93
"wedge" lore were this one device behavior: the `-11` container-listing cache
is only invalidated by RPC content writes, never by watched-dir imports. The
listing wasn't lagging toward convergence — it was never going to converge
until some unrelated write happened.

The earlier killed run (dead pid 39810) is what exposed it: with all
masking/xfails removed in 0.30.0, the strict test met the stale cache
head-on. (A first fix attempt — treating "point lookup resolves but unlisted"
as a wedged file to delete — was wrong: that state is the NORM for a fresh
import. It was reworked before landing; no wrong version was ever merged.)

### Fix (in this change, TDD offline-first)

`src/helixgen/device/sftp.py push_ir`:

- After `/addContent` confirms registration, capture the broadcast's `cid_`
  and issue a **same-name rename** of that cid — a no-op RPC write that
  forces the `-11` listing cache to refresh, so `list-irs` / `rename-ir` /
  `delete-ir` see the new IR immediately. The result dict now carries `cid`.
  Nudge is advisory (failure ignored); registration is real either way.
- The "already on device" short-circuit now distinguishes the two states the
  point lookup alone cannot: hash unlisted but present after a nudge
  (same-name rename of any listed row) = genuinely registered under a stale
  cache → "already"; still unlisted after the nudge = wedged orphan file
  (#93, e.g. a client killed between `/RemoveContent` and the file removal)
  → remove the orphan and import normally. A failed listing keeps the old
  trusting behavior (flaky transport must not be read as a wedge).

Offline tests: `tests/test_device_sftp.py` push_ir section (fresh-import
nudge + cid, already-listed, stale-listing-healed-by-nudge, wedge
remove+reimport, listing-failure-trusts). Written failing-first against the
pre-fix behavior.

Hardware validation of the nudge: fresh import left the listing at 26 rows;
one same-name rename of the new cid → 27 rows, hash present, next read.

### Run 3 (post-fix) — verbatim result

```
15 passed, 1 skipped, 58 deselected in 335.91s (0:05:35)
SKIPPED [1] tests/live/test_global_settings.py: global-settings writes are
extra-gated: set HELIXGEN_LIVE_GLOBAL=1 (in addition to HELIXGEN_LIVE=1)
```

No failures, no xfails/xpasses. The skip is the suite's own extra gate for
global-settings writes, not a masked failure. Device left tidy: 0 HGTEST IRs,
0 HGTEST setlists, no locks held; session state guard passed.

Surfaces updated in the same change: `docs/CLI.md` (`push-ir`),
`docs/helix-protocol.md` (listing-cache invalidation), `client.list_irs`
docstring, `tests/live/test_device_ir.py` comments.

## Task 3: dirty-buffer `/CreateContent` regression tests

(pending)

## Task 4: #7 reorder → sync read-back

(pending)

## `/CreateContent` status semantics observations

(pending — Task 3)

## Deferred

(none so far)
