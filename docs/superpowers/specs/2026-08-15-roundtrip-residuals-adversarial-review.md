# Round-trip residual cluster (hgc-cd2 / xh3 / ikp / 3yc + 5us / rq3) — adversarial review record (2026-08-15)

Two independent review subagents prompted to *break* the branch
(`fix/hgc-xh3-ikp-cd2-3yc`, 7 commits on `origin/main` @ `9714dab`). One was
scoped to the diff and its invariants; one was scoped to a single question —
**could this write device content a real Stadium would reject or play wrong?**
— with the 66 Line 6 factory `.sbe` blobs as ground truth.

Verdict: **1 blocker, fixed in-branch. 1 high + 3 medium/low deferred as
beads**, none of them regressions introduced here.

The blocker is the important one: it was a claim the branch asserted as
measured fact, in code, in a docstring, in a test and in a commit message —
and it was backwards. The round-trip oracle could not have caught it.

## Findings

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | **BLOCKER** | `ctm_.ptid` is keyed `eID_ << 16 \| slot << 8 \| pid_`, **not** `eID_ << 16 \| pid_`. 889 of the 890 ptid entries across the 66 blobs decode to the slot-bearing form (the 1 miss is a stale key the device left in `55-14D`), and **all 11 device-written `slot != 0` targets are registered in ptid** under it. hgc-3yc claimed the opposite — that slot-N targets are excluded because the key "has no room for a second slot" — and deliberately skipped them. The original measurement looked them up with the *wrong* key, found nothing, and inverted the conclusion. Worse than a missing registration: with the slot byte dropped, a dual-cab B-slot target would have been filed under the A slot's key and collided with it. | Fixed. `transcode._ptid_key` is the single place that packs the key; all four emit sites go through `_register_ptid`. Verified directly: 794/794 emitted keys decode to the device's formula, 10/10 emitted slot-1 targets registered. Docstring, test and commit message corrected. |
| 2 | HIGH | An invalid snapshot (`vald: False`) carries an **empty** `tamv`. `untranscode._Cg` requires a value in all 8 snapshots and otherwise drops the target, so on the 16 presets that have one, **`ctm_.stid` comes back empty and the `.hsp` carries no snapshot arrays at all** (244 targets; `02-1C` 9→0, `05-2B` 16→0, `14-4C` 19→0). Re-installing any of them makes the snapshot footswitches do nothing. | Deferred: **hgc-oqd** (P1). Pre-existing, already reported on stderr (it is the 244 "missing from some snapshots' tamv" warnings), and *symmetric* — so all 66 are still a fixed point with it present. It is the largest remaining reverse-transcoder loss and it means "restores fidelity" overstates this branch by 16 presets. |
| 3 | MEDIUM | Scribble label became **completely unbounded** — `_synth_pm` emitted `str(...)` with no cap and `mutate` only warns. The corpus proves 16 chars is safe and proves nothing above it. | Fixed. Clipped at `controllers.FS_LABEL_STORED_MAX` (16, the longest any real blob attests). 13-to-16 still survives, which is the hgc-cd2 fix; a 200-char label cannot reach hardware. A `None` label now emits `""` rather than the literal `"None"`. |
| 4 | MEDIUM | The ptid key gives `pid_` only 8 bits. No factory preset registers a pid > 255 (0 of 890), but the vocabulary goes far higher — `Agoura_AmpUSDoubleBlack` `VibTreb` is pid 1111, on an amp the tone skill favours — and packing it carries into the slot byte, binding a *different* slot and param. | Deferred: **hgc-3d1** (P3). `_register_ptid` skips the entry and warns rather than writing a known-corrupt key (the target still reaches `stid`, and the parm leaf still carries its `tid_`, which is how the device applies the value). Explicitly a judgement call, not evidence — resolving it needs a hardware capture. |
| 5 | MEDIUM | The device correlates a bypass target's `mmid` with block type: cab (`type 6`) bypass trgs are **77 without `mmid` / 0 with**; fx are 544 with / 61 without. The transcoder always emits `mmid`, so a snapshot-tracked cab bypass gets a shape absent from the corpus. Pre-existing, but hgc-xh3 multiplies how often it is emitted (173 cases). | Deferred: **hgc-ufu** (P3). The rule behind the correlation is not decoded offline. |
| 6 | LOW | A row-1 `InputNone`/`OutputNone` endpoint's snapshot bypass target is still dropped on read (`untranscode._flow_entry` skips the None pair): 22 `OUT1` + 19 `IN1` targets. Inaudible — a None endpoint passes no audio — but it means hgc-5us shipped the forward half only. | Deferred: **hgc-6av** (P4). |
| 7 | LOW | A param target on a `P35_OutputPath2A` (779) endpoint has no precedent — all 6 endpoint param targets in the corpus sit on model 783. Structurally plausible (779 has pid 1/2). | Accepted. Only reachable from a `.hsp` that records per-snapshot gain/pan on a b13 that is OutputPath2A; the alternative is dropping state the `.hsp` holds. |
| 8 | LOW | Emitted `vald: False` snapshots carry `name: "SNAPSHOT 5"` / `colr: 1`; the device writes `nil`. Pre-existing, cosmetic. | Accepted, no bead — subsumed by hgc-oqd, which has to touch the same emission. |
| — | note | None of `tests/test_untranscode.py:853/1002/1017` or `tests/test_transcode.py:26` run — there is no `tests/fixtures/device_content` corpus in a clean checkout, so the branch's central claim is unpinned by CI. | Accepted, pre-existing. Mitigated by the 6 new synthetic round-trip tests, which build their own device content via `recipe_to_sbepgsm` and need no fixture. |

## Ground truth the review established (all 66 blobs)

Worth keeping — several of these are the anchors the code now cites:

- **Constant `tamv` arrays are real**: 577 of 1170 arrays are constant, across 50/66 presets. Confirms hgc-xh3.
- **`ptid` key** = `eID_ << 16 | slot << 8 | pid_` (889/890).
- **Bypass (`type 1`) targets on non-user blocks carry no `mmid`** — exactly `('eID_','enty','id__','pid_','slot','type')`, 161/161 (OUT0 45, IN0 31, OUT1 25, IN1 21, SPLIT 20, JOIN 19). Confirms what hgc-5us/rq3 emit.
- **Param (`type 2`) targets** on endpoints/split/join carry `mmid`/`pmid`/`ppid` and are all in `ptid`.
- **`tid_` without `stid`** occurs 425 times — all with `snap: False`. Confirms the #24 controller-only binding convention.
- **Longest stored scribble label is 16** (`"Parallel Reverbs"` 38-10C, `"Ampeg Opto Comps"` 63-16D). Confirms hgc-cd2 and bounds the fix.
- **`bmap[gridpos] == id__` holds for all 1382 corpus blocks** — the identity permutation helixgen assigns is the right one, not a shortcut.
- Row-0 outputs: 91 `P35_OutputMatrix` + 41 `P35_OutputPath2A` across 132 flows; `bcnt` always 28. Confirms hgc-ikp.

## Why the oracle missed the blocker

`.sbe -> .hsp -> .sbe` compares the **`.hsp`**, and `untranscode` reads targets
from `ctm_.stid` — it never looks at `ptid`. A wrong ptid key is therefore
invisible to the fixed-point measurement: all 66 presets were a fixed point
before and after the fix.

The lesson is narrower than "the oracle is weak": **a fixed point proves the
two directions agree with each other, not that either agrees with the
device.** Anything the reverse path does not read is unverified by it, and has
to be checked against real blobs directly. The three checks that would have
caught this — decode the corpus's own `ptid` and solve for the key, diff
emitted `cg__` against the original field by field, and count how many emitted
structures have no counterpart anywhere in the corpus — are cheap, and are now
what the second reviewer's brief asks for.
