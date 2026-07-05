"""Real-export acceptance test for the decompiler round-trip.

Skips automatically on a clean clone with no data/*.hsp exports.

Compares the slot **model** at every flow ``bNN`` (including the b00/b13
endpoints) for every real export in ``data/``. Passes when all present exports
round-trip. This bar does NOT assert endpoint routing-pointer or param
fidelity, nor the unmodeled ``sources``/``meta``/``xyctrl``/snapshot-validity
fields (a future cycle); only the hardware step exercises routing.
"""
import pytest
from pathlib import Path
from helixgen.ingest import ingest_path
from helixgen.library import Library
from helixgen.hsp import read_hsp
from helixgen.decompile import decompile_body
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.ir import IrMapping


def _real_hsp_library(tmp_path):
    data_dir = Path(__file__).resolve().parent.parent / "data"
    samples = sorted(data_dir.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    lib = Library(root=tmp_path / "lib")
    for s in samples:
        ingest_path(s, lib)
    return lib, samples


def _models(b):
    out = []
    for path in (b.get("preset") or {}).get("flow") or []:
        for k in sorted(path):
            if k.startswith("b") and k[1:].isdigit():
                slot = path[k].get("slot", [{}])[0]
                out.append(slot.get("model"))
    return out


def test_real_export_decompile_roundtrip_stable(tmp_path, strip_provenance):
    lib, samples = _real_hsp_library(tmp_path)
    irs = IrMapping.load()
    failures = []
    ok = 0
    for sample in samples:
        try:
            body = read_hsp(sample)
            spec = parse_spec(decompile_body(body, lib, irs=irs))
            regen = compose_preset(spec, lib, source=str(sample), irs=irs)
            assert _models(strip_provenance(regen)) == _models(strip_provenance(body))
            ok += 1
        except Exception as e:  # noqa: BLE001 — collect all failures before asserting, for a full report
            failures.append((sample.name, f"{type(e).__name__}: {e}"))
    assert not failures, (
        f"{ok}/{len(samples)} real exports round-trip; "
        f"{len(failures)} do not. "
        f"First few: {failures[:3]}"
    )
