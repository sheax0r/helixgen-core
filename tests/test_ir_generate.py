"""Generator emits slot-level irhash on IR blocks (spec.ir or canonical fallback)."""
import json
from pathlib import Path

import pytest

from helixgen.chassis import extract_chassis_from_hsp
from helixgen.generate import GenerateError, generate_preset
from helixgen.ingest import block_from_raw
from helixgen.ir import IrMapping
from helixgen.library import Library

HSP_MAGIC = b"rpshnosj"


def _read_hsp_body(path: Path) -> dict:
    raw = path.read_bytes()
    return json.loads(raw[len(HSP_MAGIC):])


def _first_ir_slot(body: dict) -> dict:
    for path_obj in body["preset"]["flow"]:
        for v in path_obj.values():
            if isinstance(v, dict) and "slot" in v:
                slot = v["slot"][0]
                if str(slot.get("model", "")).startswith("HX2_ImpulseResponse"):
                    return slot
    raise AssertionError("no IR slot in preset")


@pytest.fixture
def stadium_library_with_ir(tmp_library, sample_serial_preset_hsp):
    """A library bootstrapped with a Stadium chassis + one IR block carrying a default hash."""
    lib = Library(tmp_library)
    lib.save_chassis(extract_chassis_from_hsp(sample_serial_preset_hsp))
    src = {"preset": "reg.hsp", "firmware": "t", "date": "2026-05-28"}
    # Use the wire format that block_from_raw expects (.hlx-style: "@model", flat params)
    raw = {
        "@model": "HX2_ImpulseResponseWithPan",
        "irhash": "ad8182e1ebe9fd95dffde5dd54b6d89c",
        "HighCut": 20100.0, "LowCut": 19.9, "Mix": 1.0, "Pan": 0.5,
        "Level": -18.0, "Delay": 0.0, "IrData": 0, "Polarity": False,
    }
    lib.save_block_with_dedup(block_from_raw(raw, src))
    lib.rebuild_index()
    return lib


def test_generate_uses_canonical_irhash_when_spec_omits_ir(stadium_library_with_ir, tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "canon",
        "paths": [{"blocks": [{"block": "With Pan"}]}],
    }))
    out = tmp_path / "out.hsp"
    generate_preset(spec, out, stadium_library_with_ir)
    body = _read_hsp_body(out)
    assert _first_ir_slot(body)["irhash"] == "ad8182e1ebe9fd95dffde5dd54b6d89c"
    assert "irhash" not in _first_ir_slot(body).get("params", {})


def test_generate_uses_spec_ir_field_by_basename(stadium_library_with_ir, tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    wav = irs_dir / "Mix 05.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    m = IrMapping(irs_dir=irs_dir)
    m.register("da881f087ca8cf6be6266b564c8c7502", wav)
    m.save()

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "sugar",
        "paths": [{"blocks": [{"block": "With Pan", "ir": "Mix 05.wav"}]}],
    }))
    out = tmp_path / "out.hsp"
    generate_preset(spec, out, stadium_library_with_ir)
    body = _read_hsp_body(out)
    assert _first_ir_slot(body)["irhash"] == "da881f087ca8cf6be6266b564c8c7502"


def test_generate_uses_spec_ir_field_by_hash(stadium_library_with_ir, tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))
    m = IrMapping(irs_dir=irs_dir)
    wav = irs_dir / "x.wav"
    wav.write_bytes(b"RIFF")
    m.register("e93d155aedcf99109f7193f607707815", wav)
    m.save()

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "byhash",
        "paths": [{"blocks": [{"block": "With Pan",
                                "ir": "e93d155aedcf99109f7193f607707815"}]}],
    }))
    out = tmp_path / "out.hsp"
    generate_preset(spec, out, stadium_library_with_ir)
    body = _read_hsp_body(out)
    assert _first_ir_slot(body)["irhash"] == "e93d155aedcf99109f7193f607707815"


def test_generate_rejects_ir_field_on_non_ir_block(tmp_library, sample_serial_preset_hsp, sample_amp_block, tmp_path):
    """An `ir` field on a non-IR block must fail with a clear error."""
    lib = Library(tmp_library)
    lib.save_chassis(extract_chassis_from_hsp(sample_serial_preset_hsp))
    src = {"preset": "x.hsp", "firmware": "t", "date": "2026-05-28"}
    lib.save_block_with_dedup(block_from_raw(sample_amp_block, src))
    lib.rebuild_index()

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "wrong",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom", "ir": "foo.wav"}]}],
    }))
    with pytest.raises(GenerateError, match="not an IR block"):
        generate_preset(spec, tmp_path / "out.hsp", lib)


def test_generate_errors_when_no_canonical_and_no_spec_ir(tmp_library, sample_serial_preset_hsp, tmp_path):
    """An IR block with no canonical default and no spec ir field MUST fail loudly."""
    lib = Library(tmp_library)
    lib.save_chassis(extract_chassis_from_hsp(sample_serial_preset_hsp))
    src = {"preset": "x.hsp", "firmware": "t", "date": "2026-05-28"}
    raw = {
        "@model": "HX2_ImpulseResponseWithPan",
        # NB: no irhash
        "HighCut": 20100.0, "LowCut": 19.9, "Mix": 1.0, "Pan": 0.5,
        "Level": -18.0, "Delay": 0.0, "IrData": 0, "Polarity": False,
    }
    lib.save_block_with_dedup(block_from_raw(raw, src))
    lib.rebuild_index()

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "broken",
        "paths": [{"blocks": [{"block": "With Pan"}]}],
    }))
    out = tmp_path / "out.hsp"
    with pytest.raises(GenerateError, match="irhash"):
        generate_preset(spec, out, lib)
