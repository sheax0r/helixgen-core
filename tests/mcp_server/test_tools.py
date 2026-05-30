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


def test_show_block_handler_returns_schema_text(mcp_library):
    """Returns header + category + params lines, matching CLI format."""
    from mcp_server.tools import show_block_handler

    # Pick any block in the library; assume at least one amp.
    amps = mcp_library.list_blocks(category="amp")
    assert amps, "fixture library has no amps to show"
    target = amps[0]

    result = show_block_handler(mcp_library, name_or_id=target.model_id)

    assert isinstance(result, str)
    lines = result.splitlines()
    # First line: "<display_name>  [<model_id>]"
    assert lines[0] == f"{target.display_name}  [{target.model_id}]"
    # Must include the category line and a params: section.
    assert any(line.startswith("category:") for line in lines)
    assert any(line == "params:" for line in lines)


def test_show_block_handler_unknown_name_raises_keyerror(mcp_library):
    """Unknown block bubbles up the underlying KeyError unchanged."""
    import pytest as _pytest
    from mcp_server.tools import show_block_handler
    with _pytest.raises(KeyError):
        show_block_handler(mcp_library, name_or_id="ThisBlockDoesNotExist")
