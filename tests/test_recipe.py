"""Unit tests for the `recipe` authoring front-end (Task 2a of the
.hsp-canonical redesign).

`apply_recipe` clones a Stadium chassis and replays a spec-shaped "recipe"
onto it, producing an `.hsp` body dict. The golden-corpus contract
(`tests/golden/`) is the real acceptance bar; these are fast, direct checks
of the core placement behaviour.
"""
from __future__ import annotations

import copy

import pytest

from tests.golden import harness


def _corpus_chassis():
    from helixgen.chassis import extract_chassis_from_hsp
    return extract_chassis_from_hsp(copy.deepcopy(harness._CHASSIS_PAYLOAD))


def test_two_block_recipe_places_blocks_in_order(tmp_path):
    from helixgen.recipe import apply_recipe

    library = harness.build_corpus_library(tmp_path)
    chassis = _corpus_chassis()
    recipe = {
        "name": "Two Block",
        "paths": [
            {"blocks": [
                {"block": "Scream 808"},
                {"block": "Brit 2204 Custom", "params": {"Drive": 0.55}},
            ]},
        ],
    }

    body = apply_recipe(recipe, library, chassis=chassis)

    from helixgen.hsp import translate_to_hsp

    path0 = body["preset"]["flow"][0]
    # b01/b02 are the two user blocks, in order (model translated to the
    # Stadium wire namespace on emit).
    assert path0["b01"]["slot"][0]["model"] == translate_to_hsp("HD2_DrvScream808")
    assert path0["b02"]["slot"][0]["model"] == translate_to_hsp("HD2_AmpBrit2204Custom")
    assert path0["b01"]["position"] == 1
    assert path0["b02"]["position"] == 2
    # The Brit's Drive override is written into the wrapped param.
    assert path0["b02"]["slot"][0]["params"]["Drive"]["value"] == 0.55
    # Meta carries the preset name + provenance.
    assert body["meta"]["name"] == "Two Block"
    assert "helixgen" in body["meta"]


def test_generate_from_recipe_returns_hsp_bytes(tmp_path):
    from helixgen.hsp import HSP_MAGIC
    from helixgen.recipe import generate_from_recipe

    library = harness.build_corpus_library(tmp_path)
    chassis = _corpus_chassis()
    recipe = {"name": "One", "paths": [{"blocks": [{"block": "Scream 808"}]}]}

    out = generate_from_recipe(recipe, library, chassis=chassis)

    assert out[:8] == HSP_MAGIC


@pytest.mark.parametrize("blocks", [
    # a 13th block on one lane
    [{"block": "Scream 808"} for _ in range(13)],
    # a full lane PLUS a split/join pair — the old count-of-blocks guard let
    # this through and the scaffolds landed on b13/b14 (hgc-x9g)
    [{"block": "Scream 808"} for _ in range(12)]
    + [{"split": {"type": "y"}}, {"join": {}}],
])
def test_recipe_lane_over_capacity_raises(tmp_path, blocks):
    # A lane has only 12 user-block slots (b01..b12). A 13th entry must raise,
    # not silently overwrite the endpoint slot (b13).
    from helixgen.generate import GenerateError
    from helixgen.recipe import apply_recipe

    library = harness.build_corpus_library(tmp_path)
    chassis = _corpus_chassis()
    recipe = {"name": "Overfull", "paths": [{"blocks": blocks}]}

    with pytest.raises(GenerateError, match="12 user slots"):
        apply_recipe(recipe, library, chassis=chassis)


@pytest.mark.parametrize("pos,lane,key", [
    (0, 0, "b00"),    # the path's input endpoint
    (13, 0, "b13"),   # the path's output endpoint
    (0, 1, "b14"),    # the row-1 input endpoint
    (99, 0, "b99"),   # no such grid slot at all
])
def test_recipe_block_on_a_non_user_slot_raises(tmp_path, pos, lane, key):
    # hgc-x9g: an explicit `pos` walked past the count-based capacity guard and
    # overwrote the endpoint — `generate` exited 0 having replaced the path's
    # input block with a drive, which `view` then reported as no block at all.
    from helixgen.generate import GenerateError
    from helixgen.recipe import apply_recipe

    library = harness.build_corpus_library(tmp_path)
    chassis = _corpus_chassis()
    recipe = {"name": "OffGrid", "paths": [
        {"blocks": [{"block": "Scream 808", "lane": lane, "pos": pos}]}]}

    with pytest.raises(GenerateError) as e:
        apply_recipe(recipe, library, chassis=chassis)
    assert f"Scream 808 resolves to lane {lane} pos {pos} ({key})" in str(e.value)


@pytest.mark.parametrize("blocks", [
    # both explicit
    [{"block": "Scream 808", "pos": 1}, {"block": "Brit 2204 Custom", "pos": 1}],
    # auto-assigned onto a later explicit one (the counter only moves forward)
    [{"block": "Scream 808"}, {"block": "Brit 2204 Custom", "pos": 1}],
])
def test_recipe_two_blocks_on_one_slot_raises(tmp_path, blocks):
    # bead hgc-x9g: two entries resolving to one bNN key used to collapse —
    # `path_dict[key] = ...` wrote twice and the loser vanished from the
    # preset, exit 0, no warning. One grid slot holds one block.
    from helixgen.generate import GenerateError
    from helixgen.recipe import apply_recipe

    library = harness.build_corpus_library(tmp_path)
    chassis = _corpus_chassis()
    recipe = {"name": "Collide", "paths": [{"blocks": blocks}]}

    with pytest.raises(GenerateError) as e:
        apply_recipe(recipe, library, chassis=chassis)
    msg = str(e.value)
    assert "Scream 808" in msg and "Brit 2204 Custom" in msg
    assert "path 0 lane 0 pos 1 (b01)" in msg
