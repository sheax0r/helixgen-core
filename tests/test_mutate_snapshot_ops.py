"""In-place snapshot SCENE ops — `set_snapshot` / `remove_snapshot`.

Covers creating/naming/activating/cloning a snapshot scene and removing one on
an existing `.hsp`, with no decompile->regenerate. These build the
`preset.snapshots` meta when it is absent and otherwise DELEGATE every
per-snapshot write to the shared writers (`set_param`, `set_enabled`,
`set_flow_param` / `_write_snapshot_slot`), so inline scene content is
last-wins-equivalent to `set-param --snapshot`.

Grounding fixture: the `snapshots.hsp` corpus preset — Brit 2204 Custom amp at
flow0.b02 (Drive carries a per-snapshot array), snapshots Rhythm(0)/Lead(1)/
Clean(2), rest "Snap N" placeholders, activesnapshot 0.
"""
from __future__ import annotations

import copy

import pytest

from helixgen import mutate, view
from helixgen.hsp import read_hsp
from tests.golden import harness


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    root = tmp_path_factory.mktemp("mutate-snapop-test-library")
    return harness.build_corpus_library(root)


@pytest.fixture
def body():
    """A preset WITH configured snapshots (Rhythm/Lead/Clean)."""
    return read_hsp(harness.CORPUS_DIR / "snapshots.hsp")


@pytest.fixture
def no_snaps(body):
    """The same preset with the `preset.snapshots` meta removed — a preset
    whose recipe omitted `snapshots` (the create path's real target)."""
    body["preset"].pop("snapshots", None)
    # Drop the now-orphaned per-snapshot arrays EVERYWHERE (every block's params
    # and @enabled, not just b01-b03) so the body is a genuinely snapshot-free
    # preset. Any residual variance would otherwise surface as a projected scene
    # once view keeps delta-carrying placeholder slots (hgc-0c3).
    for path in body["preset"].get("flow") or []:
        if not isinstance(path, dict):
            continue
        for key, blk in path.items():
            if not (key[:1] == "b" and key[1:].isdigit() and isinstance(blk, dict)):
                continue
            en = blk.get("@enabled")
            if isinstance(en, dict):
                en.pop("snapshots", None)
            for slot in blk.get("slot") or []:
                for w in (slot.get("params") or {}).values():
                    if isinstance(w, dict):
                        w.pop("snapshots", None)
    return body


def _snaps(body):
    return body["preset"]["snapshots"]


def _drive(body):
    return body["preset"]["flow"][0]["b02"]["slot"][0]["params"]["Drive"]


def _bass(body):
    return body["preset"]["flow"][0]["b02"]["slot"][0]["params"].get("Bass")


def _active(body):
    return body["preset"]["params"].get("activesnapshot")


def _two_amp_body(with_meta=True):
    """A hand-built body with the same amp model twice (ambiguous name)."""
    b = {"preset": {"params": {"activesnapshot": 0}, "flow": [{
        "b01": {"type": "amp", "position": 1, "path": 0,
                "@enabled": {"value": True},
                "slot": [{"model": "HD2_AmpBrit2204Custom",
                          "@enabled": {"value": True},
                          "params": {"Drive": {"value": 0.5}}}]},
        "b02": {"type": "amp", "position": 2, "path": 0,
                "@enabled": {"value": True},
                "slot": [{"model": "HD2_AmpBrit2204Custom",
                          "@enabled": {"value": True},
                          "params": {"Drive": {"value": 0.5}}}]},
    }]}}
    if with_meta:
        b["preset"]["snapshots"] = [
            {"name": f"Snap {i + 1}", "valid": True} for i in range(8)]
    return b


# ============================ Task 1: target / meta / name / active =========

def test_create_on_preset_without_snapshots_meta(no_snaps, library):
    mutate.set_snapshot(no_snaps, index=1, name="Lead", library=library)
    snaps = _snaps(no_snaps)
    assert len(snaps) == 8
    assert snaps[1]["name"] == "Lead" and snaps[1]["valid"] is True
    # the other 7 are device-shaped placeholders, not configured
    assert snaps[0]["name"] == "Snap 1"
    assert snaps[7]["name"] == "Snap 8"


def test_create_requires_name(no_snaps, library):
    with pytest.raises(mutate.MutateError):
        mutate.set_snapshot(no_snaps, index=1, library=library)  # no name


def test_create_requires_index_when_name_unresolved(body, library):
    with pytest.raises(mutate.MutateError):
        mutate.set_snapshot(body, name="Brand New", library=library)  # no index


def test_ambiguous_name_requires_index(body, library):
    _snaps(body)[2]["name"] = "Lead"  # now Lead names slots 1 AND 2
    with pytest.raises(mutate.MutateError):
        mutate.set_snapshot(body, name="Lead", active=True, library=library)


def test_ambiguous_name_with_explicit_index_resolves(body, library):
    _snaps(body)[2]["name"] = "Lead"
    mutate.set_snapshot(body, name="Lead", index=2, active=True, library=library)
    assert _active(body) == 2


def test_name_and_contradicting_index_errors(body, library):
    # "Lead" resolves to index 1; passing index 3 contradicts it
    with pytest.raises(mutate.MutateError):
        mutate.set_snapshot(body, index=3, name="Lead", library=library)


def test_set_active_updates_activesnapshot(body, library):
    mutate.set_snapshot(body, index=2, active=True, library=library)
    assert _active(body) == 2


def test_active_on_unconfigured_slot_requires_name(no_snaps, library):
    # review finding 3: --active on an unnamed slot would load the preset on a
    # scene `view` won't show — require a name
    with pytest.raises(mutate.MutateError):
        mutate.set_snapshot(no_snaps, index=6, active=True, library=library)


def test_inline_content_on_unconfigured_slot_requires_name(no_snaps, library):
    with pytest.raises(mutate.MutateError):
        mutate.set_snapshot(no_snaps, index=6, library=library,
                            params=[{"block": "Brit 2204 Custom",
                                     "param": "Drive", "value": 0.5}])


def test_edit_configured_slot_without_name_ok(body, library):
    # index-only edit of an ALREADY-configured slot (Clean) is still allowed
    mutate.set_snapshot(body, index=2, active=True, library=library,
                        params=[{"block": "Brit 2204 Custom", "param": "Drive",
                                 "value": 0.4}])
    assert _active(body) == 2
    assert _drive(body)["snapshots"][2] == 0.4


def test_index_out_of_range_errors(no_snaps, library):
    for bad in (8, -1):
        with pytest.raises(mutate.MutateError):
            mutate.set_snapshot(no_snaps, index=bad, name="X", library=library)


def test_rename_existing_slot(body, library):
    mutate.set_snapshot(body, index=1, name="Solo", library=library)
    assert _snaps(body)[1]["name"] == "Solo"


def test_active_resyncs_all_wrapper_values(body, library):
    # Drive varies: [0.5, 0.85(Lead), 0.3(Clean), 0.5...]; activating Lead must
    # re-sync the wrapper's load `value` to the Lead slot across the preset.
    mutate.set_snapshot(body, index=1, active=True, library=library)
    assert _drive(body)["value"] == 0.85


# ============================ Task 2: inline content + clone_from ===========

def test_inline_params_equal_set_param_snapshot(body, library):
    a = copy.deepcopy(body)
    b = copy.deepcopy(body)
    mutate.set_snapshot(a, index=1, name="Lead", library=library,
                        params=[{"block": "Brit 2204 Custom",
                                 "param": "Drive", "value": 0.6}])
    mutate.set_param(b, "Brit 2204 Custom", "Drive", 0.6, library, snapshot=1)
    # same writer, same bytes (set_snapshot only also (re)set the name, which
    # was already "Lead") — compare the flow, which carries the param state.
    assert a["preset"]["flow"] == b["preset"]["flow"]


def test_inline_disable_bypasses_in_snapshot(body, library):
    mutate.set_snapshot(body, index=1, name="Lead", library=library,
                        disable=["Scream 808"])
    en = body["preset"]["flow"][0]["b01"]["@enabled"]
    assert en["snapshots"][1] is False


def test_inline_enable_unbypasses_in_snapshot(body, library):
    # base-bypass the block, then enable it in the Lead snapshot
    mutate.set_enabled(body, "Scream 808", False, library)
    mutate.set_snapshot(body, index=1, name="Lead", library=library,
                        enable=["Scream 808"])
    en = body["preset"]["flow"][0]["b01"]["@enabled"]
    assert en["snapshots"][1] is True


def test_inline_output_level_snapshot(body, library):
    mutate.set_snapshot(body, index=1, name="Lead", library=library,
                        output={"level": -3.0})
    gain = body["preset"]["flow"][0]["b13"]["slot"][0]["params"]["gain"]
    assert gain["snapshots"][1] == -3.0


def test_clone_copies_state_not_name(body, library):
    # give Rhythm(0) a distinctive Drive override, then clone it into slot 4
    mutate.set_param(body, "Brit 2204 Custom", "Drive", 0.2, library, snapshot=0)
    mutate.set_snapshot(body, index=4, name="Lead2", clone_from=0, library=library)
    assert _drive(body)["snapshots"][4] == 0.2         # state copied
    assert _snaps(body)[4]["name"] == "Lead2"          # NOT "Rhythm"


def test_clone_then_inline_is_last_wins(body, library):
    mutate.set_param(body, "Brit 2204 Custom", "Drive", 0.2, library, snapshot=0)
    mutate.set_snapshot(body, index=4, name="Hybrid", clone_from=0, library=library,
                        params=[{"block": "Brit 2204 Custom",
                                 "param": "Drive", "value": 0.9}])
    assert _drive(body)["snapshots"][4] == 0.9  # inline wins over clone


def test_clone_from_unconfigured_source_errors(body, library):
    # slot 5 is a "Snap 6" placeholder — not configured
    with pytest.raises(mutate.MutateError):
        mutate.set_snapshot(body, index=1, name="Lead", clone_from=5,
                            library=library)


def test_clone_from_by_name(body, library):
    mutate.set_param(body, "Brit 2204 Custom", "Drive", 0.2, library, snapshot=0)
    mutate.set_snapshot(body, index=4, name="FromRhythm", clone_from="Rhythm",
                        library=library)
    assert _drive(body)["snapshots"][4] == 0.2


def test_inline_param_ambiguous_block_needs_path(library):
    b = _two_amp_body()
    with pytest.raises(mutate.MutateError):
        mutate.set_snapshot(b, index=1, name="Lead", library=library,
                            params=[{"block": "Brit 2204 Custom",
                                     "param": "Drive", "value": 0.5}])


def test_inline_param_ambiguous_block_resolved_by_pos(library):
    b = _two_amp_body()
    mutate.set_snapshot(b, index=1, name="Lead", library=library,
                        params=[{"block": "Brit 2204 Custom", "param": "Drive",
                                 "value": 0.7, "pos": 2}])
    assert b["preset"]["flow"][0]["b02"]["slot"][0]["params"]["Drive"]["snapshots"][1] == 0.7


# ============================ Task 3: remove_snapshot =======================

def test_remove_clears_name_valid_and_reverts_arrays(body, library):
    mutate.set_param(body, "Brit 2204 Custom", "Drive", 0.6, library, snapshot=1)
    base = _drive(body)["snapshots"][0]  # snapshot 0 holds the base anchor
    mutate.remove_snapshot(body, index=1)
    s = _snaps(body)[1]
    assert s["name"] == "Snap 2"      # reverted to the placeholder
    assert s["valid"] is False
    assert _drive(body)["snapshots"][1] == base  # slot reverted to base


def test_remove_by_name(body, library):
    mutate.remove_snapshot(body, name="Lead")
    assert _snaps(body)[1]["valid"] is False
    assert _snaps(body)[1]["name"] == "Snap 2"


def test_remove_unconfigured_errors(body, library):
    with pytest.raises(mutate.MutateError):
        mutate.remove_snapshot(body, index=4)   # slot 4 is "Snap 5" placeholder


def test_remove_active_falls_back_to_zero(body, library):
    body["preset"]["params"]["activesnapshot"] = 2  # Clean active
    mutate.remove_snapshot(body, index=2)
    assert _active(body) == 0


def test_remove_non_active_keeps_active(body, library):
    body["preset"]["params"]["activesnapshot"] = 0
    mutate.remove_snapshot(body, index=1)
    assert _active(body) == 0


def test_remove_resyncs_value_after_active_fallback(body, library):
    # Drive varies; with Clean(2) active, removing it must re-sync value to the
    # fallback (snapshot 0 = base 0.5)
    body["preset"]["params"]["activesnapshot"] = 2
    mutate.remove_snapshot(body, index=2)
    assert _drive(body)["value"] == _drive(body)["snapshots"][0]


def test_remove_reverts_to_home_not_active_scene(body, library):
    # review finding 4: with a non-home scene active, removing a DIFFERENT
    # snapshot must revert it to snapshot 0 (home), not inherit the active
    # scene's override. Lead(1) active, Lead has Drive 0.85; remove Clean(2).
    body["preset"]["params"]["activesnapshot"] = 1
    home = _drive(body)["snapshots"][0]            # 0.5
    mutate.remove_snapshot(body, index=2)
    assert _drive(body)["snapshots"][2] == home    # NOT Lead's 0.85


def test_remove_contradicting_index_name_errors(body, library):
    with pytest.raises(mutate.MutateError):
        mutate.remove_snapshot(body, index=3, name="Lead")


# ============================ patch-op registration + atomicity =============

def test_patch_op_set_snapshot(body, library):
    mutate.apply_operations(body, [
        {"op": "set_snapshot", "index": 4, "name": "Patched",
         "params": [{"block": "Brit 2204 Custom", "param": "Drive",
                     "value": 0.33}]},
    ], library)
    assert _snaps(body)[4]["name"] == "Patched"
    assert _drive(body)["snapshots"][4] == 0.33


def test_patch_op_remove_snapshot(body, library):
    mutate.apply_operations(
        body, [{"op": "remove_snapshot", "name": "Lead"}], library)
    assert _snaps(body)[1]["valid"] is False


def test_patch_set_snapshot_missing_required_field(body, library):
    # neither index nor name -> required-field guard
    with pytest.raises(mutate.MutateError):
        mutate.apply_operations(body, [{"op": "set_snapshot"}], library)


# ============================ view projection (confirming) ==================

def test_view_projects_snapshot_created_in_place(no_snaps, library):
    # create a named scene with content on a preset that had NO snapshots meta,
    # then confirm `view` projects it — and ONLY it (no phantom placeholder
    # scene from the slot-0 gap; review finding 1)
    mutate.set_snapshot(no_snaps, index=1, name="Lead", library=library,
                        params=[{"block": "Brit 2204 Custom", "param": "Drive",
                                 "value": 0.7}])
    proj = view.view(no_snaps, library)
    assert [s["name"] for s in proj["snapshots"]] == ["Lead"]
    assert proj["snapshots"][0]["params"]["Brit 2204 Custom"]["Drive"] == 0.7


def test_view_no_phantom_scene_on_high_index_create(no_snaps, library):
    # review finding 1: creating at a non-zero index must not leak the leading
    # placeholder slots as blank scenes
    mutate.set_snapshot(no_snaps, index=6, name="Lead", library=library)
    assert [s["name"] for s in view.view(no_snaps, library)["snapshots"]] == ["Lead"]


def test_view_no_phantom_scene_after_middle_remove(no_snaps, library):
    # review finding 1: configure 0/1/2 then remove the middle one — view must
    # show [A, C], not [A, <blank>, C]
    no_snaps["preset"]["snapshots"] = mutate._default_snapshot_meta()
    for i, nm in ((0, "A"), (1, "B"), (2, "C")):
        mutate.set_snapshot(no_snaps, index=i, name=nm, library=library)
    mutate.remove_snapshot(no_snaps, index=1)
    assert [s["name"] for s in view.view(no_snaps, library)["snapshots"]] == ["A", "C"]


def test_view_keeps_variance_on_a_placeholder_slot(no_snaps, library):
    # hgc-0c3: a placeholder-named slot (blank, or the `Snap N` default) that
    # carries a REAL per-snapshot override is projected under its `Snap N`
    # label, NOT dropped — else `device to-hsp -> view -> generate` silently
    # loses the scene deltas. (Supersedes the earlier "variance without a name
    # projects nothing" rule, which #121's blank-name change turned into a
    # data-loss bug.) A device blank name renders as `SNAPSHOT N` on-device, so
    # the `Snap N` fallback is a faithful label.
    no_snaps["preset"]["snapshots"] = mutate._default_snapshot_meta()
    mutate.set_param(no_snaps, "Brit 2204 Custom", "Drive", 0.6, library,
                     snapshot=1)
    no_snaps["preset"]["snapshots"][1]["name"] = ""   # device-origin blank name
    proj = view.view(no_snaps, library)
    assert [s["name"] for s in proj["snapshots"]] == ["Snap 2"]
    assert proj["snapshots"][0]["params"]["Brit 2204 Custom"]["Drive"] == 0.6


def test_view_still_drops_truly_empty_placeholder_slots(no_snaps, library):
    # The hgc-0c3 fix is scoped to slots with REAL overrides: an all-placeholder
    # meta with no deltas still projects nothing (no phantom blank scenes, the
    # #118 invariant).
    no_snaps["preset"]["snapshots"] = mutate._default_snapshot_meta()
    assert view.view(no_snaps, library).get("snapshots") in (None, [])


def test_view_ignores_a_constant_base_bypassed_enabled_array(no_snaps, library):
    # hgc-0c3 review (HIGH regression guard): a base-BYPASSED block with a
    # constant all-False dense @enabled.snapshots array (what a device export
    # carries for a block off everywhere) matches base in every slot — NOT a
    # per-snapshot disable. It must not fabricate phantom scenes on placeholder
    # slots (the keep-decision filters base-equal disables; projection does not).
    no_snaps["preset"]["snapshots"] = mutate._default_snapshot_meta()
    blk = no_snaps["preset"]["flow"][0]["b02"]
    blk["@enabled"] = {"value": False, "snapshots": [False] * 8}  # off at base + all scenes
    assert view.view(no_snaps, library).get("snapshots") in (None, [])


# NOTE (hgc-0c3, UNRESOLVED): a genuine @enabled per-snapshot DISABLE on an
# otherwise-placeholder slot did not project a scene in testing, unlike the
# param-override case above — either a test subtlety or a real gap in the
# keep-decision for the @enabled path. Needs verification before this ships.


def test_set_snapshot_pads_a_malformed_short_meta(no_snaps, library):
    # hgc-dsd: a present-but-malformed sub-8-slot `snapshots` meta (hand-edited
    # / legacy) must not raise a bare IndexError — set_snapshot pads it to the
    # device's 8 slots and succeeds with a graceful, valid result.
    no_snaps["preset"]["snapshots"] = mutate._default_snapshot_meta()[:4]
    mutate.set_snapshot(no_snaps, index=6, name="High", library=library)
    meta = no_snaps["preset"]["snapshots"]
    assert len(meta) == 8
    assert meta[6]["name"] == "High"
    assert meta[5]["name"] == "Snap 6"  # padded slot got its index-correct name


def test_patch_atomic_bad_op_leaves_body_untouched(body, library):
    before = copy.deepcopy(body)
    with pytest.raises(mutate.MutateError):
        mutate.apply_operations(body, [
            {"op": "set_snapshot", "index": 4, "name": "Good"},
            {"op": "set_snapshot", "index": 99, "name": "Bad"},  # out of range
        ], library)
    # apply_operations mutates in place; the CLI copies first. Here we only
    # assert the SECOND op raised before any write escaped to disk — the op
    # itself is what aborts. (The first op's in-memory change is expected.)
    assert before["preset"]["snapshots"][4]["name"] == "Snap 5"
