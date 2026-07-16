"""Repo init + advisory auto-commit for the ``~/.helixgen`` home directory.

Design: ``docs/superpowers/specs/2026-07-15-library-metadata-design.md`` §2.
``~/.helixgen`` becomes a git repo so tone/IR/library artifacts get free
history; this module owns making that repo exist and committing to it.

Advisory posture throughout: every failure warns to stderr and returns a
falsy/no-op result — nothing here ever raises out to the caller. Git is a
convenience layer over the artifact files, not a required dependency.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

GITIGNORE = "devices/\ncache/\ntone3000/\n*.bak*\nlibrary/irs/**/*.wav\n"

# Fallback commit identity so `git commit` works even on a machine with no
# git user.name/user.email configured (e.g. a fresh CI box or sandbox).
_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "helixgen",
    "GIT_AUTHOR_EMAIL": "helixgen@localhost",
    "GIT_COMMITTER_NAME": "helixgen",
    "GIT_COMMITTER_EMAIL": "helixgen@localhost",
}


def _warn(message: str) -> None:
    print(f"helixgen: {message}", file=sys.stderr)


def git_available() -> bool:
    """True iff a ``git`` executable is on PATH."""
    return shutil.which("git") is not None


def _run_git(args: list[str], *, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def ensure_home_repo(home: Path) -> bool:
    """Ensure ``home`` is a git repo; return True iff it is (now) one.

    - If git is unavailable: warn, return False.
    - If ``home/.git`` exists, or ``git rev-parse --is-inside-work-tree``
      succeeds (covers ``home`` nested inside a parent repo): already a
      repo — return True without touching anything, including an existing
      ``.gitignore``.
    - Otherwise: ``git init``, write ``.gitignore``, and make the initial
      commit. Any subprocess failure along the way warns and returns False.
    """
    if not git_available():
        _warn("git not found on PATH; skipping repo init")
        return False

    if (home / ".git").exists():
        return True

    try:
        result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=home)
    except OSError as exc:
        _warn(f"git check failed: {exc}")
        return False

    if result.returncode == 0 and result.stdout.strip() == "true":
        return True

    commit_env = {**os.environ, **_GIT_IDENTITY_ENV}

    try:
        init = _run_git(["init"], cwd=home)
        if init.returncode != 0:
            _warn(f"git init failed: {init.stderr.strip()}")
            return False

        (home / ".gitignore").write_text(GITIGNORE)

        add = _run_git(["add", "-A"], cwd=home, env=commit_env)
        if add.returncode != 0:
            _warn(f"git add failed: {add.stderr.strip()}")
            return False

        commit = _run_git(
            ["commit", "-m", "helixgen: initialize library"], cwd=home, env=commit_env
        )
        if commit.returncode != 0:
            _warn(f"git commit failed: {commit.stderr.strip()}")
            return False
    except OSError as exc:
        _warn(f"git init failed: {exc}")
        return False

    return True


def _is_repo(home: Path) -> bool:
    if not git_available():
        return False
    if (home / ".git").exists():
        return True
    try:
        result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=home)
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _should_auto_commit() -> bool:
    """Resolve the ``git_commit_tones`` preference; default-safe on load failure."""
    try:
        from helixgen.preferences import load_preferences

        pref = load_preferences().git_commit_tones
    except Exception as exc:  # noqa: BLE001 - advisory: never let prefs break commits
        _warn(f"could not load preferences ({exc}); defaulting git_commit_tones=auto")
        pref = "auto"
    return pref in ("auto", "true", True)


def auto_commit(home: Path, message: str) -> None:
    """Advisory auto-commit: stage and commit everything under ``home``.

    No-op (silently) unless the ``git_commit_tones`` preference allows it
    (``"auto"``/``True`` → commit, ``"false"``/``False`` → skip) AND ``home``
    is a git repo. "Nothing to commit" (clean tree) is silent success, not a
    warning. Every other failure warns to stderr; this function never raises.
    """
    try:
        if not _should_auto_commit():
            return
        if not home.exists() or not _is_repo(home):
            return

        commit_env = {**os.environ, **_GIT_IDENTITY_ENV}

        add = _run_git(["add", "-A"], cwd=home, env=commit_env)
        if add.returncode != 0:
            _warn(f"git add failed: {add.stderr.strip()}")
            return

        commit = _run_git(["commit", "-m", message], cwd=home, env=commit_env)
        if commit.returncode != 0:
            combined = (commit.stdout + commit.stderr).lower()
            if "nothing to commit" in combined:
                return  # silent success
            _warn(f"git commit failed: {commit.stderr.strip()}")
    except Exception as exc:  # noqa: BLE001 - advisory: never raise out of auto_commit
        _warn(f"auto-commit failed: {exc}")
