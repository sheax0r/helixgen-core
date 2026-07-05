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
        "Decompiler round-trip is incomplete for arbitrary real device exports. "
        "The parallel-routing + hardening effort (2026-07-03), followed by the "
        "snapshot-coordinate-refs hardening pass, raised the pass rate from "
        "127/211 to 194/211 (dense snapshot arrays, coordinate-aware snapshot "
        "references, the IR-no-assign `no_ir` marker, empty-block-name "
        "fallback, expression-recovery filtering). Remaining residual "
        "categories, all in the deferred P35 branch-lane I/O follow-up: "
        "P35 output routing endpoints (`P35_OutputPath2A/2B`, "
        "`P35_OutputMatrix`) and P35 input routing endpoints "
        "(`P35_InputInst1`, `P35_InputMic`, `P35_InputNone`) that "
        "library.load_block cannot resolve on branch lanes, plus two "
        "one-off outliers (a 13-block path exceeding the 12 user slots, "
        "and an ambiguous IR basename match). Tracked in "
        "docs/superpowers/specs/2026-07-03-parallel-routing-and-hardening-design.md."
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
