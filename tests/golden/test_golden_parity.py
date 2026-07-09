"""Golden-output contract: pins the CURRENT `.hsp` authoring pipeline's exact
byte-for-byte-meaningful output for a corpus of specs (see README.md).

Comparison is by PARSED DICT (after stripping the volatile
`meta.helixgen.generated_at` timestamp), not raw bytes — key order or
compaction changes must not fail this test; a change in the actual
device-relevant shape or values must.

`test_golden_parity` must stay green through the whole .hsp-canonical
rewrite. When the authoring entry point moves (the plan's Task 3), update
only `harness.run_current_pipeline`'s body — never the recipes, never the
goldens.
"""
from __future__ import annotations

from tests.golden import harness


def test_golden_parity(corpus_name, corpus_library):
    recipe = harness.load_recipe(corpus_name)
    golden = harness.load_golden(corpus_name)

    actual = harness.run_current_pipeline(recipe, corpus_library)

    assert harness.normalize(actual) == harness.normalize(golden), (
        f"corpus/{corpus_name}: current pipeline output diverged from the "
        f"pinned golden. If this divergence is intentional, re-run "
        f"tests/golden/capture_golden.py and explain why in the commit."
    )


def test_corpus_is_non_empty():
    """Guard against a silently-empty corpus (e.g. a glob typo) making
    test_golden_parity pass vacuously."""
    assert len(harness.list_corpus_names()) >= 5
