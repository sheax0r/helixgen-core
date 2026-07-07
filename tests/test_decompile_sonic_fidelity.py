"""Sonic-fidelity acceptance test for the decompiler round-trip.

Skips on a clean clone with no data/*.hsp. Complements the model bar
(test_decompile_acceptance.py) by asserting each USER block's audible state:
on-load (snapshot-0) bypass, effective per-snapshot bypass over NAMED snapshots
(source-null cells skipped as undefined recall), every slot's model AND
snapshot-0 param values, the verbatim harness, and favorite.

generate normalizes activesnapshot -> 0, so the live `@enabled.value` / param
`value` the device recalls on load mirrors snapshot 0 — a source `value` that
reflected a non-zero active snapshot is deliberately not preserved. Comparisons
therefore use the snapshot-0 effective state, not the raw stored value.

Deliberately NOT asserted (see the 2026-07-05 design spec): source-null named
snapshot cells (~30 presets, densified to True — Category-4-consistent),
redundant all-True snapshot arrays, unnamed trailing snapshot slots, top-level
sources/meta/xyctrl/snapshot metadata, and non-FS bypass-assign controllers.
Also NOT asserted: favorite reconstruction — generate emits a constant 0 for
every block and decompile does not capture favorite in the spec at all, so
the block-level `favorite` compare below only confirms both sides are the
corpus-wide constant 0, not that a real value round-trips.
"""
import pytest
from pathlib import Path
from helixgen.ingest import ingest_path
from helixgen.library import Library
from helixgen.hsp import read_hsp, _unwrap_value
from helixgen.decompile import decompile_body, _snapshot_names
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


def _user_blocks(body):
    """Yield (pi, key, bnn) for each user block (skip endpoints/split/join)."""
    for pi, path in enumerate((body.get("preset") or {}).get("flow") or []):
        if not isinstance(path, dict):
            continue
        for k in path:
            if not (isinstance(k, str) and k.startswith("b") and k[1:].isdigit()):
                continue
            bnn = path[k]
            if not isinstance(bnn, dict) or not bnn.get("slot"):
                continue
            if bnn.get("type") in ("input", "output", "split", "join"):
                continue
            yield pi, k, bnn


def _base(bnn):
    return _unwrap_value(bnn.get("@enabled", True))


def _snap_array(bnn):
    en = bnn.get("@enabled")
    return en.get("snapshots") if isinstance(en, dict) else None


def _effective(bnn, i):
    """Effective bypass in snapshot i: snapshots[i] if present-and-non-null,
    else the base value."""
    arr = _snap_array(bnn)
    if isinstance(arr, list) and i < len(arr) and arr[i] is not None:
        return arr[i]
    return _base(bnn)


def _slot_models(bnn):
    return [s.get("model") for s in bnn.get("slot") or []]


def _param_onload(wrapped):
    """A param's effective value in snapshot 0 (the state the device recalls on
    load, since generate normalizes activesnapshot -> 0): snapshots[0] if
    present-and-non-null, else the plain value."""
    if isinstance(wrapped, dict):
        arr = wrapped.get("snapshots")
        if isinstance(arr, list) and len(arr) > 0 and arr[0] is not None:
            return arr[0]
    return _unwrap_value(wrapped)


def _slot_param_values(bnn):
    """Per-slot dict of each param's snapshot-0 effective value (the knob value
    the device shows on load). Compared source-vs-regen: generate forces
    activesnapshot=0, so a source `value` that reflected a non-zero active
    snapshot is not preserved — the on-load (snapshot-0) state is."""
    out = []
    for s in bnn.get("slot") or []:
        out.append({k: _param_onload(v) for k, v in (s.get("params") or {}).items()})
    return out


def test_real_export_sonic_fidelity(tmp_path):
    lib, samples = _real_hsp_library(tmp_path)
    irs = IrMapping.load()
    failures = []
    ok = 0
    for sample in samples:
        try:
            body = read_hsp(sample)
            n_named = len(_snapshot_names(body))
            spec = parse_spec(decompile_body(body, lib, irs=irs))
            regen = compose_preset(spec, lib, source=str(sample), irs=irs)
            s_blocks = {(pi, k): bnn for pi, k, bnn in _user_blocks(body)}
            r_blocks = {(pi, k): bnn for pi, k, bnn in _user_blocks(regen)}
            assert set(s_blocks) == set(r_blocks), "block key set differs"
            for kk, sb in s_blocks.items():
                rb = r_blocks[kk]
                # generate normalizes activesnapshot -> 0, so the on-load bypass
                # `value` mirrors snapshot 0, not a source `value` tied to a
                # non-zero active snapshot. Assert the snapshot-0 effective
                # bypass, skipping source-null snapshot-0 cells (undefined recall).
                sa0 = _snap_array(sb)
                if not (isinstance(sa0, list) and len(sa0) > 0 and sa0[0] is None):
                    assert _effective(sb, 0) == _effective(rb, 0), f"{kk} on-load bypass"
                for i in range(n_named):
                    sa = _snap_array(sb)
                    # skip source-null/absent named cells (undefined recall)
                    if not (isinstance(sa, list) and i < len(sa) and sa[i] is not None):
                        continue
                    assert _effective(sb, i) == _effective(rb, i), f"{kk} snap {i}"
                assert _slot_models(sb) == _slot_models(rb), f"{kk} slot models"
                assert _slot_param_values(sb) == _slot_param_values(rb), f"{kk} params"
                assert sb.get("harness") == rb.get("harness"), f"{kk} harness"
                # NOTE: today this is constant-vs-constant, not a reconstruction
                # check — generate hardcodes favorite: 0 on every block, decompile
                # never round-trips favorite through the spec, and the whole corpus
                # is uniformly favorite == 0. It still guards against a future
                # nonzero-favorite fixture or a generate change that stops emitting 0.
                assert sb.get("favorite") == rb.get("favorite"), f"{kk} favorite"
            ok += 1
        except Exception as e:  # noqa: BLE001 — collect all before asserting
            failures.append((sample.name, f"{type(e).__name__}: {e}"))
    assert not failures, (
        f"{ok}/{len(samples)} real exports sonically round-trip; "
        f"{len(failures)} do not. First few: {failures[:3]}"
    )
