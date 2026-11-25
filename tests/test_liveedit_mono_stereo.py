"""Mono↔stereo convert (defs.mono_stereo_twin + session.convert_mono_stereo).

A twin is the same effect in the opposite channel count (a same-category model
swap). Convert = swap to the twin + re-apply the shared params by NAME (pids
differ across the two models). Verified against real twin model ids.
"""
from __future__ import annotations

import pytest

from helixgen.device import defs
from helixgen.liveedit import chain, server
from helixgen.liveedit.session import LiveError, LiveSession
from tests.test_liveedit_chain import IN_INST1, IN_NONE, OUT_MATRIX, OUT_NONE, _blk, _eb
from tests.test_liveedit_session import FakeClient, FakeLease

ADRIATIC_MONO, ADRIATIC_STEREO = 130, 93   # HD2_DelayAdriaticDelay{Mono,Stereo}


# --- the twin resolver ------------------------------------------------------
def test_twin_resolves_both_directions():
    assert defs.mono_stereo_twin(ADRIATIC_MONO) == ADRIATIC_STEREO
    assert defs.mono_stereo_twin(ADRIATIC_STEREO) == ADRIATIC_MONO


def test_twin_is_none_for_a_model_without_one():
    assert defs.mono_stereo_twin(758) is None        # an amp — no mono/stereo twin


def test_twin_stays_in_category():
    # every resolved twin shares the source's category (never crosses)
    cats = defs.load_defs()["model_categories"]
    for name in list(cats)[:400]:
        mid = defs.model_id_for(name)
        tw = defs.mono_stereo_twin(mid)
        if tw is not None:
            assert defs.category_for(tw) == defs.category_for(mid)


# --- chain surfaces the current form ----------------------------------------
def _delay_eb(mid, mix=0.42):
    pid = defs.param_id_for(mid, "Mix")
    return _eb({0: _blk(IN_INST1), 1: _blk(mid, {pid: mix}),
                13: _blk(OUT_MATRIX), 14: _blk(IN_NONE), 27: _blk(OUT_NONE)}), pid


def test_chain_surfaces_ms_form():
    eb, _ = _delay_eb(ADRIATIC_MONO)
    st = chain.build_chain(eb)
    blk = next(b for b in (s["block"] for s in st["paths"][0]["segments"]
                           if s["kind"] == "block") if b["model_id"] == ADRIATIC_MONO)
    assert blk["ms_form"] == "mono"


def test_chain_no_ms_form_without_twin():
    st = chain.build_chain(_eb({0: _blk(IN_INST1), 2: _blk(758),  # amp, no twin
                                13: _blk(OUT_MATRIX)}))
    amp = next(b for b in (s["block"] for s in st["paths"][0]["segments"]
                           if s["kind"] == "block") if b["model_id"] == 758)
    assert "ms_form" not in amp


# --- the conversion ---------------------------------------------------------
def _sess(mid):
    eb, pid = _delay_eb(mid)
    s = LiveSession(FakeClient(eb=eb), lease=FakeLease())
    s.load()
    return s, pid


def test_convert_mono_to_stereo_swaps_to_twin():
    s, _ = _sess(ADRIATIC_MONO)
    s.convert_mono_stereo(0, 1)
    assert (0, 1, ADRIATIC_STEREO) in s.client.swaps       # swapped to the stereo twin


def test_convert_preserves_a_shared_param_by_name():
    s, mono_mix = _sess(ADRIATIC_MONO)
    s.convert_mono_stereo(0, 1)
    stereo_mix = defs.param_id_for(ADRIATIC_STEREO, "Mix")
    # the stereo twin's Mix pid was written with the source value (0.42), matched
    # by NAME even though the mono and stereo Mix pids differ
    wrote = [(pid, v) for (p, b, pid, v, *_) in s.client.params if b == 1]
    assert any(pid == stereo_mix and abs(v - 0.42) < 1e-6 for (pid, v) in wrote)


def test_convert_stereo_to_mono():
    s, _ = _sess(ADRIATIC_STEREO)
    s.convert_mono_stereo(0, 1)
    assert (0, 1, ADRIATIC_MONO) in s.client.swaps


def test_convert_rejects_a_block_without_a_twin():
    eb = _eb({0: _blk(IN_INST1), 2: _blk(758), 13: _blk(OUT_MATRIX)})  # amp
    s = LiveSession(FakeClient(eb=eb), lease=FakeLease())
    s.load()
    with pytest.raises(LiveError):
        s.convert_mono_stereo(0, 2)


def test_route_convert_dispatches():
    s, _ = _sess(ADRIATIC_MONO)
    out = server._route(s, "/convert-mono-stereo", {"path": 0, "block": 1})
    assert "paths" in out
    assert (0, 1, ADRIATIC_STEREO) in s.client.swaps
