# `/CreateContent` + wedged-IR follow-ups — characterization + findings (#93, #94, #95, #96)

Hardware: Helix Stadium (stadium_xl), serial 47292244582131381, fw 1.3.2
(build 1340 2026-04-13), at 192.168.4.84. Plan:
`docs/plans/2026-07-27-createcontent-followups.md`.

## #95 — `_save_edit_buffer_to` stub cleanup made opt-in (offline)

No hardware. `_save_edit_buffer_to` deleted the created stub unconditionally
on a failed `/SavePresetWithCID`; `_push_to_slot` requires an explicit
`prechecked_empty=True` grant before `_delete_created_stub` runs. The
asymmetry was pinned by test and the same opt-in flag added; the one caller
(`device save`, which prechecks strictly under a subscription) passes
`prechecked_empty=True`, so user-visible behavior is byte-identical.

## #93 — a wedged IR must not read as present

### Reproduction (2026-07-27, fw 1.3.2 b1340)

The wedged state = backing file + path index resolving, no `-11` registry
entry (a client killed between `/RemoveContent` and the file removal).
Reproduced deterministically:

1. `helixgen device push-ir HGTEST-wedge93.wav` → `imported + registered
   instantly: HGTEST-wedge93 (6f09479dcbaa8e1018cc674db055a511)`; listing row
   `{cid_: 1507, name: HGTEST-wedge93, posi: 26}`; `/IrPathForHashGet` →
   `/data/stadium-family-fw/ir/HGTEST-wedge93.wav`.
2. Registry delete only, no file removal
   (`maintenance.delete_device_ir(..., remove_file=False)`) →
   `{'ok': True, 'cid': 1507, 'file_removed': False}`.
3. Observed state: listing (strict, incl. unusable) back to 26 rows, no
   `HGTEST-wedge93` row; `/IrPathForHashGet` STILL resolves
   `/data/stadium-family-fw/ir/HGTEST-wedge93.wav`. That is the wedge.

Caveat: the device lazily garbage-collects the orphaned file after several
minutes (2026-07-14 observation), after which the path lookup stops resolving
and the wedge self-clears — the repro window is minutes, plenty for the live
test but not indefinite.

### Failure mode confirmed (0.30.0 behavior)

With the wedge in place:

- `device_ir_hashes(verify=[hash])` reported the wedged hash **present**,
  warning `the container index is stale; treating the IR as present (backlog
  #38)...` — the documented #38 trade.
- `bridge.check_irs` → `{'present': [6f0947…], 'missing': []}` — so every
  auto-upload path (`install --auto-irs`, `sync`'s IR upload,
  `sync_preset_irs`) skipped the IR and the cab would stay silent with no
  error.

### The discriminator, verified on hardware

`/RemoveContent` (an RPC content write) refreshes the `-11` listing cache, and
a same-name rename of any listed row refreshes it again on demand. On the
wedged device: nudged row `1159 "YA KW 412 M25 Mix 05"`, `rename ok: True`,
re-list → 26 rows, wedged hash **still absent**. So "absent from a
listing taken after a CONFIRMED refresh" separates the wedge from the lag
case (where the refreshed listing contains the hash) — exactly the check
`sftp.push_ir` already used for its own "already on device" verdict.

### Fix

Extracted `push_ir`'s nudged-listing check into
`client.confirm_ir_listed(client, hash_hex)` (tri-state collapsed to bool:
listed-after-refresh → True; CONFIRMED absent after a confirmed refresh →
False = wedged; anything unconfirmable — failed listing, no nudgeable row,
dropped rename reply, unusable listed row that could be the hash — → True,
trusting the point lookup as before). `device_ir_hashes(verify=...)` now runs
it whenever the point lookup overturns a listing absence: the lag case stays
present (no regression — its false "missing" is the commoner trap), a
confirmed wedge is reported **missing** so the auto-upload paths re-push it,
and `push_ir` itself (same helper) then detects the wedge, removes the
orphaned file, and re-imports — self-healing end to end.

### Live verification (post-fix, same hardware)

- Recreated the wedge; `device_ir_hashes(verify=[hash])` now reports it
  missing with the wedge warning; `sync_preset_irs`/`upload_missing_irs`
  re-pushed it: `push_ir` removed the orphan and re-imported; the IR ended
  registered AND listed. Live regression: `tests/live/test_device_ir.py::
  test_wedged_ir_reads_missing_and_auto_upload_heals` (marker `device_ir`,
  `HGTEST` artifact, torn down including on failure).

## #94 — `--force` write-target ambiguity

(To be filled by Task 3.)

## #96 — `code == 0` reply-cid trust

(To be filled by Task 4.)
