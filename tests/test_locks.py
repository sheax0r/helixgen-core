"""Machine-local advisory device locks (workspace backlog #71, 0.22.0).

Offline unit suite: lease lifecycle, contention, staleness (dead pid /
expired TTL), the scope conflict matrix, token/pid passthrough, and the CLI
surface (`device lock` / `device unlock` / `--status`, per-verb
auto-acquire, `--no-lock`). No device, no network — the lock layer is pure
local filesystem state under $HELIXGEN_LOCKS.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen import locks
from helixgen.cli import cli

IP = "192.0.2.1"  # TEST-NET; never a real device


@pytest.fixture()
def root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HELIXGEN_LOCKS", str(tmp_path / "locks"))
    monkeypatch.delenv("HELIXGEN_LOCK_TOKEN", raising=False)
    monkeypatch.delenv("HELIXGEN_LOCK_TIMEOUT", raising=False)
    return tmp_path / "locks"


def lease_path(root: Path, scope: str, ip: str = IP) -> Path:
    return root / ip / f"{scope}.lock"


def write_lease(root: Path, scope: str, *, pid: int = 1, host: str | None = None,
                age: float = 0.0, ttl: float = 3600, label: str = "other-agent",
                token: str | None = None, ip: str = IP,
                kind: str = "auto") -> Path:
    """Plant a foreign lease file. pid=1 is a live process we never own."""
    p = lease_path(root, scope, ip)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "hostname": host or locks.hostname(),
            "acquired_at": time.time() - age, "ttl_seconds": ttl,
            "label": label, "kind": kind, "nonce": "planted"}
    if token is not None:
        data["token"] = token
    p.write_text(json.dumps(data))
    return p


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# --------------------------------------------------------------------------
# lease lifecycle
# --------------------------------------------------------------------------

def test_acquire_creates_lease_and_release_removes_it(root):
    with locks.acquire(IP, ("library",), label="t", timeout=0):
        p = lease_path(root, "library")
        assert p.exists()
        data = json.loads(p.read_text())
        assert data["pid"] == os.getpid()
        assert data["hostname"] == locks.hostname()
        assert data["label"] == "t"
        assert isinstance(data["acquired_at"], float)
        assert data["ttl_seconds"] > 0
    assert not p.exists()


def test_acquire_multiple_scopes_releases_all(root):
    with locks.acquire(IP, ("library", "irs"), label="t", timeout=0):
        assert lease_path(root, "library").exists()
        assert lease_path(root, "irs").exists()
    assert not lease_path(root, "library").exists()
    assert not lease_path(root, "irs").exists()


def test_release_never_removes_a_replaced_lease(root):
    """If our lease was (wrongly) broken and re-acquired by someone else,
    release must not delete the new holder's file (nonce check)."""
    handle = locks.acquire(IP, ("library",), label="t", timeout=0)
    write_lease(root, "library", label="usurper")  # overwrite behind our back
    handle.release()
    assert lease_path(root, "library").exists()
    assert json.loads(lease_path(root, "library").read_text())["label"] == "usurper"


def test_invalid_scope_rejected(root):
    with pytest.raises(ValueError):
        locks.acquire(IP, ("bogus",), label="t", timeout=0)


# --------------------------------------------------------------------------
# contention + staleness
# --------------------------------------------------------------------------

def test_contention_fail_fast_names_holder(root):
    write_lease(root, "library", label="other-agent", pid=1)
    with pytest.raises(locks.LockHeld) as e:
        locks.acquire(IP, ("library",), label="me", timeout=0)
    msg = str(e.value)
    assert "other-agent" in msg
    assert "pid 1" in msg
    assert locks.hostname() in msg
    assert "library" in msg


def test_live_foreign_lease_is_never_broken(root):
    p = write_lease(root, "library", pid=1, ttl=3600)
    before = p.read_text()
    with pytest.raises(locks.LockHeld):
        locks.acquire(IP, ("library",), label="me", timeout=0.3)
    assert p.read_text() == before


def test_expired_ttl_is_reclaimed_with_warning(root, capsys):
    write_lease(root, "library", pid=1, ttl=1, age=5)
    with locks.acquire(IP, ("library",), label="me", timeout=0):
        assert json.loads(lease_path(root, "library").read_text())["label"] == "me"
    assert "stale" in capsys.readouterr().err


def test_dead_pid_same_host_is_reclaimed(root, capsys):
    write_lease(root, "library", pid=dead_pid(), ttl=3600)
    with locks.acquire(IP, ("library",), label="me", timeout=0):
        pass
    assert "stale" in capsys.readouterr().err


def test_dead_pid_on_other_host_is_not_reclaimed(root):
    """pid liveness is only meaningful on the same host; a foreign-host lease
    is only reclaimable by TTL expiry."""
    write_lease(root, "library", pid=dead_pid(), host="elsewhere", ttl=3600)
    with pytest.raises(locks.LockHeld):
        locks.acquire(IP, ("library",), label="me", timeout=0)


def test_waiter_gets_lock_when_ttl_expires_mid_wait(root):
    write_lease(root, "library", pid=1, ttl=1.0, age=0.4)
    t0 = time.monotonic()
    with locks.acquire(IP, ("library",), label="me", timeout=10):
        assert time.monotonic() - t0 < 8
    assert not lease_path(root, "library").exists()


def test_partial_multi_scope_failure_leaves_nothing_behind(root):
    write_lease(root, "irs", pid=1)
    with pytest.raises(locks.LockHeld):
        locks.acquire(IP, ("library", "irs"), label="me", timeout=0)
    assert not lease_path(root, "library").exists()


def test_old_corrupt_lease_is_reclaimed(root, capsys):
    p = lease_path(root, "library")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json{{{")
    old = time.time() - 120
    os.utime(p, (old, old))
    with locks.acquire(IP, ("library",), label="me", timeout=0):
        pass
    assert "stale" in capsys.readouterr().err


def test_fresh_corrupt_lease_blocks(root):
    """A just-written unreadable lease may be a mid-write race — wait, don't break."""
    p = lease_path(root, "library")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json{{{")
    with pytest.raises(locks.LockHeld):
        locks.acquire(IP, ("library",), label="me", timeout=0)


def test_locks_are_per_device_ip(root):
    write_lease(root, "library", pid=1, ip="192.0.2.99")
    with locks.acquire(IP, ("library",), label="me", timeout=0):
        pass  # a lease on another device never conflicts


def test_lock_timeout_env_parsing(monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "12.5")
    assert locks.lock_timeout() == 12.5
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "not-a-number")
    assert locks.lock_timeout() == locks.DEFAULT_TIMEOUT
    monkeypatch.delenv("HELIXGEN_LOCK_TIMEOUT")
    assert locks.lock_timeout() == locks.DEFAULT_TIMEOUT


# --------------------------------------------------------------------------
# adversarial-review regressions (2026-07-16 review of PR #8)
# --------------------------------------------------------------------------

def test_owned_stale_lease_is_reclaimed_not_livelocked(root, monkeypatch):
    """Review finding 1 (CRITICAL): acquiring a scope covered by OUR OWN
    expired lease must break + re-acquire promptly, never spin forever."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-1")
    write_lease(root, "library", pid=1, token="tok-1", ttl=1, age=10,
                kind="session")
    t0 = time.monotonic()
    with locks.acquire(IP, ("library",), label="verb", timeout=0):
        assert time.monotonic() - t0 < 5
        assert json.loads(lease_path(root, "library").read_text())["label"] == "verb"


def test_post_create_verification_backs_off_the_younger_lease(root):
    """Review finding 2 (MAJOR): after creating a lease, a live foreign
    lease on a CONFLICTING file that is OLDER wins — we must back off."""
    ours = {"pid": os.getpid(), "hostname": locks.hostname(),
            "acquired_at": time.time(), "ttl_seconds": 900,
            "label": "me", "kind": "auto", "nonce": "mine"}
    older = write_lease(root, "all", pid=1, age=5)  # older -> wins
    loser = locks._post_create_conflict(IP, "library", ours, None)
    assert loser is not None and loser["label"] == "other-agent"
    older.unlink()
    write_lease(root, "all", pid=1, age=-5)  # younger than ours -> they lose
    assert locks._post_create_conflict(IP, "library", ours, None) is None


def test_all_vs_scope_create_race_yields_exactly_one_winner(root, monkeypatch):
    """Review finding 2 (MAJOR), end-to-end: two DISTINCT sessions racing
    `all` vs `library` through the scan->create gap (forced with a barrier
    inside _write_new) must never both acquire. Distinct live foreign pids
    + tokens model two separate processes (threads of one pid would own
    each other's leases by design)."""
    import threading

    sleeper = subprocess.Popen([sys.executable, "-c",
                                "import time; time.sleep(60)"])
    try:
        barrier = threading.Barrier(2, timeout=10)
        real_write_new = locks._write_new

        def gated(path, payload):
            if path.name.endswith(".lock") and ".break" not in path.name:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass  # second passes / loser retries: no more gating
            return real_write_new(path, payload)

        monkeypatch.setattr(locks, "_write_new", gated)
        results = {}

        def go(name, scope, pid, token):
            try:
                ls = locks.acquire(IP, (scope,), label=name, timeout=3,
                                   pid=pid, token=token)
                results[name] = ("ACQUIRED", ls)
            except locks.LockHeld as e:
                results[name] = ("BLOCKED", str(e))

        t1 = threading.Thread(target=go, args=("p-all", "all", 1, "t1"))
        t2 = threading.Thread(target=go,
                              args=("p-lib", "library", sleeper.pid, "t2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        acquired = [n for n, (st, _) in results.items() if st == "ACQUIRED"]
        assert len(acquired) == 1, results
        # ...and on disk there is exactly one live lease
        live = [r for r in locks.status(IP) if r["state"] == "live"]
        assert len(live) == 1, live
    finally:
        sleeper.kill()


def test_two_breakers_do_not_unlink_a_fresh_live_lease(root):
    """Review finding 3 (MAJOR): a breaker that decided 'stale' from an old
    read must re-verify under the break mutex — after a faster breaker has
    broken + re-acquired, the slow breaker must leave the fresh lease alone."""
    p = write_lease(root, "library", pid=1, ttl=1, age=10)  # stale
    stale = json.loads(p.read_text())
    # the fast breaker wins: breaks + acquires
    fast = locks.acquire(IP, ("library",), label="fast", timeout=0)
    # the slow breaker now acts on its OLD stale read
    assert locks._break_stale(lease_path(root, "library"), stale) is False
    assert json.loads(lease_path(root, "library").read_text())["label"] == "fast"
    fast.release()


def test_passthrough_does_not_renew_a_nearly_expired_lease(root, monkeypatch):
    """Review finding 5 (MAJOR): renewal at the TTL boundary (where waiters
    legitimately break + re-acquire) must not resurrect the lease — a
    nearly-expired owned lease is re-acquired fresh instead."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-1")
    write_lease(root, "library", pid=1, token="tok-1", ttl=10, age=9.5,
                kind="session")
    with locks.acquire(IP, ("library",), label="verb", timeout=0):
        data = json.loads(lease_path(root, "library").read_text())
        assert data["label"] == "verb"  # re-acquired, not renewed-in-place


def test_session_lease_with_dead_pid_gets_a_grace_not_instant_staleness(root):
    """Review finding 4 (MAJOR): a session lease taken via a short-lived
    wrapper shell (recorded pid dies immediately) keeps protecting the
    device for the grace window; only after it is it reclaimable."""
    write_lease(root, "library", pid=dead_pid(), ttl=3600, age=1,
                kind="session")
    with pytest.raises(locks.LockHeld):
        locks.acquire(IP, ("library",), label="me", timeout=0)
    write_lease(root, "library", pid=dead_pid(), ttl=3600,
                age=locks.SESSION_PID_GRACE_S + 5, kind="session")
    with locks.acquire(IP, ("library",), label="me", timeout=0):
        pass


def test_empty_json_lease_is_reclaimable_and_status_renders(root):
    """Review finding 9 (MINOR): a field-less `{}` lease must not block
    forever, and `device lock --status` must render it, not crash."""
    p = lease_path(root, "library")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    res = run_cli("device", "lock", "--status", "--ip", IP)
    assert res.exit_code == 0, res.output
    with pytest.raises(locks.LockHeld):  # fresh: blocks (mid-write grace)
        locks.acquire(IP, ("library",), label="me", timeout=0)
    old = time.time() - 120
    os.utime(p, (old, old))
    p2 = lease_path(root, "library")
    data = json.loads(p2.read_text())
    assert data == {}
    # backdate by rewriting mtime only won't move acquired_at (synthetic uses
    # mtime), so the grace has now lapsed:
    with locks.acquire(IP, ("library",), label="me", timeout=0):
        pass


def test_unlock_explicit_scopes_are_not_collapsed_by_all(root):
    """Review finding 10 (MINOR): `unlock --scope library --scope all` must
    release BOTH, not just `all`."""
    assert run_cli("device", "lock", "--scope", "library", "--label", "s",
                   "--ip", IP).exit_code == 0
    assert run_cli("device", "lock", "--scope", "all", "--label", "s",
                   "--ip", IP).exit_code == 0
    res = run_cli("device", "unlock", "--scope", "library", "--scope", "all",
                  "--ip", IP)
    assert res.exit_code == 0, res.output
    assert not lease_path(root, "library").exists()
    assert not lease_path(root, "all").exists()


def test_relock_prints_the_stored_token_and_applies_new_ttl_label(root,
                                                                  monkeypatch):
    """Review finding 7 (MINOR): a re-lock (passthrough) must print the
    token that actually opens the lease and honor the new --ttl/--label."""
    res = run_cli("device", "lock", "--scope", "library", "--label", "first",
                  "--ttl", "60", "--ip", IP)
    assert res.exit_code == 0, res.output
    tok = next(line.split("=", 1)[1] for line in res.output.splitlines()
               if line.startswith("HELIXGEN_LOCK_TOKEN="))
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", tok)
    res = run_cli("device", "lock", "--scope", "library", "--label", "second",
                  "--ttl", "7200", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert f"HELIXGEN_LOCK_TOKEN={tok}" in res.output
    data = json.loads(lease_path(root, "library").read_text())
    assert data["token"] == tok
    assert data["label"] == "second"
    assert data["ttl_seconds"] == 7200


def test_lease_files_are_private(root):
    with locks.acquire(IP, ("library",), label="t", timeout=0):
        mode = lease_path(root, "library").stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)


def test_ttl_zero_means_no_expiry(root):
    write_lease(root, "library", pid=1, ttl=0, age=10_000)
    with pytest.raises(locks.LockHeld):
        locks.acquire(IP, ("library",), label="me", timeout=0)


def test_install_auto_irs_also_locks_irs(root, monkeypatch, tmp_path):
    """Review finding 8 (MINOR): `device install --auto-irs` uploads device
    IRs, so it must hold `irs` as well as `library`."""
    from helixgen.hsp import HSP_MAGIC

    hsp = tmp_path / "t.hsp"
    hsp.write_bytes(HSP_MAGIC + b"{}")
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    write_lease(root, "irs", pid=1, label="other-agent")
    res = run_cli("device", "install", hsp, "HGTEST X", "--pos", "0",
                  "--auto-irs", "--ip", IP)
    assert res.exit_code != 0
    assert "other-agent" in res.output
    # without --auto-irs the irs lease is irrelevant (fails later, not on lock)
    res = run_cli("device", "install", hsp, "HGTEST X", "--pos", "0",
                  "--ip", IP)
    assert "other-agent" not in res.output


# --------------------------------------------------------------------------
# scope conflict matrix
# --------------------------------------------------------------------------

@pytest.mark.parametrize("held,wanted,conflict", [
    ("library", "library", True),
    ("library", "editbuffer", False),
    ("library", "irs", False),
    ("library", "globals", False),
    ("editbuffer", "editbuffer", True),
    ("editbuffer", "library", False),
    ("irs", "irs", True),
    ("globals", "globals", True),
    ("all", "library", True),
    ("all", "editbuffer", True),
    ("all", "irs", True),
    ("all", "globals", True),
    ("all", "all", True),
    ("library", "all", True),
    ("editbuffer", "all", True),
    ("irs", "all", True),
    ("globals", "all", True),
])
def test_scope_conflict_matrix(root, held, wanted, conflict):
    write_lease(root, held, pid=1)
    if conflict:
        with pytest.raises(locks.LockHeld):
            locks.acquire(IP, (wanted,), label="me", timeout=0)
    else:
        with locks.acquire(IP, (wanted,), label="me", timeout=0):
            pass


# --------------------------------------------------------------------------
# passthrough (token / pid) + renewal
# --------------------------------------------------------------------------

def test_token_passthrough_and_ttl_renewal(root, monkeypatch):
    p = write_lease(root, "library", pid=1, token="tok-1", age=100, ttl=900)
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-1")
    before = json.loads(p.read_text())["acquired_at"]
    with locks.acquire(IP, ("library",), label="verb", timeout=0):
        pass
    after = json.loads(p.read_text())
    assert after["acquired_at"] > before  # renewed on the covered verb
    assert after["label"] == "other-agent"  # the session lease survives
    assert p.exists()


def test_wrong_token_does_not_pass_through(root, monkeypatch):
    write_lease(root, "library", pid=1, token="tok-1")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-2")
    with pytest.raises(locks.LockHeld):
        locks.acquire(IP, ("library",), label="verb", timeout=0)


def test_all_lease_with_token_covers_every_scope(root, monkeypatch):
    p = write_lease(root, "all", pid=1, token="tok-1")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-1")
    for scope in locks.SCOPES:
        with locks.acquire(IP, (scope,), label="verb", timeout=0):
            assert not lease_path(root, scope).exists()  # covered, not re-acquired
    assert p.exists()


def test_same_pid_passthrough(root):
    write_lease(root, "library", pid=os.getpid())
    with locks.acquire(IP, ("library",), label="verb", timeout=0):
        pass
    assert lease_path(root, "library").exists()


def test_parent_pid_passthrough(root):
    """A session lease records the locking shell's pid; sibling CLI processes
    (same parent) pass through without the token."""
    write_lease(root, "library", pid=os.getppid())
    with locks.acquire(IP, ("library",), label="verb", timeout=0):
        pass
    assert lease_path(root, "library").exists()


# --------------------------------------------------------------------------
# CLI: device lock / unlock / --status
# --------------------------------------------------------------------------

def run_cli(*args):
    return CliRunner().invoke(cli, [str(a) for a in args])


def test_cli_lock_acquires_session_lease_and_prints_token(root):
    res = run_cli("device", "lock", "--scope", "all", "--label", "my-session",
                  "--ip", IP)
    assert res.exit_code == 0, res.output
    assert "HELIXGEN_LOCK_TOKEN=" in res.output
    data = json.loads(lease_path(root, "all").read_text())
    assert data["label"] == "my-session"
    assert data["kind"] == "session"
    assert data["pid"] == os.getppid()  # the invoking shell, not the CLI pid
    assert data["token"]


def test_cli_lock_requires_label(root):
    res = run_cli("device", "lock", "--ip", IP)
    assert res.exit_code != 0
    assert "--label" in res.output


def test_cli_lock_uses_env_token_and_is_idempotent(root, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-x")
    res = run_cli("device", "lock", "--scope", "library", "--label", "s",
                  "--ip", IP)
    assert res.exit_code == 0, res.output
    assert json.loads(lease_path(root, "library").read_text())["token"] == "tok-x"
    res = run_cli("device", "lock", "--scope", "library", "--label", "s",
                  "--ip", IP)
    assert res.exit_code == 0, res.output  # re-lock of our own lease renews


def test_cli_lock_conflicts_with_foreign_lease(root, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    write_lease(root, "all", pid=1, label="other-agent")
    res = run_cli("device", "lock", "--scope", "library", "--label", "s",
                  "--ip", IP)
    assert res.exit_code != 0
    assert "other-agent" in res.output


def test_cli_lock_status_json_shape(root):
    assert run_cli("device", "lock", "--scope", "library", "--label", "s",
                   "--ip", IP).exit_code == 0
    write_lease(root, "irs", pid=1, label="other-agent")
    res = run_cli("device", "lock", "--status", "--json", "--ip", IP)
    assert res.exit_code == 0, res.output
    rows = {r["scope"]: r for r in json.loads(res.output)}
    assert rows["library"]["label"] == "s"
    assert rows["library"]["ours"] is True
    assert rows["library"]["state"] == "live"
    assert rows["irs"]["ours"] is False
    assert {"scope", "label", "pid", "hostname", "age_seconds",
            "ttl_seconds", "state", "ours"} <= set(rows["library"])


def test_cli_lock_status_empty(root):
    res = run_cli("device", "lock", "--status", "--json", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == []


def test_cli_unlock_releases_own_leases(root):
    assert run_cli("device", "lock", "--scope", "editbuffer", "--scope",
                   "library", "--label", "s", "--ip", IP).exit_code == 0
    res = run_cli("device", "unlock", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert not lease_path(root, "editbuffer").exists()
    assert not lease_path(root, "library").exists()


def test_cli_unlock_foreign_needs_force(root):
    p = write_lease(root, "library", pid=1, label="other-agent")
    res = run_cli("device", "unlock", "--scope", "library", "--ip", IP)
    assert res.exit_code != 0
    assert "other-agent" in res.output
    assert p.exists()
    res = run_cli("device", "unlock", "--scope", "library", "--force",
                  "--ip", IP)
    assert res.exit_code == 0, res.output
    assert not p.exists()


def test_cli_unlock_without_scope_ignores_foreign(root):
    p = write_lease(root, "library", pid=1, label="other-agent")
    res = run_cli("device", "unlock", "--ip", IP)
    assert res.exit_code == 0, res.output  # nothing of ours to free; not an error
    assert p.exists()


# --------------------------------------------------------------------------
# CLI: per-verb auto-acquire
# --------------------------------------------------------------------------

@pytest.fixture()
def fake_client(monkeypatch, root):
    """Minimal fake HelixClient; records whether the editbuffer lease existed
    while the device call ran (proves acquire-around-the-verb)."""
    seen = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def load_preset(self, cid):
            seen["editbuffer_locked_during_call"] = \
                lease_path(root, "editbuffer").exists()
            return True

        def get_property_def(self, key):
            raise ValueError(f"unknown settings key {key!r}")

    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", FakeClient)
    return seen


def test_verb_auto_acquires_scope_and_releases(root, fake_client):
    res = run_cli("device", "load", "1", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert fake_client["editbuffer_locked_during_call"] is True
    assert not lease_path(root, "editbuffer").exists()


def test_verb_blocked_by_foreign_lease_names_holder(root, fake_client,
                                                    monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    write_lease(root, "editbuffer", pid=1, label="other-agent")
    res = run_cli("device", "load", "1", "--ip", IP)
    assert res.exit_code != 0
    assert "other-agent" in res.output
    assert "--no-lock" in res.output or "HELIXGEN_LOCK_TIMEOUT" in res.output


def test_verb_blocked_by_foreign_all_lease(root, fake_client, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    write_lease(root, "all", pid=1, label="other-agent")
    res = run_cli("device", "load", "1", "--ip", IP)
    assert res.exit_code != 0
    assert "other-agent" in res.output


def test_no_lock_escape_hatch(root, fake_client, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    write_lease(root, "editbuffer", pid=1, label="other-agent")
    res = run_cli("device", "load", "1", "--no-lock", "--ip", IP)
    assert res.exit_code == 0, res.output


def test_verb_passes_through_own_session_lease(root, fake_client, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-s")
    assert run_cli("device", "lock", "--scope", "all", "--label", "s",
                   "--ip", IP).exit_code == 0
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    res = run_cli("device", "load", "1", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert lease_path(root, "all").exists()  # session lease survives the verb


def test_settings_set_uses_globals_scope(root, fake_client, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    write_lease(root, "globals", pid=1, label="other-agent")
    res = run_cli("device", "settings", "set", "global.x", "1", "--ip", IP)
    assert res.exit_code != 0
    assert "other-agent" in res.output
    # an unrelated scope being held does NOT block globals verbs
    lease_path(root, "globals").unlink()
    write_lease(root, "library", pid=1, label="other-agent")
    res = run_cli("device", "settings", "set", "global.x", "1", "--ip", IP)
    assert "other-agent" not in res.output  # fails later, on the fake client
    assert "unknown settings key" in res.output


def test_ir_prune_dry_run_takes_no_lock(root, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    write_lease(root, "irs", pid=1, label="other-agent")
    import helixgen.device.maintenance as mt
    monkeypatch.setattr(mt, "ir_prune", lambda **kw: {
        "ok": True, "dry_run": True, "device_irs": 0, "referenced": [],
        "protected": [], "orphans": [], "deleted": [], "errors": [],
        "warnings": []})
    res = run_cli("device", "ir-prune", "--ip", IP)
    assert res.exit_code == 0, res.output
    # ... but the executing form is blocked
    res = run_cli("device", "ir-prune", "--yes", "--ip", IP)
    assert res.exit_code != 0
    assert "other-agent" in res.output


def test_sync_locks_library_and_irs(root, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    import helixgen.device.setlist_sync as ss
    monkeypatch.setattr(ss, "sync_setlists", lambda *a, **kw: {
        "pool": {}, "references": {}, "gc": {}, "errors": [], "setlists": []})
    write_lease(root, "irs", pid=1, label="other-agent")
    res = run_cli("device", "sync", "X", "--ip", IP)
    assert res.exit_code != 0
    assert "other-agent" in res.output
    # --exclude-irs drops the irs scope
    res = run_cli("device", "sync", "X", "--exclude-irs", "--ip", IP)
    assert res.exit_code == 0, res.output


def test_import_hss_list_mode_takes_no_lock(root, tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    write_lease(root, "library", pid=1, label="other-agent")
    bogus = tmp_path / "x.hss"
    bogus.write_bytes(b"not an hss")
    res = run_cli("device", "setlist", "import-hss", bogus, "--list",
                  "--ip", IP)
    # fails on the bogus file format, never on the lock
    assert "other-agent" not in res.output
