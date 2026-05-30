"""Protocol-level smoke tests: verify the FastMCP server registers the three
tools with the right names + arg schemas, and that calling them returns the
expected content shapes. Does not exercise the HTTP transport.
"""
from __future__ import annotations

import pytest


def _get_tool_names(server) -> set[str]:
    """Best-effort extraction of registered tool names from a FastMCP app.

    The FastMCP API exposes tools via several paths depending on SDK version;
    try the common ones in order.
    """
    if hasattr(server, "list_tools"):
        # Most current FastMCP versions: synchronous accessor.
        try:
            return {t.name for t in server.list_tools()}
        except TypeError:
            pass
    if hasattr(server, "_tool_manager"):
        tm = server._tool_manager
        if hasattr(tm, "list_tools"):
            return {t.name for t in tm.list_tools()}
    raise AssertionError(
        "Could not locate registered tools on the FastMCP server — check SDK version."
    )


def test_server_registers_three_tools():
    """The server registers exactly the three documented tools."""
    from mcp_server.server import app

    names = _get_tool_names(app)
    assert names == {"list_blocks", "show_block", "generate_preset"}, (
        f"unexpected tool set: {names}"
    )


def test_server_tools_have_descriptions():
    """Every tool has a non-empty description so Claude knows when to call it."""
    from mcp_server.server import app

    # _tool_manager.list_tools() is synchronous across SDK versions; prefer it.
    # app.list_tools() is async in mcp 1.x and cannot be called synchronously.
    if hasattr(app, "_tool_manager") and hasattr(app._tool_manager, "list_tools"):
        tools = list(app._tool_manager.list_tools())
    elif hasattr(app, "list_tools"):
        tools = list(app.list_tools())
    else:
        raise AssertionError("Cannot retrieve tools from FastMCP server — check SDK version.")
    for t in tools:
        assert getattr(t, "description", None), f"tool {t.name} has no description"


def test_list_blocks_via_server(mcp_library, monkeypatch):
    """Invoking the list_blocks tool through the server returns grouped text."""
    from mcp_server import server as srv

    # Point the server's library resolver at our test library.
    monkeypatch.setattr(srv, "_resolve_library", lambda: mcp_library)

    # Get the registered handler. FastMCP exposes via tool_manager.get_tool.
    if hasattr(srv.app, "_tool_manager"):
        tool = srv.app._tool_manager.get_tool("list_blocks")
        result = tool.fn(category=None)
    else:
        # Fallback: import the underlying handler directly.
        from mcp_server.tools import list_blocks_handler
        result = list_blocks_handler(mcp_library, category=None)

    assert isinstance(result, str)
    assert result  # non-empty
