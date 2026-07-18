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

# "*.tmp": the atomic-write temp files (pid-suffixed since #79a/#83c) are
# removed on success AND on failure, but a hard kill between write and
# replace can leak one -- it must never get swept into an auto_commit.
_REQUIRED_LINES = ["devices/", "cache/", "locks/", "tone3000/", "*.bak*",
                   "*.wav", "*.migrated-*", "*.tmp"]
GITIGNORE = "".join(f"{line}\n" for line in _REQUIRED_LINES)

# Fallback commit identity, injected ONLY when the home repo has no usable git
# identity configured (e.g. a fresh CI box or sandbox with no
# user.name/user.email). When the user HAS configured an identity — via
# local/global/system git config — it is left untouched so their identity is
# used for both author and committer (#79(i)). Because GIT_AUTHOR_*/GIT_COMMITTER_*
# env vars OVERRIDE git config, injecting these unconditionally would defeat
# that, so `_commit_env` gates the injection on `_has_git_identity`.
_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "helixgen",
    "GIT_AUTHOR_EMAIL": "helixgen@localhost",
    "GIT_COMMITTER_NAME": "helixgen",
    "GIT_COMMITTER_EMAIL": "helixgen@localhost",
}


def _has_git_identity(home: Path) -> bool:
    """True iff git resolves BOTH a non-empty ``user.name`` and ``user.email``
    for ``home`` — respecting local, global and system config. Advisory: any
    failure (git missing, OSError) reports "no identity" so the fallback wins."""
    try:
        name = _run_git(["config", "user.name"], cwd=home)
        email = _run_git(["config", "user.email"], cwd=home)
    except OSError:
        return False
    return (
        name.returncode == 0 and name.stdout.strip() != ""
        and email.returncode == 0 and email.stdout.strip() != ""
    )


def _commit_env(home: Path) -> dict:
    """Environment for git add/commit against ``home``. Passes ``os.environ``
    through unchanged when the user has a usable git identity; otherwise layers
    the :data:`_GIT_IDENTITY_ENV` fallback on top so the commit still succeeds."""
    env = dict(os.environ)
    if not _has_git_identity(home):
        env.update(_GIT_IDENTITY_ENV)
    return env


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
    - If ``home`` is already a repo (``home/.git`` exists, or ``git
      rev-parse --is-inside-work-tree`` succeeds — covers ``home`` nested
      inside a parent repo):
        - If ``home`` is itself the repo's toplevel (i.e. NOT nested inside
          a parent repo), ensure ``.gitignore`` carries every required ignore
          line — writing it fresh if absent, or appending only the lines a
          pre-existing ``.gitignore`` is missing (Important 3). The user's
          own content and ordering are never touched.
        - If ``home`` is nested inside a parent repo, leave everything
          (including any ``.gitignore``) completely untouched — ``auto_commit``
          refuses to commit there anyway (Critical 2).
      Either way, return True without git-initing.
    - Otherwise: ``git init`` and write ``.gitignore`` unconditionally, then
      make the initial content commit ONLY when the ``git_commit_tones``
      preference allows it (Important 4 — "auto"/"true"; "false" skips the
      commit but still leaves the repo initialized and ignore-configured).
      Any subprocess failure along the way warns and returns False.
    """
    if not git_available():
        _warn("git not found on PATH; skipping repo init")
        return False

    if _is_repo(home):
        if _is_home_toplevel(home):
            _ensure_gitignore(home)
        return True

    try:
        init = _run_git(["init"], cwd=home)
        if init.returncode != 0:
            _warn(f"git init failed: {init.stderr.strip()}")
            return False

        (home / ".gitignore").write_text(GITIGNORE)

        if _should_auto_commit():
            commit_env = _commit_env(home)
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


def _ensure_gitignore(home: Path) -> None:
    """Ensure ``home/.gitignore`` carries every line in ``_REQUIRED_LINES``.

    Missing entirely -> write the full :data:`GITIGNORE`. Exists -> append
    only the required lines it doesn't already have, preserving the file's
    existing content and line order byte-for-byte (never reorders or dedupes
    the user's own lines). Advisory: any I/O failure warns and is otherwise
    a no-op.
    """
    path = home / ".gitignore"
    if not path.exists():
        try:
            path.write_text(GITIGNORE)
        except OSError as exc:
            _warn(f"could not write .gitignore: {exc}")
        return

    try:
        text = path.read_text()
    except OSError as exc:
        _warn(f"could not read .gitignore: {exc}")
        return

    existing = {line.strip() for line in text.splitlines()}
    missing = [line for line in _REQUIRED_LINES if line not in existing]
    if not missing:
        return

    addition = "" if text.endswith("\n") or not text else "\n"
    addition += "".join(f"{line}\n" for line in missing)
    try:
        path.write_text(text + addition)
    except OSError as exc:
        _warn(f"could not update .gitignore: {exc}")


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


def _repo_toplevel(home: Path) -> Path | None:
    """Resolved toplevel directory of the repo containing ``home``, or
    ``None`` if git is unavailable, ``home`` isn't in a repo, or the query
    fails for any other reason."""
    if not git_available():
        return None
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], cwd=home)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    if not top:
        return None
    return Path(top).resolve()


def _is_home_toplevel(home: Path) -> bool:
    """True iff ``home`` is itself the toplevel of the repo it's in — i.e.
    NOT nested inside a parent repo (Critical 2 / Important 3)."""
    top = _repo_toplevel(home)
    return top is not None and top == home.resolve()


def _should_auto_commit() -> bool:
    """Resolve the ``git_commit_tones`` preference; default-safe on load failure."""
    try:
        from helixgen.preferences import load_preferences

        pref = load_preferences().git_commit_tones
    except Exception as exc:  # noqa: BLE001 - advisory: never let prefs break commits
        _warn(f"could not load preferences ({exc}); defaulting git_commit_tones=auto")
        pref = "auto"
    return pref in ("auto", "true")


def auto_commit(home: Path, message: str) -> None:
    """Advisory auto-commit: stage and commit everything under ``home``.

    No-op (silently) unless the ``git_commit_tones`` preference allows it
    (normalized to ``"auto"``/``"true"`` → commit, ``"false"`` → skip — see
    ``preferences._validate_git_commit_tones``) AND ``home`` is a git repo.
    "Nothing to commit" (clean tree) is silent success, not a warning. Every
    other failure warns to stderr; this function never raises.

    Critical 2: if ``home`` is nested inside a PARENT git repo (rather than
    being a repo's own toplevel), ``git -C home add -A`` would stage that
    parent repo's entire tree, not just ``home``. Rather than trying to
    pathspec-scope the add, we refuse outright: warn once to stderr and
    return without touching git at all.
    """
    try:
        if not _should_auto_commit():
            return
        if not home.exists() or not _is_repo(home):
            return
        if not _is_home_toplevel(home):
            _warn("not committing: HELIXGEN_HOME is inside another repo")
            return

        commit_env = _commit_env(home)

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
