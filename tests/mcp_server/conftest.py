"""Shared fixtures for mcp_server tests."""
from __future__ import annotations

import pytest

from helixgen.library import Library, default_library_path


@pytest.fixture(scope="session")
def mcp_library() -> Library:
    """A populated Library to drive the MCP tools.

    Resolves via the same env-var path as the CLI. Skips the test if the
    library has no chassis or no blocks, matching the project's existing
    skip-if-not-present convention for tests that need real ingest data.
    """
    library = Library(default_library_path())
    if not library.has_chassis():
        pytest.skip(
            f"No chassis at {library.chassis_path}. "
            "Run `helixgen bootstrap && helixgen ingest <real.hsp>` "
            "to populate, or set HELIXGEN_LIBRARY to an existing library."
        )
    if not library.list_blocks():
        pytest.skip(f"Library at {library.root} has no blocks.")
    return library
