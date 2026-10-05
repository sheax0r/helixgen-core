"""USB output taps: send each DSP path to its own USB pair, beside the main mix.

A recording setup (OBS, a DAW) often listens to dedicated USB pairs rather
than the main mix on USB 1/2 — guitar on 3/4, mic on 5/6. The `usb_taps`
preference (``{"guitar": "3/4", "mic": "5/6"}``) asks for that on every
`.hsp` helixgen writes; `helixgen usb-taps` retrofits existing ones.

The structure is the one built by hand on a Stadium XL and verified on the
hardware. On each tapped path (``preset.flow[i]``, a 2x14 grid of ``bNN``
keys — lane 0 = ``b00``..``b13``, lane 1 = ``b14``..``b27``, key IS position):

- a Y split (``P35_AppDSPSplitY``) in lane 0 AFTER every other block (lane 0
  and lane 1), at ``b11`` — or ``b12`` when ``b11`` is taken;
- an output block in lane 1 at ``b27`` carrying the USB pair's model.

The split's B side runs straight along row 1 into the tap, so the tap hears
the finished chain; the transcoder points its ``bblk`` at ``14 + pos``
(0.52.1). Neither block carries a ``snapshots`` array: an untracked block
keeps its base state on every snapshot, so recall never bypasses a tap.

Which path gets which pair:

- a path fed by ``P35_InputMic`` → the mic pair;
- the first path carrying blocks that does not end at the 1/4" output →
  the guitar pair. A SECOND such path (a dual-amp rig) gets the guitar pair
  too when both paths end at the same output, so the tap carries the blend
  (two outputs on one USB pair sum — NOT yet verified on hardware).
  Different outputs = FOH + Amp: only the FOH path is tapped, never the one
  feeding the amp (1/4"), whichever DSP it is on;
- a path with no blocks (the vestigial second path of the "2 Guitar Rig"
  template) → the mic pair;
- DSP 1 feeding DSP 2 (row-0 output ``P35_OutputPath2*``): the chain ends on
  path 2, so only path 2 is tapped, with the guitar pair.

All or nothing: a preset that can't take every tap it needs (lane 0 full,
``b27`` occupied) is left untouched, never half-patched. A preset already
carrying a USB tap anywhere is left untouched, so this never stacks —
except a tap a block has since landed after, which is moved back to the end.
The edit verbs work around the taps (:func:`lifted`), so `add-block` and
friends still run and the tap stays last.
"""
from __future__ import annotations

import copy
import sys
from contextlib import contextmanager
from typing import Any

PAIR_MODELS = {"1/2": "P35_OutputUSB1_2", "3/4": "P35_OutputUSB3_4",
               "5/6": "P35_OutputUSB5_6"}
_TAP_MODELS = frozenset(PAIR_MODELS.values())
_MIC_INPUT = "P35_InputMic"
_AMP_OUTPUTS = frozenset({"P35_OutputQtrInch"})
_SPLIT_CHOICES = (11, 12)


def _model(block: Any) -> str:
    slots = (block or {}).get("slot") or [{}]
    return slots[0].get("model", "") if isinstance(slots[0], dict) else ""


def _cells(path: dict) -> list[int]:
    """Grid indices of every user cell in use (lane 0 b01..b12, lane 1 b15..b26)."""
    return [int(k[1:]) for k in path
            if k.startswith("b") and k[1:].isdigit()
            and int(k[1:]) not in (0, 13, 14, 27)]


def _split_block(pos: int) -> dict:
    return {
        "@enabled": {"value": True}, "type": "split", "position": pos,
        "path": 0, "favorite": 0,
        "slot": [{"model": "P35_AppDSPSplitY", "@enabled": {"value": True},
                  "params": {"BalanceA": {"value": 0.5},
                             "BalanceB": {"value": 0.5},
                             "enable": {"value": True}},
                  "version": 0}],
    }


def _tap_block(model: str) -> dict:
    return {
        "@enabled": {"value": True}, "type": "output", "position": 13,
        "path": 1, "favorite": 0,
        "slot": [{"model": model, "@enabled": {"value": True},
                  "params": {"gain": {"value": 0.0}, "pan": {"value": 0.5}},
                  "version": 0}],
        "harness": {"@enabled": {"value": True}},
    }


def _roles(flows: list[dict]) -> dict[int, str]:
    """``{flow index: "guitar" | "mic"}`` for every path that wants a tap."""
    if len(flows) > 1 and _model(flows[0].get("b13")).startswith("P35_OutputPath2"):
        return {1: "guitar"}
    mic = {i for i, p in enumerate(flows) if _model(p.get("b00")) == _MIC_INPUT}
    rigs = [i for i, p in enumerate(flows) if i not in mic and _cells(p)]
    if not rigs:
        rigs = [i for i in range(len(flows)) if i not in mic][:1]
    # FOH + Amp: the rig feeding the amp (1/4") is not the recorded tone,
    # whichever path it sits on.
    main = next((i for i in rigs if _model(flows[i].get("b13")) not in _AMP_OUTPUTS),
                rigs[0] if rigs else None)
    roles: dict[int, str] = {}
    for i, p in enumerate(flows):
        if i in mic:
            roles[i] = "mic"
        elif i == main:
            roles[i] = "guitar"
        elif i in rigs:
            if _model(p.get("b13")) == _model(flows[main].get("b13")):
                roles[i] = "guitar"   # dual-amp: both rigs blend onto one pair
        else:
            roles[i] = "mic"          # empty path: the vestigial second input
    return roles


def _find_tap(path: dict) -> tuple[str, str] | None:
    """``(split key, tap model)`` of this path's tap, or None. The tap split is
    the last lane-0 split with no join after it (its B side runs to b27)."""
    if not (isinstance(path.get("b27"), dict) and path["b27"].get("type") == "output"
            and _model(path["b27"]) in _TAP_MODELS):
        return None
    lane0 = {int(k[1:]): b.get("type") for k, b in path.items()
             if k.startswith("b") and k[1:].isdigit() and 0 < int(k[1:]) < 13
             and isinstance(b, dict)}
    joins = [c for c, t in lane0.items() if t == "join"]
    splits = [c for c, t in lane0.items() if t == "split" and not any(j > c for j in joins)]
    return (f"b{max(splits):02d}", _model(path["b27"])) if splits else None


def _stale(path: dict) -> bool:
    """A block now sits after this path's tap split, so the tap misses it."""
    t = _find_tap(path)
    if t is None:
        return False
    pos = int(t[0][1:])
    return any((c if c < 14 else c - 14) >= pos for c in _cells(path) if c != pos)


def _lift(flows: list[dict], which) -> dict[int, str]:
    taken: dict[int, str] = {}
    for i in which:
        t = _find_tap(flows[i])
        if t:
            del flows[i][t[0]], flows[i]["b27"]
            taken[i] = t[1]
    return taken


def _place(flows: list[dict], wanted: dict[int, str]) -> dict:
    """Place ``{flow index: tap model}`` — all of them, or none."""
    plan: list[tuple[int, int, str]] = []
    for i, model in sorted(wanted.items()):
        p = flows[i]
        if _model(p.get("b13")) == model:
            continue                  # the main output already feeds that pair
        if "b27" in p:
            return {"status": "skipped", "detail":
                    f"path {i + 1}: b27 already holds {_model(p['b27']) or 'a block'}"}
        last = max((c if c < 14 else c - 14 for c in _cells(p)), default=0)
        pos = next((c for c in _SPLIT_CHOICES if c > last), None)
        if pos is None:
            where = ("lane 0 full" if all(f"b{c:02d}" in p for c in range(1, 13))
                     else f"block at b{last:02d}" if f"b{last:02d}" in p
                     else f"lane 1 block at b{last + 14}")
            return {"status": "skipped", "detail":
                    f"path {i + 1}: {where}, no free cell after the last block for the tap split"}
        plan.append((i, pos, model))
    if not plan:
        return {"status": "skipped", "detail": "no path to tap for the configured roles"}
    for i, pos, model in plan:
        flows[i][f"b{pos:02d}"] = _split_block(pos)
        flows[i]["b27"] = _tap_block(model)
    return {"status": "added", "detail": ", ".join(
        f"path {i + 1}: split b{pos:02d} -> {model.removeprefix('P35_Output')}"
        for i, pos, model in plan)}


def has_tap(body: dict) -> bool:
    """True when any path already carries a USB output block beside its main output."""
    for p in (body.get("preset") or {}).get("flow") or []:
        for k, blk in p.items():
            if (k.startswith("b") and k != "b13" and isinstance(blk, dict)
                    and blk.get("type") == "output" and _model(blk) in _TAP_MODELS):
                return True
    return False


def apply(body: dict, taps: dict[str, str]) -> dict:
    """Add the taps ``taps`` (``{"guitar": "3/4", "mic": "5/6"}``) asks for.

    Returns ``{"status": "added" | "present" | "skipped", "detail": str}``;
    ``body`` is mutated only on ``"added"``. A preset already carrying a tap
    is ``present`` and untouched — unless a block now sits AFTER a tap split
    (added on the hardware, or re-authored from `view`), in which case that
    tap is moved back to the end of the chain.
    """
    flows = (body.get("preset") or {}).get("flow") or []
    if has_tap(body):
        stale = [i for i, p in enumerate(flows) if _stale(p)]
        if not stale:
            return {"status": "present", "detail": "already has a USB tap"}
        trial = copy.deepcopy(flows)
        r = _place(trial, _lift(trial, stale))
        if r["status"] != "added":
            return {"status": "skipped",
                    "detail": f"stale tap (a block sits after its split) — {r['detail']}"}
        flows[:] = trial
        return {"status": "added", "detail": f"moved stale tap: {r['detail']}"}
    trial = copy.deepcopy(flows)
    r = _place(trial, {i: PAIR_MODELS[taps[role]]
                       for i, role in _roles(flows).items() if role in taps})
    if r["status"] == "added":
        flows[:] = trial
    return r


@contextmanager
def lifted(body: dict):
    """Take the taps out of ``body`` for the duration of an edit, then put
    them back after the (possibly new) last block. The edit verbs refuse a
    path with a split in it, and an appended block must land BEFORE the tap.
    A tap that no longer fits is dropped with a stderr warning naming the
    tone — never half-kept."""
    flows = (body.get("preset") or {}).get("flow") or []
    taken = _lift(flows, range(len(flows))) if isinstance(flows, list) else {}
    yield
    if taken:
        r = _place(flows, taken)
        if r["status"] != "added":
            name = (body.get("meta") or {}).get("name") or "preset"
            print(f"warning: {name!r}: USB taps REMOVED by this edit — {r['detail']}. "
                  f"The tone is now silent on its USB tap pairs.", file=sys.stderr)


def configured() -> dict[str, str]:
    """The validated `usb_taps` preference (``{}`` when unset). Raises
    ``PreferencesError`` on a malformed preferences file or block."""
    from helixgen.preferences import load_preferences, parse_usb_taps
    return parse_usb_taps(load_preferences().usb_taps)


def apply_preference(body: dict) -> dict:
    """The `usb_taps` preference applied to a copy of ``body`` (``body`` itself
    when the preference is unset or there is nothing to add).

    Called on every `.hsp` write. Advisory: a bad preference or a preset that
    can't take the taps warns on stderr, naming the tone, and never fails
    the write.
    """
    from helixgen.preferences import PreferencesError
    try:
        taps = configured()
    except PreferencesError as e:
        print(f"stored preferences ignored (usb_taps not applied): {e}", file=sys.stderr)
        return body
    if not taps or not isinstance((body.get("preset") or {}).get("flow"), list):
        return body
    out = copy.deepcopy(body)
    result = apply(out, taps)
    name = (body.get("meta") or {}).get("name") or "preset"
    if result["status"] == "added":
        print(f"usb_taps: {name!r}: added {result['detail']}", file=sys.stderr)
        return out
    if result["status"] == "skipped":
        print(f"warning: {name!r}: USB taps NOT added — {result['detail']}. "
              f"The tone is silent on the tapped USB pairs.", file=sys.stderr)
    return body
