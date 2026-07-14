# Controller depth + device info (#21) — adversarial review record (2026-07-14)

Independent review subagent prompted to break the branch
(`feat-controller-depth-device-info`, post-rebase onto 2.21.x). Verdict: no
blockers; 4 should-fix + 3 nits, all addressed in-branch. Verified clean:
rebase seam vs the 2.21.1 snapshot-bindings work (controller trgs share
`trg_index`, are not `snap/tid_`-stamped, counters consistent),
`hsp_sources` key types, 211-export `parse_spec(view(x))` round-trip,
scribble-attach and label-clobber paths, recipe scribble reset scope, doc
claims vs code.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | should-fix | FS-vs-EXP "one controller per param" check and the FS duplicate-target guard were bypassed by coordinate aliasing (`path: None` vs explicit `path: 0` compared unequal → silent last-wins overwrite) | `spec._refs_may_alias`: `None` coordinates are wildcards in both checks; tests cover alias + genuinely-distinct coordinates |
| 2 | should-fix | FS param-toggle `min`/`max` force-coerced to float, but corpus-real toggles target INT params (Interval 2→4, Transport 0→1) — 7/17 corpus presets failed exact round-trip; device msgpack type flipped int→float64 | Coercion removed end-to-end (spec/mutate/transcode); int-preservation pinned by tests at `.hsp` and `_sbepgsm` levels |
| 3 | should-fix | Evidence counts didn't reproduce: bypass-bounds shapes were bank-A-only counts presented as corpus totals (real: 566 F/T / 360 null / 25 numeric of 951); merge-switch "25 cases" (real: 87 merged switches across 66 exports) | Design doc + `generate.py` docstring corrected to re-derived full-corpus numbers |
| 4 | should-fix (HW) | `srcs.byps` mirror flips authored tones' bypass sources from the pre-#21 `byps=True` to `False` (authored `.hsp` always carries `bypass: False`); also order-dependent default on merged sources | Kept the mirror — functional evidence that `byps=False` works: factory presets with working FS bypasses carry it (2 Guitar Rig, live pull). Order-dependence fixed: a bypass controller joining a param-created source upgrades the default (explicit `.hsp` flag still wins) + test |
| 5 | nit | `view` emitted `threshold` on expression targets that `parse_spec` dropped | `threshold` is now a first-class ExpressionTarget field (parse → wire → `_build_exp_controller`); round-trip test |
| 6 | nit | `product_info` crashed (AttributeError, not HelixError) on non-dict `host`/`vers`; `"1.None.None"` on partial version | isinstance guards; firmware string requires all three parts |
| 7 | nit | `wire_footswitch(param=…)` with missing `min`/`max` wrote `null` bounds unvalidated via direct API use | `MutateError` guard + test |

Suite after fixes: 1077 passed / 7 skipped (live-device + external fixture),
incl. the 211-export model + sonic-fidelity bars and the `_sbepgsm`
byte-fidelity gate.

## Round 2 (independent re-review of PR #38, 2026-07-14) — FIX-FIRST on two

| # | Severity | Finding | Resolution |
|---|---|---|---|
| F1 | medium | EXP↔EXP duplicate-target guard still used exact-tuple membership — the coordinate-aliasing hole round 1 closed for FS↔FS and FS↔EXP (`EXP1 → {block,param}` + `EXP2 → {same, pos:0}` accepted, silent last-wins) | `_parse_expression` now uses `_refs_may_alias` like the other two checks; both bare-vs-explicit directions tested |
| F2 | medium | EXP3 (`0x01020102`) collapsed onto EXP2 `(42, 1)` — corpus-real: `Marshall and vh4` sweeps a wah from EXP3 while using real EXP2 elsewhere, so one EXP2 src drove 5 ctrls; docstring/design claimed `(42, M)` unconditionally | `_controller_locl_ctxt` maps `(42, M)` for M ∈ {0, 1} ONLY; EXP3 is skipped (consistent with view's `unknown_controllers` handling); docstring + design §2 corrected; synthetic + corpus-preset tests added |
| F3 | follow-up | label/color accepted on EXP1Toe but silently never shown (no scribble strip) | Fixed: `wire_footswitch` warns and keeps the sources entry corpus-shaped (no fs_* keys) for non-stomp switches; CLAUDE.md notes the strip scope |
| F4 | follow-up | `view` emitted bypass behaviors verbatim; a future `toedown`/`continuous` bypass export would break `parse_spec(view(x))` | Fixed: out-of-vocabulary bypass behaviors route to `unknown_controllers`, labeled |
| F5 | follow-up | `curve_index`/`color_int` were test-only API; `_curv` silently linear-fell-back on garbage | Fixed: `_curv` and `_synth_pm` now go through the helpers (unknown device-written strings still fall back — a transcode must not crash on future firmware vocabulary) |
| F6 | follow-up | `wire_footswitch(min/max, no param)` silently ignored bounds via direct API | Fixed: MutateError mirroring spec validation |
| — | follow-up | >12-char label warning was stderr-only; MCP `generate_preset` returned `warnings: []` | Fixed: the MCP handler captures generate-time stderr diagnostics into the returned `warnings` (re-emitted to real stderr) |

Rebased onto 2.22.0 (PR #37); deferred MIDI/XY entries renumbered #33/#34
(#31/#32 were claimed by #37's `.hss` + prune-residual entries); matrix
cross-references updated. Suite after round 2: 1174 passed / 7 skipped.
