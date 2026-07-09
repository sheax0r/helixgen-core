"""Fixtures for the golden-output contract test.

`corpus_library` builds the deterministic, self-contained Stadium library
(see `harness.build_corpus_library`) once per test session — every golden
recipe resolves its blocks against it, so the corpus never depends on the
user's real `~/.helixgen/library` or on gitignored `data/*.hsp` exports.
"""
from __future__ import annotations

import pytest

from tests.golden import harness


@pytest.fixture(scope="session")
def corpus_library(tmp_path_factory):
    root = tmp_path_factory.mktemp("golden-corpus-library")
    return harness.build_corpus_library(root)


def pytest_generate_tests(metafunc):
    if "corpus_name" in metafunc.fixturenames:
        metafunc.parametrize("corpus_name", harness.list_corpus_names())
