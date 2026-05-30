"""Tests for mcp_server.tools handler functions."""
from __future__ import annotations


def test_library_fixture_loads(mcp_library):
    """Smoke test: fixture resolves to a populated Library."""
    assert mcp_library.has_chassis()
    assert mcp_library.list_blocks()


def test_list_blocks_handler_returns_grouped_text(mcp_library):
    """Returns text grouped by category, one block per line."""
    from mcp_server.tools import list_blocks_handler

    result = list_blocks_handler(mcp_library, category=None)

    assert isinstance(result, str)
    assert result.strip()
    # Format: lines beginning "<category>:" followed by indented "  <name>  [<model_id>]" lines.
    lines = result.splitlines()
    assert any(line.endswith(":") for line in lines), "expected at least one category header"
    assert any(line.startswith("  ") and "[" in line and line.endswith("]") for line in lines), (
        "expected at least one indented '<name>  [<model_id>]' line"
    )


def test_list_blocks_handler_filters_by_category(mcp_library):
    """When category given, only blocks from that category appear."""
    from mcp_server.tools import list_blocks_handler

    result = list_blocks_handler(mcp_library, category="amp")
    lines = result.splitlines()
    headers = [line[:-1] for line in lines if line.endswith(":")]
    assert headers == ["amp"], f"expected only 'amp:' header, got {headers}"


def test_list_blocks_handler_unknown_category_returns_empty(mcp_library):
    """An unknown category returns an empty string, not an error."""
    from mcp_server.tools import list_blocks_handler
    assert list_blocks_handler(mcp_library, category="nonexistent") == ""
