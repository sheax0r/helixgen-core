"""Canonical path resolution for ``~/.helixgen`` and its subareas.

Centralizes default-path computation for the on-disk helixgen home
directory (design: ``docs/superpowers/specs/2026-07-15-library-metadata-
design.md``), introducing ``$HELIXGEN_HOME`` as the root override.

None of these functions create directories — pure path resolution; callers
``mkdir(parents=True, exist_ok=True)`` as needed.

Existing per-area env vars (``$HELIXGEN_LIBRARY``, ``$HELIXGEN_IRS``,
``$HELIXGEN_SETLISTS``, ``$HELIXGEN_PREFS``, ``$HELIXGEN_CACHE``) keep
working and always win over a ``$HELIXGEN_HOME``-derived default — this
module never overrides that precedence, only what a bare default computes to.

Note: ``manifest_path()`` is a NEW default location
(``setlists/manifest.json``) that ``SetlistManifest`` does not use yet — the
manifest still resolves to ``legacy_manifest_path()`` until the migration
(a later task) flips it over. Likewise ``library_irs_dir()`` is a NEW
default (``library/irs``) not yet wired as ``ir.py``'s default — that flip
is a later PR too. Both new-default functions exist now so later tasks and
the migration path have somewhere to point.
"""
from __future__ import annotations

import os
from pathlib import Path


def helixgen_home() -> Path:
    """The helixgen home root: ``$HELIXGEN_HOME`` or ``~/.helixgen``."""
    env = os.environ.get("HELIXGEN_HOME")
    if env:
        return Path(env)
    return Path.home() / ".helixgen"


def library_dir() -> Path:
    """The artifact library: ``$HELIXGEN_LIBRARY`` or ``helixgen_home()/"library"``."""
    env = os.environ.get("HELIXGEN_LIBRARY")
    if env:
        return Path(env)
    return helixgen_home() / "library"


def tones_dir() -> Path:
    """Tone artifacts: ``library_dir()/"tones"``."""
    return library_dir() / "tones"


def guitars_dir() -> Path:
    """Guitar profiles: ``library_dir()/"guitars"``."""
    return library_dir() / "guitars"


def library_irs_dir() -> Path:
    """IR artifacts: ``$HELIXGEN_IRS`` or ``library_dir()/"irs"``.

    NEW default (the old default was ``~/.helixgen/irs`` —
    see :func:`legacy_irs_dir`); not yet wired as ``ir.py``'s default.
    """
    env = os.environ.get("HELIXGEN_IRS")
    if env:
        return Path(env)
    return library_dir() / "irs"


def manifest_path() -> Path:
    """The tone-library manifest: ``$HELIXGEN_SETLISTS`` or
    ``helixgen_home()/"setlists"/"manifest.json"``.

    NEW default (the old default was ``~/.helixgen/setlists.json`` —
    see :func:`legacy_manifest_path`); not yet used by ``SetlistManifest``.
    """
    env = os.environ.get("HELIXGEN_SETLISTS")
    if env:
        return Path(env)
    return helixgen_home() / "setlists" / "manifest.json"


def legacy_manifest_path() -> Path:
    """Where the manifest lives today, pre-migration: ``helixgen_home()/"setlists.json"``."""
    return helixgen_home() / "setlists.json"


def legacy_irs_dir() -> Path:
    """Where user IRs live today, pre-migration: ``helixgen_home()/"irs"``."""
    return helixgen_home() / "irs"


def devices_dir() -> Path:
    """Per-device observed state: ``helixgen_home()/"devices"``."""
    return helixgen_home() / "devices"
