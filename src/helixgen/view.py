"""View: project a parsed Stadium .hsp body dict into a readable recipe-shape
dict (name, paths[*].blocks, snapshots, footswitches, expression, etc.) for
agents/humans to comprehend a preset.

This is a direct port of ``decompile.decompile_body`` (and every private
helper it depends on) under the hsp-canonical redesign: ``.hsp`` is the
single source of truth, and ``view()`` is the read-only projection off of it.
Unlike the old ``decompile()`` entry point, ``view()`` never reads a path off
disk and never writes a sidecar file -- it takes an already-parsed body dict
(from ``hsp.read_hsp``) and returns a plain dict. It is lossy by design; see
the fidelity notes below (carried over from decompile.py).

The fidelity bar is *round-trip stability*: composing the returned spec must
reproduce the source preset body (modulo the generated_at provenance stamp).
Only values that differ from the library exemplar are emitted, so specs stay
minimal and readable.

Limitations
-----------
* **Ambiguous display names**: when a block's display_name matches multiple
  library entries the emitted reference is the ``model_id`` instead, so the
  spec regenerates unambiguously.
* **Orphan IR hash**: if an IR slot's ``irhash`` is neither registered in the
  IR mapping *nor* equal to the block's ingest-time ``default_irhash``, the
  raw 32-hex hash is emitted as the ``ir`` field. Regenerating that spec will
  fail with ``GenerateError`` (wrapping the underlying ``IrMappingError``)
  until the IR is registered via ``helixgen register-irs``.
"""
from __future__ import annotations

import copy
import os
import sys
from typing import Any

from helixgen import controllers, flowparams
from helixgen.generate import _coerce_param_value
from helixgen.hsp import ENDPOINT_KEYS as _ENDPOINT_KEYS, _translate_model_id, _unwrap_value
from helixgen.ir import IR_MODEL_PREFIX, IrMapping
from helixgen.library import Block, Library
from helixgen.spec import StructuralEntry


def _try_load_block(library: Library, model_id: str) -> Block | None:
    """Load a block by (already-translated) model id, or return None if the
    library can't resolve it.

    Mirrors the skip-on-KeyError policy used across `_name_index` and
    `mutate._iter_slots`: a single unknown/unmodeled model id must not abort
    the whole `view` projection. `library.load_block` raises `KeyError` for an
    unknown model id.
    """
    try:
        return library.load_block(model_id)
    except KeyError:
        return None


def _device_id(body: dict) -> Any:
    return (body.get("meta") or {}).get("device_id") or "stadium_xl"


def _bnn_keys(path_dict: dict) -> list[str]:
    return sorted(
        k for k in path_dict
        if isinstance(k, str) and k.startswith("b")
        and k not in _ENDPOINT_KEYS and k[1:].isdigit()
    )


def _is_endpoint(bnn: dict) -> bool:
    return isinstance(bnn, dict) and bnn.get("type") in ("input", "output")


def _is_orphan_structural(path_dict: dict, bnn: dict) -> bool:
    """True for a split/join whose `endpoint` partner is NOT the complementary
    block type (i.e. the partner is an input/output endpoint). Such split/join
    slots cannot be reconstructed semantically and are captured verbatim."""
    typ = bnn.get("type")
    if typ not in ("split", "join"):
        return False
    partner = path_dict.get(bnn.get("endpoint"))
    partner_type = partner.get("type") if isinstance(partner, dict) else None
    want = "join" if typ == "split" else "split"
    return partner_type != want


def _is_structural_slot(path_dict: dict, key: str, bnn: dict) -> bool:
    """A routing-skeleton slot captured verbatim: any endpoint (except the main
    input b00, which drives the `input` field) or an orphaned split/join."""
    if _is_endpoint(bnn):
        return key != "b00"
    return _is_orphan_structural(path_dict, bnn)


def _input_mode(path_dict: dict, device_id: Any) -> str | None:
    b00 = path_dict.get("b00")
    if not isinstance(b00, dict) or not b00.get("slot"):
        return None
    model = b00["slot"][0].get("model", "")
    return controllers.input_mode_for_model(device_id, model)


def _flow_differs(value: Any, default: Any) -> bool:
    """Value-vs-default compare tolerant of float32 storage (real exports
    store e.g. decay 0.1 as 0.10000000149011612) and of per-channel dicts."""
    import math
    if isinstance(value, dict):
        return any(_flow_differs(v, default) for v in value.values())
    if isinstance(value, bool) or isinstance(default, bool):
        return value != default
    if isinstance(value, (int, float)) and isinstance(default, (int, float)):
        return not math.isclose(float(value), float(default),
                                rel_tol=1e-6, abs_tol=1e-9)
    return value != default


def _lift_input(path_dict: dict, device_id: Any, body: dict) -> "str | dict | None":
    """The path's `input` field: the bare mode string when everything modeled
    is at its device default, else the object form with the non-default
    input-endpoint params (pad/trim/gate/link) and any non-default impedance
    for the jacks the source uses. See the 2026-07-14 design spec §3.2."""
    mode = _input_mode(path_dict, device_id)
    if mode is None:
        return None
    slot = path_dict["b00"]["slot"][0]
    params = slot.get("params") or {}

    def val(name):
        w = params.get(name)
        if not isinstance(w, dict):
            return None
        if "1" in w and isinstance(w.get("1"), dict):  # stereo per-channel
            v1 = w["1"].get("value")
            v2 = (w.get("2") or {}).get("value")
            if v1 is None or v2 is None:
                # Malformed channel: not liftable — note a regenerate of this
                # projection normalizes the param to schema defaults (b00 is
                # never carried verbatim).
                return None
            if v1 == v2:
                return v1
            return {"1": v1, "2": v2}
        return w.get("value")

    lifts: dict[str, Any] = {}

    # Lifts mirror parse_spec's scoping (review F2): pad is only legal on an
    # instrument source and link only on the stereo "both" source, so a
    # leftover Pad/StereoLink param outside that scope stays un-lifted —
    # otherwise parse_spec(view(x)) would reject view's own output.
    has_jack = bool(flowparams.jacks_for_mode(mode))

    pad = val("Pad")
    if has_jack and pad is not None and _flow_differs(pad, 1):
        if isinstance(pad, dict):
            lifts["pad"] = {ch: v == 2 for ch, v in pad.items()}
        else:
            lifts["pad"] = pad == 2
    trim = val("Trim")
    if trim is not None and _flow_differs(trim, 0.0):
        lifts["trim"] = trim

    gate: dict[str, Any] = {}
    ng = val("noiseGate")
    th = val("threshold")
    dc = val("decay")
    if ng is not None and _flow_differs(ng, False):
        gate["enabled"] = ng
    if th is not None and _flow_differs(th, -48.0):
        gate["threshold"] = th
    if dc is not None and _flow_differs(dc, 0.1):
        gate["decay"] = dc
    if gate:
        # "enabled" must be explicit: the parse default for a gate OBJECT is
        # enabled=true, so a threshold-only lift on a gate-off block would
        # otherwise flip the gate on when regenerated.
        gate.setdefault("enabled", ng if ng is not None else False)
        lifts["gate"] = gate

    link = val("StereoLink")
    if mode == "both" and link is not None and _flow_differs(link, False):
        lifts["link"] = bool(link)

    preset_params = (body.get("preset") or {}).get("params") or {}
    jacks = flowparams.jacks_for_mode(mode)
    zs: dict[str, str] = {}
    for jack in jacks:
        z = preset_params.get(f"{jack}Z")
        if not isinstance(z, str):
            continue
        if z not in flowparams.IMPEDANCE_VALUES:
            print(f"warning: unrecognized {jack}Z value {z!r}; not lifted "
                  f"(regenerating this projection will reset it to "
                  f"{flowparams.IMPEDANCE_DEFAULT!r}).", file=sys.stderr)
            continue
        zs[jack] = z
    if any(z != flowparams.IMPEDANCE_DEFAULT for z in zs.values()):
        if len(set(zs.values())) == 1 and len(zs) == len(jacks):
            lifts["impedance"] = next(iter(zs.values()))
        else:
            lifts["impedance"] = zs

    if not lifts:
        return mode
    return {"source": mode, **lifts}


def _lift_output(path_dict: dict) -> dict | None:
    """The path's `output` field: `{"level", "pan"}` for whichever of the
    lane-0 output endpoint's gain/pan differ from the device defaults
    (0.0 dB / 0.5). None when both are default (the endpoint still
    round-trips verbatim as a structural entry)."""
    b13 = path_dict.get("b13")
    if not (isinstance(b13, dict) and b13.get("type") == "output"
            and b13.get("slot")):
        return None
    params = b13["slot"][0].get("params") or {}
    out: dict[str, Any] = {}
    if "gain" in params:
        g = _unwrap_value(params["gain"])
        if isinstance(g, (int, float)) and _flow_differs(g, 0.0):
            out["level"] = g
    if "pan" in params:
        p = _unwrap_value(params["pan"])
        if isinstance(p, (int, float)) and _flow_differs(p, 0.5):
            out["pan"] = p
    return out or None


def _iter_blocks(flow: list) -> Any:
    """Yield (path_idx, key, bnn, slot) for each user block in the flow.

    Split and join structural blocks are skipped — they are not in the library
    and carry no footswitch / expression / snapshot metadata.
    """
    for path_idx, path_dict in enumerate(flow):
        if not isinstance(path_dict, dict):
            continue
        for key in _bnn_keys(path_dict):
            bnn = path_dict.get(key)
            if isinstance(bnn, dict) and bnn.get("slot"):
                if bnn.get("type") in ("split", "join", "input", "output"):
                    continue
                yield path_idx, key, bnn, bnn["slot"][0]


def _ref_name(block) -> str:
    """Display name when non-empty, else model_id — never empty."""
    return block.display_name or block.model_id


def _name_index(flow: list, library: Library) -> dict:
    """Build a display-name → list-of-(path_idx, lane, pos) index over all placed blocks."""
    from collections import defaultdict
    idx: dict = defaultdict(list)
    for pi, path in enumerate(flow):
        if not isinstance(path, dict):
            continue
        for key in path:
            if not (isinstance(key, str) and key.startswith("b") and key[1:].isdigit()
                    and key not in _ENDPOINT_KEYS):
                continue
            bnn = path[key]
            if not isinstance(bnn, dict) or bnn.get("type") in ("split", "join", "input", "output") or not bnn.get("slot"):
                continue
            num = int(key[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
            block = _try_load_block(library, _translate_model_id(bnn["slot"][0].get("model", "")))
            if block is None:
                continue
            name = _ref_name(block)
            idx[name].append((pi, lane, pos))
    return idx


def _ref(name: str, pi: int, lane: int, pos: int, idx: dict) -> dict:
    """Return a block-reference dict, adding coordinates only when the name is ambiguous."""
    ref: dict = {"block": name}
    placements = idx.get(name, [])
    if len(placements) > 1:
        ref["lane"] = lane
        ref["pos"] = pos
        # Include path (even 0) when the name is ambiguous ACROSS paths — lane/pos
        # alone can't disambiguate a same-(lane,pos) collision between path 0 and 1.
        if len({p for (p, _, _) in placements}) > 1:
            ref["path"] = pi
    return ref


def _snapshot_names(body: dict) -> list[str]:
    """Names from preset.snapshots, trimmed of trailing `Snap N` placeholders."""
    raw = (body.get("preset") or {}).get("snapshots") or []
    names = [s.get("name", "") for s in raw]
    # A slot is a placeholder iff named exactly "Snap <i+1>".
    keep = 0
    for i, n in enumerate(names):
        if n != f"Snap {i + 1}":
            keep = i + 1
    return names[:keep]


def _warn_unrepresentable_enables(body: dict) -> None:
    """Warn for a base-bypassed block that is enabled in a named snapshot but
    has NO disable (an explicit `False`) anywhere in the named range. The
    disable-only snapshot model cannot express that enable, so it will not
    round-trip until a snapshot enable-override lands. 0/211 in the corpus."""
    names = _snapshot_names(body)
    n = len(names)
    if n == 0:
        return
    flow = (body.get("preset") or {}).get("flow") or []
    for pi, key, bnn, slot in _iter_blocks(flow):
        base = _unwrap_value(bnn.get("@enabled", True))
        if base is not False:
            continue
        en = bnn.get("@enabled")
        arr = en.get("snapshots") if isinstance(en, dict) else None
        if not isinstance(arr, list):
            continue
        named = arr[:n]
        has_enable = any(v is True for v in named)
        has_disable = any(v is False for v in named)
        if has_enable and not has_disable:
            print(
                f"warning: block {slot.get('model')!r} at path {pi} {key} is "
                f"base-bypassed but enabled in a snapshot with no disable; this "
                f"cannot round-trip under the disable-only snapshot model "
                f"(will read bypassed in every snapshot).",
                file=sys.stderr,
            )


def _recover_snapshots(body: dict, library: Library, idx: dict) -> list[dict[str, Any]]:
    """Recover the spec-level `snapshots` array from a decompiled body.

    `idx` is the display-name -> [(path_idx, lane, pos), ...] index (from
    `_name_index`); it's used to decide whether a snapshot ref needs
    coordinates (ambiguous display_name) or can stay a bare string/dict
    (unambiguous).

    Task 1 densified the per-param `snapshots` arrays on generate: every slot
    now carries an explicit value (base value fills previously-null slots)
    instead of `null`. That means a naive "is this slot non-None" check would
    record a spurious override for every non-diverging snapshot. We filter
    those out by comparing each slot's value to the param's own base value
    (`_unwrap_value(wrapped)`) and only keeping genuine differences.
    """
    names = _snapshot_names(body)
    if not names:
        return []
    # Per-snapshot accumulators keyed by (path_idx, lane, pos, display_name).
    disables: list[list[tuple]] = [[] for _ in names]
    params: list[dict[tuple, dict]] = [{} for _ in names]
    flow = (body.get("preset") or {}).get("flow") or []
    for pi, key, bnn, slot in _iter_blocks(flow):
        block = _try_load_block(library, _translate_model_id(slot.get("model", "")))
        if block is None:
            continue
        name = _ref_name(block)
        num = int(key[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
        coord = (pi, lane, pos, name)
        # @enabled snapshot overrides (False => disable in that snapshot). The
        # bNN base @enabled.value may now be False (base-bypassed block), but
        # disable-recovery keys only off explicit `snapshots[i] is False`, never
        # the base, so base bypass and per-snapshot bypass stay independent.
        en = bnn.get("@enabled")
        if isinstance(en, dict) and isinstance(en.get("snapshots"), list):
            for i, ov in enumerate(en["snapshots"]):
                if i < len(names) and ov is False:
                    disables[i].append(coord)
        # param snapshot overrides -- filter dense fills equal to base.
        for pname, wrapped in (slot.get("params") or {}).items():
            if not (isinstance(wrapped, dict) and isinstance(wrapped.get("snapshots"), list)):
                continue
            base = _coerce_param_value(block, pname, _unwrap_value(wrapped))
            for i, ov in enumerate(wrapped["snapshots"]):
                if i >= len(names) or ov is None:
                    continue
                coerced = _coerce_param_value(block, pname, ov)
                if coerced == base:
                    continue  # phantom: densify-filled base value, not a real override
                params[i].setdefault(coord, {})[pname] = coerced

    snaps: list[dict[str, Any]] = []
    for i, nm in enumerate(names):
        s: dict[str, Any] = {"name": nm}
        # disable: bare string if unambiguous, else coordinate dict
        dis = []
        for (dpi, dlane, dpos, dname) in disables[i]:
            r = _ref(dname, dpi, dlane, dpos, idx)
            dis.append(dname if len(r) == 1 else r)
        if dis:
            s["disable"] = dis
        # params: name-keyed dict if every param-block is unambiguous, else
        # the list-of-{**ref, "params": {...}} form.
        if params[i]:
            ambiguous = any(len(idx.get(pname, [])) > 1 for (_, _, _, pname) in params[i])
            if ambiguous:
                s["params"] = [
                    {**_ref(pname, ppi, plane, ppos, idx), "params": pv}
                    for (ppi, plane, ppos, pname), pv in params[i].items()
                ]
            else:
                s["params"] = {pname: pv for (_, _, _, pname), pv in params[i].items()}
        snaps.append(s)
    return snaps


def _source_hex(source: Any) -> str:
    """Render a controller source id as a stable 0x-hex string for labeling."""
    if isinstance(source, int) and not isinstance(source, bool):
        return f"0x{source:08x}"
    return str(source)


def _controller_extras(ctrl: dict) -> dict[str, Any]:
    """Non-default `curve`/`threshold` fields from a controller dict, for a
    recovered footswitch/expression entry. Defaults ("linear"/0.0/None) are
    omitted so specs stay minimal."""
    extras: dict[str, Any] = {}
    curve = ctrl.get("curve")
    if isinstance(curve, str) and curve != "linear":
        extras["curve"] = curve
    thr = ctrl.get("threshold")
    if isinstance(thr, (int, float)) and not isinstance(thr, bool) and thr != 0.0:
        extras["threshold"] = thr
    return extras


def _attach_scribble_strips(body: dict, device_id: Any, entries: list[dict[str, Any]]) -> None:
    """Lift `preset.sources` scribble-strip config (`fs_label`/`fs_color`) onto
    the FIRST recovered entry of each switch (a merge switch has one strip)."""
    sources = (body.get("preset") or {}).get("sources") or {}
    seen: set = set()
    for entry in entries:
        switch = entry.get("switch")
        if switch in seen:
            continue
        seen.add(switch)
        try:
            sid = controllers.resolve_controller_source(device_id, switch)
        except controllers.ControllerError:
            continue
        cfg = sources.get(str(sid))
        if not isinstance(cfg, dict):
            continue
        label = cfg.get("fs_label")
        if isinstance(label, str) and label:
            entry["label"] = label
        color = cfg.get("fs_color")
        if isinstance(color, str) and color not in ("", "auto") and color in controllers.FS_COLORS:
            entry["color"] = color


def _recover_footswitches(
    body: dict, library: Library, device_id: Any, idx: dict, unknowns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    flow = (body.get("preset") or {}).get("flow") or []
    out: list[dict[str, Any]] = []
    for pi, key, bnn, slot in _iter_blocks(flow):
        en = bnn.get("@enabled")
        ctrl = en.get("controller") if isinstance(en, dict) else None
        if not (isinstance(ctrl, dict) and ctrl.get("type") == "targetbypass"):
            continue
        block = _try_load_block(library, _translate_model_id(slot.get("model", "")))
        if block is None:
            continue
        num = int(key[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
        name = controllers.controller_name_for_source(device_id, ctrl.get("source"))
        behavior = ctrl.get("behavior", "latching")
        if name is None or behavior not in ("latching", "momentary"):
            # Un-tabled bypass source (EXP3, the stomp-bank-B 0x010102NN page,
            # the looper-function 0x010104NN bank, a reserved switch, ...) or
            # an out-of-vocabulary behavior (toedown / future firmware) the
            # spec cannot re-author. Keep it, labeled, instead of silently
            # dropping it — and never emit an entry parse_spec would reject.
            src = _source_hex(ctrl.get("source"))
            what = (f"unknown control behavior {behavior!r} (source {src})"
                    if name is not None else f"unknown control (source {src})")
            unknowns.append({
                "kind": "footswitch",
                "source": src,
                "label": what,
                "block": _ref_name(block),
            })
            continue
        out.append({"switch": name, **_ref(_ref_name(block), pi, lane, pos, idx),
                    "behavior": behavior,
                    **_controller_extras(ctrl)})
    return out


def _recover_expression(
    body: dict, library: Library, device_id: Any, idx: dict,
    unknowns: list[dict[str, Any]], footswitches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Recover `param`-type controllers: EXP-pedal sweeps into the spec's
    `expression` list, footswitch/toe param TOGGLES (corpus-real; the switch
    flips the param between min and max) appended to `footswitches`, and
    anything un-tabled kept in `unknown_controllers`."""
    flow = (body.get("preset") or {}).get("flow") or []
    by_pedal: dict[str, list[dict[str, Any]]] = {}
    for pi, key, _bnn, slot in _iter_blocks(flow):
        block = _try_load_block(library, _translate_model_id(slot.get("model", "")))
        if block is None:
            continue
        num = int(key[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
        for pname, wrapped in (slot.get("params") or {}).items():
            ctrl = wrapped.get("controller") if isinstance(wrapped, dict) else None
            if not (isinstance(ctrl, dict) and ctrl.get("type") == "param"):
                continue
            source_name = controllers.controller_name_for_source(device_id, ctrl.get("source"))
            lo, hi = ctrl.get("min", 0.0), ctrl.get("max", 1.0)

            def _numeric(x: Any) -> bool:
                return isinstance(x, (int, float)) and not isinstance(x, bool)

            if source_name in ("EXP1", "EXP2"):
                if not (_numeric(lo) and _numeric(hi)):
                    print(f"warning: skipping expression target on {block.display_name!r}."
                          f"{pname!r}: non-numeric sweep range ({lo!r}..{hi!r}) unsupported in v1.",
                          file=sys.stderr)
                    continue
                by_pedal.setdefault(source_name, []).append({
                    **_ref(_ref_name(block), pi, lane, pos, idx),
                    "param": pname,
                    "min": lo, "max": hi,
                    **_controller_extras(ctrl)})
            elif (source_name is not None and _numeric(lo) and _numeric(hi)
                    and ctrl.get("behavior", "latching") in ("latching", "momentary")):
                # A footswitch (or toe switch) toggling a param between two
                # values — first-class since the controller-depth work.
                footswitches.append({
                    "switch": source_name,
                    **_ref(_ref_name(block), pi, lane, pos, idx),
                    "param": pname,
                    "min": lo, "max": hi,
                    "behavior": ctrl.get("behavior", "latching"),
                    **_controller_extras(ctrl)})
            else:
                # Un-tabled source (looper bank, stomp bank B, ...) or a
                # non-numeric toggle range. Keep it labeled rather than dropping.
                src = _source_hex(ctrl.get("source"))
                unknowns.append({
                    "kind": "expression",
                    "source": src,
                    "label": f"unknown control (source {src})",
                    "block": _ref_name(block),
                    "param": pname,
                })
    return [{"pedal": p, "targets": t} for p, t in by_pedal.items()]


def _recover_midi(body: dict, library: Library, idx: dict) -> list[dict[str, Any]]:
    """Recover MIDI CC controller bindings from the helixgen-namespaced
    ``preset._helixgen_midi`` list back into the recipe ``midi`` shape,
    grouped by CC (backlog #33). Block references are re-derived from the block
    actually placed at each record's ``(path, lane, pos)`` so the projection
    round-trips (and disambiguates duplicate names) exactly like FS/EXP.

    The coordinate is authoritative — it is what the transcoder targets on
    ``device install``/``sync``. A record whose coordinate resolves to no
    placed block is DROPPED with a stderr warning (never silently projected
    via its stored block name, which install would not honor either); a
    record whose coordinate resolves to a block whose name no longer matches
    the stored one projects the coordinate-derived name with a staleness
    warning. The mutate verbs keep records reconciled (add/remove renumber,
    swap refreshes the name), so either state indicates a hand-edited or
    externally-produced ``_helixgen_midi`` list."""
    preset = body.get("preset") or {}
    recs = preset.get("_helixgen_midi")
    if not isinstance(recs, list):
        return []
    flow = preset.get("flow") or []
    by_cc: dict[int, list[dict[str, Any]]] = {}
    order: list[int] = []
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        cc = rec.get("cc")
        # Mirror bridge._hsp_midi_by_coord's guards: a hand-corrupted record
        # (cc out of 0..127, missing block) would otherwise project a `midi`
        # entry parse_spec rejects, while device install silently drops it.
        if not isinstance(cc, int) or isinstance(cc, bool) or not (0 <= cc <= 127):
            continue
        if not rec.get("block"):
            continue
        pi = rec.get("path", 0)
        lane = rec.get("lane", 0)
        pos = rec.get("pos")
        blk = None
        if (isinstance(pos, int) and isinstance(pi, int)
                and 0 <= pi < len(flow) and isinstance(flow[pi], dict)):
            bnn = flow[pi].get(f"b{14 * lane + pos:02d}")
            if isinstance(bnn, dict) and bnn.get("slot"):
                blk = _try_load_block(
                    library, _translate_model_id(bnn["slot"][0].get("model", "")))
        if blk is None:
            # Coordinate resolves to nothing -> install would drop this
            # binding too; drop it loudly rather than projecting a name-only
            # reference that misrepresents what the device would get.
            print(
                f"warning: MIDI CC {cc} record targets (path {pi}, lane {lane}, "
                f"pos {pos}) where no block is placed; binding dropped from "
                f"the projection (stale/hand-edited _helixgen_midi).",
                file=sys.stderr,
            )
            continue
        stored = rec.get("block")
        if stored not in (blk.display_name, blk.model_id):
            print(
                f"warning: MIDI CC {cc} record names block {stored!r} but "
                f"(path {pi}, lane {lane}, pos {pos}) holds "
                f"{_ref_name(blk)!r}; projecting the placed block "
                f"(coordinate is authoritative).",
                file=sys.stderr,
            )
        target: dict[str, Any] = dict(_ref(_ref_name(blk), pi, lane, pos, idx))
        if rec.get("param") is None:
            target["bypass"] = True
        else:
            target["param"] = rec["param"]
            target["min"] = rec.get("min", 0.0)
            target["max"] = rec.get("max", 1.0)
        if cc not in by_cc:
            by_cc[cc] = []
            order.append(cc)
        by_cc[cc].append(target)
    return [{"cc": cc, "targets": by_cc[cc]} for cc in order]


def _recover_commands(
    body: dict, device_id: Any,
    footswitches: list[dict[str, Any]], unknowns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Recover Command Center commands from ``preset.commands`` back into the
    recipe ``commands`` shape (backlog #16). Each source key resolves to a
    switch name (FS*/Instant*); each record's ``type`` + native params map back
    to a friendly ``command`` family. Records on an unrecognised source or an
    unknown family are skipped (kept round-trip safe). Ordinal order within a
    source is preserved.

    A command whose switch ALSO carries a recovered footswitch (block bypass /
    param toggle) assignment — a device-legal combination (Mandarin Fuzz's FS1
    does both) that helixgen cannot yet AUTHOR (`parse_spec` rejects it) — is
    NOT emitted as a first-class ``commands`` entry. It goes into
    ``unknown_controllers`` (which ``parse_spec`` ignores) with a stderr
    warning, the same never-drop / never-emit-unparseable idiom the FS/EXP
    recovery uses, so the projection stays round-trip safe."""
    preset = body.get("preset") or {}
    raw = preset.get("commands")
    if not isinstance(raw, dict):
        return []
    sources = preset.get("sources") or {}
    fs_switches = {f.get("switch") for f in footswitches}
    out: list[dict[str, Any]] = []
    for key in raw:
        try:
            source_id = int(key)
        except (TypeError, ValueError):
            continue
        switch = controllers.command_switch_for_source(device_id, source_id)
        if switch is None:
            continue
        records = raw[key]
        if not isinstance(records, list):
            continue
        scrib = sources.get(key) if isinstance(sources.get(key), dict) else {}
        for rec in sorted(
                (r for r in records if isinstance(r, dict)),
                key=lambda r: r.get("ordinal", 0)):
            entry = _recover_one_command(rec, switch, scrib)
            if entry is None:
                continue
            if switch in fs_switches:
                what = entry.get("command", "command")
                print(
                    f"warning: {switch} carries a Command Center {what} command "
                    f"AND a footswitch assignment; composing both on one switch "
                    f"is not yet authorable — command kept under "
                    f"unknown_controllers.",
                    file=sys.stderr,
                )
                unknowns.append({
                    "kind": "command",
                    "source": f"0x{source_id:08x}",
                    "switch": switch,
                    "label": (f"Command Center {what} command on a switch also "
                              f"carrying a footswitch assignment; composing "
                              f"both is not yet authorable"),
                    "command": {k: v for k, v in entry.items() if k != "switch"},
                })
                continue
            out.append(entry)
    return out


def _cmd_val(params: dict, name: str) -> int:
    w = params.get(name)
    if isinstance(w, dict) and isinstance(w.get("value"), int):
        return w["value"]
    return 0


def _recover_one_command(rec: dict, switch: str,
                         scrib: dict) -> dict[str, Any] | None:
    ctype = rec.get("type")
    params = rec.get("params") if isinstance(rec.get("params"), dict) else {}
    # Mirror parse_spec's ranges so a hand-corrupted / params-incomplete record
    # can't project a `commands` entry the parser would then reject (the #33
    # residual-2 discipline). MIDI needs a 1..16 channel; snapshot 0..7.
    if ctype == "MIDI" and _cmd_val(params, "MIDI Ch") not in range(1, 17):
        return None
    if ctype == "PresetSnapshot" and not (0 <= _cmd_val(params, "Snapshot") <= 7):
        return None
    entry: dict[str, Any] = {"switch": switch}
    if ctype == "PresetSnapshot":
        # Only the snapshot sub-action is in scope. A recall-PRESET command
        # (Preset/Setlist set) is a different, unanchored sub-action helixgen
        # does not author — skip it rather than misproject it as a snapshot.
        if _cmd_val(params, "Preset") or _cmd_val(params, "Setlist"):
            print(
                f"warning: {switch} carries a Command Center recall-preset "
                f"command (out of scope); dropped from the projection.",
                file=sys.stderr,
            )
            return None
        entry["command"] = "snapshot"
        entry["snapshot"] = _cmd_val(params, "Snapshot")
    elif ctype == "MIDI":
        sub = _cmd_val(params, "Command")
        channel = _cmd_val(params, "MIDI Ch")
        if sub == 1:
            entry.update(command="midi_cc", cc=_cmd_val(params, "CC#"),
                         value=_cmd_val(params, "Value"), channel=channel)
        elif sub == 0:
            entry.update(command="midi_pc", program=_cmd_val(params, "PC"),
                         channel=channel, bank_msb=_cmd_val(params, "MSB"),
                         bank_lsb=_cmd_val(params, "LSB"))
        elif sub == 3:
            entry.update(command="midi_note", note=_cmd_val(params, "Note"),
                         velocity=_cmd_val(params, "Velocity"), channel=channel,
                         note_off=bool(_cmd_val(params, "NoteOff")))
        elif sub == 2:
            entry.update(command="midi_mmc", message=_cmd_val(params, "Message"),
                         channel=channel)
        else:
            return None
    else:
        return None
    behavior = rec.get("behavior")
    if behavior == "momentary":
        entry["behavior"] = behavior
    if rec.get("toggle") is True:
        entry["toggle"] = True
    if switch.startswith("FS"):
        label = scrib.get("fs_label")
        if isinstance(label, str) and label:
            entry["label"] = label
        color = scrib.get("fs_color")
        if isinstance(color, str) and color not in (None, "auto"):
            entry["color"] = color
    return entry


def _entry_for(key, bnn, library, irs):
    """Build a spec entry dict (block/split/join) with explicit lane/pos."""
    num = int(key[1:])
    lane = 1 if num >= 14 else 0
    pos = num - 14 * lane
    typ = bnn.get("type")
    slot = bnn["slot"][0]
    if typ == "split":
        entry = {"split": {"model": slot.get("model"),
                           "params": {k: _unwrap_value(v) for k, v in (slot.get("params") or {}).items()}}}
        split_type = flowparams.SPLIT_MODEL_TO_TYPE.get(slot.get("model"))
        if split_type is not None:
            entry["split"]["type"] = split_type
    elif typ == "join":
        entry = {"join": {"model": slot.get("model"),
                          "params": {k: _unwrap_value(v) for k, v in (slot.get("params") or {}).items()}}}
    else:
        entry = _block_entry(bnn, library, irs)
        if entry is None:
            # Unknown/unmodeled model the library can't resolve: capture the
            # bNN verbatim as a structural slot rather than aborting the whole
            # projection (mirrors the skip-on-KeyError policy elsewhere).
            return StructuralEntry(raw=copy.deepcopy(bnn), lane=lane, pos=pos)
    entry["lane"] = lane
    entry["pos"] = pos
    return entry


def _reconstruct_path_blocks(path_dict, library, irs):
    """Ordered spec ``blocks`` list for one .hsp path.

    - Main input b00 → dropped here (drives the `input` field).
    - Endpoints (other than b00) and orphaned split/join → StructuralEntry
      (verbatim); library is never consulted for them.
    - Balanced split/join → semantic Split/Join with branch reconstruction.
    - User blocks → BlockEntry.
    """
    def all_bnn():
        return [k for k in path_dict
                if isinstance(k, str) and k.startswith("b") and k[1:].isdigit()
                and isinstance(path_dict[k], dict) and path_dict[k].get("slot")]

    def structural_entry(k):
        bnn = path_dict[k]
        num = int(k[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
        return StructuralEntry(raw=copy.deepcopy(bnn), lane=lane, pos=pos)

    keys = all_bnn()
    structural_keys = {k for k in keys if _is_structural_slot(path_dict, k, path_dict[k])}
    # user_keys: real blocks + balanced split/join (b00 excluded as an endpoint,
    # structural keys excluded, but semantic split/join stay in).
    user_keys = [k for k in keys if k != "b00" and k not in structural_keys]
    lane0 = sorted((k for k in user_keys if int(k[1:]) < 14), key=lambda k: int(k[1:]))
    lane1 = sorted((k for k in user_keys if int(k[1:]) >= 14), key=lambda k: int(k[1:]))

    def branch_span(bnn):
        b0, b1 = bnn.get("branch"), path_dict.get(bnn.get("endpoint"), {}).get("branch")
        if not b0 or not b1:
            return []
        lo, hi = sorted((int(b0[1:]), int(b1[1:])))
        return [k for k in lane1 if lo <= int(k[1:]) <= hi]

    out = []
    for k in lane0:
        bnn = path_dict[k]
        out.append(_entry_for(k, bnn, library, irs))
        if bnn.get("type") == "split":
            for bk in branch_span(bnn):
                out.append(_entry_for(bk, path_dict[bk], library, irs))
    claimed = {e_key for k in lane0 if path_dict[k].get("type") == "split"
               for e_key in branch_span(path_dict[k])}
    for bk in lane1:
        if bk not in claimed:
            out.append(_entry_for(bk, path_dict[bk], library, irs))
    # Structural slots (endpoints + orphaned split/join), in key order.
    for k in sorted(structural_keys, key=lambda k: int(k[1:])):
        out.append(structural_entry(k))
    return out


def _block_entry(bnn: dict, library: Library, irs: IrMapping | None) -> dict[str, Any] | None:
    """One bNN dict → a spec block entry (block name + non-default params).

    The block reference is the display_name when it uniquely resolves back to
    this block; otherwise the model_id is used so the spec regenerates without
    ambiguity.

    Returns None if the library can't resolve the slot's model id (an
    unknown/unmodeled block) so the caller can capture it verbatim rather than
    aborting the whole projection.
    """
    slot = bnn["slot"][0]
    model = _translate_model_id(slot.get("model", ""))
    block = _try_load_block(library, model)
    if block is None:
        return None
    name = block.display_name
    try:
        if library.find_block(name).model_id != block.model_id:
            name = block.model_id
    except (KeyError, LookupError):
        name = block.model_id
    entry: dict[str, Any] = {"block": name}

    params: dict[str, Any] = {}
    for param_name, wrapped in (slot.get("params") or {}).items():
        value = _unwrap_value(wrapped)
        default = block.exemplar.get(param_name)
        # Coerce the exemplar default to the same type before comparing, so a
        # float-vs-int mismatch doesn't spuriously register as an override.
        if default is not None:
            default = _coerce_param_value(block, param_name, default)
        coerced = _coerce_param_value(block, param_name, value)
        if default is None or coerced != default:
            params[param_name] = coerced
    if params:
        entry["params"] = params

    # Base bypass lives at the bNN level (the device reads it there); the slot
    # level is inert (~always True). See generate._to_hsp_bnn.
    base_enabled = _unwrap_value(bnn.get("@enabled", True))
    exemplar_enabled = block.exemplar.get("@enabled", True)
    if base_enabled != exemplar_enabled:
        entry["enabled"] = base_enabled

    if model.startswith(IR_MODEL_PREFIX) and slot.get("irhash"):
        irhash = slot["irhash"]
        basename = None
        if irs is not None and irhash in irs.entries:
            cand = os.path.basename(irs.entries[irhash])
            # Emit the basename ONLY if it maps back to exactly one registered
            # wav; otherwise the basename is ambiguous and regeneration would
            # raise, so emit the unambiguous 32-hex hash instead.
            n = sum(1 for p in irs.entries.values() if os.path.basename(p) == cand)
            if n == 1:
                basename = cand
        entry["ir"] = basename if basename is not None else irhash
    elif model.startswith(IR_MODEL_PREFIX):
        # IR slot with no irhash at all (device slot with no IR loaded).
        # Mark it explicitly so generate doesn't raise "IR block requires
        # an `ir` field" for a preset that never had one.
        entry["no_ir"] = True

    # Verbatim non-modeled bNN state generate would otherwise drop: the harness
    # dict (present on every real block, non-deterministic — Trails/dual/
    # ControlSource) and any extra slots (slot[1:], i.e. a dual cab).
    raw: dict[str, Any] = {}
    harness = bnn.get("harness")
    if isinstance(harness, dict):
        harness_copy = copy.deepcopy(harness)
        # Lift the author-facing Trails (delay/reverb/FX-loop spillover) out of
        # the verbatim harness into a clean top-level `trails` field, so it is
        # a single source of truth that generate re-injects. Gate symmetric
        # with generate's trails guard (`flowparams.trails_capable`): a block
        # that could not be regenerated with a `trails` field never gets one,
        # and its Trails (if any) stays verbatim inside raw.harness.
        if flowparams.trails_capable(block.category, block.model_id):
            hparams = harness_copy.get("params")
            trails_wrapped = hparams.get("Trails") if isinstance(hparams, dict) else None
            if isinstance(trails_wrapped, dict) and "value" in trails_wrapped:
                entry["trails"] = bool(trails_wrapped["value"])
                del hparams["Trails"]
        raw["harness"] = harness_copy
    slots = bnn.get("slot") or []
    if len(slots) > 1:
        raw["slots"] = copy.deepcopy(slots[1:])
    if raw:
        entry["raw"] = raw

    return entry


def view(body: dict, library: Library, *, irs: IrMapping | None = None) -> dict[str, Any]:
    """Project a parsed Stadium ``.hsp`` body into a readable recipe-shape
    dict: ``name``, ``paths[*].blocks``, and (when present) ``snapshots``,
    ``footswitches``, ``expression``.

    Read-only: ``body`` is an already-parsed dict (from ``hsp.read_hsp``);
    this function never touches the filesystem and never writes a sidecar.
    """
    if irs is None:
        irs = IrMapping.load()
    device_id = _device_id(body)
    flow = (body.get("preset") or {}).get("flow") or []

    paths: list[dict[str, Any]] = []
    for path_dict in flow:
        if not isinstance(path_dict, dict):
            continue
        blocks = [
            {"structural": b.raw, "lane": b.lane, "pos": b.pos} if isinstance(b, StructuralEntry) else b
            for b in _reconstruct_path_blocks(path_dict, library, irs)
        ]
        path_entry: dict[str, Any] = {"blocks": blocks}
        inp = _lift_input(path_dict, device_id, body)
        if inp is not None:
            path_entry["input"] = inp
        out_lift = _lift_output(path_dict)
        if out_lift is not None:
            path_entry["output"] = out_lift
        paths.append(path_entry)

    meta = body.get("meta") or {}
    spec: dict[str, Any] = {"name": meta.get("name") or "Untitled", "paths": paths}
    if meta.get("author"):
        spec["author"] = meta["author"]

    idx = _name_index(flow, library)

    _warn_unrepresentable_enables(body)

    snaps = _recover_snapshots(body, library, idx)
    if snaps:
        spec["snapshots"] = snaps

    # Un-tabled / out-of-v1-scope controllers are collected here (kept, labeled)
    # rather than silently dropped. This key lives SEPARATE from footswitches /
    # expression on purpose: parse_spec reads only known keys via .get() and
    # ignores it, so decoding a preset with unmodeled controls stays round-trip
    # safe (nothing is re-authored from `unknown_controllers`).
    unknowns: list[dict[str, Any]] = []

    fs = _recover_footswitches(body, library, device_id, idx, unknowns)

    # _recover_expression also appends footswitch-param TOGGLE entries to `fs`.
    exp = _recover_expression(body, library, device_id, idx, unknowns, fs)
    if fs:
        _attach_scribble_strips(body, device_id, fs)
        spec["footswitches"] = fs
    if exp:
        spec["expression"] = exp

    midi = _recover_midi(body, library, idx)
    if midi:
        spec["midi"] = midi

    commands = _recover_commands(body, device_id, fs, unknowns)
    if commands:
        spec["commands"] = commands

    if unknowns:
        spec["unknown_controllers"] = unknowns

    return spec
