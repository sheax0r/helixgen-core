"""Venue vocabulary: what a tone variant is played through."""
from __future__ import annotations

VENUES: tuple[str, ...] = ("headphones", "direct", "amp-in")


def validate_venue(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in VENUES:
        raise ValueError(f"venue must be one of {', '.join(VENUES)}; got {value!r}")
    return value


# --- room IR selection / blend level ---------------------------------------

import ctypes
import math
from pathlib import Path

from helixgen import ir as _ir
from helixgen.ir import IrMapping


class VenueError(ValueError):
    pass


def ir_energy_db(wav: Path) -> float:
    """``10*log10(sum(x**2))`` over channel 0 of ``wav``."""
    wav = Path(wav)
    if not wav.is_file():
        raise VenueError(f"IR file not found: {wav}")
    sf = _ir._load_libsndfile()
    info = _ir._SF_INFO()
    h = sf.sf_open(str(wav).encode(), _ir._SFM_READ, ctypes.byref(info))
    if not h:
        raise VenueError(f"could not read IR file: {wav}")
    try:
        ch = max(info.channels, 1)
        buf = (ctypes.c_float * (info.frames * ch))()
        n = sf.sf_readf_float(h, buf, info.frames)
    finally:
        sf.sf_close(h)
    total = sum(buf[i * ch] ** 2 for i in range(n))
    if total <= 0:
        raise VenueError(f"IR file is silent: {wav}")
    return 10 * math.log10(total)


def _norm(path: str) -> str:
    return path.lower().replace("-", " ").replace("_", " ")


def find_room_ir(
    close_wav: Path, irs: IrMapping, override: str | None = None
) -> tuple[str, Path] | None:
    """The room IR to blend under ``close_wav``: ``(hash, path)`` or None.

    Candidates: registered IRs whose basename has `` ROOM`` and whose text
    before it (plus a space) prefixes the close IR's basename. Prefer
    ``natural-phase`` paths, then highest energy, then lowest basename.
    ``override`` is a hash or basename and skips selection.
    """
    if override is not None:
        if override in irs.entries:
            return override, irs.resolve_by_hash(override)
        hits = [h for h, p in irs.entries.items() if Path(p).name == override]
        if not hits:
            raise VenueError(f"no registered IR with hash or basename {override!r}")
        if len(hits) > 1:
            raise VenueError(
                f"basename {override!r} is ambiguous ({', '.join(sorted(hits))}); "
                "pass the IR hash instead"
            )
        return hits[0], irs.resolve_by_hash(hits[0])

    close_name = Path(close_wav).name
    cands = []
    for h, stored in irs.entries.items():
        name = Path(stored).name
        stem, sep, _ = name.partition(" ROOM")
        if sep and close_name.startswith(stem + " "):
            cands.append((h, irs.resolve_by_hash(h)))
    if not cands:
        return None
    natural = [c for c in cands if "natural phase" in _norm(str(c[1]))]
    cands = natural or cands
    return min(cands, key=lambda c: (-ir_energy_db(c[1]), c[1].name))


def room_level_db(
    close_level: float, close_wav: Path, room_wav: Path, offset_db: float = 4.0
) -> float:
    """Room IR Level that sits ``offset_db`` under the close IR, energy-matched."""
    diff = ir_energy_db(room_wav) - ir_energy_db(close_wav)
    return round(close_level - diff - offset_db, 1)


# --- transforms ------------------------------------------------------------

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from helixgen import usbtaps
from helixgen.generate import (
    HSP_SNAPSHOT_SLOTS, GenerateError, _is_amp, _is_cab, authoring_defaults,
    compose_preset,
)
from helixgen.ir import IR_MODEL_PREFIX
from helixgen.library import Library
from helixgen.spec import SpecError, parse_spec
from helixgen.view import view

FREE_SWITCHES = ("FS1", "FS2", "FS3", "FS4", "FS5",
                 "FS7", "FS8", "FS9", "FS10", "FS11")
TILE = {"block": "Tile", "params": {"Decay": 0.2, "Mix": 0.2}}
JOIN_PARAMS = {"A Level": 0.0, "B Level": 0.0, "A Pan": 0.5, "B Pan": 0.5, "Level": 0.0}
AMP_IN_ERROR = ("cannot restore a cab from an amp-in variant; "
                "fork from the direct or headphones variant")


@dataclass
class VenueResult:
    body: dict
    venue: str
    venue_detail: dict
    warnings: list[str] = field(default_factory=list)


class _NoRoom(Exception):
    """The edited chain does not fit the grid (lane capacity or USB tap)."""


def _eff(w: Any) -> Any:
    """A ``{"value", "snapshots"?}`` wrapper as its per-snapshot effective values."""
    if not (isinstance(w, dict) and "value" in w):
        return w
    out = {k: v for k, v in w.items() if k not in ("value", "snapshots")}
    out["value"] = (list(w["snapshots"]) if "snapshots" in w
                    else [w["value"]] * HSP_SNAPSHOT_SLOTS)
    return out


def _block_form(b: dict) -> dict:
    out = {k: v for k, v in b.items()
           if k not in ("position", "path", "branch", "endpoint", "slot", "@enabled")}
    out["@enabled"] = _eff(b.get("@enabled"))
    out["slot"] = [{**{k: v for k, v in s.items() if k != "params"},
                    "@enabled": _eff(s.get("@enabled")),
                    "params": {k: _eff(v) for k, v in (s.get("params") or {}).items()}}
                   for s in b.get("slot") or [] if isinstance(s, dict)]
    return out


def _normal_form(body: dict) -> dict:
    """What a venue transform must keep, independent of grid layout.

    The lossless guard and the "everything else unchanged" tests both compare
    this, never raw dicts: regenerating a preset from `view` re-bases every
    param's ``value`` onto snapshot 0 and moves the overrides to match, which
    is the same preset on every snapshot but not the same dict. So:

    - each path is its lane-0 and lane-1 bNN blocks (endpoints included) in
      grid order, WITHOUT keys/positions/split pointers;
    - every ``{"value", "snapshots"}`` wrapper (block bypass, slot enable,
      each param) becomes its 8 per-snapshot effective values, keeping any
      controller (footswitch / expression) attached to it;
    - plus preset ``snapshots`` metadata, ``sources`` (switch scribble
      strips) and preset ``params`` minus ``activesnapshot`` (regeneration
      always writes 0, with the base synced to snapshot 0).
    """
    preset = body.get("preset") or {}
    paths = []
    for p in preset.get("flow") or []:
        keys = sorted((k for k in p if k.startswith("b") and k[1:].isdigit()),
                      key=lambda k: int(k[1:]))
        paths.append({
            "flow": {k: v for k, v in p.items() if k not in keys},
            "lane0": [_block_form(p[k]) for k in keys if int(k[1:]) < 14],
            "lane1": [_block_form(p[k]) for k in keys if int(k[1:]) >= 14],
        })
    params = {k: v for k, v in (preset.get("params") or {}).items() if k != "activesnapshot"}
    return {"paths": paths, "snapshots": preset.get("snapshots"),
            "sources": preset.get("sources"), "params": params}


def _first_diff(a: Any, b: Any, at: str = "") -> str | None:
    """Where ``a`` (regenerated) first differs from ``b`` (original), or None."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return f"{at}.{k} ({'added' if k in a else 'lost'})"
            d = _first_diff(a[k], b[k], f"{at}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{at} (length {len(b)} -> {len(a)})"
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_diff(x, y, f"{at}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{at}: {b!r} -> {a!r}"


def _untapped(body: dict) -> tuple[dict, dict[int, str]]:
    """A copy of ``body`` with its USB taps lifted, and the taps taken."""
    work = copy.deepcopy(body)
    flows = (work.get("preset") or {}).get("flow") or []
    return work, usbtaps._lift(flows, range(len(flows)))


def _recipe(body: dict, library: Library, irs: IrMapping) -> dict:
    """`view` of ``body`` without its USB taps; refuses unrepresentable controllers."""
    recipe = view(_untapped(body)[0], library, irs=irs)
    if recipe.get("unknown_controllers"):
        raise VenueError("preset cannot be transformed losslessly: it has "
                         "controllers view cannot represent (unknown_controllers)")
    return recipe


def _compose(body: dict, library: Library, irs: IrMapping,
             edit: Callable[[dict], None]) -> dict:
    taps = _untapped(body)[1]
    recipe = _recipe(body, library, irs)
    edit(recipe)
    out = compose_preset(parse_spec(recipe, source="venue"), library,
                         source="venue", irs=irs)
    if taps:
        # the `usb-taps` / edit-verb code path: put the same taps back after
        # the (possibly new) last block, all or nothing
        r = usbtaps._place(out["preset"]["flow"], taps)
        if r["status"] != "added":
            raise _NoRoom(f"no room for the USB tap: {r['detail']}")
    # what the recipe cannot carry comes across from the original
    out["meta"] = copy.deepcopy(body.get("meta") or {})
    for k in ("snapshots", "clip"):
        if k in (body.get("preset") or {}):
            out["preset"][k] = copy.deepcopy(body["preset"][k])
    return out


def _roundtrip(body: dict, library: Library, irs: IrMapping,
               edit: Callable[[dict], None]) -> dict:
    """``body`` rebuilt from its `view` recipe after ``edit(recipe)``.

    USB taps are lifted before `view` and re-placed after composing with
    usbtaps' own placement; ``meta`` (and snapshot metadata / clip) are
    copied across. Guard: the same rebuild with NO edit must have the same
    :func:`_normal_form` as ``body``, else ``VenueError`` naming the first
    difference — the preset uses something the recipe cannot express.
    """
    try:
        noop = _compose(body, library, irs, lambda r: None)
    except (GenerateError, SpecError, LookupError, _NoRoom) as e:
        raise VenueError(f"preset cannot be transformed losslessly: {e}") from e
    diff = _first_diff(_normal_form(noop), _normal_form(body))
    if diff:
        raise VenueError(f"preset cannot be transformed losslessly: {diff}")
    return _compose(body, library, irs, edit)


# --- recipe helpers ---------------------------------------------------------
#
# Edits work on the `view` recipe of one path's ``blocks`` list. Every entry
# carries explicit ``lane``/``pos`` (view emits them); inserting or removing
# a block shifts the later cells of its lane so the rest keep their layout.
# Block references (snapshots, footswitches, expression, MIDI) are bound to
# the entry dicts they name BEFORE an edit and re-pointed at those entries'
# final coordinates after it, so they survive shifts and new name clashes
# (the room IR shares the close IR's name).

def _lib_block(library: Library, entry: dict):
    try:
        return library.find_block(entry["block"])
    except (KeyError, LookupError):
        return None


def _at(blocks: list[dict], entry: dict) -> int:
    return next(i for i, b in enumerate(blocks) if b is entry)


def _lane(blocks: list[dict], lane: int) -> list[dict]:
    return [b for b in blocks if b.get("lane", 0) == lane and "structural" not in b
            and b.get("pos") is not None]


def _insert(blocks: list[dict], i: int, entry: dict) -> None:
    """Insert ``entry`` at list index ``i``, into the grid cell its ``pos``
    names; later cells of its lane move one right."""
    if entry.get("pos") is not None:
        for b in _lane(blocks, entry.get("lane", 0)):
            if b["pos"] >= entry["pos"]:
                b["pos"] += 1
    blocks.insert(i, entry)


def _remove(blocks: list[dict], entry: dict) -> None:
    """Remove ``entry``; later cells of its lane move one left."""
    del blocks[_at(blocks, entry)]
    if entry.get("pos") is not None:
        for b in _lane(blocks, entry.get("lane", 0)):
            if b["pos"] > entry["pos"]:
                b["pos"] -= 1


def _lane0_end(blocks: list[dict]) -> int:
    return max((b["pos"] for b in _lane(blocks, 0)), default=0) + 1


def _refs(recipe: dict) -> list[tuple[str, list, dict]]:
    """``(label, container, ref)`` for every block reference in ``recipe``.
    String and name-keyed forms are rewritten to their dict/list equivalents
    so each ref is a mutable dict."""
    out: list[tuple[str, list, dict]] = []
    for s in recipe.get("snapshots") or []:
        if "disable" in s:
            s["disable"] = [{"block": d} if isinstance(d, str) else d for d in s["disable"]]
            out += [(f"snapshot {s['name']!r} disable", s["disable"], r) for r in s["disable"]]
        if isinstance(s.get("params"), dict):
            s["params"] = [{"block": n, "params": ov} for n, ov in s["params"].items()]
        out += [(f"snapshot {s['name']!r} params", s["params"], r) for r in s.get("params") or []]
    fss = recipe.get("footswitches") or []
    out += [(f"footswitch {f.get('switch')}", fss, f) for f in fss if "block" in f]
    for kind, key in (("expression", "pedal"), ("midi", "cc")):
        for a in recipe.get(kind) or []:
            ts = a.get("targets") or []
            out += [(f"{kind} {a.get(key)}", ts, t) for t in ts if "block" in t]
    return out


def _bind(recipe: dict) -> list[tuple[str, list, dict, dict]]:
    """``(label, container, ref, entry)``: each reference with the block entry it names."""
    idx: dict[str, list[tuple[int, dict]]] = {}
    for pi, p in enumerate(recipe["paths"]):
        for b in p["blocks"]:
            if "block" in b:
                idx.setdefault(b["block"], []).append((pi, b))
    out = []
    for label, c, ref in _refs(recipe):
        hits = [b for pi, b in idx.get(ref["block"], [])
                if all(ref.get(k) is None or ref[k] == v
                       for k, v in (("path", pi), ("lane", b.get("lane", 0)), ("pos", b.get("pos"))))]
        if len(hits) == 1:
            out.append((label, c, ref, hits[0]))
    return out


def _rebind(recipe: dict, bound: list) -> list:
    """Point each still-present bound reference at its entry's current
    ``path``/``lane``/``pos``; return the bindings whose entry was removed."""
    where = {id(b): pi for pi, p in enumerate(recipe["paths"]) for b in p["blocks"]}
    gone = []
    for t in bound:
        _, c, ref, b = t
        if not any(x is ref for x in c):
            continue                      # the reference itself was dropped
        if id(b) not in where:
            gone.append(t)
        else:
            ref.update(path=where[id(b)], lane=b.get("lane", 0), pos=b.get("pos"))
    return gone


def _drop(container: list, ref: dict) -> None:
    del container[_at(container, ref)]


def _tidy(recipe: dict) -> None:
    """Drop controller groups an edit emptied (parse_spec wants targets)."""
    for kind in ("expression", "midi"):
        if kind in recipe:
            recipe[kind] = [a for a in recipe[kind] if a.get("targets")]
    for kind in ("footswitches", "expression", "midi"):
        if kind in recipe and not recipe[kind]:
            del recipe[kind]


def _ir_hash(entry: dict, irs: IrMapping) -> str | None:
    """The IR hash a view entry's ``ir`` (hash or unique basename) names."""
    ir = entry.get("ir")
    if not ir:
        return None
    if ir in irs.entries:
        return ir
    hits = [k for k, p in irs.entries.items() if Path(p).name == ir]
    return hits[0] if len(hits) == 1 else (ir if len(ir) == 32 else None)


def _ir_wav(entry: dict, irs: IrMapping) -> Path | None:
    h = _ir_hash(entry, irs)
    if h is None or h not in irs.entries:
        return None
    wav = irs.resolve_by_hash(h)
    return wav if wav.is_file() else None


def _is(library: Library, entry: dict, pred) -> bool:
    blk = _lib_block(library, entry) if "block" in entry else None
    return blk is not None and pred(blk)


def _instrument_path(recipe: dict, library: Library) -> int:
    for pi, p in enumerate(recipe["paths"]):
        if any(_is(library, b, _is_amp) for b in p["blocks"]):
            return pi
    return 0


def _cabs_after_amp(blocks: list[dict], library: Library) -> list[dict]:
    real = [b for b in blocks if "block" in b]
    last_amp = max((i for i, b in enumerate(real) if _is(library, b, _is_amp)), default=-1)
    return [b for b in real[last_amp + 1:] if _is(library, b, _is_cab)]


def _free_switches(recipe: dict) -> list[str]:
    used = {f.get("switch") for f in (recipe.get("footswitches") or [])
            + (recipe.get("commands") or [])}
    return [s for s in FREE_SWITCHES if s not in used]


def _find(blocks: list[dict], like: dict) -> dict:
    """The entry in a fresh recipe at ``like``'s coordinates."""
    return next(b for b in blocks if b.get("block") == like["block"]
                and b.get("lane", 0) == like.get("lane", 0) and b.get("pos") == like.get("pos"))


def _capacity(e: Exception) -> bool:
    return isinstance(e, _NoRoom) or "user slot" in str(e)


# --- → headphones -----------------------------------------------------------

def _room_ir_blocker(recipe, pi, close, cabs, library, irs, room_mode) -> str | None:
    """Why the room-IR treatment cannot run (the Tile fallback's reason), or None."""
    if room_mode == "tile":
        return "room mode tile requested"
    if len(cabs) != 1:
        return f"{len(cabs)} cab blocks after the amp; the room IR needs exactly one"
    blk = _lib_block(library, close)
    if close.get("enabled", blk.exemplar.get("@enabled", True)) is False:
        return f"the cab {close['block']!r} is bypassed at base"
    for label, _, _, e in _bind(recipe):
        if e is close and label.endswith(" disable"):
            return f"the cab is bypassed in {label.removesuffix(' disable')}"
    if not blk.model_id.startswith(IR_MODEL_PREFIX):
        return f"{close['block']!r} is a stock cab, not an IR block"
    if _ir_wav(close, irs) is None:
        return f"the close IR's wav ({close.get('ir')}) is not registered or not on disk"
    if close.get("lane", 0) != 0 or any("split" in b or "join" in b
           or (b.get("structural") or {}).get("type") in ("split", "join")
           for b in recipe["paths"][pi]["blocks"]):
        return "the path already has a split"
    return None


def _headphones(body, library, irs, *, room_ir, room_mode, room_offset_db) -> VenueResult:
    if room_mode not in ("auto", "tile"):
        raise VenueError(f"room_mode must be 'auto' or 'tile'; got {room_mode!r}")
    detail: dict[str, Any] = {"treatment": "none", "room_ir": None, "room_level_db": None,
                              "room_switch": None, "ambience_block": None,
                              "ambience_switch": None, "fallback_reason": None}
    recipe = _recipe(body, library, irs)
    pi = _instrument_path(recipe, library)
    cabs = _cabs_after_amp(recipe["paths"][pi]["blocks"], library)
    if not cabs:
        return VenueResult(copy.deepcopy(body), "headphones", detail,
                           ["no cab or IR block after the amp: chain left unchanged"])
    close = cabs[0]
    reason = _room_ir_blocker(recipe, pi, close, cabs, library, irs, room_mode)
    room = None
    if reason is None:
        close_wav = _ir_wav(close, irs)
        room = find_room_ir(close_wav, irs, override=room_ir)
        if room is None:
            reason = f"no room IR registered for {close_wav.name}"
    warnings: list[str] = []
    level = None
    if room is not None:
        params = close.get("params") or {}
        close_level = float(params["Level"] if "Level" in params
                            else authoring_defaults(_lib_block(library, close)).get("Level", -18.0))
        level = room_level_db(close_level, close_wav, room[1], room_offset_db)
        detail.update(treatment="room-ir", room_ir=room[0], room_level_db=level)
    else:
        detail.update(treatment="tile", fallback_reason=reason)
        warnings.append(f"room IR treatment skipped ({reason}); Tile ambience added instead")

    def edit(rec: dict, with_tile: bool) -> dict:
        d = dict(detail)
        blocks = rec["paths"][pi]["blocks"]
        bound = _bind(rec)
        free = _free_switches(rec)
        new_fs = []
        if room is not None:
            c = _find(blocks, close)
            i = _at(blocks, c)
            _insert(blocks, i, {"split": {"type": "y"}, "lane": 0, "pos": c["pos"]})
            blocks.insert(i + 2, {"block": c["block"], "lane": 1, "ir": room[0],
                                  "params": {"Level": level}})
            _insert(blocks, i + 3, {"join": {"params": dict(JOIN_PARAMS)},
                                    "lane": 0, "pos": c["pos"] + 1})
            if free:
                d["room_switch"] = s = free.pop(0)
                new_fs.append({"switch": s, "block": c["block"], "path": pi, "lane": 1,
                               "param": "Level", "min": -60.0, "max": level,
                               "label": "ROOM MIC", "color": "purple"})
        if with_tile:
            tile = {**copy.deepcopy(TILE), "lane": 0, "pos": _lane0_end(blocks),
                    **({"enabled": False} if room is not None else {})}
            blocks.append(tile)
            d["ambience_block"] = "Tile"
            if free:
                d["ambience_switch"] = s = free.pop(0)
                new_fs.append({"switch": s, "block": "Tile", "path": pi, "lane": 0,
                               "pos": tile["pos"], "label": "TILE", "color": "turquoise"})
        _rebind(rec, bound)
        rec["footswitches"] = (rec.get("footswitches") or []) + new_fs
        _tidy(rec)
        return d

    last: Exception | None = None
    for with_tile in (True, False) if room is not None else (True,):
        out: dict = {}
        try:
            body_out = _roundtrip(body, library, irs,
                                  lambda rec: out.update(edit(rec, with_tile)))
        except (_NoRoom, GenerateError) as e:
            if not _capacity(e):
                raise VenueError(str(e)) from e
            last = e
            continue
        if not with_tile:
            warnings.append(f"no room for the Tile block ({last}); added without it")
        if room is not None and out["room_switch"] is None:
            warnings.append("no free footswitch for the room IR level (ROOM MIC)")
        if out["ambience_block"] and out["ambience_switch"] is None:
            warnings.append("no free footswitch for the Tile block (TILE)")
        return VenueResult(body_out, "headphones", out, warnings)
    raise VenueError(f"no room in the chain for the headphones treatment: {last}")


# --- → direct (from headphones) ---------------------------------------------

def _strip_room_stage(rec: dict, detail: dict, library: Library, irs: IrMapping) -> None:
    """Remove exactly what ``detail`` recorded from the recipe ``rec``; any
    mismatch with the chain is a ``VenueError`` naming it (no best effort)."""
    pi = _instrument_path(rec, library)
    blocks = rec["paths"][pi]["blocks"]
    bound = _bind(rec)
    target = {id(ref): e for _, _, ref, e in bound}
    fss = rec.get("footswitches") or []
    removed: list[dict] = []
    dropped: list[dict] = []
    room_ir = detail.get("room_ir")
    if room_ir:
        rooms = [b for b in blocks if "block" in b and b.get("lane") == 1
                 and _ir_hash(b, irs) == room_ir]
        if len(rooms) != 1:
            raise VenueError(f"room_ir {room_ir} is not a lane-1 IR of the instrument path "
                             "(swapped or removed by hand?); cannot reverse")
        room = rooms[0]
        i = _at(blocks, room)
        split = max((j for j in range(i) if "split" in blocks[j]), default=None)
        join = next((j for j in range(i + 1, len(blocks)) if "join" in blocks[j]), None)
        if split is None or join is None:
            raise VenueError("the room IR is no longer inside a split/join; cannot reverse")
        inside = blocks[split + 1:join]
        if ([b for b in inside if b.get("lane") == 1] != [room]
                or len([b for b in inside if b.get("lane", 0) == 0]) != 1):
            raise VenueError("blocks were added inside the room IR split; cannot reverse")
        removed += [blocks[split], room, blocks[join]]
        rs = detail.get("room_switch")
        if rs:
            hits = [f for f in fss if f.get("switch") == rs and f.get("param") == "Level"
                    and target.get(id(f)) is room]
            if not hits:
                raise VenueError(f"room_switch {rs} no longer toggles the room IR Level; "
                                 "cannot reverse")
            dropped += hits
    name = detail.get("ambience_block")
    if name:
        sw = detail.get("ambience_switch")
        if sw:
            hits = [f for f in fss if f.get("switch") == sw and "param" not in f
                    and (target.get(id(f)) or {}).get("block") == name
                    and any(b is target[id(f)] for b in blocks)]
            if not hits:
                raise VenueError(f"ambience_switch {sw} no longer toggles {name!r} "
                                 "(re-wired or the block removed?); cannot reverse")
            tile = target[id(hits[0])]
            dropped += hits
        else:
            tiles = [b for b in _lane(blocks, 0) if b.get("block") == name]
            if not tiles:
                raise VenueError(f"ambience_block {name!r} is not in the instrument path; "
                                 "cannot reverse")
            tile = tiles[-1]
        removed.append(tile)
    for f in dropped:
        _drop(fss, f)
    for b in removed:
        _remove(blocks, b)
    gone = _rebind(rec, bound)
    if gone:
        label, _, _, e = gone[0]
        raise VenueError(f"{label} references {e['block']!r}, which the headphones "
                         "treatment added; cannot reverse")
    _tidy(rec)


def _direct(body, library, irs, detail) -> VenueResult:
    if detail is None:
        raise VenueError("cannot reverse a headphones variant without its venue_detail "
                         "(it records what the treatment added)")
    if detail.get("treatment") == "none":
        return VenueResult(copy.deepcopy(body), "direct", {}, [])
    _strip_room_stage(_recipe(body, library, irs), detail, library, irs)  # verify first
    out = _roundtrip(body, library, irs, lambda rec: _strip_room_stage(rec, detail, library, irs))
    return VenueResult(out, "direct", {}, [])


# --- → amp-in ---------------------------------------------------------------

EQ = {"block": "Parametric Mono", "params": {"LowCut": 90, "LowCutSlope": 2,
                                             "HighCut": 5500, "HighCutSlope": 2}}


def _amp_in(body, library, irs) -> VenueResult:
    if not any(_is(library, b, _is_amp) for p in _recipe(body, library, irs)["paths"]
               for b in p["blocks"]):
        raise VenueError("amp-in needs an amp block; this preset has no amp")
    warnings: list[str] = []

    def edit(rec: dict) -> None:
        bound = _bind(rec)
        amp = None
        for p in rec["paths"]:
            for b in list(p["blocks"]):
                if _is(library, b, _is_cab):
                    _remove(p["blocks"], b)
                elif _is(library, b, _is_amp):
                    amp = (p["blocks"], b)
        blocks, a = amp
        _insert(blocks, _at(blocks, a) + 1,
                {**copy.deepcopy(EQ), "lane": a.get("lane", 0), "pos": a["pos"] + 1})
        for label, c, ref, e in _rebind(rec, bound):
            _drop(c, ref)
            warnings.append(f"{label} on {e['block']!r} removed with the cab")
        _tidy(rec)

    try:
        out = _roundtrip(body, library, irs, edit)
    except (_NoRoom, GenerateError) as e:
        raise VenueError(f"no room in the chain for the amp-in EQ: {e}" if _capacity(e)
                         else str(e)) from e
    return VenueResult(out, "amp-in", {"eq_block": EQ["block"]}, warnings)


def apply_venue(
    body: dict, target: str, library: Library, irs: IrMapping, *,
    source_venue: str | None = None, source_detail: dict | None = None,
    room_ir: str | None = None, room_mode: str = "auto", room_offset_db: float = 4.0,
) -> VenueResult:
    """Transform ``body`` (a parsed `.hsp`, never mutated) into its ``target``
    venue form. ``source_venue``/``source_detail`` describe ``body``; None =
    unspecified, treated as direct. See docs/superpowers/specs/
    2026-11-11-venue-variants-design.md ("Transform: ...")."""
    try:
        validate_venue(target)
        validate_venue(source_venue)
    except ValueError as e:
        raise VenueError(str(e)) from e
    if target == source_venue:
        raise VenueError(f"already {target}")
    if source_venue == "amp-in":
        raise VenueError(AMP_IN_ERROR)
    if target == "headphones":
        return _headphones(body, library, irs, room_ir=room_ir, room_mode=room_mode,
                           room_offset_db=room_offset_db)
    if source_venue == "headphones":
        direct = _direct(body, library, irs, source_detail)
        if target == "direct":
            return direct
        body = direct.body
    if target == "direct":
        return VenueResult(copy.deepcopy(body), "direct", {}, [])   # unspecified = direct
    return _amp_in(body, library, irs)
