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

    result = generate_preset_handler(mcp_library, "stadium_xl", spec=spec)

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
        generate_preset_handler(mcp_library, "stadium_xl", spec=spec)


def test_generate_preset_handler_sanitizes_filename(mcp_library):
    """Spec names with path separators or unsafe chars yield safe filenames."""
    from mcp_server.tools import generate_preset_handler

    amps = mcp_library.list_blocks(category="amp")
    cabs = mcp_library.list_blocks(category="cab")
    spec = {
        "name": "../../etc/passwd",
        "paths": [{"blocks": [{"block": amps[0].display_name}, {"block": cabs[0].display_name}]}],
    }
    result = generate_preset_handler(mcp_library, "stadium_xl", spec=spec)
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
        show_block_handler(mcp_library, "stadium_xl", name_or_id="Anything")


def test_generate_preset_handler_rejects_malformed_spec(mcp_library):
    """A spec missing required keys surfaces SpecError."""
    import pytest as _pytest
    from helixgen.spec import SpecError
    from mcp_server.tools import generate_preset_handler

    # Missing 'paths' is a structural failure caught by parse_spec.
    spec = {"name": "no paths here"}
    with _pytest.raises(SpecError):
        generate_preset_handler(mcp_library, "stadium_xl", spec=spec)


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
        generate_preset_handler(mcp_library, "stadium_xl", spec=spec)


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


import base64 as _b64  # noqa: E402
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
def test_compute_irhash_returns_hash_and_reminder():
    """Happy path: synth WAV → 32-char hex hash + non-empty reminder."""
    from mcp_server.tools import compute_irhash_handler
    wav_b64 = _b64.b64encode(_synth_wav_bytes()).decode("ascii")
    result = compute_irhash_handler("stadium_xl", wav_b64)
    assert set(result.keys()) == {"irhash", "reminder"}
    assert len(result["irhash"]) == 32
    assert all(c in "0123456789abcdef" for c in result["irhash"])
    assert "Librarian" in result["reminder"]  # the upload-to-device reminder


def test_compute_irhash_rejects_bad_model():
    """Bad model → ValueError; we never reach libsndfile."""
    from mcp_server.tools import compute_irhash_handler
    wav_b64 = _b64.b64encode(_synth_wav_bytes()).decode("ascii")
    with _pytest_top.raises(ValueError, match="unsupported model"):
        compute_irhash_handler("helix_floor", wav_b64)


def test_compute_irhash_rejects_oversize():
    """Decoded size > 2 MB → ValueError before libsndfile."""
    from mcp_server.tools import compute_irhash_handler
    oversize = b"\x00" * (3 * 1024 * 1024)
    wav_b64 = _b64.b64encode(oversize).decode("ascii")
    with _pytest_top.raises(ValueError, match=r"max .*2 MB"):
        compute_irhash_handler("stadium_xl", wav_b64)


def test_compute_irhash_rejects_non_riff():
    """Bytes without RIFF/WAVE magic → ValueError before libsndfile."""
    from mcp_server.tools import compute_irhash_handler
    fake = b"NOT A WAVE FILE AT ALL" + b"\x00" * 100
    wav_b64 = _b64.b64encode(fake).decode("ascii")
    with _pytest_top.raises(ValueError, match="RIFF/WAVE magic"):
        compute_irhash_handler("stadium_xl", wav_b64)


def test_compute_irhash_rejects_invalid_base64():
    """Garbage in wav_b64 → ValueError (not a crash)."""
    from mcp_server.tools import compute_irhash_handler
    with _pytest_top.raises(ValueError, match="valid base64"):
        compute_irhash_handler("stadium_xl", "this is not base64 at all!!!")


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


def test_discover_irs_refuses_when_hosted(tmp_path, monkeypatch):
    """HELIXGEN_HOSTED=1 → refuse with a clear redirect to compute_irhash."""
    monkeypatch.setenv("HELIXGEN_HOSTED", "1")
    from mcp_server.tools import discover_irs_handler
    with _pytest_top.raises(ValueError, match="hosted deploy"):
        discover_irs_handler("stadium_xl", str(tmp_path))


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


def test_register_ir_refuses_when_hosted(tmp_path, monkeypatch):
    """HELIXGEN_HOSTED=1 → refuse with a clear redirect to compute_irhash."""
    monkeypatch.setenv("HELIXGEN_HOSTED", "1")
    from mcp_server.tools import register_ir_handler
    wav = tmp_path / "any.wav"
    wav.write_bytes(b"placeholder")
    with _pytest_top.raises(ValueError, match="hosted deploy"):
        register_ir_handler("stadium_xl", str(wav), irs_dir=tmp_path)


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


def test_register_irs_refuses_when_hosted(tmp_path, monkeypatch):
    """HELIXGEN_HOSTED=1 → refuse with redirect to compute_irhash."""
    monkeypatch.setenv("HELIXGEN_HOSTED", "1")
    from mcp_server.tools import register_irs_handler
    with _pytest_top.raises(ValueError, match="hosted deploy"):
        register_irs_handler("stadium_xl", str(tmp_path), irs_dir=tmp_path)


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
