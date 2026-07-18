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

from helixgen import home

SCHEMA_VERSION = 1

_VALID_DEVICE_MODELS = ("Stadium", "Stadium XL")
_VALID_INSTRUMENT_TYPES = ("guitar", "bass")

_TRUE_STRINGS = {"1", "true", "yes"}
_FALSE_STRINGS = {"0", "false", "no"}


class PreferencesError(ValueError):
    """Raised when preferences cannot be loaded or parsed (bad JSON, bad env, bad schema)."""


def default_prefs_path() -> Path:
    """Return the preferences file path, honoring the HELIXGEN_PREFS env var.

    Parallels ``default_irs_path`` / ``$HELIXGEN_LIBRARY``: an explicit
    ``$HELIXGEN_PREFS`` redirects the whole file; otherwise the default is
    anchored under ``home.helixgen_home()`` (so ``$HELIXGEN_HOME`` relocates it
    too, and the common no-env case is unchanged at
    ``~/.helixgen/preferences.json``). Anchoring here keeps the destructive
    ``library migrate`` prefs-key strip -- and every prefs read -- inside the
    resolved home, never the real ``~/.helixgen``.
    """
    env = os.environ.get("HELIXGEN_PREFS")
    if env:
        return Path(env)
    return home.helixgen_home() / "preferences.json"


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
    git_commit_tones: str = "auto"


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
    """The user's default guitar: names a guitar PROFILE (its slug or name/
    short_name -- see ``guitars.find_profile``), used when a tone request
    doesn't name a guitar. Stored as an opaque string here; resolution to a
    profile happens at generate time."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise PreferencesError(
            f"default_guitar must be a string or null, got {type(value).__name__}"
        )
    return value


def _validate_git_commit_tones(value: Any, *, name: str = "git_commit_tones") -> str:
    """Normalize to one of "auto" / "true" / "false".

    Accepts a real JSON boolean, the bare JSON ints ``1``/``0``, the string
    "auto" (case-insensitive), any of the standard truthy/falsy strings
    (matching ``_TRUE_STRINGS`` / ``_FALSE_STRINGS`` used elsewhere in this
    module), or ``None`` (JSON ``null`` / key absent) which means "unset" and
    defaults to ``"auto"`` — matching the ``device.model: null``-is-unset
    convention elsewhere in this module.
    """
    if value is None:
        return "auto"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and value in (0, 1):
        return "true" if value else "false"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "auto":
            return "auto"
        if lowered in _TRUE_STRINGS:
            return "true"
        if lowered in _FALSE_STRINGS:
            return "false"
    raise PreferencesError(
        f"{name}={value!r} is not recognized (use \"auto\", true/false, "
        f"1/0, or yes/no)"
    )


def _parse_instruments(raw: Any) -> list[Instrument]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PreferencesError(f"instruments must be a list, got {type(raw).__name__}")
    return [Instrument.from_dict(item, index=i) for i, item in enumerate(raw)]


# Preferences keys retired by the library-metadata migration (design §6):
# ``instruments`` is replaced by guitar profiles (``library/guitars/*.json``),
# ``preset_output_dir`` by the ``library/tones/`` default write location. Both
# are still PARSED for back-compat, but loading a FILE that still carries a
# non-empty value warns (once per load) pointing at ``library migrate``.
_DEPRECATED_KEYS = {
    "instruments": "instruments (guitar profiles replace it)",
    "preset_output_dir": "preset_output_dir (the library/tones/ default replaces it)",
}


# (file path, key) pairs already warned about this process: one command run
# (e.g. `library migrate`) loads preferences several times, and repeating the
# identical notice per load is spam, not signal (backlog #79g). Keyed by path
# so tests -- and real multi-file setups -- with distinct prefs files still
# each get their notice.
_warned_deprecated: set[tuple[str, str]] = set()


def _warn_deprecated_keys(data: dict[str, Any], path: Path) -> None:
    """Emit a one-line stderr deprecation notice for each retired key that is
    actually PRESENT and non-empty in the on-disk ``data`` (never on a default
    / absent / empty value; never to stdout, which ``--json`` reads). Each
    (file, key) pair warns at most ONCE per process."""
    for key, why in _DEPRECATED_KEYS.items():
        if data.get(key):
            marker = (str(path), key)
            if marker in _warned_deprecated:
                continue
            _warned_deprecated.add(marker)
            print(
                f"helixgen: preferences key {why} is deprecated; run "
                "`helixgen library migrate` to convert it.",
                file=sys.stderr,
            )


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

    _warn_deprecated_keys(data, resolved_path)

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
        git_commit_tones=_validate_git_commit_tones(data.get("git_commit_tones", "auto")),
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
    if "HELIXGEN_GIT_COMMIT_TONES" in os.environ:
        prefs.git_commit_tones = _validate_git_commit_tones(
            os.environ["HELIXGEN_GIT_COMMIT_TONES"], name="HELIXGEN_GIT_COMMIT_TONES"
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
        "git_commit_tones": "auto",
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
