"""Tests for helixgen.gitops: repo init + advisory auto-commit.

Skips the whole module when git is unavailable on PATH. Isolates HOME and
GIT_CONFIG_GLOBAL so a developer machine's git identity/config can't leak
into the tmp repos these tests create.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

import helixgen.gitops as gitops

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available on PATH"
)


@pytest.fixture(autouse=True)
def _isolated_git_env(tmp_path, monkeypatch):
    """Prevent any real user git config / global gitignore from leaking in."""
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    monkeypatch.delenv("HELIXGEN_GIT_COMMIT_TONES", raising=False)
    fake_home = tmp_path / "_fake_home_for_git"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "gitconfig-does-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


# ---------------------------------------------------------------------------
# git_available
# ---------------------------------------------------------------------------


def test_git_available_true_when_on_path():
    assert gitops.git_available() is True


def test_git_available_false_when_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert gitops.git_available() is False


# ---------------------------------------------------------------------------
# ensure_home_repo
# ---------------------------------------------------------------------------


def test_ensure_creates_repo_with_gitignore(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert gitops.ensure_home_repo(home) is True
    assert (home / ".git").is_dir()
    text = (home / ".gitignore").read_text()
    assert "library/irs/**/*.wav" in text and "devices/" in text


def test_ensure_creates_initial_commit(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    gitops.ensure_home_repo(home)
    log = subprocess.run(
        ["git", "-C", str(home), "log", "--oneline"],
        capture_output=True,
        text=True,
    ).stdout
    assert "helixgen: initialize library" in log


def test_ensure_idempotent_preserves_gitignore(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    gitops.ensure_home_repo(home)
    (home / ".gitignore").write_text("custom\n")
    gitops.ensure_home_repo(home)
    assert (home / ".gitignore").read_text() == "custom\n"


def test_ensure_idempotent_no_reinit(tmp_path):
    """A second call must not blow away or recreate the existing .git."""
    home = tmp_path / "home"
    home.mkdir()
    gitops.ensure_home_repo(home)
    git_dir_mtime_before = (home / ".git").stat().st_mtime
    result = gitops.ensure_home_repo(home)
    assert result is True
    assert (home / ".git").is_dir()
    # Not a strict guarantee re: mtime, but init would recreate HEAD etc.
    assert (home / ".git" / "HEAD").exists()
    assert git_dir_mtime_before == (home / ".git").stat().st_mtime


def test_git_missing_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    home = tmp_path / "home"
    home.mkdir()
    assert gitops.ensure_home_repo(home) is False
    assert not (home / ".git").exists()


def test_ensure_home_repo_nested_inside_existing_repo_does_not_reinit(tmp_path):
    """If a PARENT dir is already a git repo, do not git-init the child dir
    (rev-parse --is-inside-work-tree covers this) — but it still counts as
    "is a repo" for the caller.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)

    child = parent / "home"
    child.mkdir()

    assert gitops.ensure_home_repo(child) is True
    # no nested .git was created inside child
    assert not (child / ".git").exists()
    # and no .gitignore was written by ensure_home_repo (it never touched it)
    assert not (child / ".gitignore").exists()


def test_ensure_home_repo_subprocess_failure_warns_returns_false(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()

    def fake_run(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert gitops.ensure_home_repo(home) is False
    captured = capsys.readouterr()
    assert captured.err  # warned to stderr


# ---------------------------------------------------------------------------
# auto_commit
# ---------------------------------------------------------------------------


def test_auto_commit_commits_changes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HELIXGEN_PREFS", str(tmp_path / "prefs.json"))  # defaults: auto
    gitops.ensure_home_repo(home)
    (home / "f.txt").write_text("x")
    gitops.auto_commit(home, "test: change")
    log = subprocess.run(
        ["git", "-C", str(home), "log", "--oneline"],
        capture_output=True,
        text=True,
    ).stdout
    assert "test: change" in log


def test_auto_commit_respects_pref_false(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    prefs_path = tmp_path / "prefs.json"
    prefs_path.write_text('{"git_commit_tones": false}')
    monkeypatch.setenv("HELIXGEN_PREFS", str(prefs_path))
    gitops.ensure_home_repo(home)
    log_before = subprocess.run(
        ["git", "-C", str(home), "log", "--oneline"],
        capture_output=True,
        text=True,
    ).stdout
    (home / "f.txt").write_text("x")
    gitops.auto_commit(home, "test: change")
    log_after = subprocess.run(
        ["git", "-C", str(home), "log", "--oneline"],
        capture_output=True,
        text=True,
    ).stdout
    assert log_after == log_before
    assert "test: change" not in log_after


def test_auto_commit_never_raises_when_no_repo(tmp_path):
    gitops.auto_commit(tmp_path / "norepo", "x")  # no error, no dir even exists


def test_auto_commit_nothing_to_commit_is_silent(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    # No $HELIXGEN_PREFS set: falls back to the default path (which doesn't
    # exist under the fixture's fake HOME) -> defaults silently, no warning.
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    gitops.ensure_home_repo(home)
    capsys.readouterr()  # discard init noise, if any
    gitops.auto_commit(home, "test: no-op")  # clean tree, nothing changed
    captured = capsys.readouterr()
    assert captured.err == ""
    log = subprocess.run(
        ["git", "-C", str(home), "log", "--oneline"],
        capture_output=True,
        text=True,
    ).stdout
    assert "test: no-op" not in log


def test_auto_commit_pref_load_failure_treated_as_default(tmp_path, monkeypatch):
    """A malformed prefs file must not raise out of auto_commit; behaves as default 'auto'."""
    home = tmp_path / "home"
    home.mkdir()
    prefs_path = tmp_path / "prefs.json"
    prefs_path.write_text("{not valid json")
    monkeypatch.setenv("HELIXGEN_PREFS", str(prefs_path))
    gitops.ensure_home_repo(home)
    (home / "f.txt").write_text("x")
    gitops.auto_commit(home, "test: change despite bad prefs")
    log = subprocess.run(
        ["git", "-C", str(home), "log", "--oneline"],
        capture_output=True,
        text=True,
    ).stdout
    assert "test: change despite bad prefs" in log
