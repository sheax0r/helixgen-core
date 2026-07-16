"""Bootstrap wiring (Task 4, docs/superpowers/plans/2026-07-15-library-
metadata.md): every manifest/library write path git-initializes the helixgen
home via ``helixgen.libinit.ensure_initialized`` and, for the manifest,
advisory-commits the change via ``helixgen.gitops.auto_commit``.

Repo init is UNCONDITIONAL whenever git is present; only the *commit* is
gated by the ``git_commit_tones`` preference. Skips the whole module when
git is unavailable on PATH (matches test_gitops.py's posture).
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

import helixgen.gitops as gitops
import helixgen.libinit as libinit
from helixgen.device.manifest import SetlistManifest
from helixgen.hsp import write_hsp
from helixgen.ingest import ingest_path
from helixgen.library import Library

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available on PATH"
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """HELIXGEN_HOME is the home under test; HOME is faked (test_gitops.py's
    pattern) so a developer machine's real git identity/config/preferences
    can never leak into these tests. Also resets libinit's once-per-process
    cache so each test observes a fresh "not yet initialized" state."""
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path))
    monkeypatch.delenv("HELIXGEN_SETLISTS", raising=False)
    monkeypatch.delenv("HELIXGEN_DEVICE_SLOTS", raising=False)
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    monkeypatch.delenv("HELIXGEN_GIT_COMMIT_TONES", raising=False)
    fake_home = tmp_path / "_fake_home_for_git"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "gitconfig-does-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    libinit._initialized.clear()
    yield tmp_path
    libinit._initialized.clear()


def _git_log(home) -> str:
    return subprocess.run(
        ["git", "-C", str(home), "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout


def _write_prefs(tmp_path, monkeypatch, *, git_commit_tones) -> None:
    prefs = tmp_path / "prefs.json"
    prefs.write_text(json.dumps({"git_commit_tones": git_commit_tones}))
    monkeypatch.setenv("HELIXGEN_PREFS", str(prefs))


# ---------------------------------------------------------------------------
# SetlistManifest.save() triggers repo init + advisory commit
# ---------------------------------------------------------------------------


def test_manifest_save_on_fresh_home_creates_git_repo(tmp_path):
    manifest_path = tmp_path / "setlists" / "manifest.json"
    m = SetlistManifest(manifest_path)
    m.save()
    assert (tmp_path / ".git").is_dir()


def test_manifest_save_commits_by_default(tmp_path):
    """The very FIRST save on a fresh home folds into the repo's initial
    commit (git init's own `add -A` already picks up the just-written file --
    there's nothing left dirty for auto_commit to catch). A SECOND save (after
    a further change) is what exercises the advisory `auto_commit` call and
    produces its own "update manifest" commit."""
    manifest_path = tmp_path / "setlists" / "manifest.json"
    m = SetlistManifest(manifest_path)
    m.save()
    assert "helixgen: initialize library" in _git_log(tmp_path)

    m.tones["Placeholder"] = {"path": None, "content_hash": None,
                              "source": "authored", "slot": None}
    m.save()
    log = _git_log(tmp_path)
    assert "helixgen: update manifest" in log


def test_manifest_save_inits_repo_even_when_commits_disabled(tmp_path, monkeypatch):
    """Repo init is unconditional (git present => init); only the commit is
    gated by git_commit_tones."""
    _write_prefs(tmp_path, monkeypatch, git_commit_tones=False)
    manifest_path = tmp_path / "setlists" / "manifest.json"
    m = SetlistManifest(manifest_path)
    m.save()
    assert (tmp_path / ".git").is_dir()
    # the repo does exist and has its initial commit -- init happened
    assert "helixgen: initialize library" in _git_log(tmp_path)

    # a further change must NOT produce an "update manifest" commit
    m.tones["Placeholder"] = {"path": None, "content_hash": None,
                              "source": "authored", "slot": None}
    m.save()
    log = _git_log(tmp_path)
    assert "helixgen: update manifest" not in log
    assert log.count("\n") == 1  # still just the one (init) commit


def test_manifest_save_skips_commit_for_manifest_outside_home(
    tmp_path, tmp_path_factory, monkeypatch
):
    """A manifest resolved outside the home (e.g. an explicit
    $HELIXGEN_SETLISTS elsewhere) still gets the home git-initialized, but
    nothing is committed for it -- there's nothing of its under `home` to
    stage."""
    outside_dir = tmp_path_factory.mktemp("outside-manifest")
    outside = outside_dir / "manifest.json"
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(outside))

    m = SetlistManifest(outside)
    m.save()

    assert outside.exists()
    assert (tmp_path / ".git").is_dir()  # home still becomes a repo
    log = _git_log(tmp_path)
    assert "helixgen: update manifest" not in log


def test_manifest_save_registers_and_commits_a_tone(tmp_path):
    hsp_path = tmp_path / "t.hsp"
    write_hsp(hsp_path, {"meta": {"name": "T"}})
    manifest_path = tmp_path / "setlists" / "manifest.json"
    m = SetlistManifest(manifest_path)
    m.register_tone(hsp_path, source="authored")
    m.save()  # first save: folded into the initial commit

    on_disk = json.loads(manifest_path.read_text())
    assert on_disk["tones"]["T"]["source"] == "authored"

    m.tones["T"]["slot"] = "auto"
    m.save()  # second save: exercises auto_commit for real
    log = _git_log(tmp_path)
    assert "helixgen: update manifest" in log


# ---------------------------------------------------------------------------
# libinit.ensure_initialized: idempotent + cheap + mkdir-then-init ordering
# ---------------------------------------------------------------------------


def test_ensure_initialized_creates_missing_home_and_parents(tmp_path):
    target = tmp_path / "does" / "not" / "exist" / "yet"
    assert not target.exists()
    libinit.ensure_initialized(target)
    assert target.is_dir()
    assert (target / ".git").is_dir()


def test_ensure_initialized_is_cheap_on_repeat_calls(tmp_path, monkeypatch):
    """A module-level once-flag must prevent repeat subprocess work for a
    home already initialized in this process."""
    calls = []
    original = gitops.ensure_home_repo

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(gitops, "ensure_home_repo", counting)

    libinit.ensure_initialized(tmp_path)
    libinit.ensure_initialized(tmp_path)
    libinit.ensure_initialized(tmp_path)

    assert len(calls) == 1
    assert (tmp_path / ".git").is_dir()


def test_ensure_initialized_default_home_uses_helixgen_home(tmp_path):
    """No explicit arg -> defaults to `home.helixgen_home()`, i.e. the
    $HELIXGEN_HOME set by the fixture."""
    libinit.ensure_initialized()
    assert (tmp_path / ".git").is_dir()


# ---------------------------------------------------------------------------
# library ingest write path also triggers repo init
# ---------------------------------------------------------------------------


def test_ingest_path_triggers_repo_init(tmp_path):
    empty_source = tmp_path / "nothing-to-ingest"
    empty_source.mkdir()
    library = Library(root=tmp_path / "library")

    ingest_path(empty_source, library)

    assert (tmp_path / ".git").is_dir()
