"""Tests for mcp_server.tools handler functions."""
from __future__ import annotations


def test_library_fixture_loads(mcp_library):
    """Smoke test: fixture resolves to a populated Library."""
    assert mcp_library.has_chassis()
    assert mcp_library.list_blocks()


def test_list_blocks_handler_returns_grouped_text(mcp_library):
    """Returns text grouped by category, one block per line."""
    from mcp_server.tools import list_blocks_handler

    result = list_blocks_handler(mcp_library, "stadium_xl", category=None)

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

    result = list_blocks_handler(mcp_library, "stadium_xl", category="amp")
    lines = result.splitlines()
    headers = [line[:-1] for line in lines if line.endswith(":")]
    assert headers == ["amp"], f"expected only 'amp:' header, got {headers}"


def test_list_blocks_handler_unknown_category_returns_empty(mcp_library):
    """An unknown category returns an empty string, not an error."""
    from mcp_server.tools import list_blocks_handler
    assert list_blocks_handler(mcp_library, "stadium_xl", category="nonexistent") == ""


def test_show_block_handler_returns_schema_text(mcp_library):
    """Returns header + category + params lines, matching CLI format."""
    from mcp_server.tools import show_block_handler

    # Pick any block in the library; assume at least one amp.
    amps = mcp_library.list_blocks(category="amp")
    assert amps, "fixture library has no amps to show"
    target = amps[0]

    result = show_block_handler(mcp_library, "stadium_xl", name_or_id=target.model_id)

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
        show_block_handler(mcp_library, "stadium_xl", name_or_id="ThisBlockDoesNotExist")


def test_generate_preset_handler_writes_hsp_file(mcp_library, tmp_path):
    """Writes a .hsp file at out_path whose bytes start with HSP_MAGIC; returns its path."""
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
    out = tmp_path / "sub" / "mcp-test.hsp"   # parent dir does not exist yet

    result = generate_preset_handler(mcp_library, "stadium_xl", recipe=spec, out_path=str(out))

    assert result == {"path": str(out), "warnings": []}
    assert out.exists()
    assert out.read_bytes().startswith(HSP_MAGIC)


def test_generate_preset_handler_rejects_unknown_param(mcp_library, tmp_path):
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
        generate_preset_handler(mcp_library, "stadium_xl", recipe=spec, out_path=str(tmp_path / "x.hsp"))


def test_show_block_handler_ambiguous_name_raises_lookuperror(monkeypatch, mcp_library):
    """Ambiguous block name surfaces the underlying LookupError unchanged."""
    import pytest as _pytest
    from mcp_server.tools import show_block_handler

    def _raise_lookup(name_or_id):
        raise LookupError(f"Block {name_or_id!r} matches multiple library entries")

    monkeypatch.setattr(mcp_library, "find_block", _raise_lookup)

    with _pytest.raises(LookupError):
        show_block_handler(mcp_library, "stadium_xl", name_or_id="Anything")


def test_generate_preset_handler_rejects_malformed_spec(mcp_library, tmp_path):
    """A spec missing required keys surfaces SpecError."""
    import pytest as _pytest
    from helixgen.spec import SpecError
    from mcp_server.tools import generate_preset_handler

    # Missing 'paths' is a structural failure caught by parse_spec.
    spec = {"name": "no paths here"}
    with _pytest.raises(SpecError):
        generate_preset_handler(mcp_library, "stadium_xl", recipe=spec, out_path=str(tmp_path / "x.hsp"))


def test_generate_preset_handler_with_pan_raises_generate_error(mcp_library, tmp_path):
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
        generate_preset_handler(mcp_library, "stadium_xl", recipe=spec, out_path=str(tmp_path / "x.hsp"))


def test_list_irs_handler_empty_when_no_mapping(tmp_path):
    """An IRs dir with no mapping.json returns empty string."""
    from mcp_server.tools import list_irs_handler
    assert list_irs_handler("stadium_xl", irs_dir=tmp_path) == ""


def test_list_irs_handler_returns_sorted_lines(tmp_path):
    """When entries exist, returns one `<hash>  <path>` line per entry, sorted."""
    import json
    from mcp_server.tools import list_irs_handler

    mapping = {
        "ffffffffffffffffffffffffffffffff": "z_last.wav",
        "00000000000000000000000000000000": "a_first.wav",
    }
    (tmp_path / "mapping.json").write_text(json.dumps(mapping))

    result = list_irs_handler("stadium_xl", irs_dir=tmp_path)
    lines = result.splitlines()
    assert lines == [
        "00000000000000000000000000000000  a_first.wav",
        "ffffffffffffffffffffffffffffffff  z_last.wav",
    ]


# -- model validation ----------------------------------------------------


import pytest as _pytest_top  # noqa: E402


def _libsndfile_available() -> bool:
    try:
        from helixgen.ir import _load_libsndfile
        _load_libsndfile()
        return True
    except (OSError, RuntimeError):
        return False


def test_validate_model_rejects_non_stadium():
    """The model param refuses anything outside stadium / stadium_xl."""
    from mcp_server.tools import _validate_model
    with _pytest_top.raises(ValueError, match="unsupported model"):
        _validate_model("helix_floor")
    with _pytest_top.raises(ValueError, match="unsupported model"):
        _validate_model("")


def test_validate_model_accepts_stadium_and_xl():
    from mcp_server.tools import _validate_model
    _validate_model("stadium")
    _validate_model("stadium_xl")


def test_list_blocks_handler_rejects_bad_model(mcp_library):
    from mcp_server.tools import list_blocks_handler
    with _pytest_top.raises(ValueError, match="unsupported model"):
        list_blocks_handler(mcp_library, "hx_stomp")


def test_list_irs_handler_rejects_bad_model(tmp_path):
    from mcp_server.tools import list_irs_handler
    with _pytest_top.raises(ValueError, match="unsupported model"):
        list_irs_handler("not_a_model", irs_dir=tmp_path)


# -- compute_irhash ------------------------------------------------------


import struct as _struct  # noqa: E402


def _synth_wav_bytes(n_frames: int = 64, *, sr: int = 48000) -> bytes:
    """Minimal PCM_24 mono WAV. One non-zero sample (0x123456) at frame 0."""
    block_align = 3
    byte_rate = sr * block_align
    data = bytearray(n_frames * block_align)
    data[0:3] = (0x123456).to_bytes(3, "little", signed=True)
    fmt_chunk = (
        b"fmt " + _struct.pack("<I", 16)
        + _struct.pack("<HHIIHH", 1, 1, sr, byte_rate, block_align, 24)
    )
    data_chunk = b"data" + _struct.pack("<I", len(data)) + bytes(data)
    body = b"WAVE" + fmt_chunk + data_chunk
    return b"RIFF" + _struct.pack("<I", len(body)) + body


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_compute_irhash_returns_hash_and_reminder(tmp_path):
    """Happy path: synth WAV file → 32-char hex hash + non-empty reminder."""
    from mcp_server.tools import compute_irhash_handler
    wav = tmp_path / "ir.wav"
    _write_synth_wav_file(wav, n_frames=64)
    result = compute_irhash_handler("stadium_xl", str(wav))
    assert set(result.keys()) == {"irhash", "reminder"}
    assert len(result["irhash"]) == 32
    assert all(c in "0123456789abcdef" for c in result["irhash"])
    assert "Librarian" in result["reminder"]  # the upload-to-device reminder


def test_compute_irhash_rejects_bad_model(tmp_path):
    """Bad model → ValueError; we never touch the file."""
    from mcp_server.tools import compute_irhash_handler
    with _pytest_top.raises(ValueError, match="unsupported model"):
        compute_irhash_handler("helix_floor", str(tmp_path / "any.wav"))


def test_compute_irhash_rejects_missing_file(tmp_path):
    """Nonexistent path → ValueError before libsndfile."""
    from mcp_server.tools import compute_irhash_handler
    with _pytest_top.raises(ValueError, match="not found"):
        compute_irhash_handler("stadium_xl", str(tmp_path / "missing.wav"))


def test_compute_irhash_rejects_non_riff(tmp_path):
    """A file without RIFF/WAVE magic → ValueError before libsndfile."""
    from mcp_server.tools import compute_irhash_handler
    fake = tmp_path / "fake.wav"
    fake.write_bytes(b"NOT A WAVE FILE AT ALL" + b"\x00" * 100)
    with _pytest_top.raises(ValueError, match="RIFF/WAVE magic"):
        compute_irhash_handler("stadium_xl", str(fake))


# -- discover_irs --------------------------------------------------------


def _write_synth_wav_file(path, n_frames: int = 64, sr: int = 48000) -> None:
    """Write a synth WAV to a Path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_synth_wav_bytes(n_frames, sr=sr))


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_discover_irs_walks_directory_and_returns_hashes(tmp_path):
    """Local invocation: walks the dir, hashes each WAV, returns list of dicts."""
    from mcp_server.tools import discover_irs_handler
    _write_synth_wav_file(tmp_path / "a.wav", n_frames=64)
    _write_synth_wav_file(tmp_path / "sub" / "b.wav", n_frames=128)
    # non-WAV file is ignored
    (tmp_path / "readme.txt").write_text("not a wav")

    result = discover_irs_handler("stadium_xl", str(tmp_path))
    assert isinstance(result, list)
    assert len(result) == 2
    for entry in result:
        assert set(entry.keys()) == {"hash", "path", "basename"}
        assert len(entry["hash"]) == 32
    basenames = sorted(e["basename"] for e in result)
    assert basenames == ["a.wav", "b.wav"]


def test_discover_irs_rejects_bad_model(tmp_path):
    from mcp_server.tools import discover_irs_handler
    with _pytest_top.raises(ValueError, match="unsupported model"):
        discover_irs_handler("hx_stomp", str(tmp_path))


def test_discover_irs_rejects_non_directory(tmp_path):
    """Path that isn't a directory → ValueError."""
    f = tmp_path / "afile.txt"
    f.write_text("not a dir")
    from mcp_server.tools import discover_irs_handler
    with _pytest_top.raises(ValueError, match="not a directory"):
        discover_irs_handler("stadium_xl", str(f))


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_discover_irs_skips_unhashable_files(tmp_path):
    """Non-48k files are skipped silently; valid ones still returned."""
    from mcp_server.tools import discover_irs_handler
    _write_synth_wav_file(tmp_path / "good.wav", n_frames=64, sr=48000)
    _write_synth_wav_file(tmp_path / "bad.wav", n_frames=64, sr=44100)
    result = discover_irs_handler("stadium_xl", str(tmp_path))
    basenames = [e["basename"] for e in result]
    assert "good.wav" in basenames
    assert "bad.wav" not in basenames


# -- register_ir ---------------------------------------------------------


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_ir_writes_mapping(tmp_path):
    """Happy path: register one WAV → mapping.json gains a hash→canonical-path entry."""
    import json as _j
    from mcp_server.tools import register_ir_handler

    wav = tmp_path / "src" / "test.wav"
    _write_synth_wav_file(wav, n_frames=64)
    irs_dir = tmp_path / "irs"

    result = register_ir_handler("stadium_xl", str(wav), irs_dir=irs_dir)

    assert {"hash", "path", "reminder"} <= set(result.keys())
    assert len(result["hash"]) == 32
    assert all(c in "0123456789abcdef" for c in result["hash"])
    mapping_path = irs_dir / "mapping.json"
    assert mapping_path.exists()
    mapping = _j.loads(mapping_path.read_text())
    assert mapping[result["hash"]] == result["path"]


def test_register_ir_uses_cache_across_calls(tmp_path, monkeypatch):
    """A second register of an unchanged WAV serves the hash from the cache
    (zero recomputes). Stubs the hash fn, so no libsndfile is needed."""
    import helixgen.irhash_cache as ihc
    from mcp_server.tools import register_ir_handler

    calls: list[str] = []

    def _stub(p):
        calls.append(str(p))
        return "b" * 32

    monkeypatch.setattr(ihc, "compute_stadium_irhash", _stub)
    irs_dir = tmp_path / "irs"
    wav = tmp_path / "cab.wav"
    wav.write_bytes(b"RIFF\0\0\0\0WAVE")

    register_ir_handler("stadium_xl", str(wav), irs_dir=irs_dir)
    register_ir_handler("stadium_xl", str(wav), irs_dir=irs_dir)

    assert len(calls) == 1  # second call is a cache hit


def test_register_ir_rejects_bad_model(tmp_path):
    from mcp_server.tools import register_ir_handler
    with _pytest_top.raises(ValueError, match="unsupported model"):
        register_ir_handler("hx_stomp", str(tmp_path / "any.wav"), irs_dir=tmp_path)


def test_register_ir_rejects_missing_wav(tmp_path):
    """Wav path doesn't exist → ValueError before any libsndfile call."""
    from mcp_server.tools import register_ir_handler
    with _pytest_top.raises(ValueError, match="not found"):
        register_ir_handler("stadium_xl", str(tmp_path / "missing.wav"), irs_dir=tmp_path)


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_ir_idempotent_for_same_file(tmp_path):
    """Registering the same WAV twice → no error, mapping has one entry for that hash."""
    import json as _j
    from mcp_server.tools import register_ir_handler
    wav = tmp_path / "test.wav"
    _write_synth_wav_file(wav, n_frames=64)
    irs_dir = tmp_path / "irs"

    r1 = register_ir_handler("stadium_xl", str(wav), irs_dir=irs_dir)
    r2 = register_ir_handler("stadium_xl", str(wav), irs_dir=irs_dir)
    assert r1 == r2
    mapping = _j.loads((irs_dir / "mapping.json").read_text())
    assert list(mapping.keys()) == [r1["hash"]]


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_ir_refuses_conflict_without_force(tmp_path):
    """Same hash, different canonical path → IrMappingError surfaces as ValueError."""
    import shutil
    from mcp_server.tools import register_ir_handler
    irs_dir = tmp_path / "irs"
    wav = tmp_path / "a.wav"
    _write_synth_wav_file(wav, n_frames=64)
    register_ir_handler("stadium_xl", str(wav), irs_dir=irs_dir)
    moved = tmp_path / "elsewhere" / "b.wav"
    moved.parent.mkdir()
    shutil.copy(str(wav), str(moved))
    with _pytest_top.raises(ValueError, match="already mapped"):
        register_ir_handler("stadium_xl", str(moved), irs_dir=irs_dir)


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_ir_overwrites_with_force(tmp_path):
    """force=True replaces the canonical path for an already-mapped hash."""
    import shutil
    from mcp_server.tools import register_ir_handler
    irs_dir = tmp_path / "irs"
    wav = tmp_path / "a.wav"
    _write_synth_wav_file(wav, n_frames=64)
    r1 = register_ir_handler("stadium_xl", str(wav), irs_dir=irs_dir)
    moved = tmp_path / "elsewhere" / "b.wav"
    moved.parent.mkdir()
    shutil.copy(str(wav), str(moved))
    r2 = register_ir_handler("stadium_xl", str(moved), irs_dir=irs_dir, force=True)
    assert r2["hash"] == r1["hash"]
    assert r2["path"] != r1["path"]
    assert r2["path"].endswith("b.wav")


# -- register_irs (bulk directory) ---------------------------------------


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_irs_walks_directory_and_persists(tmp_path):
    """Happy path: bulk-register a directory → mapping.json gains one entry per WAV."""
    import json as _j
    from mcp_server.tools import register_irs_handler

    _write_synth_wav_file(tmp_path / "src" / "a.wav", n_frames=64)
    _write_synth_wav_file(tmp_path / "src" / "sub" / "b.wav", n_frames=128)
    irs_dir = tmp_path / "irs"

    result = register_irs_handler("stadium_xl", str(tmp_path / "src"), irs_dir=irs_dir)

    assert sorted(result["registered"]) == ["a.wav", "b.wav"]
    assert result["already_registered"] == []
    assert result["conflicts"] == []
    assert result["failed"] == []
    mapping = _j.loads((irs_dir / "mapping.json").read_text())
    assert len(mapping) == 2


def test_register_irs_rejects_bad_model(tmp_path):
    from mcp_server.tools import register_irs_handler
    with _pytest_top.raises(ValueError, match="unsupported model"):
        register_irs_handler("hx_stomp", str(tmp_path), irs_dir=tmp_path)


def test_register_irs_rejects_non_directory(tmp_path):
    """Path that isn't a directory → ValueError."""
    f = tmp_path / "notadir.txt"
    f.write_text("nope")
    from mcp_server.tools import register_irs_handler
    with _pytest_top.raises(ValueError, match="not a directory"):
        register_irs_handler("stadium_xl", str(f), irs_dir=tmp_path)


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_irs_idempotent_for_same_directory(tmp_path):
    """Second bulk call → everything in already_registered, nothing newly registered."""
    from mcp_server.tools import register_irs_handler
    _write_synth_wav_file(tmp_path / "src" / "a.wav", n_frames=64)
    _write_synth_wav_file(tmp_path / "src" / "b.wav", n_frames=128)
    irs_dir = tmp_path / "irs"

    r1 = register_irs_handler("stadium_xl", str(tmp_path / "src"), irs_dir=irs_dir)
    r2 = register_irs_handler("stadium_xl", str(tmp_path / "src"), irs_dir=irs_dir)

    assert sorted(r1["registered"]) == ["a.wav", "b.wav"]
    assert r2["registered"] == []
    assert sorted(r2["already_registered"]) == ["a.wav", "b.wav"]


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_irs_records_conflicts_without_force(tmp_path):
    """A WAV whose hash is already mapped to a different path lands in conflicts."""
    import shutil
    from mcp_server.tools import register_irs_handler
    irs_dir = tmp_path / "irs"
    src1 = tmp_path / "src1"
    src2 = tmp_path / "src2"
    _write_synth_wav_file(src1 / "shared.wav", n_frames=64)
    shutil.copytree(str(src1), str(src2))
    register_irs_handler("stadium_xl", str(src1), irs_dir=irs_dir)

    r = register_irs_handler("stadium_xl", str(src2), irs_dir=irs_dir)
    assert r["registered"] == []
    assert r["conflicts"] == ["shared.wav"]


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_irs_force_overwrites_conflict(tmp_path):
    """force=True moves conflicts into registered (new canonical path wins)."""
    import shutil
    from mcp_server.tools import register_irs_handler
    irs_dir = tmp_path / "irs"
    src1 = tmp_path / "src1"
    src2 = tmp_path / "src2"
    _write_synth_wav_file(src1 / "shared.wav", n_frames=64)
    shutil.copytree(str(src1), str(src2))
    register_irs_handler("stadium_xl", str(src1), irs_dir=irs_dir)

    r = register_irs_handler("stadium_xl", str(src2), irs_dir=irs_dir, force=True)
    assert r["registered"] == ["shared.wav"]
    assert r["conflicts"] == []


@_pytest_top.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_irs_collects_failed_files(tmp_path):
    """Per-file hashing errors land in `failed` without aborting the bulk run."""
    from mcp_server.tools import register_irs_handler
    _write_synth_wav_file(tmp_path / "src" / "good.wav", n_frames=64, sr=48000)
    _write_synth_wav_file(tmp_path / "src" / "bad.wav", n_frames=64, sr=44100)
    irs_dir = tmp_path / "irs"

    r = register_irs_handler("stadium_xl", str(tmp_path / "src"), irs_dir=irs_dir)
    assert r["registered"] == ["good.wav"]
    failed_basenames = [e["basename"] for e in r["failed"]]
    assert failed_basenames == ["bad.wav"]
    assert "reason" in r["failed"][0]


def test_register_irs_skips_non_wav_files(tmp_path):
    """Non-WAV files in the directory are ignored entirely (not in any bucket)."""
    from mcp_server.tools import register_irs_handler
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "readme.txt").write_text("not a wav")
    (tmp_path / "src" / "notes.md").write_text("# also not a wav")
    irs_dir = tmp_path / "irs"

    r = register_irs_handler("stadium_xl", str(tmp_path / "src"), irs_dir=irs_dir)
    assert r == {"registered": [], "already_registered": [], "conflicts": [], "failed": []}


def test_controller_mapping_handler_returns_canonical_set():
    """controller_mapping_handler returns the device-accurate assignable set."""
    from mcp_server.tools import controller_mapping_handler
    import json as _json

    mapping = controller_mapping_handler("stadium_xl")
    assert isinstance(mapping, list)
    ids = {row["id"] for row in mapping}
    assert "FS11" in ids
    assert "FS6" not in ids and "FS12" not in ids
    assert {"EXP1", "EXP2", "EXP1Toe"} <= ids
    # JSON-serialisable and carries English + aliases.
    _json.dumps(mapping)
    row = next(r for r in mapping if r["id"] == "FS5")
    assert row["english"] == "Footswitch 5 (top row, 5th from left)"
    assert row["aliases"]


def test_controller_mapping_handler_rejects_unknown_model():
    from mcp_server.tools import controller_mapping_handler
    import pytest as _pytest
    with _pytest.raises(ValueError):
        controller_mapping_handler("nord_stage")


# -- device_install_preset (file-read path) ------------------------------


def test_device_install_preset_rejects_missing_file(tmp_path):
    """Nonexistent .hsp path → ValueError before any device connection."""
    from mcp_server.tools import device_install_preset_handler
    with _pytest_top.raises(ValueError, match="not found"):
        device_install_preset_handler(
            "stadium_xl", hsp_path=str(tmp_path / "nope.hsp"), name="X", pos=1)


def test_device_install_preset_rejects_non_hsp(tmp_path):
    """A file without .hsp magic → ValueError before any device connection."""
    from mcp_server.tools import device_install_preset_handler
    bad = tmp_path / "bad.hsp"
    bad.write_bytes(b"NOTMAGIC{}")
    with _pytest_top.raises(ValueError, match="not a .hsp"):
        device_install_preset_handler(
            "stadium_xl", hsp_path=str(bad), name="X", pos=1)


def test_device_install_preset_reads_file_and_installs(tmp_path, monkeypatch, hsp_library):
    """Happy path with the device client + transcoder stubbed: the handler reads
    the .hsp body off disk, transcodes it (no template), pushes the blob into the
    target slot, and returns the cid."""
    import mcp_server.tools as tools_mod
    from helixgen.generate import compose_preset
    from helixgen.hsp import dumps_hsp
    from helixgen.spec import parse_spec

    preset = compose_preset(parse_spec(
        {"name": "D", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}),
        hsp_library, source="t")
    hsp = tmp_path / "d.hsp"
    hsp.write_bytes(dumps_hsp(preset))

    seen = {}

    class _Raw:
        def push_to_slot(self, container, pos, name, blob):
            seen["push"] = (container, pos, name, blob)
            return 4242

    class _FakeClient:
        _raw = _Raw()

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def find_by_pos(self, container, pos, *, strict=False): return None
        def device_ir_hashes(self): return set()

        def mutating(self):
            import contextlib
            return contextlib.nullcontext(self)

    def _fake_transcode(body, *, strict=True):
        seen["body"] = body
        return b"XCODED"

    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", lambda **kw: _FakeClient())
    monkeypatch.setattr("helixgen.device.transcode.hsp_to_sbepgsm", _fake_transcode)

    result = tools_mod.device_install_preset_handler(
        "stadium_xl", hsp_path=str(hsp), name="D", pos=3)

    # this preset references no IRs, so the (default auto_irs=True) IR check
    # runs but finds nothing missing -> irs comes back empty.
    assert result == {"ok": True, "cid": 4242, "irs": []}
    assert seen["push"][1:] == (3, "D", b"XCODED")   # (container, pos, name, blob)
    assert isinstance(seen["body"], dict) and "preset" in seen["body"]


def test_device_install_preset_aborts_on_listing_failure_no_write(
        tmp_path, monkeypatch, hsp_library):
    """#40: find_by_pos is now called strictly — a listing timeout must raise
    and abort BEFORE any IR upload or push_to_slot write is attempted (never
    silently read the unconfirmed slot as empty)."""
    import mcp_server.tools as tools_mod
    from helixgen.generate import compose_preset
    from helixgen.hsp import dumps_hsp
    from helixgen.spec import parse_spec
    from helixgen.device import HelixError

    preset = compose_preset(parse_spec(
        {"name": "D", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}),
        hsp_library, source="t")
    hsp = tmp_path / "d.hsp"
    hsp.write_bytes(dumps_hsp(preset))

    seen = {}

    class _Raw:
        def push_to_slot(self, container, pos, name, blob):
            seen["push"] = True
            return 4242

    class _FakeClient:
        _raw = _Raw()

        def __enter__(self): return self
        def __exit__(self, *a): return False

        def find_by_pos(self, container, pos, *, strict=False):
            seen["strict"] = strict
            raise HelixError("no reply listing container -2 (timeout or "
                             "connection drop); refusing to treat it as empty")

        def device_ir_hashes(self): return set()

        def mutating(self):
            import contextlib
            return contextlib.nullcontext(self)

    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", lambda **kw: _FakeClient())
    monkeypatch.setattr("helixgen.device.transcode.hsp_to_sbepgsm",
                        lambda body, strict=True: b"XCODED")

    with _pytest_top.raises(ValueError, match="no reply"):
        tools_mod.device_install_preset_handler(
            "stadium_xl", hsp_path=str(hsp), name="D", pos=3)
    assert seen.get("strict") is True
    assert "push" not in seen


def test_device_save_preset_aborts_on_listing_failure_no_write(monkeypatch):
    """#40: device_save_preset_handler's find_by_pos check is strict too —
    a listing timeout must raise and abort before save_edit_buffer_to runs."""
    import mcp_server.tools as tools_mod
    from helixgen.device import HelixError

    seen = {}

    class _Raw:
        def save_edit_buffer_to(self, container, pos, name):
            seen["save"] = True
            return 4242

    class _FakeClient:
        _raw = _Raw()

        def __enter__(self): return self
        def __exit__(self, *a): return False

        def find_by_pos(self, container, pos, *, strict=False):
            seen["strict"] = strict
            raise HelixError("no reply listing container -2")

    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", lambda **kw: _FakeClient())

    with _pytest_top.raises(ValueError, match="no reply"):
        tools_mod.device_save_preset_handler(
            "stadium_xl", name="D", pos=3)
    assert seen.get("strict") is True
    assert "save" not in seen


def _fake_client_cls(cid=4242):
    """Build a minimal fake HelixClient class for the auto_irs wiring tests
    below — device_ir_hashes is stubbed by the caller via bridge.check_irs
    (monkeypatched directly), so this fake's device_ir_hashes is never
    actually consulted."""
    class _Raw:
        def push_to_slot(self, container, pos, name, blob):
            return cid

    class _FakeClient:
        _raw = _Raw()

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def find_by_pos(self, container, pos, *, strict=False): return None
        def device_ir_hashes(self): return set()

        def mutating(self):
            import contextlib
            return contextlib.nullcontext(self)

    return _FakeClient


def test_device_install_preset_auto_irs_default_uploads_missing(
        tmp_path, monkeypatch, hsp_library):
    """auto_irs defaults to True: a missing IR is diffed + uploaded (via the
    shared ir_upload core) BEFORE the preset is pushed, and the per-IR result
    lands in result['irs']."""
    import mcp_server.tools as tools_mod
    from helixgen.generate import compose_preset
    from helixgen.hsp import dumps_hsp
    from helixgen.spec import parse_spec

    preset = compose_preset(parse_spec(
        {"name": "D", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}),
        hsp_library, source="t")
    hsp = tmp_path / "d.hsp"
    hsp.write_bytes(dumps_hsp(preset))

    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", lambda **kw: _fake_client_cls()())
    monkeypatch.setattr("helixgen.device.transcode.hsp_to_sbepgsm",
                        lambda body, strict=True: b"XCODED")
    monkeypatch.setattr("helixgen.device.bridge.check_irs",
                        lambda client, body: {"present": set(), "missing": {"aa11"}})

    calls = []

    def _fake_upload(ip, hashes):
        calls.append((ip, list(hashes)))
        return [{"hash": h, "ok": True, "outcome": "imported",
                 "note": f"imported IR x ({h})"} for h in hashes]

    monkeypatch.setattr("helixgen.device.ir_upload.upload_missing_irs", _fake_upload)

    result = tools_mod.device_install_preset_handler(
        "stadium_xl", hsp_path=str(hsp), name="D", pos=3, ip="9.9.9.9")

    assert calls == [("9.9.9.9", ["aa11"])]
    assert result["ok"] is True
    assert result["cid"] == 4242
    assert result["irs"] == [{"hash": "aa11", "ok": True, "outcome": "imported",
                              "note": "imported IR x (aa11)"}]


def test_device_install_preset_auto_irs_false_skips_upload(
        tmp_path, monkeypatch, hsp_library):
    """auto_irs=False: the missing IR is reported but never uploaded — the
    shared upload core is not invoked at all."""
    import mcp_server.tools as tools_mod
    from helixgen.generate import compose_preset
    from helixgen.hsp import dumps_hsp
    from helixgen.spec import parse_spec

    preset = compose_preset(parse_spec(
        {"name": "D", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}),
        hsp_library, source="t")
    hsp = tmp_path / "d.hsp"
    hsp.write_bytes(dumps_hsp(preset))

    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", lambda **kw: _fake_client_cls()())
    monkeypatch.setattr("helixgen.device.transcode.hsp_to_sbepgsm",
                        lambda body, strict=True: b"XCODED")
    monkeypatch.setattr("helixgen.device.bridge.check_irs",
                        lambda client, body: {"present": set(), "missing": {"aa11"}})

    calls = []
    monkeypatch.setattr("helixgen.device.ir_upload.upload_missing_irs",
                        lambda ip, hashes: calls.append((ip, list(hashes))))

    result = tools_mod.device_install_preset_handler(
        "stadium_xl", hsp_path=str(hsp), name="D", pos=3, auto_irs=False)

    assert calls == []  # upload core never invoked
    assert result["ok"] is True
    assert result["irs"] == [{
        "hash": "aa11", "ok": False, "outcome": "skipped_auto_irs_off",
        "note": ("IR aa11 is referenced but not on the device; enable "
                 "auto_irs, or import it (helixgen register-irs / the "
                 "editor), or the cab will be silent"),
    }]


def test_device_install_preset_untranscodable_hsp_touches_no_device(
        tmp_path, monkeypatch, hsp_library):
    """Transcode/validate runs FIRST (it's pure-offline): an untranscodable
    .hsp fails before ANY device work — no client connection, no IR uploads
    for a preset that was never going to install (and no IR results lost to
    an escaping transcode error)."""
    import mcp_server.tools as tools_mod
    from helixgen.generate import compose_preset
    from helixgen.hsp import dumps_hsp
    from helixgen.spec import parse_spec

    preset = compose_preset(parse_spec(
        {"name": "D", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}),
        hsp_library, source="t")
    hsp = tmp_path / "d.hsp"
    hsp.write_bytes(dumps_hsp(preset))

    device_touched = []

    def _no_client(**kw):
        device_touched.append("client")
        raise AssertionError("HelixClient must not be constructed")

    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", _no_client)
    monkeypatch.setattr("helixgen.device.ir_upload.upload_missing_irs",
                        lambda ip, hashes: device_touched.append("irs"))

    def _bad_transcode(body, *, strict=True):
        raise ValueError("unresolved model HD2_DoesNotExist")

    monkeypatch.setattr("helixgen.device.transcode.hsp_to_sbepgsm", _bad_transcode)

    with _pytest_top.raises(ValueError, match="unresolved model"):
        tools_mod.device_install_preset_handler(
            "stadium_xl", hsp_path=str(hsp), name="D", pos=3)

    assert device_touched == []  # no client, no IR uploads — nothing at all


def test_device_install_preset_missing_mapping_surfaces_error_but_still_installs(
        tmp_path, monkeypatch, hsp_library):
    """If the local IR mapping.json can't be loaded, the install still
    proceeds (unlike the CLI's hard `--auto-irs` abort) — the failure is
    surfaced per-hash in result['irs'] with outcome 'no_mapping' instead."""
    import mcp_server.tools as tools_mod
    from helixgen.generate import compose_preset
    from helixgen.hsp import dumps_hsp
    from helixgen.spec import parse_spec

    preset = compose_preset(parse_spec(
        {"name": "D", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}),
        hsp_library, source="t")
    hsp = tmp_path / "d.hsp"
    hsp.write_bytes(dumps_hsp(preset))

    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", lambda **kw: _fake_client_cls()())
    monkeypatch.setattr("helixgen.device.transcode.hsp_to_sbepgsm",
                        lambda body, strict=True: b"XCODED")
    monkeypatch.setattr("helixgen.device.bridge.check_irs",
                        lambda client, body: {"present": set(), "missing": {"aa11"}})

    import helixgen.ir as _ir

    def _broken_load(cls):
        raise OSError("mapping.json is corrupt")

    monkeypatch.setattr(_ir.IrMapping, "load", classmethod(_broken_load))

    result = tools_mod.device_install_preset_handler(
        "stadium_xl", hsp_path=str(hsp), name="D", pos=3)

    assert result["ok"] is True
    assert result["cid"] == 4242
    assert len(result["irs"]) == 1
    assert result["irs"][0]["hash"] == "aa11"
    assert result["irs"][0]["outcome"] == "no_mapping"
    assert result["irs"][0]["ok"] is False


def test_generate_preset_handler_surfaces_generate_warnings(mcp_library, tmp_path):
    """Generate-time stderr diagnostics (e.g. an unshowable EXP1Toe scribble
    label, a >12-char label) come back in the returned `warnings` list — an
    MCP caller cannot read the server's stderr."""
    from mcp_server.tools import generate_preset_handler

    amps = mcp_library.list_blocks(category="amp")
    spec = {
        "name": "MCP Warn Preset",
        "paths": [{"blocks": [{"block": amps[0].display_name}]}],
        "footswitches": [
            {"switch": "FS3", "block": amps[0].display_name,
             "label": "THIRTEEN CHR."},
        ],
    }
    out = tmp_path / "warn.hsp"
    result = generate_preset_handler(
        mcp_library, "stadium_xl", recipe=spec, out_path=str(out))
    assert out.exists()
    assert any("at most 12" in w for w in result["warnings"]), result["warnings"]
