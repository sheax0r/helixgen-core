# Plan: `/CreateContent` + wedged-IR follow-ups (#93, #94, #96, #95)

## Context

Implements the four follow-ups filed off the 0.30.0 #38 fix:
`docs/BACKLOG.md` **#93** (a wedged IR now reads as present, so nothing
re-uploads it), **#94** (`--force` still resolves the write target by an
ambiguous `(name, pos)` match), **#96** (`_create_content_checked`'s `code == 0`
fast path returns the unverified create-reply cid), and **#95** (the
`_save_edit_buffer_to` stub-cleanup gate — pure code, no hardware, folded in
because it lives in the same `_push_to_slot` area).

Three of these are **characterization-first**: #93 needs hardware to reproduce a
wedged IR, #94 needs hardware to establish what the device actually does with a
`/CreateContent` aimed at an occupied `posi` (the same uncatalogued behavior as
#69), #96 needs hardware to establish whether a code-0 create's reply cid can
ever be wrong. **Characterize first, then implement** — each backlog entry names
a preferred fix, but do not implement it against a guess. If the hardware says
the premise is wrong, say so and file the correction instead.

Run this plan only after `docs/plans/2026-07-27-hw-validation-live-suite.md` has
landed — it re-establishes that the live suite is honest on hardware, and both
plans take device locks (they must never run concurrently).

Repo rules apply: TDD (failing test first), stdlib + click only, agent-facing
surfaces (verb `--help`, `CLAUDE.md`, `docs/CLI.md`, `docs/helix-protocol.md`)
updated in the same change, deferred work filed as a numbered `docs/BACKLOG.md`
entry (never a TODO comment).

### Hardware/environment facts for this run

- The Stadium is reachable and persisted; `helixgen device info` works with no
  `--ip`. If it is NOT reachable, STOP — do not fake, stub, or skip your way to
  a green run. Leave the task unchecked and report.
- Live tests: `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python3 -m pytest -m "live and <marker>" tests/live -q`
  (system `python3` has `click`, `pyzmq`, `pytest`, `pytest-xdist`).
- **Device locks:** `tests/live`'s `cli` fixture takes the REAL `all` lease
  itself, so do NOT hold a session lease while running pytest. For exploratory
  device probing take `helixgen device lock --scope all --label
  "ralphex:createcontent-followups"` and release it (`helixgen device unlock`)
  before any pytest run.
- Every device artifact you create by hand gets an `HGTEST` prefix and is torn
  down, including on failure. Take a `helixgen device backup` before the first
  exploratory write. Device writes are preapproved for test runs; never leave
  the device broken.

### Task 1: #95 — make `_save_edit_buffer_to`'s stub cleanup opt-in (offline)

Pure code, no hardware. Do this first so the rest of the plan works on top.

- [x] Failing test first: `_save_edit_buffer_to` currently deletes the created
      stub unconditionally on a failed `/SavePresetWithCID`, while `_push_to_slot`
      requires an explicit `prechecked_empty` grant before `_delete_created_stub`
      runs. Pin the asymmetry as a test
- [x] Give `_save_edit_buffer_to` the same opt-in flag; update its one caller
      (`device save`, which prechecks strictly under a subscription) to pass it
- [x] Update agent-facing surfaces if any user-visible behavior changed
      (none: `device save` passes `prechecked_empty=True`, so its cleanup
      behavior is byte-identical; only the internal `_raw` default flipped to
      the safe opt-in — no help text or docs described the old unconditional
      cleanup)

### Task 2: #93 — a wedged IR must not read as present

- [x] Reproduce a wedged IR on hardware (backing file + path index resolve, no
      `-11` registry entry — the state `delete-ir --force-wedge` exists to clean).
      Use an `HGTEST`-named IR. Record the exact reproduction steps and the
      observed listing/point-lookup outputs in the findings doc (Task 5)
- [x] Confirm the reported failure mode: with 0.30.0's
      `device_ir_hashes(verify=...)`, the auto-upload paths (`install --auto-irs`,
      `sync`'s IR upload, `sync_preset_irs`) **skip** the wedged IR and the
      preset's cab stays silent with no error
- [x] Failing test first (offline, against the fake/injected socket), then
      implement the distinction: "resolves but absent from `-11`" (wedged → still
      needs upload) vs "absent from `-11` because the container index lags"
      (present → skip). Do not regress the lag case — its false "missing" is the
      commoner and more misleading one, which is why the 0.30.0 trade was made
- [x] Verify live: re-run the wedge reproduction and confirm the auto-upload path
      now self-heals it. Add live coverage under the `device_ir` marker with full
      `HGTEST` teardown (`delete-ir --force-wedge` is the CLI's own remedy)
- [x] Update the stderr warning wording, `docs/CLI.md`, and `CLAUDE.md`'s
      wedged-IR paragraph to match the new behavior

### Task 3: #94 — characterize, then de-ambiguate the `--force` write target

- [ ] **Characterize on hardware first:** what does the device do with a
      `/CreateContent` aimed at an **occupied** `posi`? (The same uncatalogued
      behavior #69 left open.) Probe with `HGTEST` artifacts: does it create at
      the requested position, relocate, refuse, or overwrite? Record the raw
      replies verbatim in the findings doc
- [ ] Failing test first, then implement the entry's preferred fix if
      characterization supports it: snapshot the container's cids before
      `/CreateContent` and accept only a cid **absent** from that snapshot,
      raising rather than writing into a match that cannot be attributed
      (`_push_to_slot(prechecked_empty=False)`, the `slots restore --force` path)
- [ ] Decide and document the residue: a possibly-orphaned empty stub left at the
      same `posi` when the create landed separately. Either clean it up safely or
      state in `docs/CLI.md` why it is left, and file anything punted
- [ ] Verify live under the `device_write` marker; add regression coverage

### Task 4: #96 — characterize the `code == 0` reply cid, then close the asymmetry

- [ ] **Characterize on hardware first:** is the create-reply cid ever wrong when
      the status code is `0`? Probe repeatedly (clean and dirty edit buffer, empty
      and occupied targets), cross-checking each reply cid against a point
      `/GetContentRef`'s `name`/`posi`. Record verbatim
- [ ] Failing test first, then implement whichever option the evidence supports:
      (a) always confirm by re-list (costs one listing per create), or (b) cheaply
      cross-check the reply cid's `name`/`posi` via a point `/GetContentRef`
      before writing into it. Prefer (b) if it is sufficient — (a) costs a full
      pool listing on the common clean-buffer path
- [ ] If the evidence shows the reply cid is reliable at `code == 0`, do NOT
      churn the code: record the evidence, keep the fast path, and rewrite the
      backlog entry to say the asymmetry is characterized and accepted
- [ ] Verify live under the `device_write` marker

### Task 5: Findings doc, backlog, agent-facing surfaces, release

- [ ] Write `docs/superpowers/specs/2026-07-27-createcontent-followups.md`: the
      characterization results for #93/#94/#96 (verbatim device replies), what was
      implemented, and what was deferred
- [ ] `docs/BACKLOG.md`: close #93/#94/#95/#96 with one-line shipped notes
      pointing at the findings doc — or rewrite any entry that could not be
      closed to say precisely what remains and why. Also revisit **#69**, whose
      "what does the device do with an occupied `posi`" gap Task 3 characterizes
- [ ] Update `docs/helix-protocol.md` (`/CreateContent` semantics), `docs/CLI.md`,
      `CLAUDE.md` and affected verb `--help` for every behavior change
- [ ] Bump the version in `pyproject.toml` **and** `src/helixgen/__init__.py`
      together (minor bump — this changes device-write and IR-upload behavior),
      and note in the plan's final report that a plugin companion PR is needed
      for any CLI-visible change (the plugin pins core exactly, so it can only
      describe behavior in a **released** core version)
- [ ] Run the full offline suite and confirm green

## Validation Commands

Run from the repo root:

- `PYTHONPATH=$PWD/src python3 -m pytest` — full offline suite (golden-output
  contract, 211-export round-trip acceptance, `tests/test_cli_parity.py` pinning
  the `--help` contract). Live tests auto-skip without `HELIXGEN_LIVE=1`.

There is no separate lint/format/type-check step configured in this repo.

Live (REQUIRED for this plan — the characterization tasks are not satisfiable
offline):

- `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python3 -m pytest -m "live and (device_write or device_ir or sync)" tests/live -q`
