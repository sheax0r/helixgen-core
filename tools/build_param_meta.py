#!/usr/bin/env python3
"""Regenerate ``src/helixgen/device/_param_ui.json`` from the Helix Stadium
editor app bundle.

Sibling of ``tools/build_device_defs.py``, same contract: run it offline
against the app bundle, commit the emitted asset, and helixgen never needs the
app at runtime.

Where ``_defs_data.json`` carries each param's numeric id, type and RAW range
(``{id,type,min,max,def}``), this asset carries what that raw range MEANS —
the display name, the unit, the raw->display scale, and, for discrete params,
the enum labels. Two app resources supply it:

- ``P35ModelUIDefs.json`` — per model, an ordered ``params`` list whose ``id``
  is the ``.hsp``-compatible param key (``LowCut``) and whose ``name`` is the
  editor's display form (``Low Cut``). Each param carries a ``display_tag``
  that is EITHER a control-type name (resolved in ``P35Controls.json``) OR an
  inline list of enum labels (amp ``Channel``, ``Jack``, …).
- ``P35Controls.json`` — the control types: ``format`` / ``formatUnits``
  (which is where the unit hides: ``"%+.1f dB"``, ``"%.0f Hz"``), the
  ``dspToDisplayScale`` (``generic_knob`` = 10, so a stored 0.5 displays as
  5.0), and, for discrete controls, a ``format`` that is a flat list of enum
  labels.

Params the editor does not expose (``IrData``, ``AmpCabPeak*``, ``AmpCabZFir``,
…) simply have no UIDefs entry. That absence is a signal — "not a knob, don't
put it in front of an author" — so this script never invents an entry for them.
The one deliberate exception is ``SyncSelect1``/``SyncSelect2``, which the app
renders with the ``sync_note`` control but does not list in UIDefs.

Usage::

    python3 tools/build_param_meta.py [APP_RESOURCES_DIR]

``APP_RESOURCES_DIR`` defaults to the standard install location below, or to
the ``HELIX_APP_RESOURCES`` env var.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

DEFAULT_RESOURCES = "/Users/michael.shea/Helix Stadium Debug.app/Contents/Resources"

UIDEFS_NAME = "P35ModelUIDefs.json"
CONTROLS_NAME = "P35Controls.json"

# Params the app renders with a control it never lists in UIDefs.
IMPLIED_TAGS = {"SyncSelect1": "sync_note", "SyncSelect2": "sync_note"}

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "src" / "helixgen" / "device" / "_param_ui.json"

# A printf conversion at the START of the string, followed by the unit text:
# "%+.1f dB" -> "dB", "%.0f %%" -> "%", "%.2f \"" -> "in". A string whose
# conversion is NOT leading ("Left %.0f") is a value LABEL, not a unit.
_UNIT_RE = re.compile(r"^%[-+#0-9.]*[dfsu]\s*(\S.*)$")


def _unit_from_format(fmt: Any) -> Optional[str]:
    """Extract a unit suffix from one format string, or ``None``."""
    if not isinstance(fmt, str):
        return None
    m = _UNIT_RE.match(fmt.strip())
    if not m:
        return None
    unit = m.group(1).strip().replace("%%", "%")
    if unit == '"':
        unit = "in"  # cab mic distance, rendered by the app as inches
    return unit or None


def _control_unit(control: dict) -> Optional[str]:
    """First unit the control's format strings mention (in display order)."""
    candidates: list[Any] = [control.get("formatUnits")]
    fmt = control.get("format")
    if isinstance(fmt, list):
        for entry in fmt:
            if isinstance(entry, dict):
                candidates.append(entry.get("formatUnits"))
                candidates.append(entry.get("format"))
    else:
        candidates.append(fmt)
    for cand in candidates:
        unit = _unit_from_format(cand)
        if unit:
            return unit
    return None


def _control_labels(control: dict) -> Optional[list[str]]:
    """Enum labels, when the control's ``format`` is a flat list of strings."""
    fmt = control.get("format")
    if isinstance(fmt, list) and fmt and all(isinstance(x, str) for x in fmt):
        return list(fmt)
    return None


def build_controls(controls: dict) -> dict[str, dict]:
    """Reduce P35Controls to ``{tag: {unit?, scale?, labels?}}`` (dropping empties)."""
    out: dict[str, dict] = {}
    for tag, control in controls.items():
        if not isinstance(control, dict):
            continue
        entry: dict[str, Any] = {}
        unit = _control_unit(control)
        if unit:
            entry["unit"] = unit
        scale = control.get("dspToDisplayScale")
        if isinstance(scale, (int, float)) and scale != 1:
            entry["scale"] = scale
        labels = _control_labels(control)
        if labels:
            entry["labels"] = labels
        if entry:
            out[tag] = entry
    return out


def build_models(uidefs: dict, controls: dict[str, dict]) -> dict[str, dict]:
    """``{model_str: {param_id: {name, tag?|labels?}}}`` from the UIDefs."""
    out: dict[str, dict] = {}
    for model_str, mdef in uidefs.items():
        if not isinstance(mdef, dict):
            continue
        params: dict[str, dict] = {}
        for pdef in mdef.get("params") or []:
            if not isinstance(pdef, dict):
                continue
            pid = pdef.get("id")
            if not isinstance(pid, str):
                continue
            entry: dict[str, Any] = {}
            name = pdef.get("name")
            if isinstance(name, str) and name and name != pid:
                entry["name"] = name
            tag = pdef.get("display_tag")
            if isinstance(tag, str):
                if tag in controls:
                    entry["tag"] = tag
            elif isinstance(tag, list) and all(isinstance(x, str) for x in tag):
                entry["labels"] = list(tag)
            # An empty entry is still kept: a param the editor LISTS is a param
            # an author may set, even when its control carries no unit, scale
            # or labels. Absence from this table is the "internal, don't
            # expose" signal, so never conflate the two.
            params[pid] = entry
        for pid, tag in IMPLIED_TAGS.items():
            if pid not in params and tag in controls:
                params[pid] = {"tag": tag}
        if params:
            out[model_str] = params
    return out


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _app_version(resources: Path) -> Optional[str]:
    """CFBundleShortVersionString of the app the resources came from."""
    plist = resources.parent / "Info.plist"
    if not plist.exists():
        return None
    try:
        import plistlib

        with plist.open("rb") as fh:
            info = plistlib.load(fh)
    except Exception:  # pragma: no cover - provenance is best effort
        return None
    version = info.get("CFBundleShortVersionString")
    return str(version) if version is not None else None


def build(resources: Path) -> dict:
    uidefs_path = resources / UIDEFS_NAME
    controls_path = resources / CONTROLS_NAME
    uidefs = json.loads(uidefs_path.read_text(encoding="utf-8"))
    raw_controls = json.loads(controls_path.read_text(encoding="utf-8"))

    controls = build_controls(raw_controls)
    models = build_models(uidefs, controls)
    return {
        "source": {
            "uidefs": UIDEFS_NAME,
            "controls": CONTROLS_NAME,
            "uidefs_sha256": _digest(uidefs_path),
            "controls_sha256": _digest(controls_path),
            "app_version": _app_version(resources),
        },
        "controls": controls,
        "models": models,
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

    n_params = sum(len(p) for p in data["models"].values())
    print(
        f"wrote {OUT_PATH.relative_to(REPO_ROOT)}: "
        f"{len(data['models'])} models, {n_params} params, "
        f"{len(data['controls'])} control types "
        f"(app {data['source']['app_version']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
