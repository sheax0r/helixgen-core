#!/usr/bin/env python3
"""Regenerate ``src/helixgen/device/_defs_data.json`` from the Helix Stadium
editor app bundle.

The Line 6 "Helix Stadium.app" ships three resource files that, together,
describe every model the device exposes and the numeric ids the *network*
protocol addresses them by:

- ``P35ModelCatalog.json`` — the on-device pick-list: categories, display short
  names, and (a subset of) model-id strings. No numeric ids.
- ``modeldefs/p35md-<ver>.bin`` — the authoritative model table (the newest
  version present is used: :func:`find_modeldefs`). A short JSON
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
import re
import struct
import sys
from pathlib import Path

DEFAULT_RESOURCES = "/Applications/Line6/Helix Stadium.app/Contents/Resources"

CATALOG_NAME = "P35ModelCatalog.json"
COMMANDDEFS_NAME = "commanddefs/P35EditCommandDefs.json"

# 8-byte magic that precedes the msgpack body inside the modeldefs .bin.
MODELDEFS_MAGIC = b"ldompgsm"

# Written relative to this file: ../src/helixgen/device/_defs_data.json
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "src" / "helixgen" / "device" / "_defs_data.json"


def _releases(resources: Path) -> list[Path]:
    """Every ``modeldefs/p35md-*.bin``, oldest first (the app keeps each past
    release's table side by side; versions compare numerically)."""
    def version(p: Path) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", p.stem.split("-", 1)[1]))

    return sorted((resources / "modeldefs").glob("p35md-*.bin"), key=version)


def find_modeldefs(resources: Path) -> Path:
    """The highest-versioned ``modeldefs/p35md-*.bin``."""
    found = _releases(resources)
    if not found:
        raise FileNotFoundError(f"no modeldefs/p35md-*.bin under {resources}")
    return found[-1]


def find_previous_modeldefs(resources: Path) -> Path | None:
    """The release before :func:`find_modeldefs`'s, or ``None``."""
    found = _releases(resources)
    return found[-2] if len(found) > 1 else None


def added_params(models: dict, previous: dict) -> dict[str, set[str]]:
    """``{model: {param names}}`` a model GAINED since ``previous`` — by pid, so
    a rename (1.4's ``Pad.1`` -> ``Pad_1``, same pid) is not "new". Models new
    in this release are skipped: there is no older content to stay faithful to.

    The transcoder leaves these out of content that never carried them, so a
    pre-release tone still round-trips byte-exact and an older firmware is
    never sent a pid it does not know (1.4 itself tolerates them missing:
    factory content on fw 1.4 lacks ``CabIdx``).
    """
    out: dict[str, set[str]] = {}
    for name, mdef in models.items():
        old = previous.get(name)
        if not isinstance(mdef, dict) or not isinstance(old, dict):
            continue
        old_pids = {p.get("id") for p in (old.get("params") or {}).values()
                    if isinstance(p, dict)}
        new = {pn for pn, p in (mdef.get("params") or {}).items()
               if isinstance(p, dict) and p.get("id") not in old_pids}
        if new:
            out[name] = new
    return out


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


def carried_added(vendored: dict) -> dict[str, dict[int, str]]:
    """``{model: {pid: release}}`` of the ``added`` marks in an existing asset.

    :func:`added_params` only diffs against the release just before the newest,
    so a regen against a later app (or one without the older bin) would drop
    earlier releases' marks; ``build`` carries these forward instead.
    """
    names = {str(mid): name for name, mid in (vendored.get("models") or {}).items()}
    out: dict[str, dict[int, str]] = {}
    for mid, params in (vendored.get("model_params") or {}).items():
        for meta in params.values():
            if meta.get("added") and mid in names:
                out.setdefault(names[mid], {})[meta.get("id")] = meta["added"]
    return out


def build(resources: Path, vendored: dict | None = None) -> dict:
    catalog_path = resources / CATALOG_NAME
    modeldefs_path = find_modeldefs(resources)
    commanddefs_path = resources / COMMANDDEFS_NAME

    catalog = parse_catalog(catalog_path)
    md_header, models = parse_modeldefs(modeldefs_path)
    previous_path = find_previous_modeldefs(resources)
    added = (added_params(models, parse_modeldefs(previous_path)[1])
             if previous_path else {})
    carried = carried_added(vendored or {})
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
            if pname in added.get(model_str, ()):
                entry["added"] = modeldefs_path.name
            elif pdef.get("id") in carried.get(model_str, {}):
                entry["added"] = carried[model_str][pdef.get("id")]
            params_out[pname] = entry
        model_params[str(mid)] = params_out

    return {
        "source": {
            "catalog": CATALOG_NAME,
            "modeldefs": modeldefs_path.name,
            "previous_modeldefs": previous_path.name if previous_path else None,
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

    vendored = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
    data = build(resources, vendored)
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
