# Transcoder snapshot-bypass semantics — RE findings + fix review (2026-07-13)

## Symptom

The same tone ("Dream On - Aerosmith", 2 named snapshots, 3 footswitches)
behaved differently depending on how it reached the device:

- **helixgen-synced copy (slot 3A):** loads with **every block active** in the
  Lead snapshot; snapshot switching does not correctly apply per-scene bypass.
- **Stadium-app-imported copy (slot 2D):** correct — loads with the comp /
  second amp / first IR bypassed (Lead), toggles them per scene.

## Method

Pulled both presets off the device (`device pull`), decoded the `_sbepgsm`
blobs, and diffed the `cg__` snapshot machinery and `sfg_` block state. The
app-imported copy is device ground truth; every divergence below was verified
against it and reproduced from the source `.hsp`.

## Corrected device-content semantics (`_sbepgsm`)

These four rules were previously un-pinned (the snapshots synthesis design doc
said only "tamv value = the target's value in snapshot i") and the transcoder
guessed all four wrong:

1. **Block-level `enbl` is the base bypass.** `enbl=0` means the block loads
   bypassed. The model instance's `enbl` (inside `mdls[0]`) stays `1` even
   when the block is bypassed. The `.hsp` source of truth is
   `bNN["@enabled"]["value"]` (which, per the value==active invariant, mirrors
   snapshot 0).
2. **A bypass target's `tamv` value is BYPASS polarity** — `True` = the block
   is **bypassed** in that snapshot: the **inverse** of the `.hsp`
   `@enabled.snapshots` booleans (`True` = enabled).
3. **Snapshot-tracked entities are bound to their target.** A block whose
   bypass is snapshot-tracked carries `snap=True, tid_=<bypass trg id>` at
   block level; a snapshot-tracked param carries `snap=True, tid_=<param trg
   id>` on its parm leaf. Untracked entities carry `snap=False, tid_=0`. An
   **FS-only** bypass target (controller-driven, no snapshot variation) does
   **not** set the block's `tid_` — the binding is snapshot machinery, not
   controller machinery (`ctrl[]` wires controllers via its own `tid_`).
   The old synthesized scheme (`tid_ = id__` on every block) also **collided**
   with real target ids once snapshot targets existed (e.g. a delay block's
   `tid_` pointing at its own Mix *param* target).
4. **All 8 snapshots are real state, not padding.** The `.hsp`'s dense
   per-snapshot arrays (including the trailing `"Snap N"` slots, which hold
   the not-in-any-scene state) and the `"Snap N"` names are what the app's own
   import writes. The transcoder previously truncated to the *named* snapshots
   and padded 3..8 with the **last named snapshot's values** (so "Snap 3..8"
   were copies of "Clean").

Sparse legacy `@enabled.snapshots` arrays (`None` entries) fall back to the
base value — `bool(None)` had silently meant "enabled=False → bypassed".

## Fix

`bridge._snapshot_arrays` now emits device-polarity, full-length,
base-filled arrays; `bridge.hsp_to_paths` lifts `spec["enabled"]=False` from
`@enabled.value`; `bridge.hsp_snapshot_meta` returns all 8 snapshot metas;
`transcode._make_user_block` honors `spec["enabled"]`;
`transcode._canonical_flow` / `_make_structural_block` zero `tid_`;
`transcode._synth_cg_from_recipe` returns the tracked-target bindings and
`transcode._bind_snapshot_targets` stamps them onto the synthesized `sfg_`.

Offline validation: transcoding the dream-on `.hsp` with the fix reproduces
the app-imported blob's block `enbl`/`snap`/`tid_` state and all 8 `tamv`
rows/names/`exsw` exactly.

## Known, deliberate non-matches vs the app's import

- The app snapshot-tracks the **DSP-B input block** (a bypass target that
  never varies, `False` in every snapshot). Synthesizing it would be inert
  bookkeeping; skipped.
- `srcs[].byps` is `True` in helixgen output for FS bypass sources, `False`
  in the app's blob. FS behavior (incl. momentary) was hardware-validated at
  2.18.0 with `byps=True`; left unchanged. If FS LED/polarity oddities ever
  surface, revisit this flag first.
- helixgen emits `iras: []` in each snapshot and `ctm_.sirt: []`; the app's
  blob omits both keys. Device accepts and stores them (both pulled blobs came
  back from the device).

## Adversarial review

Three independent skeptic agents (regression / protocol-fidelity / edge-case
lenses) were dispatched to refute the fix before shipping. Findings and
resolutions: see the PR description.
