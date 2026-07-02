"""Protocol-level smoke tests: verify the FastMCP server registers the four
tools with the right names + arg schemas, and that calling them returns the
expected content shapes. Does not exercise the HTTP transport.
"""
from __future__ import annotations

import inspect

import pytest


def _get_tool_names(server) -> set[str]:
    """Best-effort extraction of registered tool names from a FastMCP app.

    Prefer the synchronous `_tool_manager.list_tools()` accessor; fall back
    to `server.list_tools()` only if it's a regular function (not a coroutine
    function — calling an async method synchronously would emit an unawaited-
    coroutine warning).
    """
    if hasattr(server, "_tool_manager") and hasattr(server._tool_manager, "list_tools"):
        return {t.name for t in server._tool_manager.list_tools()}
    if hasattr(server, "list_tools") and not inspect.iscoroutinefunction(server.list_tools):
        return {t.name for t in server.list_tools()}
    raise AssertionError(
        "Could not locate registered tools on the FastMCP server — check SDK version."
    )


def test_server_registers_documented_tools():
    """The server registers exactly the ten documented tools."""
    from mcp_server.server import app

    names = _get_tool_names(app)
    expected = {
        "list_blocks",
        "show_block",
        "generate_preset",
        "list_irs",
        "compute_irhash",
        "discover_irs",
        "register_ir",
        "register_irs",
        "decompile_preset",
        "patch_preset",
    }
    assert names == expected, f"unexpected tool set: {names}"


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
        result = tool.fn(model="stadium_xl", category=None)
    else:
        # Fallback: import the underlying handler directly.
        from mcp_server.tools import list_blocks_handler
        result = list_blocks_handler(mcp_library, "stadium_xl", category=None)

    assert isinstance(result, str)
    assert result  # non-empty


def test_generate_preset_via_server_returns_embedded_resource(mcp_library, monkeypatch):
    """Server-level generate_preset returns an MCP EmbeddedResource, not a raw dict.

    This is the protocol-correctness check. The raw handler returns a plain
    dict (tested in test_tools.py); the server wraps it so FastMCP's content
    conversion produces a binary blob, not a JSON-in-text block.
    """
    import base64
    from mcp.types import EmbeddedResource, BlobResourceContents
    from helixgen.hsp import HSP_MAGIC
    from mcp_server import server as srv

    monkeypatch.setattr(srv, "_resolve_library", lambda: mcp_library)

    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    spec = {
        "name": "Protocol Test",
        "paths": [{"blocks": [{"block": amps[0].display_name}, {"block": cabs[0].display_name}]}],
    }

    if hasattr(srv.app, "_tool_manager"):
        tool = srv.app._tool_manager.get_tool("generate_preset")
        result = tool.fn(model="stadium_xl", spec=spec)
    else:
        import pytest as _pytest_inner
        _pytest_inner.skip("FastMCP._tool_manager not available on this SDK version")

    assert isinstance(result, EmbeddedResource), f"got {type(result).__name__}"
    assert isinstance(result.resource, BlobResourceContents)
    assert result.resource.mimeType == "application/octet-stream"
    assert str(result.resource.uri).endswith(".hsp")
    decoded = base64.b64decode(result.resource.blob)
    assert decoded.startswith(HSP_MAGIC)
