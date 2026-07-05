from helixgen.generate import _build_snapshot_overrides, _wrap_value_with_snapshots, resolve_blocks
from helixgen.spec import parse_spec


def test_snapshot_override_resolves_by_coordinate(hsp_library):
    """Two placed blocks share a display_name (a split-style duplicate); a
    snapshot disables one by coordinate and param-overrides the other by
    coordinate. The override must land on the coordinate-selected chain
    index, not just the first match.
    """
    spec = parse_spec({
        "name": "P",
        "paths": [{"blocks": [
            {"block": "Tube Drive", "lane": 0, "pos": 1},
            {"block": "Tube Drive", "lane": 0, "pos": 2},
        ]}],
        "snapshots": [{"name": "A", "params": [
            {"block": "Tube Drive", "lane": 0, "pos": 2, "params": {"Gain": 0.4}}]}],
    })
    resolved = resolve_blocks(spec, hsp_library)
    _enabled, param_map = _build_snapshot_overrides(spec, resolved)
    # chain index 1 (pos 2) carries the override, not chain index 0
    assert (0, 1) in param_map and (0, 0) not in param_map


def test_wrap_densifies_enabled_array():
    # base True (block enabled), disabled only in snapshot 0
    overrides = [False, None, None, None, None, None, None, None]
    wrapped = _wrap_value_with_snapshots(True, overrides)
    assert wrapped["snapshots"] == [False, True, True, True, True, True, True, True]


def test_wrap_densifies_param_array():
    overrides = [None, 0.30, None, None, None, None, None, None]
    wrapped = _wrap_value_with_snapshots(0.12, overrides)
    assert wrapped["snapshots"] == [0.12, 0.30, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12]


def test_wrap_no_variation_stays_plain():
    assert _wrap_value_with_snapshots(0.5, [None] * 8) == {"value": 0.5}
    assert _wrap_value_with_snapshots(0.5, None) == {"value": 0.5}
