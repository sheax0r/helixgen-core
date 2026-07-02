"""Sidecar-spec convention + load-or-decompile orchestration for surgical edits."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from helixgen.decompile import decompile
from helixgen.library import Library


def sidecar_path(hsp_path: Path) -> Path:
    hsp_path = Path(hsp_path)
    return hsp_path.with_name(hsp_path.stem + ".spec.json")


def load_spec_for_preset(preset_path: Path, library: Library, irs=None) -> tuple[dict, Path]:
    """Return (spec_dict, spec_path) for a preset.

    - .json input → loaded directly.
    - .hsp input  → sidecar if present; else decompile, write the sidecar,
      and return it.
    """
    preset_path = Path(preset_path)
    if preset_path.suffix == ".json":
        return json.loads(preset_path.read_text()), preset_path
    side = sidecar_path(preset_path)
    if side.exists():
        return json.loads(side.read_text()), side
    spec = decompile(preset_path, library, irs=irs)
    side.write_text(json.dumps(spec, indent=2))
    return spec, side
