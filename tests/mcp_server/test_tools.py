"""Tests for mcp_server.tools handler functions."""
from __future__ import annotations


def test_library_fixture_loads(mcp_library):
    """Smoke test: fixture resolves to a populated Library."""
    assert mcp_library.has_chassis()
    assert mcp_library.list_blocks()
