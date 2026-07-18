"""Unit coverage for the offline-suite real-home-untouched guard (#79k).

Exercises the ``_snapshot_tree`` primitive the session-scoped
``_real_home_untouched_guard`` fixture is built on: it must detect an added
file, a removed file, and an in-place content change, and tolerate a missing
root. The fixture itself is validated end-to-end by the full suite passing
(nothing leaks) plus the manual throwaway-write sanity check described in the
plan; here we pin the mechanism so a future refactor can't silently blind it.
"""
from __future__ import annotations

from pathlib import Path

from tests.conftest import _snapshot_tree


def test_snapshot_missing_root_is_empty(tmp_path: Path):
    assert _snapshot_tree(tmp_path / "does-not-exist") == {}


def test_snapshot_detects_added_file(tmp_path: Path):
    before = _snapshot_tree(tmp_path)
    (tmp_path / "new.json").write_text("{}")
    after = _snapshot_tree(tmp_path)
    assert before != after
    assert set(after) - set(before) == {str(tmp_path / "new.json")}


def test_snapshot_detects_removed_file(tmp_path: Path):
    f = tmp_path / "gone.json"
    f.write_text("{}")
    before = _snapshot_tree(tmp_path)
    f.unlink()
    after = _snapshot_tree(tmp_path)
    assert before != after
    assert set(before) - set(after) == {str(f)}


def test_snapshot_detects_content_change(tmp_path: Path):
    f = tmp_path / "data.json"
    f.write_text("a")
    before = _snapshot_tree(tmp_path)
    # Append so size shifts — robust regardless of mtime granularity.
    f.write_text("a-much-longer-body")
    after = _snapshot_tree(tmp_path)
    assert before != after
    assert before[str(f)] != after[str(f)]


def test_snapshot_recurses_into_subdirs(tmp_path: Path):
    sub = tmp_path / "library" / "tones"
    sub.mkdir(parents=True)
    (sub / "t.hsp").write_text("x")
    snap = _snapshot_tree(tmp_path)
    assert str(sub / "t.hsp") in snap
