"""Tests for the content-addressed IR-hash cache (irhash_cache.py).

The cache wraps the expensive `compute_stadium_irhash`, keying results on
absolute-path + mtime_ns + size so unchanged WAVs are never re-hashed. Most
tests here stub the hash function, so they need no libsndfile.
"""
import json
import os
from pathlib import Path

import pytest

from helixgen import irhash_cache
from helixgen.irhash_cache import IRHASH_ALGO, IrHashCache, cached_irhash, default_cache_path


def _touch(path: Path, content: bytes = b"payload") -> Path:
    """Create a plain file (contents irrelevant — the cache only stats it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class _Spy:
    """Stand-in for compute_stadium_irhash that counts calls per path."""

    def __init__(self, value="deadbeef" * 4):
        self.value = value
        self.calls: list[str] = []

    def __call__(self, wav_path):
        self.calls.append(str(wav_path))
        return self.value


# -- cached_irhash: hit / miss ------------------------------------------------

def test_hit_does_not_recompute(tmp_path, monkeypatch):
    wav = _touch(tmp_path / "cab.wav")
    spy = _Spy("0045e64c" * 4)
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", spy)
    cache = IrHashCache.load(tmp_path / "irhash.json")

    first = cached_irhash(wav, cache=cache)
    second = cached_irhash(wav, cache=cache)

    assert first == second == "0045e64c" * 4
    assert len(spy.calls) == 1  # second call served from cache


def test_miss_then_hit_across_fresh_load(tmp_path, monkeypatch):
    wav = _touch(tmp_path / "cab.wav")
    spy = _Spy("abc123" * 5)
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", spy)
    cache_path = tmp_path / "irhash.json"

    cache = IrHashCache.load(cache_path)
    cached_irhash(wav, cache=cache)
    cache.save()

    # brand-new load from the same file must serve a hit without recompute
    reloaded = IrHashCache.load(cache_path)
    value = cached_irhash(wav, cache=reloaded)

    assert value == "abc123" * 5
    assert len(spy.calls) == 1


def test_none_cache_uses_default_path(tmp_path, monkeypatch):
    wav = _touch(tmp_path / "cab.wav")
    cache_file = tmp_path / "cache" / "irhash.json"
    monkeypatch.setenv("HELIXGEN_IRHASH_CACHE", str(cache_file))
    spy = _Spy("feedface" * 4)
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", spy)

    value = cached_irhash(wav)  # cache=None → default on-disk cache

    assert value == "feedface" * 4
    assert cache_file.exists()  # default cache persisted lazily


# -- invalidation -------------------------------------------------------------

def test_mtime_change_invalidates(tmp_path, monkeypatch):
    wav = _touch(tmp_path / "cab.wav")
    spy = _Spy("1111" * 8)
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", spy)
    cache = IrHashCache.load(tmp_path / "irhash.json")

    cached_irhash(wav, cache=cache)
    # bump mtime without changing size
    st = wav.stat()
    os.utime(wav, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    spy.value = "2222" * 8
    again = cached_irhash(wav, cache=cache)

    assert again == "2222" * 8  # recomputed and re-stored
    assert len(spy.calls) == 2
    assert cache.get(wav) == "2222" * 8


def test_size_change_invalidates(tmp_path, monkeypatch):
    wav = _touch(tmp_path / "cab.wav", b"small")
    spy = _Spy("3333" * 8)
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", spy)
    cache = IrHashCache.load(tmp_path / "irhash.json")

    cached_irhash(wav, cache=cache)
    # rewrite larger, but force mtime identical so only size differs
    st = wav.stat()
    wav.write_bytes(b"a much larger payload than before")
    os.utime(wav, ns=(st.st_atime_ns, st.st_mtime_ns))
    spy.value = "4444" * 8
    again = cached_irhash(wav, cache=cache)

    assert again == "4444" * 8
    assert len(spy.calls) == 2


# -- get() semantics ----------------------------------------------------------

def test_get_returns_none_when_file_gone(tmp_path, monkeypatch):
    wav = _touch(tmp_path / "cab.wav")
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", _Spy("5555" * 8))
    cache = IrHashCache.load(tmp_path / "irhash.json")
    cached_irhash(wav, cache=cache)

    wav.unlink()
    assert cache.get(wav) is None  # can't stat → not a hit


def test_key_is_resolved_absolute_path(tmp_path, monkeypatch):
    wav = _touch(tmp_path / "cab.wav")
    spy = _Spy("6666" * 8)
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", spy)
    cache = IrHashCache.load(tmp_path / "irhash.json")

    cached_irhash(wav, cache=cache)
    # a non-resolved path spelling of the same file must hit
    alias = tmp_path / "." / "cab.wav"
    assert cache.get(alias) == "6666" * 8
    assert len(spy.calls) == 1


# -- cold-start tolerance -----------------------------------------------------

def test_algo_mismatch_is_cold(tmp_path):
    cache_path = tmp_path / "irhash.json"
    cache_path.write_text(json.dumps({
        "version": 1,
        "algo": "some-other-algo",
        "entries": {"/x/cab.wav": {"mtime_ns": 1, "size": 1, "irhash": "old"}},
    }))
    cache = IrHashCache.load(cache_path)
    assert cache.entries == {}


def test_unknown_version_is_cold(tmp_path):
    cache_path = tmp_path / "irhash.json"
    cache_path.write_text(json.dumps({
        "version": 999,
        "algo": IRHASH_ALGO,
        "entries": {"/x/cab.wav": {"mtime_ns": 1, "size": 1, "irhash": "old"}},
    }))
    assert IrHashCache.load(cache_path).entries == {}


def test_corrupt_cache_starts_empty(tmp_path):
    cache_path = tmp_path / "irhash.json"
    cache_path.write_text("{not valid json at all")
    assert IrHashCache.load(cache_path).entries == {}


def test_missing_cache_starts_empty(tmp_path):
    assert IrHashCache.load(tmp_path / "does-not-exist.json").entries == {}


# -- atomic save --------------------------------------------------------------

def test_save_is_atomic_on_failure(tmp_path, monkeypatch):
    cache_path = tmp_path / "irhash.json"
    wav = _touch(tmp_path / "cab.wav")
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", _Spy("good" * 8))
    cache = IrHashCache.load(cache_path)
    cached_irhash(wav, cache=cache)
    cache.save()
    original = cache_path.read_bytes()

    # a failing replace must leave the existing cache untouched
    def _boom(*a, **k):
        raise OSError("simulated interrupt")

    monkeypatch.setattr(irhash_cache.os, "replace", _boom)
    other = _touch(tmp_path / "cab2.wav")
    cache.put(other, "new" * 8)
    with pytest.raises(OSError):
        cache.save()

    assert cache_path.read_bytes() == original  # not corrupted
    assert not (tmp_path / "irhash.json.tmp").exists()  # no stray temp left


def test_save_creates_parent_dir_lazily(tmp_path, monkeypatch):
    cache_path = tmp_path / "nested" / "dir" / "irhash.json"
    wav = _touch(tmp_path / "cab.wav")
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", _Spy("z" * 32))
    cache = IrHashCache.load(cache_path)
    cached_irhash(wav, cache=cache)
    cache.save()
    assert cache_path.exists()


# -- maintenance --------------------------------------------------------------

def test_prune_missing_drops_gone_files(tmp_path, monkeypatch):
    a = _touch(tmp_path / "a.wav")
    b = _touch(tmp_path / "b.wav")
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", _Spy("h" * 32))
    cache = IrHashCache.load(tmp_path / "irhash.json")
    cache.put(a, "h" * 32)
    cache.put(b, "h" * 32)

    b.unlink()
    dropped = cache.prune_missing()

    assert dropped == 1
    assert cache.get(a) == "h" * 32
    assert str(b.resolve()) not in cache.entries


def test_clear_empties_and_removes_file(tmp_path, monkeypatch):
    wav = _touch(tmp_path / "cab.wav")
    monkeypatch.setattr(irhash_cache, "compute_stadium_irhash", _Spy("c" * 32))
    cache_path = tmp_path / "irhash.json"
    cache = IrHashCache.load(cache_path)
    cached_irhash(wav, cache=cache)
    cache.save()
    assert cache_path.exists()

    cache.clear()

    assert cache.entries == {}
    assert not cache_path.exists()


# -- default_cache_path resolution -------------------------------------------

def test_default_path_honors_full_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIXGEN_IRHASH_CACHE", str(tmp_path / "custom.json"))
    monkeypatch.delenv("HELIXGEN_CACHE", raising=False)
    assert default_cache_path() == tmp_path / "custom.json"


def test_default_path_honors_cache_dir_override(monkeypatch, tmp_path):
    monkeypatch.delenv("HELIXGEN_IRHASH_CACHE", raising=False)
    monkeypatch.setenv("HELIXGEN_CACHE", str(tmp_path / "cachedir"))
    assert default_cache_path() == tmp_path / "cachedir" / "irhash.json"


def test_default_path_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("HELIXGEN_IRHASH_CACHE", raising=False)
    monkeypatch.delenv("HELIXGEN_CACHE", raising=False)
    assert default_cache_path() == Path.home() / ".helixgen" / "cache" / "irhash.json"
