"""Protocol-level smoke tests: verify the FastMCP server registers the
documented tools with the right names + arg schemas, and that calling them
operates on `.hsp` file paths (no base64). Does not exercise the HTTP transport.
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
    """The server registers exactly the documented tools."""
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
        "view_preset",
        "patch_preset",
        "controller_mapping",
        # device_* tools — networked Helix Stadium control.
        "device_list_presets",
        "device_list_setlists",
        "device_read_preset",
        "device_load_preset",
        "device_create_preset",
        "device_rename_preset",
        "device_delete_preset",
        "device_set_param",
        "device_settings_list",
        "device_settings_get",
        "device_settings_set",
        "device_save_preset",
        "device_install_preset",
        "device_setlist_list",
        "device_setlist_add",
        "device_setlist_remove",
        "device_sync_setlist",
        "device_sync_all",
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


def test_generate_preset_via_server_writes_file(mcp_library, monkeypatch, tmp_path):
    """Server-level generate_preset writes a .hsp file and returns its path."""
    from helixgen.hsp import HSP_MAGIC
    from mcp_server import server as srv

    monkeypatch.setattr(srv, "_resolve_library", lambda: mcp_library)

    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    spec = {
        "name": "Protocol Test",
        "paths": [{"blocks": [{"block": amps[0].display_name}, {"block": cabs[0].display_name}]}],
    }
    out = tmp_path / "protocol.hsp"

    if not hasattr(srv.app, "_tool_manager"):
        pytest.skip("FastMCP._tool_manager not available on this SDK version")
    tool = srv.app._tool_manager.get_tool("generate_preset")
    result = tool.fn(model="stadium_xl", recipe=spec, out_path=str(out))

    assert result == {"path": str(out), "warnings": []}
    assert out.read_bytes().startswith(HSP_MAGIC)


def test_view_then_patch_preset_via_server(mcp_library, monkeypatch, tmp_path):
    """generate → view → patch round-trip through the server dispatch on a
    .hsp file path — no base64 anywhere in the loop."""
    from helixgen.hsp import HSP_MAGIC
    from mcp_server import server as srv

    monkeypatch.setattr(srv, "_resolve_library", lambda: mcp_library)

    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    recipe = {
        "name": "Protocol View/Patch",
        "paths": [{"blocks": [{"block": amps[0].display_name}, {"block": cabs[0].display_name}]}],
    }
    out = tmp_path / "vp.hsp"

    if not hasattr(srv.app, "_tool_manager"):
        pytest.skip("FastMCP._tool_manager not available on this SDK version")

    gen_tool = srv.app._tool_manager.get_tool("generate_preset")
    gen_tool.fn(model="stadium_xl", recipe=recipe, out_path=str(out))
    before = out.read_bytes()

    view_tool = srv.app._tool_manager.get_tool("view_preset")
    projection = view_tool.fn(model="stadium_xl", hsp_path=str(out))
    assert projection["name"] == "Protocol View/Patch"
    assert projection["paths"][0]["blocks"][0]["block"] == amps[0].display_name

    patch_tool = srv.app._tool_manager.get_tool("patch_preset")
    patched = patch_tool.fn(
        model="stadium_xl", hsp_path=str(out),
        operations=[{"op": "set_enabled", "block": amps[0].display_name, "enabled": False}],
    )
    assert patched == {"path": str(out), "warnings": []}
    # The file was edited in place — bytes changed, still a valid .hsp.
    after = out.read_bytes()
    assert after.startswith(HSP_MAGIC)
    assert after != before
