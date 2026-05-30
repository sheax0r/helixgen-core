"""FastMCP server wiring: registers the three helixgen tools.

Each tool delegates to the corresponding pure-Python handler in
`mcp_server.tools`. The library is resolved per-request via the standard
`helixgen.library.default_library_path()` (overridable via HELIXGEN_LIBRARY).
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from helixgen.library import Library, default_library_path
from mcp_server import tools as _tools


def _resolve_library() -> Library:
    """Construct a Library at the configured path. Cheap; no caching for v1."""
    return Library(default_library_path())


app = FastMCP("helixgen")


@app.tool()
def list_blocks(category: str | None = None) -> str:
    """List Helix blocks in the library, optionally filtered to one category.

    Categories: amp, cab, drive, delay, reverb, modulation, filter, eq,
    dynamics, pitch, volume, send. Output is grouped by category with one
    block per line as `<display_name>  [<model_id>]`.
    """
    return _tools.list_blocks_handler(_resolve_library(), category=category)


@app.tool()
def show_block(name_or_id: str) -> str:
    """Show a Helix block's parameter schema: types, defaults, observed ranges.

    Accepts the display name (e.g. "Brit Plexi Brt"), the model id
    (e.g. "HD2_AmpBritPlexiBrt"), or an alias. **Always call this before
    writing params for a block** — param names are case-sensitive and the
    generator rejects unknown ones.
    """
    return _tools.show_block_handler(_resolve_library(), name_or_id=name_or_id)


@app.tool()
def generate_preset(spec: dict[str, Any]) -> dict[str, Any]:
    """Generate a Helix Stadium .hsp preset from an inline JSON spec.

    The spec follows the helixgen schema (see https://github.com/sheax0r/helixgen):
    a `name`, optional `author`, 1-2 `paths` each with `blocks`, and optional
    `snapshots` / `footswitches` / `expression`. The `ir` field on IR blocks
    is accepted but ignored server-side (no IR registry in this deployment).

    Returns a dict with `mimeType` (application/octet-stream), `name`
    (safe filename ending in .hsp), and `blob` (base64-encoded .hsp bytes).
    """
    return _tools.generate_preset_handler(_resolve_library(), spec=spec)
