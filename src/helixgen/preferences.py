"""User preferences: explicit, editable settings replacing memory-driven skill behaviour.

Resolution order per key (first hit wins):

1. ``HELIXGEN_<KEY>`` env var, if set.
2. The value in the resolved preferences file (``$HELIXGEN_PREFS`` or the
   default ``~/.helixgen/preferences.json``).
3. Built-in default (Claude memory is a skill-level seed only; this module
   never reads memory).
"""
from __future__ import annotations

import datetime
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
_VALID_NORMALIZE_MODES = ("play", "sample", "looper")
_VALID_MEASURE_VIA = ("meters", "capture")

#: The shipped loudness target, in dB of total loudness, and where it came
#: from. It is a CONSTANT, not a per-rig measurement: the factory presets are
#: identical across Stadiums, so the same reference reproduces on similar
#: hardware, and separate runs only land on a common level if they all use the
#: same number. Scaffolded into a fresh profile so nobody has to type it.
#:
#: METERS METRIC ONLY. Integrated LUFS (``--measure-via capture``) is at or
#: below 0 by construction, so this target is meaningless there -- `device
#: normalize` refuses a positive target in capture mode rather than trimming
#: every tone to the cap.
DEFAULT_TARGET_DB = 17.5

#: The measurement stimulus shipped inside this package: 10 guitar-DI notes
#: E2-C5, EXACTLY 240000 samples (5.00 s at 48 kHz), 24-bit mono, peak -3
#: dBFS, CC0 (FreePats FSBS Electric Guitar Direct).
#:
#: It lives here, not in the plugin, so that "replay a fixed stimulus" is the
#: default for EVERY caller rather than only for users whose agent ran a
#: setup skill first -- and so that no profile ever records a path into a
#: VERSIONED plugin cache directory, which rots on the next update.
DEFAULT_STIMULUS_LOOP_SECONDS = 5.0

#: Output volume used for the stimulus when the rig has NOT been calibrated.
#: Not a calibration — a deterministic starting point, so an uncalibrated run
#: is at least the same every time instead of "whatever the slider was at".
#: The alternative is what actually happened in the field: a system left at
#: 100% drove the jack ~17 dB hotter than a guitar does and every measurement
#: was an artifact of it.
DEFAULT_STIMULUS_VOLUME = 50

#: An instrument-level source at the jack sits in roughly this window. Outside
#: it, the stimulus is being driven at a level no guitar produces, and every
#: derived trim is an artifact of the playback level rather than the tone.
PLAUSIBLE_INPUT_DB = (-45.0, -22.0)


def bundled_stimulus() -> Path | None:
    """The packaged stimulus wav, or None when it is somehow absent (a
    partial install, a zipped egg). Never raises: a missing asset must
    degrade to hand-played windows, not break the verb."""
    candidate = Path(__file__).resolve().parent / "assets" / "helix-cal-loop.wav"
    return candidate if candidate.is_file() else None
DEFAULT_TARGET_SOURCE = {
    "kind": "factory_preset",
    "name": "Stadium Rock Rig",
    "measured_total_db": 17.51,
    "measured_on": "2026-07-29",
    "measure_via": "meters",
    "note": ("the factory reference, measured on a Stadium XL; factory "
             "presets carry output level 0.0 dB, so total == chain gain. "
             "Per-category targets were tested and rejected: lead-over-"
             "rhythm was only 1.4 dB and two clean presets disagreed by "
             "6.3 dB."),
}

#: A calibration older than this is reported as stale (advisory only). It is
#: valid exactly as long as the physical rig is unchanged, which nothing here
#: can observe -- so this is a nudge to re-run two cheap measurements, never a
#: block.
CALIBRATION_STALE_DAYS = 90

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
class NormalizationSample:
    """The recorded stimulus `sample` mode replays into the instrument jack.

    ``loop_seconds`` is recorded because measurement windows should contain
    WHOLE loop cycles -- a window that cuts a cycle reports a phase-dependent
    number. ``playback_cmd`` is a format string taking ``{path}``; the default
    is sox, which loops INSIDE one effects chain. (``afplay`` in a shell loop
    is not gapless: ~0.8-0.9 s of process startup per invocation turns a
    5.00 s loop into a ~5.9 s jittering period.)"""

    path: str | None = None
    loop_seconds: float | None = None
    playback_cmd: str = "play -q {path} repeat 9999"
    output_device: str | None = None
    volume: int | None = None

    def to_dict(self) -> dict:
        return {"path": self.path, "loop_seconds": self.loop_seconds,
                "playback_cmd": self.playback_cmd,
                "output_device": self.output_device, "volume": self.volume}


@dataclass
class NormalizationCalibration:
    """What the source level was nulled against, and what it reached.

    ``reference_input_db`` is the JACK level read while the user played BY
    HAND -- the whole point is that it is chain-independent, so the stimulus
    can be matched to it on any preset. (Nulling against ``gain_db`` instead
    would "converge" instantly at any arbitrary level: on a clean chain that
    is precisely the quantity that does NOT move with source level.)
    ``reference_guitar`` is recorded because instruments differ by 10+ dB
    (active EMG vs P-90), so a reference taken with another guitar is not
    a reference at all."""

    reference_input_db: float | None = None
    reference_guitar: str | None = None
    achieved_input_db: float | None = None
    calibrated_on: str | None = None

    def to_dict(self) -> dict:
        return {"reference_input_db": self.reference_input_db,
                "reference_guitar": self.reference_guitar,
                "achieved_input_db": self.achieved_input_db,
                "calibrated_on": self.calibrated_on}


@dataclass
class Normalization:
    """The loudness-normalization protocol: which stimulus, which target, and
    the calibration that makes the numbers comparable between sessions.

    Every field except ``mode`` defaults to None, meaning "no opinion" -- the
    CLI's own defaults stay in force, so an ABSENT block is exactly today's
    behavior. ``mode`` defaults to ``play`` (the user plays the guitar), the
    one mode that needs no cabling and no calibration."""

    mode: str = "sample"
    target_db: float | None = None
    target_source: dict | None = None
    seconds: float | None = None
    tolerance_db: float | None = None
    measure_via: str | None = None
    capture_input: str | None = None
    sample: NormalizationSample = field(default_factory=NormalizationSample)
    calibration: NormalizationCalibration = field(
        default_factory=NormalizationCalibration)

    def resolved_stimulus(self) -> Path | None:
        """The stimulus this profile should replay: the configured
        ``sample.path`` if set, else the one bundled with this package.

        Falling back to the bundled asset is what makes `sample` a workable
        DEFAULT: a user who has configured nothing still gets a fixed,
        repeatable stimulus instead of being asked to hand-play a window per
        target, forever, on every run."""
        if self.sample.path:
            return Path(self.sample.path).expanduser()
        return bundled_stimulus()

    @property
    def source(self) -> str | None:
        """The `--source` gate this mode REQUIRES, or None when it has no
        opinion. Only `looper` has one: an on-device looper leaves the jack
        structurally silent, so the gate must move to chain-out level or every
        window scores as "not playing". `play` and `sample` both drive the
        instrument jack, which is the CLI default anyway -- returning None
        there keeps a run's reported provenance honest (nothing was
        configured, so nothing came from preferences)."""
        return "loop" if self.mode == "looper" else None

    @property
    def is_calibrated(self) -> bool:
        """True once a source-level reference has been recorded. ``play`` mode
        never needs one (the guitar IS the stimulus)."""
        return self.calibration.reference_input_db is not None

    def calibration_warnings(self, *, default_guitar: str | None) -> list[str]:
        """Advisory reasons to re-calibrate before trusting a run. Empty when
        uncalibrated (nothing to be stale about -- `play` mode is always ready)
        or when the calibration is fresh and was taken with the default guitar.
        Never raises: a hand-edited date is ignored, not fatal."""
        if not self.is_calibrated:
            return []
        out = []
        ref = self.calibration.reference_guitar
        if ref and default_guitar and ref != default_guitar:
            out.append(
                f"the source level was calibrated with guitar {ref!r}, but "
                f"your default_guitar is {default_guitar!r} — instruments "
                f"differ by 10+ dB, so re-run `helixgen device calibrate`")
        on = self.calibration.calibrated_on
        if on:
            try:
                day = datetime.date.fromisoformat(str(on)[:10])
            except ValueError:
                return out
            age = (datetime.date.today() - day).days
            if age > CALIBRATION_STALE_DAYS:
                out.append(
                    f"the source level was calibrated on {on} ({age} days "
                    f"ago) — a calibration is only valid while the physical "
                    f"rig is unchanged; re-run `helixgen device calibrate`")
        return out

    def to_dict(self) -> dict:
        return {"mode": self.mode, "target_db": self.target_db,
                "target_source": self.target_source, "seconds": self.seconds,
                "tolerance_db": self.tolerance_db,
                "measure_via": self.measure_via,
                "capture_input": self.capture_input,
                "sample": self.sample.to_dict(),
                "calibration": self.calibration.to_dict()}


def _normalize_block_error(field_name: str, value: Any, expected: str) -> "PreferencesError":
    return PreferencesError(
        f"normalization.{field_name} must be {expected}, got {value!r}")


def _validate_choice(raw: Any, *, field_name: str, choices: tuple[str, ...],
                     default: Any = None) -> str | None:
    if raw is None:
        return default
    if isinstance(raw, str) and raw.strip().lower() in choices:
        return raw.strip().lower()
    raise _normalize_block_error(field_name, raw, f"one of {choices}")


def _validate_number(raw: Any, *, field_name: str) -> float | None:
    """A JSON number (or a numeric string, which is what an env var always
    is). Booleans are rejected -- ``True`` is an int in Python, and a
    ``target_db`` of 1.0 from a typo'd ``true`` would be silently obeyed."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise _normalize_block_error(field_name, raw, "a number")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            pass
    raise _normalize_block_error(field_name, raw, "a number")


def _parse_normalization(raw: Any) -> Normalization:
    if raw is None:
        return Normalization()
    if not isinstance(raw, dict):
        raise PreferencesError(
            "normalization must be an object, e.g. {\"mode\": \"play\"}")

    sample_raw = raw.get("sample") or {}
    calib_raw = raw.get("calibration") or {}
    for name, block in (("sample", sample_raw), ("calibration", calib_raw)):
        if not isinstance(block, dict):
            raise _normalize_block_error(name, block, "an object")

    target_source = raw.get("target_source")
    if target_source is not None and not isinstance(target_source, dict):
        raise _normalize_block_error("target_source", target_source, "an object")

    sample_path = sample_raw.get("path")
    if sample_path is not None and not isinstance(sample_path, str):
        raise _normalize_block_error("sample.path", sample_path, "a string")
    playback_cmd = sample_raw.get("playback_cmd")
    if playback_cmd is not None and not isinstance(playback_cmd, str):
        raise _normalize_block_error("sample.playback_cmd", playback_cmd,
                                     "a string")
    sample_volume = sample_raw.get("volume")
    if sample_volume is not None:
        volume_number = _validate_number(sample_volume,
                                         field_name="sample.volume")
        if not 0 <= volume_number <= 100:
            raise _normalize_block_error("sample.volume", sample_volume,
                                         "a number between 0 and 100")
        sample_volume = int(round(volume_number))
    for name in ("reference_guitar", "calibrated_on"):
        value = calib_raw.get(name)
        if value is not None and not isinstance(value, str):
            raise _normalize_block_error(f"calibration.{name}", value,
                                         "a string")

    sample = NormalizationSample(
        path=sample_path,
        loop_seconds=_validate_number(sample_raw.get("loop_seconds"),
                                      field_name="sample.loop_seconds"),
        playback_cmd=playback_cmd or NormalizationSample.playback_cmd,
        output_device=sample_raw.get("output_device"),
        volume=sample_volume,
    )
    calibration = NormalizationCalibration(
        reference_input_db=_validate_number(
            calib_raw.get("reference_input_db"),
            field_name="calibration.reference_input_db"),
        reference_guitar=calib_raw.get("reference_guitar"),
        achieved_input_db=_validate_number(
            calib_raw.get("achieved_input_db"),
            field_name="calibration.achieved_input_db"),
        calibrated_on=calib_raw.get("calibrated_on"),
    )
    return Normalization(
        mode=_validate_choice(raw.get("mode"), field_name="mode",
                              choices=_VALID_NORMALIZE_MODES, default="play"),
        target_db=_validate_number(raw.get("target_db"), field_name="target_db"),
        target_source=target_source,
        seconds=_validate_number(raw.get("seconds"), field_name="seconds"),
        tolerance_db=_validate_number(raw.get("tolerance_db"),
                                      field_name="tolerance_db"),
        measure_via=_validate_choice(raw.get("measure_via"),
                                     field_name="measure_via",
                                     choices=_VALID_MEASURE_VIA),
        capture_input=raw.get("capture_input"),
        sample=sample,
        calibration=calibration,
    )


def _apply_normalization_env(n: Normalization) -> None:
    """Per-key env overrides for the scalar normalization keys (the nested
    sample/calibration blocks are structured data, like ``instruments``, and
    are file-only). A malformed value is an ERROR, never a silent fallback:
    ``HELIXGEN_NORMALIZE_TARGET_DB=loud`` must not quietly normalize a whole
    library to the anchor instead."""
    env = os.environ
    if "HELIXGEN_NORMALIZE_MODE" in env:
        n.mode = _validate_choice(env["HELIXGEN_NORMALIZE_MODE"],
                                  field_name="HELIXGEN_NORMALIZE_MODE",
                                  choices=_VALID_NORMALIZE_MODES,
                                  default="play")
    if "HELIXGEN_NORMALIZE_TARGET_DB" in env:
        n.target_db = _validate_number(env["HELIXGEN_NORMALIZE_TARGET_DB"],
                                       field_name="HELIXGEN_NORMALIZE_TARGET_DB")
    if "HELIXGEN_NORMALIZE_SECONDS" in env:
        n.seconds = _validate_number(env["HELIXGEN_NORMALIZE_SECONDS"],
                                     field_name="HELIXGEN_NORMALIZE_SECONDS")
    if "HELIXGEN_NORMALIZE_TOLERANCE_DB" in env:
        n.tolerance_db = _validate_number(
            env["HELIXGEN_NORMALIZE_TOLERANCE_DB"],
            field_name="HELIXGEN_NORMALIZE_TOLERANCE_DB")
    if "HELIXGEN_NORMALIZE_MEASURE_VIA" in env:
        n.measure_via = _validate_choice(
            env["HELIXGEN_NORMALIZE_MEASURE_VIA"],
            field_name="HELIXGEN_NORMALIZE_MEASURE_VIA",
            choices=_VALID_MEASURE_VIA)
    if "HELIXGEN_NORMALIZE_CAPTURE_INPUT" in env:
        n.capture_input = env["HELIXGEN_NORMALIZE_CAPTURE_INPUT"]


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
    normalization: Normalization = field(default_factory=Normalization)


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
        normalization=_parse_normalization(data.get("normalization")),
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
    _apply_normalization_env(prefs.normalization)

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
        "normalization": {
            **Normalization().to_dict(),
            # Scaffolded with the shipped reference rather than null: a
            # target left unset makes `device normalize` anchor on its own
            # first target, which level-matches WITHIN one preset and leaves
            # separate runs nowhere near each other.
            "target_db": DEFAULT_TARGET_DB,
            "target_source": dict(DEFAULT_TARGET_SOURCE),
        },
    }


def save_normalization(changes: dict, path: Path | None = None) -> Path:
    """Merge ``changes`` into the file's ``normalization`` block and write it
    back, preserving every other preference verbatim.

    Nested ``sample`` / ``calibration`` dicts merge key-by-key rather than
    replacing wholesale, so recording a calibration never silently drops a
    stimulus path the user set by hand. Writes atomically (tmp + rename),
    matching ``scaffold_default``."""
    resolved_path = path if path is not None else default_prefs_path()

    data: dict[str, Any] = {}
    if resolved_path.exists():
        try:
            data = json.loads(resolved_path.read_text())
        except json.JSONDecodeError as exc:
            raise PreferencesError(
                f"malformed JSON in {resolved_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PreferencesError(f"{resolved_path} must contain a JSON object")

    data.setdefault("schema_version", SCHEMA_VERSION)
    block = data.get("normalization")
    if not isinstance(block, dict):
        block = {}
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(block.get(key), dict):
            block[key] = {**block[key], **value}
        else:
            block[key] = value
    data["normalization"] = block

    # Write THROUGH a symlink (a dotfiles-managed preferences.json is
    # commonly one): replacing the link itself would leave the real file
    # stale and silently unlinked from the user's repo.
    target = resolved_path.resolve() if resolved_path.is_symlink() else resolved_path
    target.parent.mkdir(parents=True, exist_ok=True)
    # A FIXED tmp name is shared by every concurrent writer, so two runs
    # interleave bytes into one file and `os.replace` publishes the mix.
    # This is the first read-modify-write of this file, so it needs its own.
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return resolved_path


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
