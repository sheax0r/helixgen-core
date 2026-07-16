# Adversarial review — machine-local advisory device locks (0.22.0, PR #8)

Independent adversarial review of the workspace-#71 lock system
(2026-07-16), prompted to break the change: TOCTOU races, deadlocks,
scope-matrix holes, stale-break races, token concerns, portability,
tests/live regressions. 14 findings; disposition below. Every CONFIRMED
finding came with a runnable repro; the fixes each carry a regression test
in `tests/test_locks.py` (the "adversarial-review regressions" section).

## Findings and disposition

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | CRITICAL | Infinite 100%-CPU livelock acquiring a scope covered by your OWN stale/expired lease (step 2 skipped owned leases without breaking them; step 3's lost-create `continue` never hit the deadline check). | **FIXED**: an owned expired/nearly-expired lease on the *target* scope is cleared and re-acquired fresh; every loop path now sleeps and honors the deadline. Reviewer's repro now completes in 0.0 s. |
| 2 | MAJOR | Cross-file TOCTOU: `all` and a granular scope could both be acquired (the pre-create conflict scan and the create are not one atomic step). | **FIXED**: post-create verification — after creating its lease, an acquirer re-scans the conflicting files; on a live foreign conflict the deterministically YOUNGER `(acquired_at, nonce)` lease backs off (both racers run the same rule, so exactly one proceeds). Regression test forces the interleaving with a barrier + two distinct foreign identities. Residual: a mixed-version race against a pre-0.22.0 helixgen that never verifies could still double-hold (backlog #72). |
| 3 | MAJOR | Two waiters both "breaking" the same stale lease: the slower one unlinked the faster one's fresh live lease. | **FIXED**: stale-breaks are serialized through a `<scope>.lock.break` mutex file (O_EXCL; self-cleans after 10 s if a breaker crashed) and the stale decision is re-verified UNDER the mutex — a live lease found there is left alone. |
| 4 | MAJOR | A session lock taken via a short-lived wrapper (script/make/`sh -c`) records a pid that dies immediately → the lease read as stale within a second and protected nothing. | **FIXED (mitigated)**: session leases get `SESSION_PID_GRACE_S` (120 s from last acquisition/renewal) before pid-death counts as stale — covered verbs renew, so an ACTIVE wrapper-based session survives indefinitely; an idle one is reclaimable after the grace (which also caps the damage of a crashed suite's leftover lease). Documented loudly in `device lock`'s output + docs/CLI.md ("run it from your long-lived shell"). |
| 5 | MAJOR | Passthrough `_renew` at the TTL boundary could resurrect an expired lease on top of a waiter's legitimate re-acquisition. | **FIXED**: leases within `RENEW_MARGIN_S` (2 s) of expiry are never renewed in place — they are re-acquired fresh (waiters may only break *after* expiry, so a renewal with >2 s margin cannot land on a legally re-acquired lease). Residual micro-window if a process stalls >2 s between check and write (backlog #72). |
| 6 | MAJOR | `os.kill(pid, 0)` on Windows TERMINATES the probed process — staleness checks would kill lease holders. | **FIXED**: pid-liveness probing is disabled on `win32` (treat as alive; TTL staleness only) and the POSIX-first claim is documented. Full Windows validation deferred (backlog #72). |
| 7 | MINOR | Passthrough `device lock` printed a fresh token NOT stored anywhere, claimed `locked`, and silently ignored `--ttl`/`--label`. | **FIXED**: new `locks.session_lock` engine — re-locking an owned scope renews in place with the new label/ttl, reports `renewed`, and always prints the lease's STORED token. |
| 8 | MINOR | Scope hole: `device install --auto-irs` uploads device IRs holding only `library`. | **FIXED**: `install --auto-irs` now takes `library`+`irs` (mirrors `sync`); docs table updated. |
| 9 | MINOR | A field-less `{}` lease was permanently unreclaimable AND crashed `device lock --status` (human format). | **FIXED**: structurally invalid leases are treated like corrupt files (blocking synthetic lease, reclaimable after the 30 s grace); status rendering guards missing fields. |
| 10 | MINOR | `unlock --scope library --scope all` released only `all` (the acquisition-side `all`-collapse normalization applied to release). | **FIXED**: release validates but never collapses the explicit scope list. |
| 11 | MINOR | Lease files were 0644 with the token in plaintext (local users could read + pass through). | **FIXED**: lease files (and the tmp/renewal files) are created 0600. |
| 12 | NIT | `--ttl 0` = "never TTL-expires" was undocumented. | **FIXED**: documented in `--ttl` help + docs/CLI.md. |
| 13 | NIT | `read_lease` mapped permission-`OSError` to `None` ("no lease") — an unreadable foreign lease was acquired over. | **FIXED**: a permission-denied lease is surfaced as a blocking synthetic lease that never self-stales ("never break what you can't verify"). |
| 14 | NIT | IP sanitization can collide (`192.168.4.84` vs `192_168_4_84` share a dir). | **DEFERRED** (backlog #72) — collision direction is over-locking (safe). |

## Verified non-findings (reviewer's due diligence)

Scope matrix complete (every mutating verb decorated; `when=` kwarg names
match); no `ctx.invoke` double-acquire; multi-scope acquisition globally
sorted (no ABBA deadlock); `--no-lock` present on all wrapped verbs; live
conftest teardown ordering correct (state guard before unlock; lock-acquire
failure fails the session cleanly; skip-mode never locks); fail-fast /
backoff / NaN-timeout handling sane; O_EXCL-fallback partial writes covered
by the corrupt-lease grace.

## Residuals

Backlog **#72** carries: Windows portability validation, ip-sanitization
collisions, the `LeaseSet.release` read→unlink micro-window when a verb
outlives its own 900 s auto-TTL, the renewal margin's stall window, and the
mixed-version post-create verification caveat.
