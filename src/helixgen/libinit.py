"""Bootstrap hook: make sure the helixgen home exists and is a git repo.

Every write path that puts a file under ``~/.helixgen`` (the manifest, the
block library, and — in later PRs — tone/guitar/IR metadata) calls
:func:`ensure_initialized` first. It does two things, in order:

1. ``mkdir(parents=True, exist_ok=True)`` the home directory. ``gitops``
   deliberately does NOT do this itself (its contract is "operate on an
   existing directory"; ``git -C <missing dir>`` just fails) — someone has to,
   and every caller needing the same three lines would be repetitive and
   easy to get subtly wrong (e.g. forgetting ``parents=True`` on a fresh
   install where neither ``~/.helixgen`` nor its parent exists yet).
2. :func:`helixgen.gitops.ensure_home_repo` — git-init the home (writing
   ``.gitignore`` + an initial commit) if it isn't a repo already.

Repo init is **unconditional** whenever git is on PATH; only *commits* are
gated by the ``git_commit_tones`` preference (see ``gitops.auto_commit``).

Idempotent and cheap to call from every write path: a module-level
once-per-process cache means a repeat call for a home directory already
initialized this process does no filesystem or subprocess work at all — the
common case, since a single CLI invocation may call this from several write
sites (e.g. ``generate`` auto-registering into the manifest).
"""
from __future__ import annotations

import sys
from pathlib import Path

from helixgen import gitops, home

# Homes we've already initialized in this process (keyed by the resolved
# string path passed in / defaulted). Deliberately process-lifetime, not
# persisted anywhere -- a new process re-checks once, which is cheap because
# `ensure_home_repo` itself no-ops fast when `.git` already exists.
_initialized: set[str] = set()


def _warn(message: str) -> None:
    print(f"helixgen: {message}", file=sys.stderr)


def ensure_initialized(home_dir: Path | None = None) -> None:
    """Ensure ``home_dir`` (default: :func:`helixgen.home.helixgen_home`)
    exists and is a git repo. No-op (does not raise) if the directory can't
    be created; advisory like the rest of ``gitops``. Safe and cheap to call
    on every write — repeat calls for the same home in this process return
    immediately.
    """
    target = Path(home_dir) if home_dir is not None else home.helixgen_home()
    key = str(target)
    if key in _initialized:
        return

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warn(f"could not create helixgen home {target}: {exc}")
        return

    gitops.ensure_home_repo(target)
    _initialized.add(key)
