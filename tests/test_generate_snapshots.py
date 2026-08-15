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


def test_wrap_value_mirrors_active_snapshot_zero():
    """`value` is the device's live/on-load state and must mirror
    snapshots[activesnapshot]. activesnapshot is always 0, so when snapshot 0
    overrides the value, `value` must equal snapshots[0] — not the base.
    Real device exports always keep value == snapshots[activesnapshot]."""
    wrapped = _wrap_value_with_snapshots(0.12, [0.30, None, None, None, None, None, None, None])
    assert wrapped["snapshots"][0] == 0.30
    assert wrapped["value"] == 0.30


def test_wrap_value_matches_base_when_snapshot_zero_unset():
    """When snapshot 0 has no override, `value` stays at the base (== snaps[0])."""
    wrapped = _wrap_value_with_snapshots(0.12, [None, 0.30, None, None, None, None, None, None])
    assert wrapped["snapshots"][0] == 0.12
    assert wrapped["value"] == 0.12


def test_a_legacy_alias_resolves_in_snapshot_refs(hsp_library):
    """A recipe written against a pre-hgc-3ll library names blocks by their OLD
    display names. Placement already accepted those aliases; the snapshot and
    footswitch resolvers did not, so such a recipe placed its blocks fine and
    then failed on its own `disable` list for the same block. The WHOLE recipe
    has to resolve, not just `paths`.
    """
    from helixgen.library import Block, Library

    # store a block under a superseded name: loading it makes that name a
    # legacy alias, exactly as an un-migrated user library does
    hsp_library.save_block(Block(
        model_id="HX2_CompressorLAStudioCompMono", category="dynamics",
        display_name="ressor LAStudio Comp Mono",
        params={"Gain": {"type": "float"}},
        exemplar={"@model": "HX2_CompressorLAStudioCompMono", "@type": "fx",
                  "@enabled": True, "Gain": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-08-14"}))
    reloaded = Library(root=hsp_library.root)
    old_name = "ressor LAStudio Comp Mono"
    block = reloaded.find_block(old_name)
    assert block is not None and old_name in block.aliases
    assert block.display_name != old_name, "the stored name should be superseded"

    spec = parse_spec({
        "name": "P",
        "paths": [{"blocks": [{"block": old_name}]}],
        "snapshots": [{"name": "A"}, {"name": "B", "disable": [old_name]}],
    })
    resolved = resolve_blocks(spec, reloaded)
    enabled, _params = _build_snapshot_overrides(spec, resolved)
    assert enabled, f"snapshot ref by legacy alias {old_name!r} did not resolve"
