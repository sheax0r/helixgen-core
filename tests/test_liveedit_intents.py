"""The live editor's adjective->knob map (hgc-nci): the "which params" half
ported from tweak-ui. Highlight matching itself runs in the page JS
(tests/js/liveedit_page_scenarios.js); here we check the data + resolution."""
from helixgen.liveedit import intents


def _by_id(iid):
    return next(i for i in intents.load() if i["id"] == iid)


# --- the adjective -> param map ----------------------------------------------

def test_load_returns_role_param_dir_for_a_known_adjective():
    crunchier = _by_id("crunchier")
    moves = {(m["role"], m["param"]): m["dir"] for m in crunchier["moves"]}
    # pedal gain up, amp drive back, amp mids up -- pedal-forward crunch
    assert moves[("drive_pedal", "Gain")] == 1
    assert moves[("amp", "Drive")] == -1
    assert moves[("amp", "Mid")] == 1


def test_every_intent_has_a_label_aliases_and_at_least_one_move():
    data = intents.load()
    assert len(data) >= 20
    for i in data:
        assert i["label"] and isinstance(i["aliases"], list) and i["aliases"]
        assert i["moves"], f"{i['id']} has no moves"
        for m in i["moves"]:
            assert set(m) == {"role", "param", "dir"} and m["dir"] in (-1, 0, 1)


# --- alias / type-ahead resolution -------------------------------------------

def test_resolve_alias_matches_label_and_synonyms_by_prefix():
    assert any(i["id"] == "crunchier" for i in intents.resolve_alias("crunchy"))
    assert any(i["id"] == "crunchier" for i in intents.resolve_alias("crunch"))
    assert any(i["id"] == "brighter" for i in intents.resolve_alias("bright"))
    assert any(i["id"] == "droney" for i in intents.resolve_alias("droney"))
    assert intents.resolve_alias("") == []
    assert intents.resolve_alias("   ") == []


def test_aliases_list_is_deduped_and_covers_labels_and_synonyms():
    al = [a.lower() for a in intents.aliases()]
    assert len(al) == len(set(al))                 # no dupes
    assert "crunchier" in al and "crunchy" in al   # a label and one of its synonyms


# --- IR params excluded ------------------------------------------------------

def test_ir_params_are_dropped(monkeypatch, tmp_path):
    # a synthetic recipe with one IR move and one real knob move
    data = [{"id": "x", "label": "X", "aliases": ["x"], "meaning": "",
             "moves": [{"role": "cab", "param": "IR", "dir": 1},
                       {"role": "cab", "param": "HighCut", "dir": -1}]}]
    f = tmp_path / "_intents.json"
    f.write_text(__import__("json").dumps(data))
    monkeypatch.setattr(intents, "_DATA", f)
    intents.load.cache_clear()
    try:
        moves = intents.load()[0]["moves"]
        assert [(m["param"]) for m in moves] == ["HighCut"]   # IR dropped
    finally:
        intents.load.cache_clear()                            # restore real data


# --- role classification (JS mirrors this table) -----------------------------

def test_role_for_category_maps_sample_blocks():
    assert intents.role_for_category("amp") == "amp"
    assert intents.role_for_category("preamp") == "amp"
    assert intents.role_for_category("distortion") == "drive_pedal"
    assert intents.role_for_category("cab_ir_interp") == "cab"
    assert intents.role_for_category("ir") == "cab"
    assert intents.role_for_category("delay") == "delay"
    assert intents.role_for_category("reverb") == "reverb"
    assert intents.role_for_category("modulation") == "modulation"
    # dynamics splits by model name
    assert intents.role_for_category("dynamics", "HD2_DynNoiseGate") == "gate"
    assert intents.role_for_category("dynamics", "HD2_DynLACompressor") == "comp"
    # categories no adjective targets
    assert intents.role_for_category("wah") is None
    assert intents.role_for_category("eq") is None
    assert intents.role_for_category(None) is None
