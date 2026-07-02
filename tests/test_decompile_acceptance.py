"""Real-export acceptance test for the decompiler round-trip.

Skips automatically on a clean clone with no data/*.hsp exports.
"""
import pytest
from pathlib import Path
from helixgen.ingest import ingest_path
from helixgen.library import Library
from helixgen.hsp import read_hsp
from helixgen.decompile import decompile_body
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec


def _real_hsp_library(tmp_path):
    data_dir = Path(__file__).resolve().parent.parent / "data"
    samples = sorted(data_dir.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    lib = Library(root=tmp_path / "lib")
    for s in samples:
        ingest_path(s, lib)
    return lib, samples


def test_real_export_decompile_roundtrip_stable(tmp_path, strip_provenance):
    lib, samples = _real_hsp_library(tmp_path)
    for sample in samples:
        body = read_hsp(sample)
        spec = parse_spec(decompile_body(body, lib))
        regen = compose_preset(spec, lib, source=str(sample))
        # Compare flow block models — the load-bearing, decompiler-owned content.
        def models(b):
            out = []
            for path in (b.get("preset") or {}).get("flow") or []:
                for k in sorted(path):
                    if k.startswith("b") and k not in ("b00", "b13") and k[1:].isdigit():
                        slot = path[k].get("slot", [{}])[0]
                        out.append(slot.get("model"))
            return out
        assert models(strip_provenance(regen)) == models(strip_provenance(body)), sample.name
