"""User preferences: explicit, editable settings replacing memory-driven skill behaviour.

Resolution order per key (first hit wins):

1. ``HELIXGEN_<KEY>`` env var, if set.
2. The value in the resolved preferences file (``$HELIXGEN_PREFS`` or the
   default ``~/.helixgen/preferences.json``).
3. Built-in default (Claude memory is a skill-level seed only; this module
   never reads memory).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_VALID_DEVICE_MODELS = ("Stadium", "Stadium XL")
_VALID_INSTRUMENT_TYPES = ("guitar", "bass")

_TRUE_STRINGS = {"1", "true", "yes"}
_FALSE_STRINGS = {"0", "false", "no"}


class PreferencesError(ValueError):
    """Raised when preferences cannot be loaded or parsed (bad JSON, bad env, bad schema)."""


def default_prefs_path() -> Path:
    """Return the preferences file path, honoring the HELIXGEN_PREFS env var.

    Parallels ``default_irs_path`` / ``$HELIXGEN_LIBRARY``: the whole-file
    location can be redirected via env; otherwise it lives under the
    ``~/.helixgen`` convention.
    """
    env = os.environ.get("HELIXGEN_PREFS")
    if env:
        return Path(env)
    return Path.home() / ".helixgen" / "preferences.json"


def _parse_bool_env(name: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE_STRINGS:
        return True
    if lowered in _FALSE_STRINGS:
        return False
    raise PreferencesError(
        f"{name}={raw!r} is not a recognized boolean "
        f"(use 1/0, true/false, or yes/no)"
    )


def _default_reveal_in_finder() -> bool:
    return sys.platform == "darwin"


@dataclass
class Instrument:
    """A single guitar/bass entry under `instruments`."""

    name: str
    type: str
    pickups: str | None = None
    selector: str | None = None
    active: bool | None = None
    genres: list[str] = field(default_factory=list)
    notes: str | None = None

    @classmethod
    def from_dict(cls, data: Any, *, index: int) -> "Instrument":
        if not isinstance(data, dict):
            raise PreferencesError(
                f"instruments[{index}] must be an object, got {type(data).__name__}"
            )
        name = data.get("name")
        type_ = data.get("type")
        if not isinstance(name, str) or not name:
            raise PreferencesError(f"instruments[{index}].name is required (non-empty string)")
        if type_ not in _VALID_INSTRUMENT_TYPES:
            raise PreferencesError(
                f"instruments[{index}].type must be one of {_VALID_INSTRUMENT_TYPES}, "
                f"got {type_!r}"
            )
        genres = data.get("genres", [])
        if not isinstance(genres, list) or not all(isinstance(g, str) for g in genres):
            raise PreferencesError(f"instruments[{index}].genres must be a list of strings")
        return cls(
            name=name,
            type=type_,
            pickups=data.get("pickups"),
            selector=data.get("selector"),
            active=data.get("active"),
            genres=list(genres),
            notes=data.get("notes"),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "pickups": self.pickups,
            "selector": self.selector,
            "active": self.active,
            "genres": list(self.genres),
            "notes": self.notes,
        }


@dataclass
class Preferences:
    """Resolved preferences: env overrides layered over the file, layered over defaults."""

    schema_version: int = SCHEMA_VERSION
    device_model: str | None = None
    favor_irs: bool = False
    reveal_in_finder: bool = field(default_factory=_default_reveal_in_finder)
    guard_paid_irs_in_git: bool = True
    preset_output_dir: str | None = None
    author: str | None = None
    default_guitar: str | None = None
    instruments: list[Instrument] = field(default_factory=list)
    volume_normalize_snapshots: bool = True
    volume_normalize_baseline: bool = True


def _normalize_device_model_key(value: str) -> str:
    """Fold case + separators (spaces/underscores/hyphens) for matching."""
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())


def _validate_device_model(model: Any) -> str | None:
    if model is None:
        return None
    if isinstance(model, str):
        key = _normalize_device_model_key(model)
        for canonical in _VALID_DEVICE_MODELS:
            if _normalize_device_model_key(canonical) == key:
                return canonical
    raise PreferencesError(
        f"device.model must be one of {_VALID_DEVICE_MODELS} or null, got {model!r}"
    )


def _validate_default_guitar(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PreferencesError(
            f"default_guitar must be a string or null, got {type(value).__name__}"
        )
    return value


def _parse_instruments(raw: Any) -> list[Instrument]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PreferencesError(f"instruments must be a list, got {type(raw).__name__}")
    return [Instrument.from_dict(item, index=i) for i, item in enumerate(raw)]


def load_preferences(path: Path | None = None) -> Preferences:
    """Load preferences, applying per-key env overrides.

    - ``path`` is used verbatim if given (this is how callers in the test
      suite point at a tmp_path fixture without touching HOME/env).
    - When ``path`` is None, the file location is resolved via
      ``default_prefs_path()`` ($HELIXGEN_PREFS or ~/.helixgen/preferences.json).
      A missing file at an *explicitly set* $HELIXGEN_PREFS is an error; a
      missing file at the default path silently yields all defaults.
    - Malformed JSON raises PreferencesError naming the path.
    """
    explicit_env = path is None and os.environ.get("HELIXGEN_PREFS")
    resolved_path = path if path is not None else default_prefs_path()

    data: dict[str, Any] = {}
    if resolved_path.exists():
        try:
            data = json.loads(resolved_path.read_text())
        except json.JSONDecodeError as exc:
            raise PreferencesError(f"malformed JSON in {resolved_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PreferencesError(f"{resolved_path} must contain a JSON object")
    elif explicit_env:
        raise PreferencesError(
            f"$HELIXGEN_PREFS points at {resolved_path}, but no such file exists"
        )
    # else: missing default-path file -> data stays {} -> all defaults

    device_block = data.get("device") or {}
    if not isinstance(device_block, dict):
        raise PreferencesError("device must be an object, e.g. {\"model\": \"Stadium XL\"}")
    device_model = _validate_device_model(device_block.get("model"))

    schema_version = data.get("schema_version", SCHEMA_VERSION)

    prefs = Preferences(
        schema_version=schema_version,
        device_model=device_model,
        favor_irs=bool(data.get("favor_irs", False)),
        reveal_in_finder=bool(data.get("reveal_in_finder", _default_reveal_in_finder())),
        guard_paid_irs_in_git=bool(data.get("guard_paid_irs_in_git", True)),
        preset_output_dir=data.get("preset_output_dir"),
        author=data.get("author"),
        default_guitar=_validate_default_guitar(data.get("default_guitar")),
        instruments=_parse_instruments(data.get("instruments")),
        volume_normalize_snapshots=bool(data.get("volume_normalize_snapshots", True)),
        volume_normalize_baseline=bool(data.get("volume_normalize_baseline", True)),
    )

    # --- per-key env overrides (first hit wins, applied last) ---
    if "HELIXGEN_DEVICE_MODEL" in os.environ:
        prefs.device_model = _validate_device_model(os.environ["HELIXGEN_DEVICE_MODEL"])
    if "HELIXGEN_FAVOR_IRS" in os.environ:
        prefs.favor_irs = _parse_bool_env("HELIXGEN_FAVOR_IRS", os.environ["HELIXGEN_FAVOR_IRS"])
    if "HELIXGEN_REVEAL_IN_FINDER" in os.environ:
        prefs.reveal_in_finder = _parse_bool_env(
            "HELIXGEN_REVEAL_IN_FINDER", os.environ["HELIXGEN_REVEAL_IN_FINDER"]
        )
    if "HELIXGEN_GUARD_PAID_IRS" in os.environ:
        prefs.guard_paid_irs_in_git = _parse_bool_env(
            "HELIXGEN_GUARD_PAID_IRS", os.environ["HELIXGEN_GUARD_PAID_IRS"]
        )
    if "HELIXGEN_PRESET_DIR" in os.environ:
        prefs.preset_output_dir = os.environ["HELIXGEN_PRESET_DIR"]
    if "HELIXGEN_AUTHOR" in os.environ:
        prefs.author = os.environ["HELIXGEN_AUTHOR"]
    if "HELIXGEN_DEFAULT_GUITAR" in os.environ:
        prefs.default_guitar = os.environ["HELIXGEN_DEFAULT_GUITAR"]
    if "HELIXGEN_VOLUME_NORMALIZE_SNAPSHOTS" in os.environ:
        prefs.volume_normalize_snapshots = _parse_bool_env(
            "HELIXGEN_VOLUME_NORMALIZE_SNAPSHOTS",
            os.environ["HELIXGEN_VOLUME_NORMALIZE_SNAPSHOTS"],
        )
    if "HELIXGEN_VOLUME_NORMALIZE_BASELINE" in os.environ:
        prefs.volume_normalize_baseline = _parse_bool_env(
            "HELIXGEN_VOLUME_NORMALIZE_BASELINE",
            os.environ["HELIXGEN_VOLUME_NORMALIZE_BASELINE"],
        )
    # instruments are structured data; not env-overridable (per design doc).

    return prefs


def _default_scaffold_dict() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "_comment": (
            "helixgen user preferences. Edit freely. Env vars HELIXGEN_<KEY> "
            "and $HELIXGEN_PREFS override this file. See CLAUDE.md."
        ),
        "device": {"model": None},
        "favor_irs": False,
        "reveal_in_finder": _default_reveal_in_finder(),
        "guard_paid_irs_in_git": True,
        "preset_output_dir": None,
        "author": None,
        "default_guitar": None,
        "instruments": [],
        "volume_normalize_snapshots": True,
        "volume_normalize_baseline": True,
    }


def scaffold_default(path: Path | None = None, *, force: bool = False) -> Path:
    """Write a default preferences file if absent (idempotent); return its path.

    Refuses to overwrite an existing file unless ``force=True``. Writes
    atomically (tmp file + rename), matching ``IrMapping.save``.
    """
    resolved_path = path if path is not None else default_prefs_path()

    if resolved_path.exists() and not force:
        return resolved_path

    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = resolved_path.with_suffix(resolved_path.suffix + ".tmp")
    tmp.write_text(json.dumps(_default_scaffold_dict(), indent=2) + "\n")
    os.replace(tmp, resolved_path)
    return resolved_path
