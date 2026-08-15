#!/usr/bin/env python3
"""Regenerate ``src/helixgen/device/_param_ui.json`` from the Helix Stadium
editor app bundle.

Sibling of ``tools/build_device_defs.py``, same contract: run it offline
against the app bundle, commit the emitted asset, and helixgen never needs the
app at runtime.

Where ``_defs_data.json`` carries each param's numeric id, type and RAW range
(``{id,type,min,max,def}``), this asset carries what that raw range MEANS —
the display name, the unit, the raw->display scale, and, for discrete params,
the enum labels. It also carries the editor's own display name for each MODEL
(``model_names``), which is the only authoritative source for what a block is
called (see :func:`build_model_names`). Two app resources supply it:

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
The one deliberate exception is a tempo-syncable param's two companions, which
the app names on the HOST param's entry (``{"id": "Time", "sync":
"TempoSync1", "note": "SyncSelect1"}``) instead of listing separately; those
are derived, so they read as the knobs they are.

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

# A tempo-syncable param names its two companion params on its OWN entry
# (`{"id": "Time", "sync": "TempoSync1", "note": "SyncSelect1"}`) and the
# companions get no entry of their own. They are knobs the author sets, so
# derive them rather than letting them read as internal plumbing: the `note`
# half is the note-division enum, the `sync` half the on/off toggle (left
# without a control — its type already says bool, and inventing labels for it
# would be guessing).
IMPLIED_PARAM_KEYS = {"note": "sync_note", "sync": None}

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "src" / "helixgen" / "device" / "_param_ui.json"

# A printf conversion at the START of the string, followed by the unit text:
# "%+.1f dB" -> "dB", "%.0f %%" -> "%", "%.2f \"" -> "in". A string whose
# conversion is NOT leading ("Left %.0f") is a value LABEL, not a unit.
_UNIT_RE = re.compile(r"^%[-+#0-9.]*[bdeEfFgGiosuxX]\s*(\S.*)$")


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
    """The control's BASE unit — the one its stored value is actually in.

    A bound-switched control renders the same value in different units at
    different magnitudes (``time_ms``: ms below 1000, sec above;
    ``time_sec_DynamicHall``: ms below 1 second, sec above). The bound that
    rescales carries a ``unitsMultiplier``; the bound that does NOT is the one
    whose unit matches the stored value, so those win. Taking the first bound
    instead would call a DynamicHall's 13-second decay "13.2 ms".
    """
    plain: list[Any] = [control.get("formatUnits")]
    rescaled: list[Any] = []
    fmt = control.get("format")
    if isinstance(fmt, list):
        for entry in fmt:
            if not isinstance(entry, dict):
                continue
            bucket = rescaled if entry.get("unitsMultiplier") is not None else plain
            bucket.append(entry.get("formatUnits"))
            bucket.append(entry.get("format"))
    else:
        plain.append(fmt)
    for cand in plain + rescaled:
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
        # Some controls state the display range outright instead of scaling to
        # it (`pan`, `tilt`, `amp_sag_ripple`: raw -1..1 shown as -10..10).
        for src, dst in (("minDisplayValue", "display_min"),
                         ("maxDisplayValue", "display_max")):
            if isinstance(control.get(src), (int, float)):
                entry[dst] = control[src]
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
        implied: dict[str, dict] = {}
        for pdef in mdef.get("params") or []:
            if not isinstance(pdef, dict):
                continue
            for key, tag in IMPLIED_PARAM_KEYS.items():
                companion = pdef.get(key)
                if isinstance(companion, str) and companion:
                    implied[companion] = {"tag": tag} if tag in controls else {}
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
        for pid, entry in implied.items():
            params.setdefault(pid, entry)
        if params:
            out[model_str] = params
    return out


# --- model display names (hgc-3ll) -----------------------------------------
#
# A model's UIDefs entry carries the editor's own `name` ("LA Studio Comp").
# That name alone is NOT a usable handle, for two reasons this builder fixes so
# the runtime lookup stays a dumb dict get:
#
# 1. It omits the channel: a mono model names its stereo twin in `stereo_model`
#    and the twin gets NO entry of its own, so both would read "LA Studio
#    Comp". A model with a twin therefore gets " Mono" / " Stereo" appended.
#    A model whose id merely ENDS in Mono/Stereo with no twin (`HD2_ReturnMono1`
#    = "Return 1") is not a variant of anything and keeps its bare name.
# 2. Genuinely different models share a name: every `HD2_Preamp*` shares its
#    `HD2_Amp*` sibling's name, and the `Agoura_Amp*` reissues share both.
#    The plain amp keeps the bare name (it is the one recipes already spell);
#    the others are qualified " (Preamp)" / " (Agoura)".
#
# The emitted table is asserted unique, so a display name resolves to exactly
# one model and can be used as an unambiguous handle.

# The channel word sits at the end of the id, but a version bump (`...MonoV2`)
# or a codename tail (`...Stereo_Victoria`) can follow it.
_CHANNEL_RE = re.compile(r"(Mono|Stereo)(\d*)(?=(?:V\d+|_[A-Za-z0-9]+)?$)")

# Namespaces that are the primary home of a model; anything else (Agoura_, …)
# is a reissue and loses the bare name in a collision.
_PRIMARY_NAMESPACES = frozenset({"HD2", "HX2", "VIC", "P35", "L6SPB"})

# Line 6's legacy stompbox modelers, which the editor files under "Legacy".
# They share names with the modern models they predate (DL4 "Ping Pong" vs
# Delay "Ping Pong"); the modern one keeps the bare name.
_LEGACY_FAMILIES = ("DL4", "DM4", "FM4", "MM4")

# Signal-flow endpoints and split nodes, not blocks: `ingest` never catalogs
# them (see DSP_INFRASTRUCTURE_KEYS) and their names collide with the real
# Send/Return blocks, so they stay out of the table entirely.
_NON_BLOCK_PREFIXES = ("C63_", "P35_Input", "P35_Output", "P35_AppDSP")


def _base_key(model_str: str) -> str:
    """Model key with the channel word removed (the model's identity)."""
    return _CHANNEL_RE.sub("", model_str)


def _is_legacy(base_key: str) -> bool:
    """True for a legacy stompbox model (``HD2_DL4PingPong``)."""
    _, _, rest = base_key.partition("_")
    return rest.startswith(_LEGACY_FAMILIES)


def _rank(base_key: str) -> tuple:
    """Sort key: the smallest base key in a collision keeps the bare name."""
    namespace, _, rest = base_key.partition("_")
    return (
        rest.startswith("Preamp"),
        _is_legacy(base_key),
        namespace not in _PRIMARY_NAMESPACES,
        base_key,
    )


def _qualifier(base_key: str) -> str:
    """Deterministic fragment distinguishing a base key that lost the bare name."""
    namespace, _, rest = base_key.partition("_")
    if not rest:
        return base_key
    if rest.startswith("Preamp"):
        return "Preamp"
    if _is_legacy(base_key):
        return "Legacy"
    if namespace in _PRIMARY_NAMESPACES:
        return base_key
    return namespace


def build_model_names(uidefs: dict) -> dict[str, str]:
    """``{model_str: display_name}``, unique, covering stereo twins."""
    raw: dict[str, str] = {}
    for model_str, mdef in uidefs.items():
        if not isinstance(mdef, dict) or not model_str:
            continue
        if model_str.startswith(_NON_BLOCK_PREFIXES):
            continue
        name = mdef.get("name")
        if not isinstance(name, str) or not name:
            continue
        raw[model_str] = name
        twin = mdef.get("stereo_model")
        if isinstance(twin, str) and twin and twin not in uidefs:
            raw[twin] = name

    by_name: dict[str, dict[str, list[str]]] = {}
    for model_str, name in raw.items():
        by_name.setdefault(name, {}).setdefault(_base_key(model_str), []).append(model_str)

    out: dict[str, str] = {}
    for name, by_base in by_name.items():
        ranked = sorted(by_base, key=_rank)
        for position, base in enumerate(ranked):
            qualified = name if position == 0 else f"{name} ({_qualifier(base)})"
            variants = sorted(by_base[base])
            for model_str in variants:
                channel = _CHANNEL_RE.search(model_str)
                if len(variants) > 1 and channel:
                    suffix = " " + channel.group(1)
                    if channel.group(2):
                        suffix += " " + channel.group(2)
                    out[model_str] = qualified + suffix
                else:
                    out[model_str] = qualified

    clashes: dict[str, list[str]] = {}
    for model_str, name in out.items():
        clashes.setdefault(name, []).append(model_str)
    for name, models in sorted(clashes.items()):
        if len(models) > 1:
            raise SystemExit(
                f"error: display name {name!r} is not unique — {sorted(models)}. "
                f"Extend _qualifier()/_rank() to separate them."
            )
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
    for path in (uidefs_path, controls_path):
        if not path.is_file():
            raise SystemExit(
                f"error: resource not found: {path}. This app version may name "
                f"it differently — check {resources} and update the constants "
                f"at the top of this script."
            )
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
        "model_names": build_model_names(uidefs),
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
        f"{len(data['controls'])} control types, "
        f"{len(data['model_names'])} model names "
        f"(app {data['source']['app_version']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
