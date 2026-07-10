# Golden-output contract

This directory pins the exact `.hsp` output of the **current** (pre-rewrite)
authoring pipeline for a corpus of specs. It exists so the `.hsp`-canonical
redesign can prove later phases didn't change device-validated behavior: as long as
`test_golden_parity` stays green, whatever phase N produces still matches
what the pipeline produced before the rewrite started.

## Layout

- `harness.py` — shared machinery:
  - `build_corpus_library(root)` — builds a deterministic, self-contained
    Stadium `Library` (synthetic chassis + a fixed block set: drive, amp,
    cab, delay, reverb, and an IR-capable cab). Every recipe resolves its
    blocks against this library, so the corpus never depends on the user's
    real `~/.helixgen/library` or on gitignored `data/*.hsp` exports —
    it's reproducible on a clean clone.
  - `run_current_pipeline(spec_dict, library) -> bytes` — **the one call
    site** that turns a spec into `.hsp` bytes. Today it's `parse_spec` +
    `compose_preset` + `dumps_hsp`. When the redesign moves the authoring
    entry point (the plan's Task 3, to `recipe.apply_recipe`), update only
    this function's body. The recipes, the goldens, and the comparison
    logic in `test_golden_parity` all stay unchanged.
  - `normalize(hsp_bytes) -> dict` — strips the magic header, parses the
    JSON payload, and drops the volatile `meta.helixgen.generated_at`
    timestamp so two runs of the same pipeline compare equal regardless of
    when each ran.
- `conftest.py` — session-scoped `corpus_library` fixture (built once via
  `build_corpus_library`) and a `pytest_generate_tests` hook that
  parametrizes any test taking a `corpus_name` argument over every recipe
  in `corpus/`.
- `corpus/<name>.recipe.json` — the spec fed to the pipeline (today: a tone
  spec in the existing `spec.json` shape; the plan's docstring note calls
  this the eventual "recipe" format).
- `corpus/<name>.hsp` — the pinned golden output: real `.hsp` bytes (8-byte
  magic + JSON), committed as-is.
- `capture_golden.py` — one-shot script to (re)generate `corpus/*.hsp` from
  `corpus/*.recipe.json` by running `run_current_pipeline`. **Not** run by
  pytest.
- `test_golden_parity.py` — the pytest test itself.

## The corpus

| recipe | exercises |
|---|---|
| `goldfinger` | plain serial chain, param overrides, int→float coercion (`Distance`/`HighCut`/`LowCut`). Copied verbatim from `tests/fixtures/specs/goldfinger.json`. |
| `snapshots` | 3 named snapshots: param overrides + a `disable` in the active (snapshot 0) and a later slot. |
| `footswitches` | FS3 latching + FS4 momentary bypass assignments. |
| `expression` | EXP1 single target, EXP2 multi-target (two blocks, custom min/max). |
| `ir_block` | an `HX2_ImpulseResponse*` block resolving its canonical `default_irhash` (spec omits `ir`). |
| `dual_cab_raw` | a cab block carrying `raw.harness` (dual-cab flag) + `raw.slots` (second physical slot), preserved verbatim. |
| `split_join` | a parallel split/join region with a lane-1 branch block. |
| `combined` | input routing (`both`/`none`) + snapshots + footswitches + expression + an IR block together in one 2-path preset — the integration case. |

## Comparison semantics

`test_golden_parity` compares **parsed dicts**, not raw bytes: it strips the
8-byte magic header, `json.loads`s both the golden and the freshly-generated
payload, drops `meta.helixgen.generated_at` from each, and asserts the two
dicts are equal. This means JSON key-order or whitespace/compaction
differences never cause a false failure — only an actual change in the
device-relevant shape or values does.

## Updating the corpus

Only do this deliberately, and explain why in the commit message — this test
existing to catch *unintended* drift.

- **New recipe:** add `corpus/<name>.recipe.json`, then run
  `PYTHONPATH=$PWD/src python tests/golden/capture_golden.py` to generate
  its `corpus/<name>.hsp`. Commit both.
- **Intentional behavior change** (e.g. a bug fix that legitimately changes
  output): re-run `capture_golden.py` to refresh every golden, review the
  diff to confirm only the expected recipes changed, and commit.
- **Repointing the pipeline** (redesign Task 3+): edit only
  `harness.run_current_pipeline`. If the new entry point produces
  equivalent output, `test_golden_parity` should pass without touching
  `corpus/`. If it doesn't, that's exactly the regression this harness
  exists to catch.

## Slot-skeleton decision (redesign Task 1a)

`library.Block` does **not** store a verbose `bNN` slot skeleton — `Block.exemplar`
is a flat dict of unwrapped scalars plus a couple of `@`-prefixed keys
(`@model`, `@type`, `@enabled`), e.g. `{"@model": "...", "@type": "fx",
"@enabled": true, "Drive": 0.1, "Tone": 0.5, ...}`, as seen in both
`tests/golden/harness._corpus_blocks()` and every real ingested block on disk.
There is no per-model cache of the nested `{"slot": [...], "params": {"Name":
{"value": ...}}}` shape anywhere in the library. So a future `add_block` must
go with option (b): synthesize the verbose `bNN` skeleton at mutation time by
porting `generate._to_hsp_bnn`'s logic — start from `copy.deepcopy(block.exemplar)`
for param defaults, take `type` from the exemplar's `@type` (falling back to
`generate._hsp_type_for_block`'s per-category default: `amp`→`amp`, `cab`→`cab`,
else `fx`), wrap each param value through the plain-value wrapper (`{"value":
x}`, no `snapshots`/`controller` unless explicitly requested), and leave
`harness` absent unless the caller supplies verbatim `raw.harness` (or, for
delay/reverb, an author-facing `trails` flag) — `_to_hsp_bnn` never synthesizes
a harness out of thin air for the common case. This is a straight port, not a
new algorithm; `mutate.add_block` actually *calls* `generate._to_hsp_bnn`
(and the other compose-path helpers it imports) rather than replacing it, so
Phase 3 must relocate `_to_hsp_bnn` (and those other helpers) into
`mutate.py` or a shared module -- not delete them.
