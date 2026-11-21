"""Concrete tone-affecting change entries, registered onto changes.REGISTRY.

Each entry is a content detector + idempotent in-place migration for one
shipped engine fix. Importing this module registers them all (via
:func:`helixgen.changes.register`). Entries are added here as fixes land;
`library upgrade` and `helixgen changes` pick them up automatically.
"""
from __future__ import annotations

import re
from typing import Any, List, Tuple

from helixgen.changes import ChangeEntry, Finding, register
from helixgen.generate import HSP_SNAPSHOT_SLOTS

_BLOCK_KEY = re.compile(r"b\d+$")


# ---------------------------------------------------------------------------
# fs-bypass-snapshot-assign (core #143, engine 0.84.0)
# ---------------------------------------------------------------------------
def _stale_bypass_coords(body: dict) -> List[Tuple[int, str]]:
    """(flow_index, block_key) for every block whose ``@enabled`` carries a
    footswitch/EXP bypass controller but no per-snapshot ``snapshots`` array.

    A controller on ``@enabled`` (as opposed to on a param's value wrapper) is
    exactly a bypass assignment; a dense/varying ``snapshots`` array means the
    fix already applied (or the tone authored a varying bypass) — left alone.
    """
    coords: List[Tuple[int, str]] = []
    flow = ((body.get("preset") or {}).get("flow")) or []
    for fi, path_dict in enumerate(flow):
        if not isinstance(path_dict, dict):
            continue
        for key, wrapper in path_dict.items():
            if not _BLOCK_KEY.fullmatch(key) or not isinstance(wrapper, dict):
                continue
            enabled = wrapper.get("@enabled")
            if not isinstance(enabled, dict):
                continue
            if enabled.get("controller") is not None and "snapshots" not in enabled:
                coords.append((fi, key))
    return coords


def _fs_bypass_detect(ctx: Any) -> Finding:
    coords = _stale_bypass_coords(ctx.body)
    if not coords:
        return Finding(False, [], "unchanged")
    return Finding(True, [k for _, k in coords], "would-assign",
                   f"{len(coords)} bypass block(s)")


def _fs_bypass_apply(ctx: Any) -> Finding:
    coords = _stale_bypass_coords(ctx.body)
    for fi, key in coords:
        enabled = ctx.body["preset"]["flow"][fi][key]["@enabled"]
        base = enabled.get("value")
        enabled["snapshots"] = [True if base is None else bool(base)] * HSP_SNAPSHOT_SLOTS
    if not coords:
        return Finding(False, [], "unchanged")
    return Finding(True, [k for _, k in coords], "assigned",
                   f"{len(coords)} bypass block(s)")


# ---------------------------------------------------------------------------
# factory-info-clear (core #117/#132, engine 0.65.0)
# ---------------------------------------------------------------------------
def _factory_info_detect(ctx: Any) -> Finding:
    donors = ctx.resources.get("factory_donor_infos") or set()
    info = (ctx.body.get("meta") or {}).get("info")
    if isinstance(info, str) and info and info in donors:
        return Finding(True, [], "would-clear", "factory meta.info leakage")
    return Finding(False, [], "unchanged")


def _factory_info_apply(ctx: Any) -> Finding:
    if not _factory_info_detect(ctx).affected:
        return Finding(False, [], "unchanged")
    ctx.body["meta"]["info"] = ""
    return Finding(True, [], "cleared", "factory meta.info leakage")


register(ChangeEntry(
    id="factory-info-clear",
    landed_in="0.65.0",
    kind="cosmetic",
    summary="clear leaked factory rig notes from meta.info",
    research=(
        "Tones generated before hgc-a8p copied the donor chassis wholesale and "
        "kept its factory rig notes in meta.info. The donor string is recorded "
        "verbatim in the library's chassis.json; a tone whose meta.info matches "
        "it exactly is factory leakage and is cleared. A note the user set "
        "themselves never matches, so it is left alone."),
    detect=_factory_info_detect,
    apply=_factory_info_apply,
))


register(ChangeEntry(
    id="fs-bypass-snapshot-assign",
    landed_in="0.84.0",
    kind="bugfix",
    summary="footswitch/EXP-bypassed blocks are snapshot-assigned "
            "(fixes path-dependent snapshot recall)",
    research=(
        "Pre-0.84 wire_footswitch attached a bypass controller to a block's "
        "@enabled without a per-snapshot array, so a stomp survived snapshot "
        "switches and recall was path-dependent (Soft->Heavy->Soft != "
        "load->Soft). The fix pins a dense [bool(value)]*8 array; a missing "
        "value is ENABLED. An authored varying array is left untouched."),
    detect=_fs_bypass_detect,
    apply=_fs_bypass_apply,
))
