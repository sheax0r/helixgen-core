#!/usr/bin/env python3
"""Fill a helixgen block library with every user-placeable model it lacks,
generated from the vendored device defs (``src/helixgen/device/_defs_data.json``,
built by ``tools/build_device_defs.py`` from the app's modeldefs).

The library was historically built only from real ``.hsp`` exports (``ingest``),
so a model nobody had exported was simply missing. But the modeldefs already
carry, for EVERY model, the full param set with each param's type and raw
default — the same param names, value types and units a ``.hsp`` stores
(verified against all 330 sighted library blocks: identical param sets, 100%
type agreement). So an entry is generated exactly the way ``ingest`` would have
built it from an export of a freshly-placed block: a synthesized raw block
(``@model``/``@type``/``@enabled``/``@position``/``@path`` + every param at its
default, keys sorted like a real export) fed through ``ingest.block_from_raw``.

Which models: every model in the app's model catalog (the on-device pick-list),
plus each catalog Mono model's Stereo partner (``...Mono...`` -> ``...Stereo...``
when that model exists in defs — the catalog lists only the Mono half; the
app's UIDefs ``stereo_model`` pairing agrees with this rule for every model).
The model-id string IS the device's own string, which is what Stadium ``.hsp``
exports carry (327 of 330 sighted blocks match it exactly; the 3 that don't are
HX-era ``.hlx`` spellings).

Skipped: I/O / split / merge / looper / FX-loop endpoints (chassis, not library
blocks), the IR block (needs an ``irhash``), and models with capture payload
params (``Weights``: the FlexNet captures) whose defs default is a placeholder,
not a usable value. Existing entries are NEVER overwritten — a sighted block
keeps its observed exemplar.

Usage::

    python3 tools/build_library_from_defs.py [--library PATH]

``--library`` defaults to ``$HELIXGEN_LIBRARY`` / ``~/.helixgen/library``. The
source of truth for the shipped library is the plugin repo's ``data/library``.
"""
from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from helixgen.device import defs as device_defs  # noqa: E402
from helixgen.device import modelmap  # noqa: E402
from helixgen.ingest import block_from_raw, infer_category  # noqa: E402
from helixgen.ir import IR_MODEL_PREFIX  # noqa: E402
from helixgen.library import Library, default_library_path  # noqa: E402

# Catalog category -> library category, used only when the model-id prefix
# (``ingest.infer_category``, what an export ingest would pick) says nothing.
_CATALOG_CATEGORY = {
    "Amp": "amp", "Preamp": "amp", "Cab": "cab", "Delay": "delay",
    "Distortion": "drive", "Dynamics": "dynamics", "EQ": "eq",
    "Modulation": "modulation", "Pitch/Synth": "pitch", "Reverb": "reverb",
    "Volume/Pan": "volume", "Wah/Filter": "filter",
}
_SKIP_CATALOG = {"Input", "Output", "Split", "Merge", "Looper", "FX Loop"}
_SKIP_PARAMS = {"Weights"}  # capture payload; defs default is a placeholder
_TYPES = {"f": float, "i": int, "b": bool}


def _stereo_partner(model: str, models: dict) -> str | None:
    head, sep, tail = model.rpartition("Mono")
    partner = f"{head}Stereo{tail}" if sep else None
    return partner if partner in models else None


def _raw_block(model: str, category: str, params: dict) -> dict:
    raw = {
        "@model": model,
        "@type": {"amp": "amp", "cab": "cab"}.get(category, "fx"),
        "@enabled": True,
        "@position": 0,
        "@path": 0,
    }
    for name in sorted(params):
        meta = params[name]
        raw[name] = _TYPES[meta["type"]](meta.get("def", 0))
    return raw


def fill_library(library: Library) -> tuple[list[str], dict[str, str]]:
    """Write every missing catalog model into ``library``.

    Returns ``(written model ids, {skipped model id: reason})``.
    """
    data = device_defs.load_defs()
    models, catalog = data["models"], data["catalog"]
    # A sighted block may spell a model differently (HX-era ``HD2_DrvScream808``
    # IS ``HD2_DistScream808Mono``): coverage is by DEVICE model, not string.
    have = set()
    for b in library.list_blocks():
        have.add(b.model_id)
        dev = modelmap.device_model_id(b.model_id)
        if dev is not None:
            have.add(device_defs.model_name_for(dev))
    source = {
        "preset": f"device-defs:{data['source']['modeldefs']}",
        "firmware": data["source"]["modeldefs_header"].get("ver", ""),
        "date": datetime.date.today().isoformat(),
    }

    wanted: dict[str, str] = {}  # model -> library category
    skipped: dict[str, str] = {}
    for model, entry in sorted(catalog.items()):
        cat_name = entry.get("category_name")
        if cat_name in _SKIP_CATALOG or model not in models:
            continue
        if model.startswith(IR_MODEL_PREFIX):
            skipped[model] = "IR block needs an irhash"
            continue
        params = device_defs.model_params_for(models[model])
        if _SKIP_PARAMS & set(params):
            skipped[model] = "capture payload params (Weights)"
            continue
        category = infer_category(model)
        if category == "uncategorized":
            category = _CATALOG_CATEGORY.get(cat_name, "uncategorized")
        if category == "uncategorized":
            skipped[model] = f"no library category for catalog {cat_name!r}"
            continue
        wanted[model] = category
        partner = _stereo_partner(model, models)
        if partner:
            wanted.setdefault(partner, category)

    written: list[str] = []
    for model, category in sorted(wanted.items()):
        if model in have:
            continue
        params = device_defs.model_params_for(models[model])
        raw = _raw_block(model, category, params)
        raw["@category"] = category
        block = block_from_raw(raw, source)
        del block.exemplar["@category"]
        library.save_block(block)
        written.append(model)
    return written, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--library", default=None)
    args = ap.parse_args(argv)
    root = Path(args.library) if args.library else default_library_path()
    written, skipped = fill_library(Library(root))
    print(f"wrote {len(written)} block(s) into {root}")
    for model, why in sorted(skipped.items()):
        print(f"  skipped {model}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
