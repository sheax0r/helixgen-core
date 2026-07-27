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

### Characterization (2026-07-27, fw 1.3.2 b1340): `/CreateContent` at an occupied posi INSERTS

Probed with `HGTEST` artifacts in the pool (`-2`), all replies verbatim
(`/status [reqid, newCid, code]`; code 1 throughout = the #38 dirty-buffer
flag, the active preset carried unsaved edits):

1. Create `HGTEST94-A` at **empty** posi 66 → reply `cid 1512 code 1`;
   listed `{cid_: 1512, name: HGTEST94-A, posi: 66}`.
2. Create `HGTEST94-B` at **occupied** posi 66 → reply `cid 1513 code 1`;
   listed: `{cid_: 1513, HGTEST94-B, posi: 66}`, `{cid_: 1512, HGTEST94-A,
   posi: 67}` — **inserted at the requested posi, incumbent shifted +1**.
3. Create `HGTEST94-A` (duplicate name) at occupied posi 66 → reply
   `cid 1514 code 1`; listed 1514@66, 1513@67, 1512@68 — same insert, name
   collisions permitted.
4. Mid-pool: create `HGTEST94-MID` at occupied posi 10 (66-row pool) →
   reply `cid 1515 code 1`; listed 1515@10 and **every** subsequent preset
   (old 10..65) shifted to 11..66.

So the device **never refuses, never overwrites, never relocates** the new
entry: it inserts at the requested posi and shifts the incumbent and all
subsequent entries down one. The reply cid matched the listed cid in every
probe. Two adjacent facts, needed to clean up after a probe:
`/RemoveContent` does NOT shift back — it leaves a **gap** at the deleted
posi — and a block `/ReorderContainerContent` (all shifted cids, target
posi) restores a contiguous pool in one op.

### What that means for the #38-era assumptions

The feared "write into a pre-existing occupant" can NOT happen when the
create lands: the fresh stub always sits at the requested posi and the
same-name incumbent has been shifted away, so `_confirm_created`'s
`(name, posi)` match finds OUR stub. The danger window is exactly: the
create **drops entirely** AND a same-name incumbent holds the posi — then
the confirming re-list matches the incumbent and `_set_content_data` would
overwrite a preset helixgen never created. The backlog entry's preferred
fix (pre-create cid snapshot) targets precisely that window.

### Fix

`_create_attributed` (new, shared by `_push_to_slot` and
`_save_edit_buffer_to`): when the caller did NOT precheck the slot empty
(`slots restore --force`; `install_into_pool` with a caller-supplied pos),
the container's cids are snapshotted (strict — a listing timeout raises
BEFORE the create) and a confirmed cid found in the snapshot raises instead
of writing: since a landed create always allocates a new cid, a
pre-existing cid means the create did not land and the match is the
incumbent. The prechecked paths are unchanged and pay no extra listing.

Consequence for the failed-write residue: a cid the snapshot proved fresh
is **attributably ours**, so a failed unprechecked write now deletes that
exact cid (previously it warned "may predate this call" and orphaned the
stub). The residue that cannot be cleaned is the gate-refusal case where
the dropped create lands late: an empty stub named like the tone may then
appear at the posi (shifting the occupant down one) after we already
raised. Documented in the error message and `docs/CLI.md` (re-list, delete
the stub, retry) — it is unobservable at raise time.

### Verification

Offline regression: `tests/test_device_client.py` —
`test_push_to_slot_unprechecked_refuses_preexisting_cid`,
`test_push_to_slot_unprechecked_accepts_fresh_cid_after_insert`,
`test_push_to_slot_unprechecked_failed_write_deletes_attributed_stub`,
`test_push_to_slot_aborts_before_create_on_snapshot_failure`,
`test_save_edit_buffer_to_unprechecked_refuses_preexisting_cid`.
Live (`device_write` marker, both runs green on the hardware above):
`tests/live/test_device_write.py::
test_force_create_at_occupied_posi_inserts_and_attributes` — stages an
incumbent at the pool tail, runs the unprechecked path at its posi, and
asserts fresh-cid insert at the requested posi + incumbent shifted +1, with
full `HGTEST` teardown.

`#69` note: the "what does the device do with an occupied posi" gap this
closes is the **pool** half. The setlist half (two references stacked at
one position) stays uncharacterized and refused (Task 5 revisits #69).

## #96 — `code == 0` reply-cid trust

(To be filled by Task 4.)
