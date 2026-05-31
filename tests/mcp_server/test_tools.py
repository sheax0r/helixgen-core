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


def test_generate_preset_handler_returns_base64_hsp(mcp_library):
    """Returns a dict with mimeType, name, and base64 blob whose bytes start with HSP_MAGIC."""
    import base64
    from helixgen.hsp import HSP_MAGIC
    from mcp_server.tools import generate_preset_handler

    # Pick the first amp and first cab from the library to build a minimal spec.
    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    assert amps and cabs, "fixture library missing amps/cabs"

    spec = {
        "name": "MCP Test Preset",
        "paths": [
            {
                "blocks": [
                    {"block": amps[0].display_name},
                    {"block": cabs[0].display_name},
                ]
            }
        ],
    }

    result = generate_preset_handler(mcp_library, spec=spec)

    assert isinstance(result, dict)
    assert result["mimeType"] == "application/octet-stream"
    assert result["name"].endswith(".hsp")
    decoded = base64.b64decode(result["blob"])
    assert decoded.startswith(HSP_MAGIC), (
        f"expected HSP_MAGIC prefix; got {decoded[:8]!r}"
    )


def test_generate_preset_handler_rejects_unknown_param(mcp_library):
    """Bad spec surfaces ParamValidationError unchanged."""
    import pytest as _pytest
    from helixgen.generate import ParamValidationError
    from mcp_server.tools import generate_preset_handler

    amps = mcp_library.list_blocks(category="amp")
    assert amps
    spec = {
        "name": "broken",
        "paths": [
            {"blocks": [{"block": amps[0].display_name, "params": {"NoSuchParam": 0.5}}]}
        ],
    }
    with _pytest.raises(ParamValidationError):
        generate_preset_handler(mcp_library, spec=spec)


def test_generate_preset_handler_sanitizes_filename(mcp_library):
    """Spec names with path separators or unsafe chars yield safe filenames."""
    from mcp_server.tools import generate_preset_handler

    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    spec = {
        "name": "../../etc/passwd",
        "paths": [{"blocks": [{"block": amps[0].display_name}, {"block": cabs[0].display_name}]}],
    }
    result = generate_preset_handler(mcp_library, spec=spec)
    # No path traversal, no slashes, no null bytes.
    assert "/" not in result["name"]
    assert "\\" not in result["name"]
    assert ".." not in result["name"]
    assert result["name"].endswith(".hsp")


def test_show_block_handler_ambiguous_name_raises_lookuperror(monkeypatch, mcp_library):
    """Ambiguous block name surfaces the underlying LookupError unchanged."""
    import pytest as _pytest
    from mcp_server.tools import show_block_handler

    def _raise_lookup(name_or_id):
        raise LookupError(f"Block {name_or_id!r} matches multiple library entries")

    monkeypatch.setattr(mcp_library, "find_block", _raise_lookup)

    with _pytest.raises(LookupError):
        show_block_handler(mcp_library, name_or_id="Anything")


def test_generate_preset_handler_rejects_malformed_spec(mcp_library):
    """A spec missing required keys surfaces SpecError."""
    import pytest as _pytest
    from helixgen.spec import SpecError
    from mcp_server.tools import generate_preset_handler

    # Missing 'paths' is a structural failure caught by parse_spec.
    spec = {"name": "no paths here"}
    with _pytest.raises(SpecError):
        generate_preset_handler(mcp_library, spec=spec)


def test_generate_preset_handler_with_pan_raises_generate_error(mcp_library):
    """Using a HX2_ImpulseResponse* block without an IR mapping raises GenerateError.

    The deploy ships no user IR registry, so With-Pan-style blocks have no
    canonical irhash and no spec mapping to resolve to.
    """
    import pytest as _pytest
    from helixgen.generate import GenerateError
    from mcp_server.tools import generate_preset_handler

    # Find a With-Pan-style block in the library, if present.
    with_pan_blocks = [
        b for b in mcp_library.list_blocks()
        if b.model_id.startswith("HX2_ImpulseResponse")
    ]
    if not with_pan_blocks:
        import pytest as _pytest_mod
        _pytest_mod.skip("library has no HX2_ImpulseResponse* blocks to test against")

    # Need an amp too so the path is otherwise valid.
    amps = mcp_library.list_blocks(category="amp")
    assert amps

    spec = {
        "name": "needs IR",
        "paths": [{"blocks": [
            {"block": amps[0].display_name},
            {"block": with_pan_blocks[0].display_name},
        ]}],
    }
    with _pytest.raises(GenerateError):
        generate_preset_handler(mcp_library, spec=spec)


def test_list_irs_handler_empty_when_no_mapping(tmp_path):
    """An IRs dir with no mapping.json returns empty string."""
    from mcp_server.tools import list_irs_handler
    assert list_irs_handler(irs_dir=tmp_path) == ""


def test_list_irs_handler_returns_sorted_lines(tmp_path):
    """When entries exist, returns one `<hash>  <path>` line per entry, sorted."""
    import json
    from mcp_server.tools import list_irs_handler

    mapping = {
        "ffffffffffffffffffffffffffffffff": "z_last.wav",
        "00000000000000000000000000000000": "a_first.wav",
    }
    (tmp_path / "mapping.json").write_text(json.dumps(mapping))

    result = list_irs_handler(irs_dir=tmp_path)
    lines = result.splitlines()
    assert lines == [
        "00000000000000000000000000000000  a_first.wav",
        "ffffffffffffffffffffffffffffffff  z_last.wav",
    ]
