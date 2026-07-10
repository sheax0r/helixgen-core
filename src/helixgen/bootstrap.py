"""Bootstrap: clone or pull sensorium/phelix and ingest its blocks/."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from helixgen.ingest import IngestSummary, ingest_path
from helixgen.library import Library, default_cache_path


PHELIX_REPO_URL = "https://github.com/sensorium/phelix"

# A git ref we hand to `fetch`/`checkout`/`clone --branch`. Reject anything
# that could be parsed as an option (leading '-') or smuggle shell/path tricks;
# a conforming ref is a branch/tag name of the usual [A-Za-z0-9._/-] alphabet.
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _validate_ref(ref: str) -> str:
    """Reject a git ref that could be misparsed as an option or is malformed."""
    if not isinstance(ref, str) or ref.startswith("-") or not _REF_RE.match(ref):
        raise ValueError(
            f"invalid git ref {ref!r}: must match {_REF_RE.pattern} "
            f"and not start with '-'"
        )
    return ref


def clone_or_pull_phelix(cache_dir: Path, *, ref: str = "main") -> Path:
    """Clone the phelix repo into cache_dir, or fetch+checkout if it already exists."""
    _validate_ref(ref)
    cache_dir = Path(cache_dir)
    if (cache_dir / ".git").exists():
        # `ref` is validated (no leading '-'), so it cannot be misparsed as an
        # option. We do NOT add a `--` separator here: for `git checkout`, `--`
        # forces the argument to be read as a pathspec rather than a branch.
        subprocess.run(
            ["git", "-C", str(cache_dir), "fetch", "origin", ref], check=True
        )
        subprocess.run(
            ["git", "-C", str(cache_dir), "checkout", ref], check=True
        )
    else:
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--branch", ref, "--", PHELIX_REPO_URL, str(cache_dir)],
            check=True,
        )
    return cache_dir


def bootstrap(
    library: Library, *, ref: str = "main", cache_dir: Path | None = None
) -> IngestSummary:
    """Clone (or pull) phelix and ingest its blocks/ folder into the library."""
    if cache_dir is None:
        cache_dir = default_cache_path() / "phelix"
    repo = clone_or_pull_phelix(cache_dir, ref=ref)
    blocks_dir = repo / "blocks"
    if not blocks_dir.exists():
        raise FileNotFoundError(
            f"phelix repo at {repo} does not contain a 'blocks/' directory."
        )
    return ingest_path(blocks_dir, library)
