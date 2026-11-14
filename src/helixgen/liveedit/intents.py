"""Static tone-adjective -> affected-knob map for the live editor's search box
(hgc-nci).

Only the "which params" half of the tweak-ui tone engine is ported here: an
adjective names the real knobs that bear on that quality, the page highlights
them, and the user turns them. This is NOT a dial -- the dial / projection /
coordinated-move machinery is deliberately left behind.

`_intents.json` is copied verbatim from the unmerged tweak-ui branch's
`helixgen/tweak/_intents.json`. `resolve_alias` and the category->role map are
ported (trimmed) from that branch's `tweak/intents.py` and `tweak/chain.py`
(`classify_role`). liveedit stays self-contained -- no import of `tweak`.

IR-resolving params are dropped: which IR a `cab` block loads is not a static
knob, so an IR move would point at nothing the page can light.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

_DATA = Path(__file__).with_name("_intents.json")

# defs.category_for(...) -> intent role. Ported from tweak/chain.py `_ROLE`
# (+ the dynamics gate/comp split). The page JS mirrors this table; keep both
# in step. Categories with no entry (wah, eq, pitch, ...) have no role and
# never match an adjective -- as in tweak-ui.
_CATEGORY_ROLE = {
    "amp": "amp", "preamp": "amp", "distortion": "drive_pedal",
    "cab_ir_interp": "cab", "ir": "cab", "delay": "delay",
    "reverb": "reverb", "modulation": "modulation",
}

_IR_PARAMS = frozenset({"ir", "irselect", "irindex", "irmodel"})


def role_for_category(category: Optional[str], model: Optional[str] = None) -> Optional[str]:
    """Intent role for a block's editor category (and model name, to tell a
    noise gate from a compressor). None for categories no adjective targets."""
    if category == "dynamics":
        return "gate" if "gate" in (model or "").lower() else "comp"
    return _CATEGORY_ROLE.get(category)


def _is_ir_param(param: object) -> bool:
    return str(param).strip().lower() in _IR_PARAMS


@lru_cache(maxsize=1)
def load() -> List[dict]:
    """The adjective -> affected-params map, IR params removed. Each entry:
    ``{id, label, aliases, meaning, moves: [{role, param, dir}]}``."""
    raw = json.loads(_DATA.read_text())
    out = []
    for d in raw:
        moves = [{"role": m["role"], "param": m["param"], "dir": int(m.get("dir", 0))}
                 for m in d.get("moves", []) if not _is_ir_param(m.get("param"))]
        out.append({
            "id": d["id"], "label": d["label"],
            "aliases": list(d.get("aliases", [])),
            "meaning": d.get("meaning", ""),
            "moves": moves,
        })
    return out


def aliases() -> List[str]:
    """Every type-ahead term (labels + aliases), de-duped in first-seen order,
    for the page's completion list."""
    terms, seen = [], set()
    for intent in load():
        for t in [intent["label"], *intent["aliases"]]:
            k = t.lower()
            if k not in seen:
                seen.add(k)
                terms.append(t)
    return terms


def resolve_alias(text: str) -> List[dict]:
    """Intents whose label or an alias starts with `text` (type-ahead). Ported
    from tweak/intents.resolve_alias."""
    q = (text or "").strip().lower()
    if not q:
        return []
    return [i for i in load()
            if any(t.lower().startswith(q) for t in [i["label"], *i["aliases"]])]


def payload() -> dict:
    """What the server hands the page: the static map plus the alias list."""
    return {"intents": load(), "aliases": aliases()}
