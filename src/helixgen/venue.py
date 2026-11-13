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
import re
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


_PAIR_HALF = re.compile(r" ROOM[ -]?[LR]\.wav$", re.IGNORECASE)


def find_room_ir(
    close_wav: Path, irs: IrMapping, override: str | None = None
) -> tuple[str, Path] | None:
    """The room IR to blend under ``close_wav``: ``(hash, path)`` or None.

    Candidates: registered IRs whose basename has `` ROOM`` and whose text
    before it (plus a space) prefixes the close IR's basename. Prefer single
    captures over ``ROOM L``/``ROOM R`` stereo-pair halves, then
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
    # ``ROOM L`` / ``ROOM R`` are the two halves of one stereo capture; used
    # alone as a centred mono mic, half a pair is the wrong tool when the pack
    # also ships single room captures (``ROOM 1``, ``ROOM-2``, ...).
    singles = [c for c in cands if not _PAIR_HALF.search(c[1].name)]
    cands = singles or cands
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
#
# Every transform edits a deep copy of the body in place with `helixgen.mutate`
# verbs and touches nothing else: blocks it does not add or remove come through
# byte for byte (snapshot arrays on named and unnamed slots, controllers,
# harness, raw slots), and so do `sources`, snapshot metadata and `meta`.
# The edited path's USB tap is lifted around the edit and re-placed after it,
# as the edit verbs do (`usbtaps.lifted`), but as its own bNN dicts; a tap that
# no longer fits counts as a capacity failure, never a dropped tap.

import copy
from dataclasses import dataclass, field
from typing import Any

from helixgen import controllers, mutate, usbtaps
from helixgen.generate import (
    GenerateError, ParamValidationError, _is_amp, _is_cab, authoring_defaults,
)
from helixgen.ir import IR_MODEL_PREFIX
from helixgen.library import Library
from helixgen.spec import SpecError

FREE_SWITCHES = ("FS1", "FS2", "FS3", "FS4", "FS5",
                 "FS7", "FS8", "FS9", "FS10", "FS11")
TILE = {"block": "Tile", "params": {"Decay": 0.2, "Mix": 0.2}}
JOIN_PARAMS = {"A Level": 0.0, "B Level": 0.0, "A Pan": 0.5, "B Pan": 0.5, "Level": 0.0}
EQ = {"block": "Parametric Mono", "params": {"LowCut": 90, "LowCutSlope": 2,
                                             "HighCut": 5500, "HighCutSlope": 2}}
AMP_IN_ERROR = ("cannot restore a cab from an amp-in variant; "
                "fork from the direct or headphones variant")


@dataclass
class VenueResult:
    body: dict
    venue: str
    venue_detail: dict
    warnings: list[str] = field(default_factory=list)


class _NoRoom(Exception):
    """The USB tap does not fit after the edit."""


def _untapped(body: dict, path: int) -> tuple[dict, dict[int, tuple[str, dict, dict]]]:
    """A deep copy of ``body`` with the USB tap of the path being edited
    lifted, and the tap taken: ``{path: (split key, split bNN, b27 bNN)}``.
    Other paths keep theirs untouched."""
    work = copy.deepcopy(body)
    flows = work["preset"]["flow"]
    t = usbtaps._find_tap(flows[path])
    taps = {path: (t[0], flows[path][t[0]], flows[path]["b27"])} if t else {}
    usbtaps._lift(flows, taps)
    return work, taps


def _retap(work: dict, taps: dict[int, tuple[str, dict, dict]]) -> None:
    """Put the lifted taps back where `usbtaps` places taps (the first of b11 /
    b12 after every block), as themselves: their own bNN dicts, not fresh
    ones, with a device-written split ``branch`` beneath them following the move."""
    if not taps:
        return
    flows = work["preset"]["flow"]
    r = usbtaps._place(flows, {i: usbtaps._model(t[2]) for i, t in taps.items()})
    if r["status"] != "added":
        raise _NoRoom(f"no room for the USB tap: {r['detail']}")
    for i, (key, split, tap) in taps.items():
        p = flows[i]
        new = usbtaps._find_tap(p)[0]
        if new != key:
            split = {**split, "position": int(new[1:])}
            if split.get("branch") == f"b{14 + int(key[1:])}":
                split["branch"] = f"b{14 + int(new[1:])}"
        p[new], p["b27"] = split, tap


def _slots(body: dict, library: Library, pi: int | None = None) -> list[tuple]:
    """``(path, key, Block, lane, pos)`` for each user block, in path then column order."""
    return sorted(((fi, k, blk, ln, p) for fi, k, _, blk, ln, p in mutate._iter_slots(body, library)
                   if pi is None or fi == pi), key=lambda t: (t[0], t[4], t[3]))


def _bnn(body: dict, fi: int, key: str) -> dict:
    return body["preset"]["flow"][fi][key]


def _slot0(bnn: dict) -> dict:
    return (bnn.get("slot") or [{}])[0]


def _instrument_path(body: dict, library: Library) -> int:
    """The first path with an amp in it; path 0 when there is none."""
    return next((t[0] for t in _slots(body, library) if _is_amp(t[2])), 0)


def _cabs_after_amp(body: dict, library: Library, pi: int) -> list[tuple]:
    sl = _slots(body, library, pi)
    last = max((i for i, t in enumerate(sl) if _is_amp(t[2])), default=-1)
    return [t for t in sl[last + 1:] if _is_cab(t[2])]


def _live_cabs_after_amp(body: dict, library: Library, pi: int) -> list[tuple]:
    """Cabs after the last amp that can sound; a dead one is left in the chain
    untouched and does not count toward the room IR's "exactly one cab"."""
    return [t for t in _cabs_after_amp(body, library, pi) if _live(body, t)]


def _bypass_switched(body: dict, fi: int, key: str, lane: int, pos: int) -> bool:
    """A footswitch / EXP toe / MIDI bypass assignment can turn this block on."""
    en = _bnn(body, fi, key).get("@enabled")
    if isinstance(en, dict) and isinstance(en.get("controller"), dict):
        return True
    return any(isinstance(r, dict) and r.get("param") is None and
               (r.get("path"), r.get("lane"), r.get("pos")) == (fi, lane, pos)
               for r in mutate._midi_records(body))


def _live(body: dict, t: tuple) -> bool:
    """Can this block ever sound? On in some snapshot (or at base), or switchable.
    Snapshots only disable, so a block off everywhere with no switch is dead."""
    fi, key, _, lane, pos = t
    en = _bnn(body, fi, key).get("@enabled")
    en = en if isinstance(en, dict) else {"value": True}
    return (any(v is not False for v in [en.get("value"), *(en.get("snapshots") or [])])
            or _bypass_switched(body, fi, key, lane, pos))


def _varies(w: Any) -> bool:
    snaps = w.get("snapshots") if isinstance(w, dict) else None
    return isinstance(snaps, list) and len({repr(v) for v in snaps}) > 1


def _sid(body: dict, switch: str) -> int:
    return controllers.resolve_controller_source(mutate._chassis_device_id(body), switch)


def _free_switches(body: dict) -> list[str]:
    return [s for s in FREE_SWITCHES if not mutate._source_in_use(body, _sid(body, s))]


def _release(body: dict, sid: int, drop: bool = False) -> None:
    """Blank a source's scribble strip once nothing uses it: the shape every
    unused switch has in a real preset's ``sources`` (the entry itself stays,
    unless ``drop``: the transform created it, so the plain preset had none)."""
    if not mutate._source_in_use(body, sid):
        sources = body["preset"].get("sources") or {}
        if drop:
            sources.pop(str(sid), None)
            return
        entry = sources.get(str(sid))
        if isinstance(entry, dict):
            if "fs_label" in entry:
                entry["fs_label"] = ""
            if "fs_color" in entry:
                entry["fs_color"] = "auto"


def _note_fresh(body: dict, detail: dict, switch: str) -> None:
    """Record a switch whose ``sources`` entry does not exist yet, so reversing
    can remove it again (real presets already carry one per switch)."""
    if str(_sid(body, switch)) not in (body["preset"].get("sources") or {}):
        detail.setdefault("sources_added", []).append(switch)


def _ir_wav(bnn: dict, irs: IrMapping) -> Path | None:
    h = _slot0(bnn).get("irhash")
    if h not in irs.entries:
        return None
    wav = irs.resolve_by_hash(h)
    return wav if wav.is_file() else None


def _snapshot_name(body: dict, i: int) -> str:
    meta = (body["preset"].get("snapshots") or [])
    name = meta[i].get("name") if i < len(meta) and isinstance(meta[i], dict) else None
    return name or f"#{i + 1}"


# --- → headphones -----------------------------------------------------------

def _room_ir_blocker(body, pi, cabs, irs, room_mode) -> str | None:
    """Why the room-IR treatment cannot run (the Tile fallback's reason), or None."""
    if room_mode == "tile":
        return "room mode tile requested"
    if len(cabs) != 1:
        return f"{len(cabs)} cab blocks after the amp; the room IR needs exactly one"
    fi, key, blk, lane, _ = cabs[0]
    bnn = _bnn(body, fi, key)
    en = bnn.get("@enabled") if isinstance(bnn.get("@enabled"), dict) else {}
    if en.get("value") is False:
        return f"the cab {blk.display_name!r} is bypassed at base"
    off = [i for i, v in enumerate(en.get("snapshots") or []) if v is False]
    if off:
        return f"the cab is bypassed in snapshot {_snapshot_name(body, off[0])!r}"
    if _bypass_switched(body, *cabs[0][:2], lane, cabs[0][4]):
        return f"the cab {blk.display_name!r} has a bypass switch assigned"
    if not blk.model_id.startswith(IR_MODEL_PREFIX):
        return f"{blk.display_name!r} is a stock cab, not an IR block"
    if _ir_wav(bnn, irs) is None:
        return (f"the close IR's wav ({_slot0(bnn).get('irhash')}) is not registered "
                "or not on disk")
    path = body["preset"]["flow"][pi]
    if lane != 0 or any(isinstance(b, dict) and b.get("type") in ("split", "join")
                        for b in path.values()):
        return "the path already has a split"
    return None


def _headphones(body, library, irs, *, room_ir, room_mode, room_offset_db) -> VenueResult:
    if room_mode not in ("auto", "tile"):
        raise VenueError(f"room_mode must be 'auto' or 'tile'; got {room_mode!r}")
    detail: dict[str, Any] = {"treatment": "none", "room_ir": None, "room_level_db": None,
                              "room_switch": None, "ambience_block": None,
                              "ambience_switch": None, "fallback_reason": None}
    pi = _instrument_path(body, library)
    base, _ = _untapped(body, pi)
    cabs = _live_cabs_after_amp(base, library, pi)
    if not cabs:
        return VenueResult(copy.deepcopy(body), "headphones", detail,
                           ["no live cab or IR block after the amp: chain left unchanged"])
    reason = _room_ir_blocker(base, pi, cabs, irs, room_mode)
    _, ckey, cblk, _, cpos = cabs[0]
    room = level = None
    if reason is None:
        close_wav = _ir_wav(_bnn(base, pi, ckey), irs)
        room = find_room_ir(close_wav, irs, override=room_ir)
        if room is None:
            reason = f"no room IR registered for {close_wav.name}"
    warnings: list[str] = []
    if room is not None:
        lvl = (_slot0(_bnn(base, pi, ckey)).get("params") or {}).get("Level")
        close_level = float(lvl["value"] if isinstance(lvl, dict) and "value" in lvl
                            else authoring_defaults(cblk).get("Level", -18.0))
        level = room_level_db(close_level, close_wav, room[1], room_offset_db)

    def edit(work: dict, use_room: bool, with_tile: bool) -> dict:
        d = dict(detail)
        free = _free_switches(work)
        if use_room:
            d.update(treatment="room-ir", room_ir=room[0], room_level_db=level)
            keys = mutate.wrap_in_split(
                work, cblk.display_name, cblk.model_id, library, path=pi, lane=0, pos=cpos,
                sibling_params={"Level": level}, sibling_ir=room[0], irs=irs,
                join_params=JOIN_PARAMS)
            if free:
                d["room_switch"] = s = free.pop(0)
                _note_fresh(work, d, s)
                mutate.wire_footswitch(
                    work, s, cblk.display_name, "latching", library, path=pi, lane=1,
                    pos=int(keys["sibling"][1:]) - 14, param="Level", min=-60.0, max=level,
                    label="ROOM MIC", color="purple")
        if with_tile:
            tpos = int(mutate.add_block(work, TILE["block"], library, path=pi,
                                        params=TILE["params"])[1:])
            if use_room:
                mutate.set_enabled(work, TILE["block"], False, library, path=pi, lane=0, pos=tpos)
            d["ambience_block"] = TILE["block"]
            if free:
                d["ambience_switch"] = s = free.pop(0)
                _note_fresh(work, d, s)
                mutate.wire_footswitch(work, s, TILE["block"], "latching", library, path=pi,
                                       lane=0, pos=tpos, label="TILE", color="turquoise")
        return d

    # room IR + disabled Tile, then room IR alone, then the Tile fallback: the
    # first that fits the grid (lane capacity, USB tap) wins.
    attempts = ([(True, True), (True, False)] if room is not None else []) + [(False, True)]
    last: Exception | None = None
    for use_room, with_tile in attempts:
        work, taps = _untapped(body, pi)
        try:
            out = edit(work, use_room, with_tile)
            _retap(work, taps)
        except (mutate.LaneFullError, _NoRoom) as e:
            last = e
            continue
        if use_room and not with_tile:
            warnings.append(f"no room for the Tile block ({last}); added without it")
        if not use_room:
            if room is not None:
                reason = f"no room in the chain for the room IR split ({last})"
            out["fallback_reason"] = reason
            warnings.append(f"room IR treatment skipped ({reason}); Tile ambience added instead")
            out["treatment"] = "tile"
        if out["room_ir"] and out["room_switch"] is None:
            warnings.append("no free footswitch for the room IR level (ROOM MIC)")
        if out["ambience_block"] and out["ambience_switch"] is None:
            warnings.append("no free footswitch for the Tile block (TILE)")
        return VenueResult(work, "headphones", out, warnings)
    # Not even Tile fits: leave the chain unchanged rather than fail the caller.
    detail["fallback_reason"] = (reason := f"no room in the chain for the Tile block ({last})")
    return VenueResult(copy.deepcopy(body), "headphones", detail,
                       [f"headphones treatment skipped ({reason}); chain left unchanged"])


# --- → direct (from headphones) ---------------------------------------------

def _strip_room_stage(work: dict, detail: dict, library: Library) -> None:
    """Remove exactly what ``detail`` recorded from ``work`` (taps lifted).
    Everything is verified before anything is edited; a mismatch with the
    chain is a ``VenueError`` naming it (no best effort)."""
    pi = _instrument_path(work, library)
    path = work["preset"]["flow"][pi]
    added: list[tuple[str, dict, int | None]] = []   # (name, bNN, recorded switch source)
    close = tile = None
    room_ir = detail.get("room_ir")
    if room_ir:
        rooms = [k for k, b in path.items() if k[1:].isdigit() and int(k[1:]) >= 14
                 and isinstance(b, dict) and _slot0(b).get("irhash") == room_ir]
        if len(rooms) != 1:
            raise VenueError(f"room_ir {room_ir} is not a lane-1 IR of the instrument path "
                             "(swapped or removed by hand?); cannot reverse")
        rk = rooms[0]
        sk = next((k for k, b in path.items() if isinstance(b, dict)
                   and b.get("type") == "split" and b.get("branch") == rk), None)
        chain = mutate._lane0_chain(path)
        jk = path[sk].get("endpoint") if sk else None
        join = path.get(jk) if jk in chain else None
        if not (isinstance(join, dict) and join.get("type") == "join"
                and join.get("endpoint") == sk and chain.index(sk) < chain.index(jk)):
            raise VenueError("the room IR is no longer inside a split/join; cannot reverse")
        inside = chain[chain.index(sk) + 1:chain.index(jk)]
        close = next((t for t in _slots(work, library, pi) if t[1] in inside), None)
        if join.get("branch") != rk or len(inside) != 1 or close is None:
            raise VenueError("blocks were added inside the room IR split; cannot reverse")
        rs = detail.get("room_switch")
        sid = _sid(work, rs) if rs else None
        if rs:
            ctrl = ((_slot0(path[rk]).get("params") or {}).get("Level") or {}).get("controller")
            if not (isinstance(ctrl, dict) and ctrl.get("source") == sid
                    and ctrl.get("type") == "param"):
                raise VenueError(f"room_switch {rs} no longer toggles the room IR Level; "
                                 "cannot reverse")
        added.append(("the room IR", path[rk], sid))
    name = detail.get("ambience_block")
    if name:
        tiles = [t for t in _slots(work, library, pi) if t[3] == 0 and t[2].display_name == name]
        sw = detail.get("ambience_switch")
        sid = _sid(work, sw) if sw else None
        if sw:
            tiles = [t for t in tiles if ((_bnn(work, pi, t[1]).get("@enabled") or {})
                                          .get("controller") or {}).get("source") == sid]
            if not tiles:
                raise VenueError(f"ambience_switch {sw} no longer toggles {name!r} "
                                 "(re-wired or the block removed?); cannot reverse")
        elif not tiles:
            raise VenueError(f"ambience_block {name!r} is not in the instrument path; "
                             "cannot reverse")
        tile = tiles[-1]
        added.append((repr(name), _bnn(work, pi, tile[1]), sid))
    for what, bnn, sid in added:          # nothing else may point at them
        for w in mutate._iter_controller_wrappers(work, only=bnn):
            c = w.get("controller")
            if (isinstance(c, dict) and c.get("source") != sid) or _varies(w):
                raise VenueError(f"a {'controller' if isinstance(c, dict) else 'snapshot'} "
                                 f"references {what}, which the headphones treatment added; "
                                 "cannot reverse")

    fresh = {_sid(work, sw) for sw in detail.get("sources_added") or []}
    for _, bnn, sid in added:             # verified: now edit
        if sid is not None:
            for w in mutate._iter_controller_wrappers(work, only=bnn):
                w.pop("controller", None)
            _release(work, sid, drop=sid in fresh)
    if tile is not None:
        mutate.remove_block(work, tile[2].display_name, library, path=pi, lane=0, pos=tile[4])
    if close is not None:
        mutate.unwrap_split(work, close[2].display_name, library, path=pi, lane=0, pos=close[4])


def _direct(body, library, detail) -> VenueResult:
    if detail is None:
        raise VenueError("cannot reverse a headphones variant without its venue_detail "
                         "(it records what the treatment added)")
    if detail.get("treatment") == "none":
        return VenueResult(copy.deepcopy(body), "direct", {}, [])
    work, taps = _untapped(body, _instrument_path(body, library))
    _strip_room_stage(work, detail, library)
    _retap(work, taps)      # only blocks were removed, so the tap fits where it was
    return VenueResult(work, "direct", {}, [])


# --- → amp-in ---------------------------------------------------------------

def _amp_in(body, library) -> VenueResult:
    amps = [t for t in _slots(body, library) if _is_amp(t[2])]
    if not amps:
        raise VenueError("amp-in needs an amp block; this preset has no amp")
    work, taps = _untapped(body, amps[-1][0])     # the EQ lands on the last amp's path
    sl = _slots(work, library)
    amps = [t for t in sl if _is_amp(t[2])]
    device = mutate._chassis_device_id(work)
    warnings: list[str] = []
    for fi, key, blk, lane, pos in (t for t in sl if _is_cab(t[2])):
        wrappers = list(mutate._iter_controller_wrappers(work, only=_bnn(work, fi, key)))
        sids = [w["controller"].get("source") for w in wrappers
                if isinstance(w.get("controller"), dict)]
        warnings += [f"{controllers.controller_name_for_source(device, s) or f'controller {s}'} "
                     f"on {blk.display_name!r} removed with the cab" for s in sids]
        if any(_varies(w) for w in wrappers):
            warnings.append(f"per-snapshot values on {blk.display_name!r} removed with the cab")
        try:
            mutate.remove_block(work, blk.display_name, library, path=fi, lane=lane, pos=pos)
        except mutate.MutateError as e:
            raise VenueError(f"amp-in cannot remove {blk.display_name!r} (path {fi}, "
                             f"{key}): it sits inside a split") from e
        for s in sids:
            _release(work, s)
    fi, _, amp, lane, pos = amps[-1]
    if lane != 0:
        raise VenueError(f"amp-in puts the EQ after the last amp, {amp.display_name!r}, "
                         "which sits on lane 1; not supported")
    try:
        mutate.add_block(work, EQ["block"], library, path=fi, after=amp.display_name,
                         params=EQ["params"])
        _retap(work, taps)
    except (mutate.LaneFullError, _NoRoom) as e:
        raise VenueError(f"no room in the chain for the amp-in EQ: {e}") from e
    return VenueResult(work, "amp-in", {"eq_block": EQ["block"]}, warnings)


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
    try:
        if target == "headphones":
            return _headphones(body, library, irs, room_ir=room_ir, room_mode=room_mode,
                               room_offset_db=room_offset_db)
        if source_venue == "headphones":
            direct = _direct(body, library, source_detail)
            if target == "direct":
                return direct
            body = direct.body
        if target == "direct":
            return VenueResult(copy.deepcopy(body), "direct", {}, [])   # unspecified = direct
        return _amp_in(body, library)
    except (SpecError, mutate.MutateError, GenerateError, ParamValidationError) as e:
        raise VenueError(str(e)) from e
