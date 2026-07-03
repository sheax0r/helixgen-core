"""Real-export acceptance test for the decompiler round-trip.

Skips automatically on a clean clone with no data/*.hsp exports.

Marked ``xfail``: the decompiler round-trip is known-incomplete for arbitrary
real device exports in v1 (see the marker reason and the hardening spec). This
test measures the gap rather than asserting a guarantee the feature does not yet
meet — when the hardening work lands and every export round-trips, it XPASSes,
which is the signal to drop the marker.
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
            if k.startswith("b") and k not in ("b00", "b13") and k[1:].isdigit():
                slot = path[k].get("slot", [{}])[0]
                out.append(slot.get("model"))
    return out


@pytest.mark.xfail(
    reason=(
        "Decompiler round-trip is known-incomplete for arbitrary real device "
        "exports in v1. Unsupported constructs: parallel split/looper routing "
        "(P35_* infrastructure blocks), duplicate same-model block instances "
        "referenced by footswitch/expression/snapshot, expression min/max "
        "outside [0,1], and IRs with no registered or default hash. As of "
        "2026-07-02, ~65/211 of the author's exports round-trip. Tracked in "
        "docs/superpowers/specs/2026-07-02-decompiler-real-preset-hardening.md."
    ),
    strict=False,
)
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
        except Exception as e:  # noqa: BLE001 — measuring the round-trip gap, not asserting per-preset
            failures.append((sample.name, f"{type(e).__name__}: {e}"))
    # When hardening lands, `failures` empties and this XPASSes — the signal to
    # remove the xfail marker. Until then it documents the measured gap.
    assert not failures, (
        f"{ok}/{len(samples)} real exports round-trip; "
        f"{len(failures)} do not (see xfail reason for the categories). "
        f"First few: {failures[:3]}"
    )
