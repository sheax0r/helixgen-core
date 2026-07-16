"""Content-addressed cache for expensive Stadium IR hashes.

`compute_stadium_irhash` is the hot path (two libsndfile float round-trips plus
an MD5 over a temp WAV). This module wraps it with an on-disk cache keyed by
**absolute resolved path + mtime_ns + size**, so an unchanged WAV is never
re-hashed across `register-irs`, `ir-scan`, and `irhash`.

The cache is a pure-local perf layer — deliberately separate from
`mapping.json` (the user-facing hash→wav registration binding in `ir.py`). It
holds only the hash *string*, never processed-IR bytes, and never touches the
network or the device.

Layout of the on-disk JSON (default `~/.helixgen/cache/irhash.json`)::

    {"version": 1, "algo": "stadium-irhash-v1",
     "entries": {"/abs/Cab.wav": {"mtime_ns": 171…, "size": 72154, "irhash": "0045…"}}}
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .ir import compute_stadium_irhash

# Bump this if the hash pipeline in ir.py ever changes: a running tag that
# differs from the file's tag makes the whole on-disk cache cold (recompute).
IRHASH_ALGO = "stadium-irhash-v1"

_CACHE_VERSION = 1


def default_cache_path() -> Path:
    """Resolve the cache file path from env overrides, else the home default.

    Precedence: `$HELIXGEN_IRHASH_CACHE` (full file path) >
    `$HELIXGEN_CACHE` (a cache *dir*, file is `irhash.json` within) >
    `~/.helixgen/cache/irhash.json`.
    """
    full = os.environ.get("HELIXGEN_IRHASH_CACHE")
    if full:
        return Path(full)
    cache_dir = os.environ.get("HELIXGEN_CACHE")
    if cache_dir:
        return Path(cache_dir) / "irhash.json"
    return Path.home() / ".helixgen" / "cache" / "irhash.json"


class IrHashCache:
    """On-disk, stat-validated cache of `path → irhash`.

    Load once, `put` many, `save` once for batch scans. A corrupt, missing, or
    stale-`version`/`algo` file loads as an empty cache — never raises.
    """

    def __init__(self, path: Path, entries: dict[str, dict] | None = None):
        self.path = Path(path)
        self.entries: dict[str, dict] = entries if entries is not None else {}

    @classmethod
    def load(cls, path: Path | None = None) -> "IrHashCache":
        path = Path(path) if path is not None else default_cache_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            # missing or unreadable/corrupt → cold start
            return cls(path, {})
        if not isinstance(data, dict):
            return cls(path, {})
        if data.get("version") != _CACHE_VERSION or data.get("algo") != IRHASH_ALGO:
            # a future/other pipeline wrote this — treat as cold
            return cls(path, {})
        entries = data.get("entries")
        if not isinstance(entries, dict):
            entries = {}
        return cls(path, entries)

    @staticmethod
    def _key(wav_path: Path | str) -> str:
        return str(Path(wav_path).resolve())

    def get(self, wav_path: Path | str) -> str | None:
        """Return the cached irhash iff the entry's mtime_ns+size match the
        file on disk right now; otherwise None (miss, stale, or file gone)."""
        key = self._key(wav_path)
        entry = self.entries.get(key)
        if entry is None:
            return None
        try:
            st = os.stat(key)
        except OSError:
            return None  # file vanished — not a hit
        if entry.get("mtime_ns") == st.st_mtime_ns and entry.get("size") == st.st_size:
            return entry.get("irhash")
        return None

    def put(self, wav_path: Path | str, irhash: str) -> None:
        """Record `irhash` for `wav_path`, stamped with its current stat."""
        key = self._key(wav_path)
        st = os.stat(key)
        self.entries[key] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "irhash": irhash,
        }

    def save(self) -> None:
        """Atomically write the cache (temp file + os.replace).

        Never leaves a half-written cache: on any failure before the replace,
        the temp file is removed and the existing cache is untouched.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {
            "version": _CACHE_VERSION,
            "algo": IRHASH_ALGO,
            "entries": self.entries,
        }
        try:
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def clear(self) -> None:
        """Empty the in-memory cache and remove the on-disk file."""
        self.entries = {}
        try:
            self.path.unlink()
        except OSError:
            pass

    def prune_missing(self) -> int:
        """Drop entries whose backing file no longer exists. Returns count dropped."""
        gone = [k for k in self.entries if not os.path.exists(k)]
        for k in gone:
            del self.entries[k]
        return len(gone)


def cached_irhash(wav_path: Path | str, *, cache: IrHashCache | None = None) -> str:
    """Return the Stadium irhash for `wav_path`, using the on-disk cache.

    On a stat-validated hit, returns instantly. On miss/stale, calls
    `compute_stadium_irhash`, stores the result, and (for the default cache)
    persists it. Pass an explicit `cache` for batch scans and call `save()`
    once at the end; a `None` cache uses the process-wide default file and
    saves after each miss.
    """
    own_cache = cache is None
    if own_cache:
        cache = IrHashCache.load()

    hit = cache.get(wav_path)
    if hit is not None:
        return hit

    irhash = compute_stadium_irhash(wav_path)
    cache.put(wav_path, irhash)
    if own_cache:
        cache.save()
    return irhash
