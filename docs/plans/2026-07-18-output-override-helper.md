# Plan: `has_output_override` helper + recipe-reference clarity (#90)

## Context

Implements `docs/BACKLOG.md #90`. The recipe projection field `path.output`
(`OutputSpec`, `spec.py:74`; `PathSpec.output: OutputSpec | None = None`,
`spec.py:87`) is `None` by default and means **"the output block's level/pan are
at device defaults (0.0 dB / 0.5 pan)"** — **NOT** "the path has no output
block." Every DSP path always terminates in a `b13` output endpoint carrying a
dB `gain` (Level) param (`flowparams.py:146` `OUTPUT_FIELD_TO_HSP =
{"level":"gain","pan":"pan"}`; `view.py:235` `_lift_output` returns `None` when
both are default). A volume-normalization pass misread `path.output is None` as
"no volume target to attach to" and silently skipped. Decision (2026-07-18):
make the projection unmistakable — add a **truthy `has_output_override` helper**
so callers gate on intent, not `None`-vs-value, and **document the field** in
`docs/recipe-reference.md`. Do NOT add an "always synthesize an output block"
invariant (redundant — `b13` always exists). Pairs with plugin #91 (companion
PR carrying the mirrored `recipe-reference.md` + the tone-skill guard).

Repo rules: TDD (failing test first), stdlib + click only, agent-facing
surfaces updated in the same change.

### Task 1: `has_output_override` helper (TDD)

- [x] Add a failing test (in `tests/test_spec_flow.py`, matching the existing
      `PathSpec`/`OutputSpec` test patterns) asserting a truthy
      `has_output_override` that returns **False** when a path's `output` is
      `None` or carries no non-default field, and **True** when `level` or `pan`
      is set. Cover both a default path and one with `output={"level": -3.0}`.
- [x] Implement the minimal helper — a `PathSpec.has_output_override` property
      (or equivalently a small function beside `OutputSpec`) returning whether
      the path carries a meaningful output override
      (`self.output is not None and (self.output.level is not None or
      self.output.pan is not None)`). Keep it a pure accessor; no behavior
      change to parse/lift/mutate.
- [x] Update the one obvious internal caller that currently gates on
      `path.output is None` for override-presence, if one exists, to use the
      helper (grep `path.output`/`.output is None`). Do NOT expand scope beyond
      swapping the predicate — no normalization behavior changes here.

### Task 2: document the field in `recipe-reference.md`

- [ ] In `docs/recipe-reference.md`, at the "Optional: per-path output
      level/pan" section (around line 91) and the snapshot `output` section
      (around line 165), add an explicit note: **`output` absent or `null`
      means the output block is at device defaults (0.0 dB / 0.5 pan), NOT that
      the path has no output block** — every path terminates in a `b13` output
      whose `gain` always exists. Point normalization/volume readers at
      `has_output_override` rather than an `is None` check.
- [ ] Update any other agent-facing surface the change touches (core
      `CLAUDE.md` if it references this field; the helper is internal so there
      is no verb `--help` to change).
- [ ] Move this plan to `docs/plans/completed/`.

## Validation Commands

Run from the repo root:

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (includes the
  golden-output contract, the 211-export round-trip acceptance test, and
  `tests/test_cli_parity.py`). Live tests under `tests/live/` auto-skip without
  `HELIXGEN_LIVE=1`.

No separate lint/format/type-check step. No device/live marker needed — this is
pure local projection/doc work.
