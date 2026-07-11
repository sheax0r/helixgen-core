#!/usr/bin/env python3
"""Regenerate ``src/helixgen/device/_defs_data.json`` from the Helix Stadium
editor app bundle.

The Line 6 "Helix Stadium Debug.app" ships three resource files that, together,
describe every model the device exposes and the numeric ids the *network*
protocol addresses them by:

- ``P35ModelCatalog.json`` — the on-device pick-list: categories, display short
  names, and (a subset of) model-id strings. No numeric ids.
- ``modeldefs/p35md-1_3_0_0.bin`` — the authoritative model table. A short JSON
  header, a NUL byte, an 8-byte magic ``ldompgsm`` (reverse of ``msgpmodl``),
  then a msgpack map keyed by model-id string. Each value carries the numeric
  ``id`` plus a ``params`` map of ParamName -> {id, type, min, max, def}.
- ``commanddefs/P35EditCommandDefs.json`` — TWO concatenated JSON objects (a
  header then a defs object); a small name->id table of editor command groups
  (their ``commands`` bodies ship empty in 1.3.0).

This script decodes all three and emits a compact, deterministic (sorted-keys)
JSON asset that helixgen vendors so it never needs the app at runtime. msgpack
is used ONLY here (build time); the runtime loader in ``defs.py`` is pure stdlib.

Usage::

    .venv-poc/bin/python tools/build_device_defs.py [APP_RESOURCES_DIR]

``APP_RESOURCES_DIR`` defaults to the standard install location (below) or the
``HELIX_APP_RESOURCES`` env var.
"""
from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

DEFAULT_RESOURCES = "/Users/michael.shea/Helix Stadium Debug.app/Contents/Resources"

CATALOG_NAME = "P35ModelCatalog.json"
MODELDEFS_NAME = "modeldefs/p35md-1_3_0_0.bin"
COMMANDDEFS_NAME = "commanddefs/P35EditCommandDefs.json"

# 8-byte magic that precedes the msgpack body inside the modeldefs .bin.
MODELDEFS_MAGIC = b"ldompgsm"

# Written relative to this file: ../src/helixgen/device/_defs_data.json
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "src" / "helixgen" / "device" / "_defs_data.json"


def _read_two_json_objects(text: str):
    """Decode a stream of one-or-more back-to-back JSON objects."""
    dec = json.JSONDecoder()
    out = []
    idx = 0
    n = len(text)
    while idx < n:
        # skip inter-object whitespace / stray NULs
        while idx < n and text[idx] in " \t\r\n\x00":
            idx += 1
        if idx >= n:
            break
        obj, end = dec.raw_decode(text, idx)
        out.append(obj)
        idx = end
    return out


def parse_catalog(path: Path) -> dict:
    """Return {model_str: {category_id, category, short_name}} from the catalog."""
    cat = json.loads(path.read_text())
    models: dict[str, dict] = {}
    for category in cat.get("categories", []):
        cid = category.get("id")
        cname = category.get("name")
        cshort = category.get("shortName")
        for model in category.get("models", []):
            if not model:
                continue
            models[model] = {
                "category_id": cid,
                "category_name": cname,
                "category_short": cshort,
            }
    return models


def parse_modeldefs(path: Path):
    """Return (header_dict, {model_str: model_def_dict}) from the msgpack .bin."""
    import msgpack  # build-time only

    raw = path.read_bytes()
    header, header_end = json.JSONDecoder().raw_decode(raw.decode("latin-1"))
    magic_at = raw.find(MODELDEFS_MAGIC, header_end)
    if magic_at < 0:
        raise ValueError(f"modeldefs magic {MODELDEFS_MAGIC!r} not found")
    body = raw[magic_at + len(MODELDEFS_MAGIC):]
    models = msgpack.unpackb(body, raw=False, strict_map_key=False)
    if not isinstance(models, dict):
        raise ValueError("modeldefs body is not a msgpack map")
    return header, models


def parse_commanddefs(path: Path):
    """Return (header_dict, {command_name: id}) from the two-object JSON file."""
    text = path.read_text(encoding="latin-1")
    objs = _read_two_json_objects(text)
    header = objs[0] if objs else {}
    defs = objs[1] if len(objs) > 1 else {}
    commands = {
        name: entry.get("id")
        for name, entry in defs.items()
        if isinstance(entry, dict)
    }
    return header, commands


def build(resources: Path) -> dict:
    catalog_path = resources / CATALOG_NAME
    modeldefs_path = resources / MODELDEFS_NAME
    commanddefs_path = resources / COMMANDDEFS_NAME

    catalog = parse_catalog(catalog_path)
    md_header, models = parse_modeldefs(modeldefs_path)
    cmd_header, commands = parse_commanddefs(commanddefs_path)

    name_to_id: dict[str, int] = {}
    id_to_name: dict[str, str] = {}
    model_params: dict[str, dict] = {}
    model_categories: dict[str, str] = {}

    for model_str, mdef in models.items():
        if not isinstance(mdef, dict):
            continue
        mid = mdef.get("id")
        if not isinstance(mid, int):
            continue
        name_to_id[model_str] = mid
        id_to_name[str(mid)] = model_str
        category = mdef.get("category")
        if category is not None:
            model_categories[model_str] = category
        params_out: dict[str, dict] = {}
        for pname, pdef in (mdef.get("params") or {}).items():
            if not isinstance(pdef, dict):
                continue
            entry = {"id": pdef.get("id"), "type": pdef.get("type")}
            for k in ("min", "max", "def"):
                if k in pdef:
                    entry[k] = pdef[k]
            params_out[pname] = entry
        model_params[str(mid)] = params_out

    return {
        "source": {
            "catalog": CATALOG_NAME,
            "modeldefs": Path(MODELDEFS_NAME).name,
            "commanddefs": COMMANDDEFS_NAME,
            "modeldefs_header": md_header,
            "commanddefs_header": cmd_header,
        },
        "models": name_to_id,
        "model_names": id_to_name,
        "model_categories": model_categories,
        "catalog": catalog,
        "commands": commands,
        "model_params": model_params,
    }


def main(argv: list[str]) -> int:
    resources = Path(
        argv[1]
        if len(argv) > 1
        else os.environ.get("HELIX_APP_RESOURCES", DEFAULT_RESOURCES)
    )
    if not resources.is_dir():
        print(f"error: resources dir not found: {resources}", file=sys.stderr)
        return 2

    data = build(resources)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        fh.write("\n")

    n_models = len(data["models"])
    n_params = sum(len(p) for p in data["model_params"].values())
    print(
        f"wrote {OUT_PATH.relative_to(REPO_ROOT)}: "
        f"{n_models} models, {n_params} params, "
        f"{len(data['catalog'])} cataloged, {len(data['commands'])} commands"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
