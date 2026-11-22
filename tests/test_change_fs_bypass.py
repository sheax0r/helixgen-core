"""Registry entry: footswitch-bypass snapshot-assign (Task 3).

Pre-0.84 `wire_footswitch` attached a bypass controller to a block's
``@enabled`` without a per-snapshot ``snapshots`` array, so snapshot recall was
path-dependent. The migration pins a dense ``[bool(value)] * 8`` array. The
detector is content-based: a ``controller`` on ``@enabled`` with no dense
``snapshots`` array.
"""
from __future__ import annotations

from types import SimpleNamespace

from helixgen import changes, changes_entries  # noqa: F401  (registers entries)
from helixgen.generate import HSP_SNAPSHOT_SLOTS


def _entry():
    (e,) = [e for e in changes.REGISTRY if e.id == "fs-bypass-snapshot-assign"]
    return e


def _body():
    return {"preset": {"flow": [{
        # affected: bypass controller, no snapshots
        "b03": {"@enabled": {"value": False, "controller": {"source": 5}},
                "slot": {"0": {"model": "x"}}},
        # not affected: already dense
        "b05": {"@enabled": {"value": True, "controller": {"source": 6},
                             "snapshots": [True] * HSP_SNAPSHOT_SLOTS},
                "slot": {"0": {"model": "y"}}},
        # not affected: enabled block with no footswitch controller
        "b07": {"@enabled": {"value": True}, "slot": {"0": {"model": "z"}}},
        # odd shape: no @enabled at all -> skipped, no crash
        "b09": {"slot": {"0": {"model": "w"}}},
        # endpoint, ignored
        "input": {"ch1": {}},
    }]}}


def test_entry_is_registered():
    e = _entry()
    assert e.landed_in == "0.84.0"
    assert e.kind == "bugfix"


def test_detect_finds_only_stale_bypass_blocks():
    f = _entry().detect(SimpleNamespace(body=_body()))
    assert f.affected is True
    assert f.blocks == ["b03"]


def test_apply_pins_dense_snapshots_and_is_idempotent():
    ctx = SimpleNamespace(body=_body())
    e = _entry()
    first = e.apply(ctx)
    assert first.affected is True
    assert ctx.body["preset"]["flow"][0]["b03"]["@enabled"]["snapshots"] == [False] * HSP_SNAPSHOT_SLOTS
    # untouched neighbours
    assert ctx.body["preset"]["flow"][0]["b05"]["@enabled"]["snapshots"] == [True] * HSP_SNAPSHOT_SLOTS
    assert "snapshots" not in ctx.body["preset"]["flow"][0]["b07"]["@enabled"]
    # idempotent: a second detect sees nothing
    assert e.detect(ctx).affected is False


def test_missing_value_defaults_enabled():
    body = {"preset": {"flow": [{
        "b01": {"@enabled": {"controller": {"source": 5}}, "slot": {}},
    }]}}
    _entry().apply(SimpleNamespace(body=body))
    assert body["preset"]["flow"][0]["b01"]["@enabled"]["snapshots"] == [True] * HSP_SNAPSHOT_SLOTS
