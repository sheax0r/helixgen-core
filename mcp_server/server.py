"""FastMCP server wiring: registers the four helixgen tools.

Each tool delegates to the corresponding pure-Python handler in
`mcp_server.tools`. The library is resolved per-request via the standard
`helixgen.library.default_library_path()` (overridable via HELIXGEN_LIBRARY).
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import BlobResourceContents, EmbeddedResource

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
def generate_preset(spec: dict[str, Any]) -> EmbeddedResource:
    """Generate a Helix Stadium .hsp preset from an inline JSON spec.

    The spec follows the helixgen schema (see https://github.com/sheax0r/helixgen):
    a `name`, optional `author`, 1-2 `paths` each with `blocks`, and optional
    `snapshots` / `footswitches` / `expression`.

    **IR caveat:** the `With Pan` family of blocks (model_id prefix
    `HX2_ImpulseResponse*`) requires a user-IR registry that this deploy
    does not ship — they will error. Use a `Mic Ir_*` cab block instead
    (those carry canonical factory IRs and work fine).

    **On param errors:** if the error message says `Unknown param(s)`, call
    `show_block` with the offending block name to retrieve the correct param
    names, then retry `generate_preset` with the corrected spec. Param
    names are case-sensitive.

    Returns an MCP `EmbeddedResource` whose `resource.blob` is the base64-
    encoded `.hsp` bytes; `resource.uri` is `file:///<sanitized-name>.hsp`.
    """
    result = _tools.generate_preset_handler(_resolve_library(), spec=spec)
    return EmbeddedResource(
        type="resource",
        resource=BlobResourceContents(
            uri=f"file:///{result['name']}",
            mimeType=result["mimeType"],
            blob=result["blob"],
        ),
    )


@app.tool()
def list_irs() -> str:
    """List user impulse responses (IRs) registered with helixgen.

    Returns text with one line per IR: `<hash>  <wav-path>`, or an empty
    string when no IRs are registered. **On the public claude.ai deployment
    this is always empty** — IRs are local-only. Call this before deciding
    between an IR cab (`With Pan` + `ir`) vs. a stock cab (`Mic Ir_*`):
    empty result → use a stock cab; non-empty → an IR is available and can
    be referenced by basename (e.g. `"YA VX30 212 BLU Mix 01.wav"`).
    """
    return _tools.list_irs_handler()
