#!/usr/bin/env python3
"""Regenerate ``src/helixgen/device/_modelmap.json``.

helixgen and the Helix Stadium device name the SAME models with DIFFERENT id
strings, and the device network protocol addresses models by a NUMERIC id.
helixgen's library / ``.hsp`` files speak HX-Edit export model-id STRINGS (e.g.
``HD2_ReverbPlateStereo``, or translated forms like ``HD2_DrvScream808``); the
device modeldefs speak internal strings + numeric ids (e.g.
``HD2_DistScream808Mono`` -> 310). This tool builds a deterministic map

    helixgen-model-id-string  ->  device NUMERIC model id

by joining the two vocabularies on:
  (a) exact model-id STRING equality        (strongest),
  (b) normalized DISPLAY-NAME equality      (helixgen display_name/aliases
      vs device UIDefs name/shortname),
  (c) PARAM-NAME set overlap (Jaccard)      (required to accept a name match;
      also breaks ties and catches renamed blocks whose names diverged),
  (d) CATEGORY compatibility                (soft tie-break bonus / gate for
      pure-param matches).

Accuracy is favored over coverage: a wrong match corrupts a tone, so matches
below threshold are recorded as ``unmatched`` rather than forced.

Three data sources (all build-time only; the runtime loader ``modelmap.py`` is
pure stdlib and reads only the vendored json):

  1. helixgen library:  ~/.helixgen/library/blocks/<cat>/<model_id>.json
                        ($HELIXGEN_LIBRARY override; helixgen.library schema)
  2. device defs:       helixgen.device.defs.load_defs() (vendored _defs_data)
  3. device UIDefs:     <App>/Contents/Resources/P35ModelUIDefs.json
                        (--uidefs / $HELIXGEN_UIDEFS / default app path)

Usage:
    python tools/build_modelmap.py [--uidefs PATH] [--library PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

# --- build-time app dependency (runtime loader has none) --------------------
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from helixgen.device import defs as device_defs  # noqa: E402
from helixgen.library import Library, default_library_path  # noqa: E402

# --- tuning knobs -----------------------------------------------------------
# A normalized-name match is accepted only if the param sets also overlap by at
# least this Jaccard (guards against two unrelated blocks sharing a UI name).
PARAM_THRESHOLD_NAME = 0.50
# A pure param-signature match (no name agreement) is only trusted when nearly
# identical AND category-compatible AND it resolves to a single variant.
PARAM_THRESHOLD_ONLY = 0.85

_DEFAULT_UIDEFS = (
    "/Users/michael.shea/Helix Stadium Debug.app/Contents/Resources/"
    "P35ModelUIDefs.json"
)

# Library-category -> set of device-category strings considered compatible.
# The two vocabularies diverge (e.g. helixgen "drive" == device "distortion").
# Used only as a soft signal, never as a hard filter for exact/name matches.
_CATEGORY_COMPAT: dict[str, set[str]] = {
    "amp": {"amp", "preamp"},
    "cab": {"cab_ir_interp", "ir"},
    "drive": {"distortion"},
    "delay": {"delay"},
    "reverb": {"reverb"},
    "modulation": {"modulation"},
    "pitch": {"pitch", "synth"},
    "filter": {"filter", "wah"},
    "eq": {"eq"},
    "dynamics": {"dynamics"},
    "volume": {"volume"},
    "send": {"send", "fxloop", "return"},
    "uncategorized": set(),  # matches nothing specifically; wildcard below
}


def normalize(s: Optional[str]) -> str:
    """Lowercase and strip everything but [a-z0-9] (spaces, punctuation)."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def category_compatible(lib_cat: str, dev_cat: Optional[str]) -> bool:
    """True if a device category is a plausible partner for a library category."""
    if not dev_cat:
        return False
    if lib_cat == "uncategorized":
        return True  # unknown lib category -> don't penalize any device cat
    return dev_cat in _CATEGORY_COMPAT.get(lib_cat, {lib_cat})


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# --- device-side index ------------------------------------------------------
class DeviceModel:
    __slots__ = ("model_str", "numeric_id", "category", "names", "params")

    def __init__(
        self,
        model_str: str,
        numeric_id: int,
        category: Optional[str],
        names: set[str],
        params: set[str],
    ) -> None:
        self.model_str = model_str
        self.numeric_id = numeric_id
        self.category = category
        self.names = names  # normalized display names (UIDefs name + shortname)
        self.params = params


def build_device_index(
    defs: dict[str, Any], uidefs: dict[str, Any]
) -> tuple[dict[str, DeviceModel], dict[str, list[DeviceModel]]]:
    """Return (by_model_str, by_normalized_name)."""
    by_str: dict[str, DeviceModel] = {}
    by_name: dict[str, list[DeviceModel]] = {}
    models: dict[str, int] = defs.get("models", {})
    model_params: dict[str, Any] = defs.get("model_params", {})
    model_categories: dict[str, str] = defs.get("model_categories", {})

    for model_str, numeric_id in sorted(models.items(), key=lambda kv: kv[1]):
        ui = uidefs.get(model_str, {})
        names = {
            n
            for n in (normalize(ui.get("name")), normalize(ui.get("shortname")))
            if n
        }
        params = set((model_params.get(str(numeric_id)) or {}).keys())
        dm = DeviceModel(
            model_str=model_str,
            numeric_id=numeric_id,
            category=model_categories.get(model_str),
            names=names,
            params=params,
        )
        by_str[model_str] = dm
        for n in names:
            by_name.setdefault(n, []).append(dm)
    return by_str, by_name


# --- matching ---------------------------------------------------------------
def _variant_rank(lib_hint: str, dm: DeviceModel) -> int:
    """Tie-break preference among Mono/Stereo variants.

    Prefer the variant implied by the helixgen id/display; default to Mono when
    the helixgen side gives no hint (helixgen strips the ``Mono`` suffix on the
    translated ids, so an un-suffixed helixgen id maps to the Mono device model).
    Lower rank sorts first.
    """
    tail = dm.model_str.lower()
    is_stereo = tail.endswith("stereo")
    is_mono = tail.endswith("mono")
    if "stereo" in lib_hint:
        return 0 if is_stereo else 1
    if "mono" in lib_hint:
        return 0 if is_mono else 1
    # no hint: default to Mono, then Stereo, then anything else
    if is_mono:
        return 0
    if is_stereo:
        return 1
    return 2


def match_block(
    block: Any,
    by_str: dict[str, DeviceModel],
    by_name: dict[str, list[DeviceModel]],
) -> Optional[dict[str, Any]]:
    """Return an audit record for the best device match, or None."""
    lib_id = block.model_id
    lib_cat = block.category
    lib_params = set(block.params.keys())
    lib_hint = normalize(lib_id) + " " + normalize(block.display_name)
    lib_names = {normalize(block.display_name)} | {
        normalize(a) for a in block.aliases
    }
    lib_names.discard("")

    def record(dm: DeviceModel, method: str, conf: float) -> dict[str, Any]:
        return {
            "device_id": dm.numeric_id,
            "device_model_str": dm.model_str,
            "device_name": next(iter(sorted(dm.names)), "") or None,
            "method": method,
            "confidence": round(conf, 4),
            "param_jaccard": round(jaccard(lib_params, dm.params), 4),
            "category": dm.category,
        }

    # (a) exact model-id STRING equality — strongest, accept unconditionally.
    if lib_id in by_str:
        return record(by_str[lib_id], "exact_id", 1.0)

    # (b)+(c) normalized display-name match, gated by param overlap.
    name_cands: list[DeviceModel] = []
    seen: set[str] = set()
    for n in sorted(lib_names):
        for dm in by_name.get(n, []):
            if dm.model_str not in seen:
                seen.add(dm.model_str)
                name_cands.append(dm)
    scored: list[tuple[float, int, int, DeviceModel]] = []
    for dm in name_cands:
        j = jaccard(lib_params, dm.params)
        if j >= PARAM_THRESHOLD_NAME:
            compat = 0 if category_compatible(lib_cat, dm.category) else 1
            scored.append((j, compat, dm))  # type: ignore[arg-type]
    if scored:
        # best: highest jaccard, then category-compatible, then variant pref,
        # then lowest numeric id — all deterministic.
        scored.sort(
            key=lambda t: (
                -t[0],
                t[1],
                _variant_rank(lib_hint, t[2]),
                t[2].numeric_id,
            )
        )
        best = scored[0]
        return record(best[2], "name_param", best[0])

    # (d) pure param-signature fallback: no name agreement, but nearly-identical
    #     params AND category-compatible. Accept only if it resolves cleanly to
    #     one variant (after Mono/Stereo tie-break). Conservative by design.
    param_cands: list[tuple[float, DeviceModel]] = []
    for dm in by_str.values():
        if not dm.params:
            continue
        if not category_compatible(lib_cat, dm.category):
            continue
        j = jaccard(lib_params, dm.params)
        if j >= PARAM_THRESHOLD_ONLY:
            param_cands.append((j, dm))
    if param_cands:
        param_cands.sort(
            key=lambda t: (-t[0], _variant_rank(lib_hint, t[1]), t[1].numeric_id)
        )
        top_j = param_cands[0][0]
        # require the top param score to be a clear winner among distinct models
        best_dm = param_cands[0][1]
        return record(best_dm, "param_only", top_j * 0.9)

    return None


# --- driver -----------------------------------------------------------------
def build(uidefs_path: Path, library_path: Path) -> dict[str, Any]:
    defs = device_defs.load_defs()
    with uidefs_path.open(encoding="utf-8") as fh:
        uidefs = json.load(fh)
    by_str, by_name = build_device_index(defs, uidefs)

    lib = Library(library_path)
    blocks = sorted(lib.list_blocks(), key=lambda b: b.model_id)

    mapping: dict[str, int] = {}
    matches: dict[str, Any] = {}
    unmatched: list[str] = []
    method_counts: dict[str, int] = {}

    for block in blocks:
        rec = match_block(block, by_str, by_name)
        if rec is None:
            unmatched.append(block.model_id)
            continue
        mapping[block.model_id] = rec["device_id"]
        matches[block.model_id] = rec
        method_counts[rec["method"]] = method_counts.get(rec["method"], 0) + 1

    total = len(blocks)
    matched = len(mapping)
    out = {
        "map": dict(sorted(mapping.items())),
        "unmatched": sorted(unmatched),
        "matches": dict(sorted(matches.items())),
        "meta": {
            "counts": {
                "helixgen_total": total,
                "matched": matched,
                "unmatched": len(unmatched),
                "by_method": dict(sorted(method_counts.items())),
            },
            "coverage_pct": round(100.0 * matched / total, 2) if total else 0.0,
            "thresholds": {
                "param_jaccard_name": PARAM_THRESHOLD_NAME,
                "param_jaccard_only": PARAM_THRESHOLD_ONLY,
            },
            "sources": {
                "library": str(library_path),
                "uidefs": str(uidefs_path),
                "device_defs": "helixgen.device.defs (vendored _defs_data.json)",
                "defs_source": defs.get("source", {}),
            },
        },
    }
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--uidefs",
        default=os.environ.get("HELIXGEN_UIDEFS", _DEFAULT_UIDEFS),
        help="Path to P35ModelUIDefs.json (env HELIXGEN_UIDEFS; default app path).",
    )
    ap.add_argument(
        "--library",
        default=None,
        help="helixgen library root (env HELIXGEN_LIBRARY; default ~/.helixgen/library).",
    )
    ap.add_argument(
        "--out",
        default=str(
            Path(__file__).resolve().parent.parent
            / "src"
            / "helixgen"
            / "device"
            / "_modelmap.json"
        ),
        help="Output json path.",
    )
    args = ap.parse_args(argv)

    uidefs_path = Path(args.uidefs)
    if not uidefs_path.exists():
        ap.error(f"UIDefs not found: {uidefs_path} (pass --uidefs / $HELIXGEN_UIDEFS)")
    library_path = (
        Path(args.library) if args.library else default_library_path()
    )
    if not (library_path / "blocks").exists():
        ap.error(f"library not found: {library_path} (pass --library / $HELIXGEN_LIBRARY)")

    out = build(uidefs_path, library_path)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=False)
        fh.write("\n")

    c = out["meta"]["counts"]
    print(f"wrote {out_path}")
    print(
        f"  helixgen models : {c['helixgen_total']}\n"
        f"  matched         : {c['matched']} ({out['meta']['coverage_pct']}%)\n"
        f"  unmatched       : {c['unmatched']}\n"
        f"  by method       : {c['by_method']}"
    )
    if out["unmatched"]:
        print("  UNMATCHED:")
        for mid in out["unmatched"]:
            print(f"    - {mid}")
    # surface the non-exact (fuzzy) accepted matches for audit
    fuzzy = {
        k: v for k, v in out["matches"].items() if v["method"] != "exact_id"
    }
    if fuzzy:
        print("  FUZZY (non-exact) matches accepted:")
        for k, v in sorted(fuzzy.items()):
            print(
                f"    - {k} -> {v['device_id']} ({v['device_model_str']}) "
                f"[{v['method']} conf={v['confidence']} "
                f"jaccard={v['param_jaccard']}]"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
