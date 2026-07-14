"""Bootstrap: clone or pull sensorium/phelix and ingest its blocks/."""
from __future__ import annotations

import subprocess
from pathlib import Path

from helixgen.ingest import IngestSummary, ingest_path
from helixgen.library import Library, default_cache_path


PHELIX_REPO_URL = "https://github.com/sensorium/phelix"


def clone_or_pull_phelix(cache_dir: Path, *, ref: str = "main") -> Path:
    """Clone the phelix repo into cache_dir, or fetch+checkout if it already exists."""
    cache_dir = Path(cache_dir)
    if (cache_dir / ".git").exists():
        subprocess.run(["git", "-C", str(cache_dir), "fetch", "origin", ref], check=True)
        subprocess.run(["git", "-C", str(cache_dir), "checkout", ref], check=True)
    else:
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--branch", ref, PHELIX_REPO_URL, str(cache_dir)],
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
