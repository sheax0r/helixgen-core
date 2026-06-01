"""Pure-Python handlers for MCP tools. No MCP types; FastMCP wraps these at
registration time. Importable + directly testable.
"""
from __future__ import annotations

import base64
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from helixgen.generate import generate_preset
from helixgen.ir import IrMapping
from helixgen.library import Library


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def list_blocks_handler(library: Library, category: str | None = None) -> str:
    """Return library blocks grouped by category, matching `helixgen list-blocks`.

    Format mirrors the CLI: one `<category>:` header per category, followed
    by indented `  <display_name>  [<model_id>]` lines sorted by name.
    Unknown category returns an empty string (not an error) so callers can
    distinguish "no such category" from "library empty."
    """
    blocks = library.list_blocks(category=category)
    if not blocks:
        return ""

    by_category: dict[str, list] = {}
    for b in blocks:
        by_category.setdefault(b.category, []).append(b)

    lines: list[str] = []
    for cat in sorted(by_category):
        lines.append(f"{cat}:")
        for b in sorted(by_category[cat], key=lambda x: x.display_name):
            lines.append(f"  {b.display_name}  [{b.model_id}]")
    return "\n".join(lines)


def show_block_handler(library: Library, name_or_id: str) -> str:
    """Return a block's schema (params, defaults, ranges) as text.

    Format mirrors `helixgen show-block`: header, category, aliases (if any),
    then one indented line per param with type, default, and observed-range
    or values where present. KeyError / LookupError propagate to the caller
    (FastMCP translates these to MCP errors at the registration boundary).
    """
    block = library.find_block(name_or_id)

    lines: list[str] = []
    lines.append(f"{block.display_name}  [{block.model_id}]")
    lines.append(f"category: {block.category}")
    if block.aliases:
        lines.append(f"aliases: {', '.join(block.aliases)}")
    lines.append("params:")
    for name, schema in block.params.items():
        meta_bits = [schema["type"], f"default={schema.get('default')!r}"]
        if "observed_range" in schema:
            meta_bits.append(f"observed={schema['observed_range']}")
        if "values" in schema:
            meta_bits.append(f"values={schema['values']}")
        lines.append(f"  {name}  ({', '.join(meta_bits)})")
    return "\n".join(lines)


def _safe_filename(name: str) -> str:
    """Convert an arbitrary preset name to a safe basename for the .hsp blob.

    Strips path separators, collapses unsafe characters to underscores,
    and falls back to 'preset' when the result would be empty.
    """
    cleaned = _FILENAME_SAFE.sub("_", name).strip("._-")
    return f"{cleaned or 'preset'}.hsp"


def generate_preset_handler(library: Library, spec: dict[str, Any]) -> dict[str, Any]:
    """Generate a Helix Stadium .hsp from an inline spec dict.

    Returns a dict suitable for an MCP EmbeddedResource:
      - mimeType: application/octet-stream
      - name:     safe basename ending in .hsp
      - blob:     base64-encoded .hsp bytes (magic header + JSON body)

    Underlying SpecError / ParamValidationError / GenerateError propagate;
    the MCP server boundary translates them to protocol errors.
    """
    with tempfile.TemporaryDirectory(prefix="helixgen-mcp-") as tmp_dir:
        tmp = Path(tmp_dir)
        spec_path = tmp / "spec.json"
        out_path = tmp / "preset.hsp"
        spec_path.write_text(json.dumps(spec))
        generate_preset(spec_path, out_path, library)
        raw = out_path.read_bytes()

    return {
        "mimeType": "application/octet-stream",
        "name":     _safe_filename(spec.get("name", "preset")),
        "blob":     base64.b64encode(raw).decode("ascii"),
    }


def list_irs_handler(irs_dir: Path | None = None) -> str:
    """Return registered user IRs as text, matching `helixgen list-irs`.

    One line per IR: `<hash>  <wav-path>`, sorted by hash. Empty string when
    no IRs are registered or the mapping file is absent — callers branch on
    truthiness to decide whether to use IRs vs. stock cabs.
    """
    mapping = IrMapping.load(irs_dir)
    if not mapping.entries:
        return ""
    return "\n".join(
        f"{h}  {mapping.entries[h]}" for h in sorted(mapping.entries)
    )
