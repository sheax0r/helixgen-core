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
    lines = text.splitlines()
    assert "devices/" in lines
    # Critical 1: WAVs are ignored outright (the default IR dir is
    # ~/.helixgen/irs/, not library/irs/ — a glob scoped to library/irs/**
    # misses it entirely).
    assert "*.wav" in lines
    assert "library/irs/**/*.wav" not in lines
    # Minor 6: the migration rename of setlists.json -> setlists.json.migrated-v2
    # must never get committed either.
    assert "*.migrated-*" in lines


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


def test_ensure_idempotent_appends_missing_lines_to_custom_gitignore(tmp_path):
    """A second call is not "hands off" any more (Important 3): it appends
    any required ignore lines the file is missing, but never touches the
    user's own content or its order."""
    home = tmp_path / "home"
    home.mkdir()
    gitops.ensure_home_repo(home)
    (home / ".gitignore").write_text("custom\n")
    gitops.ensure_home_repo(home)
    text = (home / ".gitignore").read_text()
    lines = text.splitlines()
    assert lines[0] == "custom"
    for required in ("devices/", "cache/", "tone3000/", "*.bak*", "*.wav", "*.migrated-*", "*.tmp"):
        assert required in lines


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


# ---------------------------------------------------------------------------
# Critical 2: nested-repo hijack — auto_commit must never stage/commit a
# parent repo's tree when $HELIXGEN_HOME sits inside it.
# ---------------------------------------------------------------------------


def test_auto_commit_nested_in_parent_repo_skips_and_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    # Unrelated work-in-progress living in the parent repo, sitting there
    # uncommitted -- this must never get swept up by `git -C home add -A`.
    (parent / "wip.txt").write_text("someone else's unrelated work")

    home = parent / "home"
    home.mkdir()
    assert gitops.ensure_home_repo(home) is True  # nested: counts as "is a repo"

    (home / "tone.hsp").write_text("fake preset content")
    capsys.readouterr()
    gitops.auto_commit(home, "test: should not touch parent")
    captured = capsys.readouterr()
    assert "HELIXGEN_HOME is inside another repo" in captured.err

    log = subprocess.run(
        ["git", "-C", str(parent), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert log.strip() == ""  # no commit happened at all

    status = subprocess.run(
        ["git", "-C", str(parent), "status", "--porcelain", "-uall"],
        capture_output=True, text=True,
    ).stdout
    # both files remain untracked/unstaged -- nothing was `add -A`'d
    assert "wip.txt" in status
    assert "home/tone.hsp" in status
    for line in status.splitlines():
        assert not line.startswith("A ") and not line.startswith("M ")


# ---------------------------------------------------------------------------
# Important 3: a pre-existing repo (home IS the toplevel, but wasn't created
# via ensure_home_repo) must still get the ignore rules.
# ---------------------------------------------------------------------------


def test_ensure_home_repo_existing_toplevel_without_gitignore_gets_rules(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(home)], check=True)  # pre-existing, no .gitignore
    assert gitops.ensure_home_repo(home) is True
    text = (home / ".gitignore").read_text()
    lines = text.splitlines()
    for required in ("devices/", "cache/", "tone3000/", "*.bak*", "*.wav", "*.migrated-*", "*.tmp"):
        assert required in lines


def test_ensure_home_repo_existing_toplevel_with_custom_gitignore_appends_missing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    (home / ".gitignore").write_text("mystuff/\n*.secret\n")
    assert gitops.ensure_home_repo(home) is True
    text = (home / ".gitignore").read_text()
    lines = text.splitlines()
    # user's own lines preserved, in order, untouched
    assert lines[0] == "mystuff/"
    assert "*.secret" in lines
    for required in ("devices/", "cache/", "tone3000/", "*.bak*", "*.wav", "*.migrated-*", "*.tmp"):
        assert required in lines


def test_ensure_home_repo_nested_still_leaves_gitignore_untouched(tmp_path):
    """Nested-in-parent-repo case stays fully hands-off (auto_commit already
    skips there; writing a .gitignore that would never get committed anyway
    is pointless and could surprise a parent repo's own tracked files)."""
    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    child = parent / "home"
    child.mkdir()
    assert gitops.ensure_home_repo(child) is True
    assert not (child / ".gitignore").exists()


# ---------------------------------------------------------------------------
# Important 4: git_commit_tones=false must not get an initial content commit
# (repo init + .gitignore still happen unconditionally).
# ---------------------------------------------------------------------------


def test_ensure_home_repo_no_initial_content_commit_when_pref_false(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    prefs_path = tmp_path / "prefs.json"
    prefs_path.write_text('{"git_commit_tones": false}')
    monkeypatch.setenv("HELIXGEN_PREFS", str(prefs_path))

    assert gitops.ensure_home_repo(home) is True
    assert (home / ".git").is_dir()
    assert (home / ".gitignore").exists()  # written unconditionally

    log = subprocess.run(
        ["git", "-C", str(home), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert log.strip() == ""  # no content commit


# ---------------------------------------------------------------------------
# Minor 7: leak-surface regression tests
# ---------------------------------------------------------------------------


def test_minor7a_wav_under_home_irs_untracked_after_init_and_auto_commit(tmp_path, monkeypatch):
    """Critical-1 regression: a WAV at the CURRENT default IR location
    (<home>/irs/, see helixgen.ir) must never end up in the git index."""
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    from helixgen import libinit

    home = tmp_path / "home"
    libinit.ensure_initialized(home)

    irs_dir = home / "irs"
    irs_dir.mkdir(parents=True, exist_ok=True)
    (irs_dir / "some-ir.wav").write_bytes(b"RIFF....fake wav bytes")

    gitops.auto_commit(home, "test: add a wav")

    tracked = subprocess.run(
        ["git", "-C", str(home), "ls-files"], capture_output=True, text=True
    ).stdout
    assert "irs/some-ir.wav" not in tracked


def test_minor7b_auto_commit_nested_never_touches_files_outside_home(tmp_path, monkeypatch):
    """Same scenario as the Critical-2 test above, phrased as the Minor-7(b)
    leak-surface check: a file OUTSIDE home in the parent repo stays
    uncommitted."""
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    outside_file = parent / "unrelated-parent-file.txt"
    outside_file.write_text("must never be committed by helixgen")

    home = parent / "home"
    home.mkdir()
    gitops.ensure_home_repo(home)
    (home / "tone.hsp").write_text("x")
    gitops.auto_commit(home, "test: nested skip")

    log = subprocess.run(
        ["git", "-C", str(parent), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert log.strip() == ""
    status = subprocess.run(
        ["git", "-C", str(parent), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert "unrelated-parent-file.txt" in status  # still untracked, never staged


def test_minor7c_existing_repo_without_gitignore_and_with_custom_one(tmp_path):
    """Combined check mirroring Important-3's two sub-cases in one place."""
    # (a) no .gitignore at all
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q", str(bare)], check=True)
    gitops.ensure_home_repo(bare)
    assert (bare / ".gitignore").exists()
    assert "*.wav" in (bare / ".gitignore").read_text().splitlines()

    # (b) custom .gitignore with its own lines
    custom = tmp_path / "custom"
    custom.mkdir()
    subprocess.run(["git", "init", "-q", str(custom)], check=True)
    (custom / ".gitignore").write_text("keep-me/\n")
    gitops.ensure_home_repo(custom)
    lines = (custom / ".gitignore").read_text().splitlines()
    assert lines[0] == "keep-me/"
    assert "*.wav" in lines


def test_minor7d_pref_false_no_content_commit(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    prefs_path = tmp_path / "prefs.json"
    prefs_path.write_text('{"git_commit_tones": false}')
    monkeypatch.setenv("HELIXGEN_PREFS", str(prefs_path))
    gitops.ensure_home_repo(home)
    log = subprocess.run(
        ["git", "-C", str(home), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert log.strip() == ""
