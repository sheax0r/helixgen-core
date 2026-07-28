"""Machine-local advisory device locks (workspace backlog #71, 0.22.0).

Offline unit suite: lease lifecycle, contention, staleness (dead pid /
expired TTL), the scope conflict matrix, token/pid passthrough, and the CLI
surface (`device lock` / `device unlock` / `--status`, per-verb
auto-acquire, `--no-lock`). No device, no network — the lock layer is pure
local filesystem state under $HELIXGEN_LOCKS.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
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


class _ThreadSafeErr:
    """Accumulating stderr sink for the heartbeat tests. `capsys.readouterr()`
    is seek/read/TRUNCATE on a buffer shared with the daemon heartbeat thread,
    so draining it in a poll loop can discard the one warning the test waits
    for (a 2s timeout with no clue why). Collect under a lock instead."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buf = []

    def write(self, s):
        with self._lock:
            self._buf.append(s)
        return len(s)

    def flush(self):
        pass

    @property
    def text(self) -> str:
        with self._lock:
            return "".join(self._buf)


@contextlib.contextmanager
def capture_err():
    """Swap in the sink for the duration of the block. Deliberately NOT a
    fixture: pytest's capture re-assigns sys.stderr when it resumes for the
    call phase, which would undo a swap made during fixture setup."""
    sink = _ThreadSafeErr()
    old = sys.stderr
    sys.stderr = sink
    try:
        yield sink
    finally:
        sys.stderr = old


def lease_path(root: Path, scope: str, ip: str = IP) -> Path:
    return root / ip / f"{scope}.lock"


def write_lease(root: Path, scope: str, *, pid: int = 1, host: str | None = None,
                age: float = 0.0, ttl: float = 3600, label: str = "other-agent",
                token: str | None = None, ip: str = IP,
                kind: str = "auto", pid_start: str | None = None) -> Path:
    """Plant a foreign lease file. pid=1 is a live process we never own."""
    p = lease_path(root, scope, ip)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "hostname": host or locks.hostname(),
            "acquired_at": time.time() - age, "ttl_seconds": ttl,
            "label": label, "kind": kind, "nonce": "planted"}
    if token is not None:
        data["token"] = token
    if pid_start is not None:
        data["pid_start"] = pid_start
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


# --------------------------------------------------------------------------
# (pid, start_time) lease identity (#97b Task 1)
# --------------------------------------------------------------------------

def test_lease_records_owner_pid_start_time(root):
    """A lease naming a pid also records that process's start time, so the
    pid can never be confused with a later recycled one."""
    with locks.acquire(IP, ("library",), label="me", timeout=0):
        data = json.loads(lease_path(root, "library").read_text())
    assert data["pid"] == os.getpid()
    assert data["pid_start"] == locks._pid_start(os.getpid())
    assert data["pid_start"]  # a real, non-empty value on this platform


def test_recycled_pid_reads_dead(root, capsys):
    """THE reason `pid_start` exists: a lease whose pid is alive but is now a
    DIFFERENT process (pid numbers are recycled) must read as dead, not as an
    immortal live lease."""
    # pid 1 is unmistakably alive, and just as unmistakably not the process
    # that took this lease
    write_lease(root, "library", pid=1, ttl=3600,
                pid_start="Thu Jan  1 00:00:00 1970")
    lease = locks.read_lease(lease_path(root, "library"))
    assert locks.is_stale(lease)
    with locks.acquire(IP, ("library",), label="me", timeout=0):
        assert json.loads(
            lease_path(root, "library").read_text())["label"] == "me"
    assert "stale" in capsys.readouterr().err
    # ...while the same pid WITHOUT a recorded start time still blocks
    write_lease(root, "irs", pid=1, ttl=3600)
    with pytest.raises(locks.LockHeld):
        locks.acquire(IP, ("irs",), label="me", timeout=0)


def test_matching_pid_start_is_live(root):
    """The other half: same pid, same start time = genuinely the same live
    process, so the lease is live and blocks."""
    write_lease(root, "library", pid=os.getpid(), ttl=3600,
                pid_start=locks._pid_start(os.getpid()), token="theirs")
    lease = locks.read_lease(lease_path(root, "library"))
    assert not locks.is_stale(lease)


def test_lease_without_pid_start_falls_back_to_pid_liveness(root):
    """Leases written before this field (and the acquisition meta-lock) still
    judge liveness by the pid alone — absent means 'unknown', not 'dead'."""
    write_lease(root, "library", pid=os.getpid(), ttl=3600)
    assert not locks.is_stale(locks.read_lease(lease_path(root, "library")))
    write_lease(root, "irs", pid=dead_pid(), ttl=3600)
    assert locks.is_stale(locks.read_lease(lease_path(root, "irs")))


@pytest.mark.parametrize("pid", [0, -1, 2 ** 31 - 1, None, "x"])
def test_pid_start_never_raises_on_a_bad_or_dead_pid(pid):
    """Reading a start time is a MISS, never an exception: dead pids, foreign
    pids, and nonsense all return None."""
    assert locks._pid_start(pid) is None


def test_pid_start_of_a_dead_pid_is_a_miss():
    assert locks._pid_start(dead_pid()) is None


def test_unreadable_pid_start_does_not_kill_a_live_lease(root, monkeypatch):
    """An UNREADABLE start time is not evidence of recycling: `ps` can be
    missing from $PATH (cron, slim container), time out, or lack `lstart`
    (busybox) — and os.kill already proved the pid alive. Treating that as
    death would break a live lease on a transient probe failure, with no
    grace for `kind: "pid"` — the very mid-workflow reclaim #97 exists to
    stop. The TTL still bounds it."""
    monkeypatch.setattr(locks, "_pid_start", lambda pid: None)
    write_lease(root, "library", pid=os.getpid(), ttl=3600, kind="pid",
                pid_start="Thu Jan  1 00:00:00 1970")
    assert not locks.is_stale(locks.read_lease(lease_path(root, "library")))


def test_unreadable_pid_start_still_lets_the_ttl_expire(root, monkeypatch):
    monkeypatch.setattr(locks, "_pid_start", lambda pid: None)
    write_lease(root, "library", pid=os.getpid(), ttl=60, age=90, kind="pid",
                pid_start="Thu Jan  1 00:00:00 1970")
    assert locks.is_stale(locks.read_lease(lease_path(root, "library")))


def test_pid_start_is_read_independently_of_tz_and_locale(monkeypatch):
    """`ps` renders lstart in the CALLING process's $TZ/$LC_TIME. If that
    leaked into the recorded value, the SAME live process probed from a
    differently-configured environment would compare unequal — reading as a
    recycled pid and making a live lease instantly stale."""
    monkeypatch.setenv("TZ", "UTC")
    baseline = locks._pid_start(os.getpid())
    assert baseline
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    assert locks._pid_start(os.getpid()) == baseline


def test_recycled_pid_on_other_host_is_still_not_probed(root):
    """Start times are only meaningful on the recording host; a foreign-host
    lease stays TTL-only."""
    write_lease(root, "library", pid=os.getpid(), host="elsewhere", ttl=3600,
                pid_start="Thu Jan  1 00:00:00 1970")
    assert not locks.is_stale(locks.read_lease(lease_path(root, "library")))


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


def test_distinct_ips_get_distinct_lock_dirs(root):
    """#72: the IP sanitizer must not collide distinct device identities.

    The old ``re.sub([^A-Za-z0-9._-], "_")`` mapped every disallowed char
    (``:``, ``%``, brackets, ...) to ``_`` — and ``_`` is itself allowed — so
    it was many-to-one. Distinct identities that differ only in disallowed
    characters collapsed onto the same lock directory and wrongly shared
    advisory locks. Here ``fe80::1`` and the lookalike ``fe80:_1`` both used
    to sanitize to ``fe80__1``; they must now get distinct lock dirs.
    """
    a = locks.lock_dir("fe80::1")
    b = locks.lock_dir("fe80:_1")
    assert a != b
    # and a plain IPv4 (no disallowed chars) is unchanged / readable
    assert locks.lock_dir("192.0.2.7").name == "192.0.2.7"


def test_lock_dir_is_injective_across_disallowed_chars(root):
    """A spread of identities — including ones colliding under the old rule —
    must all map to distinct lock directories."""
    ids = ["fe80::1", "fe80:_1", "fe80::1%en0", "fe80::1%en1",
           "[fe80::1]", "10.0.0.1", "10:0:0:1", "2001:db8::1"]
    names = [locks.lock_dir(x).name for x in ids]
    assert len(set(names)) == len(ids)


@pytest.mark.parametrize("ident", [".", ".."])
def test_lock_dir_never_escapes_the_locks_root(root, ident):
    """`.` and `..` pass the character filter untouched, so `--ip ..` used to
    resolve the lock dir to the locks root's PARENT (`~/.helixgen`) and write
    lease files — which carry the private lock token — outside `locks/`, the
    only path the home repo's .gitignore excludes. An auto-commit could then
    capture the token."""
    d = locks.lock_dir(ident)
    assert d.parent == locks.locks_root()
    assert d.name not in {".", ".."}
    assert locks.locks_root() in locks.lock_path(ident, "all").parents


def test_locks_root_follows_helixgen_home(monkeypatch, tmp_path):
    """$HELIXGEN_LOCKS wins; else the root derives from $HELIXGEN_HOME
    (like every other home subarea); else ~/.helixgen/locks."""
    monkeypatch.delenv("HELIXGEN_LOCKS", raising=False)
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path / "home"))
    assert locks.locks_root() == tmp_path / "home" / "locks"
    monkeypatch.setenv("HELIXGEN_LOCKS", str(tmp_path / "explicit"))
    assert locks.locks_root() == tmp_path / "explicit"
    monkeypatch.delenv("HELIXGEN_LOCKS")
    monkeypatch.delenv("HELIXGEN_HOME")
    assert locks.locks_root() == Path.home() / ".helixgen" / "locks"


def test_home_gitignore_excludes_locks():
    from helixgen import gitops

    assert "locks/" in gitops.GITIGNORE


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
    """Review finding 2 (MAJOR) / backlog #88, end-to-end: two DISTINCT
    sessions racing `all` vs `library` through the scan->create->rescan gap
    must never both acquire. Distinct live foreign pids + tokens model two
    separate processes (threads of one pid would own each other's leases by
    design).

    Deterministic reproduction. `_post_create_conflict`'s tiebreak is only
    sound if BOTH creates land before EITHER rescans; this harness forces the
    double-commit order with two ordering events, independent of the
    scheduler (see the plan's "exact double-commit interleaving" note):

        1. p-old  (`all`,     OLDER acquired_at) scans -> nothing -> reaches
           `_write_new`, signals `old_scanned`, then WAITS for the younger
           session to commit.
        2. p-young (`library`, YOUNGER acquired_at) scans -> nothing -> waits
           for `old_scanned`, then creates its lease and RESCANS `all` ->
           still ABSENT -> commits, then signals `young_committed`.
        3. p-old wakes, creates `all`, RESCANS -> sees the younger `library`
           lease -> theirs(younger) < ours(older) == False -> does NOT back
           off -> ALSO commits.

    A correct (serialized) acquisition makes p-old's SCAN observe the
    already-committed `library` lease, so it blocks -> exactly one winner. The
    event waits are BOUNDED so the fixed code (where the loser is serialized
    out and never reaches `_write_new`) falls through gracefully rather than
    deadlocking.
    """
    import threading

    base = time.time()
    old_scanned = threading.Event()
    young_committed = threading.Event()
    # Force the ages deterministically: `all` older (smaller ts) than
    # `library`, both comfortably inside their TTL so neither reads as stale.
    forced_at = {"all.lock": base - 10.0, "library.lock": base}
    real_write_new = locks._write_new
    real_pcc = locks._post_create_conflict

    def gated_write_new(path, payload):
        if path.name in forced_at:
            payload["acquired_at"] = forced_at[path.name]
            if path.name == "library.lock":      # the younger session
                old_scanned.wait(timeout=2.0)    # let p-old scan first
            else:                                 # the older `all` session
                old_scanned.set()
                young_committed.wait(timeout=2.0)  # let p-young commit first
        return real_write_new(path, payload)

    def gated_pcc(ip, scope, payload, token):
        result = real_pcc(ip, scope, payload, token)
        if scope == "library":
            young_committed.set()  # p-young has finished its create + rescan
        return result

    monkeypatch.setattr(locks, "_write_new", gated_write_new)
    monkeypatch.setattr(locks, "_post_create_conflict", gated_pcc)
    results = {}

    def go(name, scope, token):
        try:
            ls = locks.acquire(IP, (scope,), label=name, timeout=5,
                               pid=1, token=token)  # pid 1 = live, never ours
            results[name] = ("ACQUIRED", ls)
        except locks.LockHeld as e:
            results[name] = ("BLOCKED", str(e))

    t1 = threading.Thread(target=go, args=("p-old", "all", "t-old"))
    t2 = threading.Thread(target=go, args=("p-young", "library", "t-young"))
    t1.start(); t2.start(); t1.join(); t2.join()
    acquired = [n for n, (st, _) in results.items() if st == "ACQUIRED"]
    assert len(acquired) == 1, results
    # ...and on disk there is exactly one live lease
    live = [r for r in locks.status(IP) if r["state"] == "live"]
    assert len(live) == 1, live


# --------------------------------------------------------------------------
# acquisition meta-lock (backlog #88, Task 2): serializes scan->create->verify
# --------------------------------------------------------------------------

def test_acquire_meta_is_mutually_exclusive(root):
    """(a) Only one holder at a time: a second grab of a held meta-lock
    fails; once released it is grabbable again."""
    meta = locks._meta_lock_path(IP)
    assert locks._acquire_meta(meta) is not None
    assert locks._acquire_meta(meta) is None    # already held
    meta.unlink()
    assert locks._acquire_meta(meta) is not None  # free again
    meta.unlink()


def test_stale_acquire_meta_is_broken_and_acquisition_proceeds(root):
    """(b) A crashed acquirer's abandoned meta-lock (mtime past the TTL) is
    reclaimed — the first pass unlinks it, the next pass grabs it — so it can
    never wedge other acquirers. Mirrors `_break_stale`'s crashed-mutex
    reclaim (unlink, re-assess next poll)."""
    meta = locks._meta_lock_path(IP)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"pid": dead_pid(), "t": 0}))  # crashed pid
    old = time.time() - (locks._ACQUIRE_META_TTL_S + 5)
    os.utime(meta, (old, old))
    assert locks._acquire_meta(meta) is None     # reclaims (unlinks) the stale
    assert not meta.exists()
    assert locks._acquire_meta(meta) is not None  # next pass proceeds
    meta.unlink()


def test_fresh_acquire_meta_is_not_broken(root):
    """A meta-lock within its TTL is a LIVE holder — never reclaimed."""
    meta = locks._meta_lock_path(IP)
    assert locks._acquire_meta(meta) is not None  # holder grabs it now
    assert locks._acquire_meta(meta) is None     # contender must not break it
    assert meta.exists()
    meta.unlink()


def test_break_stale_meta_clears_a_crashed_breakers_mutex(root):
    """A breaker that crashed mid-reclaim leaves a `.break` mutex behind; a
    later reclaimer whose OWN `_write_new` of that mutex fails must clear the
    crashed mutex (mtime past `_BREAK_MUTEX_TTL_S`) so meta reclaim never wedges
    permanently. Mirrors `_break_stale`'s crashed-mutex handling."""
    meta = locks._meta_lock_path(IP)
    meta.parent.mkdir(parents=True, exist_ok=True)
    # A stale meta from a crashed acquirer (mtime past the TTL, dead pid).
    meta.write_text(json.dumps({"pid": dead_pid(), "t": 0, "nonce": "stale"}))
    old = time.time() - (locks._ACQUIRE_META_TTL_S + 5)
    os.utime(meta, (old, old))
    # A crashed breaker's mutex, itself past the mutex TTL.
    mpath = meta.with_name(meta.name + ".break")
    mpath.write_text(json.dumps({"pid": dead_pid(), "t": 0}))
    mold = time.time() - (locks._BREAK_MUTEX_TTL_S + 5)
    os.utime(mpath, (mold, mold))

    # First pass: our own mutex create loses to the crashed one; we clear it.
    locks._break_stale_meta(meta)
    assert not mpath.exists()  # crashed breaker's mutex cleared
    assert meta.exists()       # meta not yet reclaimed (we re-assess next pass)
    # Second pass: mutex is free, so the stale meta is now broken.
    locks._break_stale_meta(meta)
    assert not meta.exists()


def test_corrupt_meta_within_ttl_is_treated_as_live(root):
    """A meta-lock whose JSON is unparseable but whose mtime is within the TTL
    is a live holder — not reclaimed (fail-closed, matching scope leases)."""
    meta = locks._meta_lock_path(IP)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text("{not valid json")
    assert locks._meta_is_stale(meta) is False    # young → live
    assert locks._acquire_meta(meta) is None       # contender must not break it
    assert meta.exists()
    meta.unlink()


def test_corrupt_meta_past_ttl_is_reclaimed(root):
    """A corrupt meta-lock past the TTL has no readable pid, so it is judged
    stale (owner unknown/dead) and reclaimed rather than wedging acquirers."""
    meta = locks._meta_lock_path(IP)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text("{not valid json")
    old = time.time() - (locks._ACQUIRE_META_TTL_S + 5)
    os.utime(meta, (old, old))
    assert locks._meta_is_stale(meta) is True
    assert locks._acquire_meta(meta) is None       # reclaims (unlinks) the stale
    assert not meta.exists()
    assert locks._acquire_meta(meta) is not None   # next pass proceeds
    meta.unlink()


def test_meta_lock_contention_serializes_without_deadlock(root):
    """(c) Many threads hammering the meta-lock never deadlock and never both
    hold it — observed peak concurrency stays 1, and every worker returns."""
    import threading

    meta = locks._meta_lock_path(IP)
    meta.parent.mkdir(parents=True, exist_ok=True)
    state = {"now": 0, "max": 0}
    guard = threading.Lock()
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            if locks._acquire_meta(meta):
                with guard:
                    state["now"] += 1
                    state["max"] = max(state["max"], state["now"])
                time.sleep(0.001)  # widen the critical section
                with guard:
                    state["now"] -= 1
                meta.unlink(missing_ok=True)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "worker deadlocked on the meta-lock"
    assert state["max"] == 1, state  # never two holders at once


def test_release_meta_only_unlinks_its_own_nonce(root):
    """A holder whose critical section overran the TTL must not delete a fresh
    reclaimer's meta-lock: `_release_meta` unlinks only when the nonce still
    matches (#72 TOCTOU guard). Otherwise a stale holder's `finally` could
    re-open the #88 double-commit window by deleting a live meta-lock."""
    meta = locks._meta_lock_path(IP)
    stale_nonce = locks._acquire_meta(meta)          # holder A grabs it
    assert stale_nonce is not None
    # A stalls past the TTL; reclaimer B breaks + re-creates a FRESH meta-lock:
    meta.unlink()
    fresh_nonce = locks._acquire_meta(meta)
    assert fresh_nonce is not None and fresh_nonce != stale_nonce
    # A resumes and releases with its OLD nonce — must leave B's file alone
    locks._release_meta(meta, stale_nonce)
    assert meta.exists(), "release deleted a fresh reclaimer's meta-lock"
    # B's own release (current nonce) clears it
    locks._release_meta(meta, fresh_nonce)
    assert not meta.exists()


def test_acquire_blocked_by_foreign_meta_lock_raises_create_race(root):
    """A persistently-held foreign acquisition meta-lock surfaces as the
    synthetic 'create race' holder once the timeout lapses (a transient race
    artifact, not a real foreign lease), and no scope lease is created."""
    meta = locks._meta_lock_path(IP)
    meta.parent.mkdir(parents=True, exist_ok=True)
    assert locks._acquire_meta(meta) is not None     # foreign acquirer holds it
    with pytest.raises(locks.LockHeld) as exc:
        locks.acquire(IP, ("library",), label="me", timeout=0)
    assert "create race" in str(exc.value)
    assert not lease_path(root, "library").exists()
    meta.unlink()


def test_acquire_proceeds_once_foreign_meta_lock_released(root):
    """Meta contention is transient: once the foreign meta-lock is released the
    acquirer spins through and succeeds rather than erroring."""
    import threading

    meta = locks._meta_lock_path(IP)
    meta.parent.mkdir(parents=True, exist_ok=True)
    assert locks._acquire_meta(meta) is not None
    threading.Timer(0.15, lambda: meta.unlink(missing_ok=True)).start()
    with locks.acquire(IP, ("library",), label="me", timeout=5):
        assert lease_path(root, "library").exists()
    assert not meta.exists()


# --------------------------------------------------------------------------
# meta-lock stale-reclaim hardening (backlog #88 follow-up): the
# check-then-unlink TOCTOU in `_acquire_meta`'s reclaim, now fixed by the
# mutex-serialized + pid-checked `_break_stale_meta`. These two tests
# reproduce W1/W2 and assert the fix holds (Task 3 removed their xfail markers).
# --------------------------------------------------------------------------

def test_w1_live_or_young_holder_meta_is_not_broken(root):
    """W1: a meta whose owner is still ALIVE (or whose mtime is younger than
    the TTL) must not be reclaimed by a contender. Today the reclaim is a pure
    mtime check with no pid-liveness gate, so an alive holder whose critical
    section overran the TTL is wrongly broken."""
    meta = locks._meta_lock_path(IP)
    meta.parent.mkdir(parents=True, exist_ok=True)

    # (a) young mtime, dead pid: fresh -> never broken (holds today already).
    meta.write_text(json.dumps({"pid": dead_pid(), "t": 0, "nonce": "young"}))
    assert locks._acquire_meta(meta) is None
    assert meta.exists()
    meta.unlink()

    # (b) OLD mtime, ALIVE pid: an overrunning-but-live holder. Broken purely
    #     on mtime today (no pid-liveness check) -> W1. It must survive.
    meta.write_text(json.dumps({"pid": os.getpid(), "t": 0, "nonce": "live"}))
    old = time.time() - (locks._ACQUIRE_META_TTL_S + 5)
    os.utime(meta, (old, old))
    assert locks._acquire_meta(meta) is None, \
        "contender must not grab a live holder's meta"
    assert meta.exists(), "a live holder's meta was broken on mtime alone (W1)"
    meta.unlink()


def test_w2_racing_reclaimers_never_delete_a_fresh_meta(root, monkeypatch):
    """W2 (the check-then-unlink TOCTOU): a stale meta C pre-exists. Two
    acquirers both observe C as stale and both proceed to unlink it; the second
    unlink lands AFTER the first acquirer has already recreated a FRESH meta and
    entered the critical section, deleting that fresh file and letting the
    second acquirer ALSO enter. The meta's peak holder concurrency must stay 1.

    Deterministic: `Path.unlink` on the meta is gated so the first reclaimer
    parks between its staleness check and its unlink; a second acquirer then
    reclaims C and creates a fresh meta, and only then is the parked unlink
    released. Waits are bounded (a fallback timer unparks the reclaimer) so the
    fixed, serialized reclaim — where the second acquirer can never reclaim
    while the first holds the break-mutex — falls through instead of hanging."""
    import threading

    meta = locks._meta_lock_path(IP)
    meta.parent.mkdir(parents=True, exist_ok=True)
    # A stale meta from a crashed acquirer (mtime past the TTL, foreign pid).
    meta.write_text(json.dumps({"pid": dead_pid(), "t": 0, "nonce": "stale"}))
    old = time.time() - (locks._ACQUIRE_META_TTL_S + 5)
    os.utime(meta, (old, old))

    real_unlink = Path.unlink
    parked = threading.Event()    # a reclaimer reached its unlink of C
    release = threading.Event()   # let that parked unlink proceed
    armed = {"on": True}          # only the FIRST meta-file unlink parks

    def gated_unlink(self, *a, **k):
        if self.name == meta.name and armed["on"]:
            armed["on"] = False
            parked.set()
            release.wait(timeout=2.0)
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", gated_unlink)

    state = {"now": 0, "max": 0}
    guard = threading.Lock()

    def cs_hold(nonce):
        with guard:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        release.set()          # someone reached the CS -> safe to unpark
        time.sleep(0.3)        # widen the critical section for overlap
        with guard:
            state["now"] -= 1
        locks._release_meta(meta, nonce)

    def reclaim_and_hold():
        n = None
        while n is None:
            n = locks._acquire_meta(meta)
            if n is None:
                time.sleep(0.002)
        cs_hold(n)

    # Fallback so the FIXED (serialized) reclaim never deadlocks: if no acquirer
    # reaches the CS while the reclaimer is parked, unpark it anyway.
    threading.Timer(1.0, release.set).start()

    b = threading.Thread(target=reclaim_and_hold, daemon=True)
    b.start()
    parked.wait(timeout=2.0)   # b observed C stale, parked before unlinking
    a = threading.Thread(target=reclaim_and_hold, daemon=True)
    a.start()
    a.join(timeout=3.0)
    b.join(timeout=3.0)
    assert not a.is_alive() and not b.is_alive(), "reclaimer deadlocked"
    assert state["max"] == 1, \
        f"two acquirers entered the meta critical section at once (W2): {state}"


def test_meta_lock_released_after_acquire(root):
    """The acquisition meta-lock is transient — never left behind after a
    successful acquire, nor after a blocked one (both exit via the `finally`)."""
    with locks.acquire(IP, ("library",), label="me", timeout=0):
        pass
    assert not locks._meta_lock_path(IP).exists()

    write_lease(root, "library", pid=1, ttl=3600, age=1)  # live foreign holder
    with pytest.raises(locks.LockHeld):
        locks.acquire(IP, ("library",), label="me", timeout=0)
    assert not locks._meta_lock_path(IP).exists()


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
    """`device install` must hold `irs` as well as `library` — with
    --auto-irs it uploads device IRs, and even without it the IR presence
    check's wedge discriminator (confirm_ir_listed, #93) may issue a rename
    nudge, an IR-container write."""
    from helixgen.hsp import HSP_MAGIC

    hsp = tmp_path / "t.hsp"
    hsp.write_bytes(HSP_MAGIC + b"{}")
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    write_lease(root, "irs", pid=1, label="other-agent")
    res = run_cli("device", "install", hsp, "HGTEST X", "--pos", "0",
                  "--auto-irs", "--ip", IP)
    assert res.exit_code != 0
    assert "other-agent" in res.output
    # without --auto-irs too: the presence check's nudge is still a write
    res = run_cli("device", "install", hsp, "HGTEST X", "--pos", "0",
                  "--ip", IP)
    assert res.exit_code != 0
    assert "other-agent" in res.output


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

        def edit_buffer_blocks(self):
            return []

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


# --------------------------------------------------------------------------
# release / renew TOCTOU micro-windows (#72, Task 2)
# --------------------------------------------------------------------------

def test_renew_does_not_clobber_reacquired_lease(root):
    """A renewal that raced a break + re-acquire must no-op, not resurrect
    our lease on top of the new owner's."""
    path = lease_path(root, "editbuffer")
    path.parent.mkdir(parents=True, exist_ok=True)
    # our (now stale) view of the lease we intend to renew
    our_view = {"pid": os.getpid(), "hostname": locks.hostname(),
                "acquired_at": time.time() - 1000, "ttl_seconds": 900,
                "label": "me", "kind": "session", "nonce": "OURS"}
    # meanwhile the file was broken + re-acquired by another owner
    foreign = {"pid": 1, "hostname": locks.hostname(),
               "acquired_at": time.time(), "ttl_seconds": 900,
               "label": "other-agent", "kind": "session", "nonce": "THEIRS"}
    path.write_text(json.dumps(foreign))
    locks._renew(path, our_view)
    on_disk = json.loads(path.read_text())
    assert on_disk["nonce"] == "THEIRS"      # new owner's lease survives
    assert on_disk["label"] == "other-agent"


def test_release_does_not_delete_reacquired_lease(root, monkeypatch):
    """`device unlock` judges a lease stale/ours, but it gets re-acquired
    before the unlink — release must not delete the new owner's lease."""
    tok = "mytoken"
    path = lease_path(root, "editbuffer")
    path.parent.mkdir(parents=True, exist_ok=True)
    stale_ours = {"pid": os.getpid(), "hostname": locks.hostname(),
                  "acquired_at": time.time() - 10_000, "ttl_seconds": 900,
                  "label": "me", "kind": "session", "nonce": "OURS",
                  "token": tok}
    path.write_text(json.dumps(stale_ours))
    foreign = {"pid": 1, "hostname": locks.hostname(),
               "acquired_at": time.time(), "ttl_seconds": 900,
               "label": "other-agent", "kind": "session", "nonce": "THEIRS"}
    real_read = locks.read_lease
    state = {"n": 0}

    def racing_read(p):
        state["n"] += 1
        result = real_read(p)
        if state["n"] == 1 and Path(p) == path:
            # between the judge-read and the unlink, another owner re-acquires
            path.write_text(json.dumps(foreign))
        return result

    monkeypatch.setattr(locks, "read_lease", racing_read)
    with pytest.raises(locks.LockError):
        locks.release_scopes(IP, ["editbuffer"], token=tok)
    on_disk = json.loads(path.read_text())
    assert on_disk["nonce"] == "THEIRS"      # the new owner's lease survives


def test_release_removes_our_own_lease_normally(root):
    """Guard is race-only: an uncontended owned lease still releases."""
    tok = "mytoken"
    with locks.acquire(IP, ("library",), label="me", token=tok, timeout=0):
        assert lease_path(root, "library").exists()
        out = locks.release_scopes(IP, ["library"], token=tok)
    assert out["released"] == ["library"]
    assert not lease_path(root, "library").exists()


def test_release_no_scope_keeps_reacquired_lease_without_error(root, monkeypatch):
    """Bare `device unlock` (no --scope): a lease judged ours but re-acquired
    by another owner before the unlink must be KEPT (not deleted, not an
    error) — the non-explicit sibling of the explicit-scope guard, which is
    the exact clobber #72 exists to prevent."""
    tok = "mytoken"
    path = lease_path(root, "editbuffer")
    path.parent.mkdir(parents=True, exist_ok=True)
    ours = {"pid": os.getpid(), "hostname": locks.hostname(),
            "acquired_at": time.time(), "ttl_seconds": 900, "label": "me",
            "kind": "session", "nonce": "OURS", "token": tok}
    foreign = {"pid": 1, "hostname": locks.hostname(),
               "acquired_at": time.time(), "ttl_seconds": 900,
               "label": "other-agent", "kind": "session", "nonce": "THEIRS"}
    path.write_text(json.dumps(ours))
    real_read = locks.read_lease
    swapped = {"done": False}

    def racing_read(p):
        result = real_read(p)
        if Path(p) == path and not swapped["done"]:
            swapped["done"] = True  # re-acquired between judge-read and unlink
            path.write_text(json.dumps(foreign))
        return result

    monkeypatch.setattr(locks, "read_lease", racing_read)
    out = locks.release_scopes(IP, None, token=tok)  # no exception
    assert "editbuffer" not in out["released"]
    assert any(k["scope"] == "editbuffer" for k in out["kept"])
    assert json.loads(path.read_text())["nonce"] == "THEIRS"  # foreign survives


def test_session_lock_falls_back_to_fresh_acquire_when_renew_raced(root,
                                                                   monkeypatch):
    """#72: session_lock renews an owned live lease in place, but if that
    rewrite no-ops because the lease was broken + re-acquired since we read
    it (`_rewrite` returns False), it must fall back to a fresh `acquire` —
    which then contends with the new owner rather than reporting success on
    a lease it no longer holds."""
    tok = "tok-s"
    path = lease_path(root, "editbuffer")
    path.parent.mkdir(parents=True, exist_ok=True)
    ours = {"pid": os.getppid(), "hostname": locks.hostname(),
            "acquired_at": time.time(), "ttl_seconds": 900, "label": "held",
            "kind": "session", "nonce": "OURS", "token": tok}
    foreign = {"pid": 1, "hostname": locks.hostname(),
               "acquired_at": time.time(), "ttl_seconds": 900,
               "label": "other-agent", "kind": "session", "nonce": "THEIRS"}
    path.write_text(json.dumps(ours))

    def fake_rewrite(p, payload, *, expect_nonce=None):
        # model the real no-op: the lease was re-acquired, so refuse the write
        Path(p).write_text(json.dumps(foreign))
        return False

    monkeypatch.setattr(locks, "_rewrite", fake_rewrite)
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", tok)
    with pytest.raises(locks.LockHeld):
        locks.session_lock(IP, ["editbuffer"], label="s", ttl=900, timeout=0)
    assert json.loads(path.read_text())["nonce"] == "THEIRS"  # new owner's lease


# --------------------------------------------------------------------------
# #97 — agent-held session leases vs shell-lifetime pid binding
# --------------------------------------------------------------------------

def test_97_reclaimed_lease_original_token_is_not_owner(root, monkeypatch,
                                                        capsys):
    """#97 characterization: a session lease whose recorded pid is dead (the
    agent's Bash-call shell exited) is legitimately reclaimed by a contender
    after SESSION_PID_GRACE_S — and the original $HELIXGEN_LOCK_TOKEN then
    authenticates NOTHING: `owned()` is False against the contender's lease
    and a lock-taking call is an ordinary blocked newcomer. Pinning this
    prevents a later regression where a dangling token silently takes over
    someone else's lease."""
    write_lease(root, "all", pid=dead_pid(), token="tok-97", kind="session",
                age=locks.SESSION_PID_GRACE_S + 5, label="agent-workflow")
    # the contender (e.g. a live-test pytest run): no token, foreign pid
    with locks.acquire(IP, ("editbuffer",), label="live-test-suite",
                       pid=1, timeout=0):
        assert "breaking stale device lock" in capsys.readouterr().err
        assert not lease_path(root, "all").exists()  # session lease is GONE
        contender = locks.read_lease(lease_path(root, "editbuffer"))
        assert not locks.owned(contender, "tok-97")
        monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
        with pytest.raises(locks.LockHeld) as e:
            locks.acquire(IP, ("editbuffer",), label="agent-workflow",
                          timeout=0)
        assert "live-test-suite" in str(e.value)


def test_97_token_use_renews_a_dead_pid_session_lease_inside_grace(root,
                                                                   monkeypatch):
    """#97 characterization (the renewal question): renewal IS wired to
    token-authenticated use. A covered verb's acquire passes through the
    owned session lease and renews it — even with the recorded pid already
    dead — so every LOCK-TAKING call resets the SESSION_PID_GRACE_S clock.
    The observed staleness came from calls that never touch the lock layer:
    read-only verbs (`device measure`, `meters`, ...) take no lock, so a
    measure-heavy stretch performs zero renewals and the grace clock runs
    from the last mutating call."""
    p = write_lease(root, "all", pid=dead_pid(), token="tok-97",
                    kind="session", age=60, ttl=7200)
    before = json.loads(p.read_text())["acquired_at"]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with locks.acquire(IP, ("editbuffer",), label="device snapshot",
                       timeout=0):
        pass
    after = json.loads(p.read_text())
    assert after["acquired_at"] > before  # grace clock reset
    assert after["kind"] == "session" and after["token"] == "tok-97"


def test_97_past_grace_owned_covering_lease_is_not_resurrected(root,
                                                               monkeypatch):
    """#97 characterization: once the grace lapses, the pid-death check
    short-circuits renewal. A token-authenticated acquire of a granular
    scope neither passes through nor renews the now-stale owned `all`
    session lease — it creates a fresh transient lease for just its own
    scope, leaving the session's `all` file stale on disk, reclaimable by
    any contender. This silent narrowing is what let the 2026-07-27
    contender in."""
    p = write_lease(root, "all", pid=dead_pid(), token="tok-97",
                    kind="session", age=locks.SESSION_PID_GRACE_S + 5,
                    ttl=7200)
    before = json.loads(p.read_text())["acquired_at"]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with locks.acquire(IP, ("editbuffer",), label="device snapshot",
                       timeout=0):
        eb = json.loads(lease_path(root, "editbuffer").read_text())
        assert eb["kind"] == "auto"  # fresh transient lease, not the session
    assert json.loads(p.read_text())["acquired_at"] == before  # not renewed


# --------------------------------------------------------------------------
# #97 (Task 2): detached leases — a lease an agent can actually hold
# --------------------------------------------------------------------------

def test_97_detached_lease_survives_a_dead_shell(root, monkeypatch):
    """#97 fix: a DETACHED lease records no pid, so the agent's Bash-call
    shell exiting cannot make it reclaimable. Past SESSION_PID_GRACE_S — the
    exact point where the 2026-07-27 contender got in — it is still live and
    still blocks."""
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    age=locks.SESSION_PID_GRACE_S + 5,
                    ttl=locks.DEFAULT_DETACHED_TTL, label="agent-workflow")
    lease = locks.read_lease(p)
    assert not locks.is_stale(lease)
    assert "detached" in locks.describe(lease)  # no bogus "pid None"
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    with pytest.raises(locks.LockHeld) as e:
        locks.acquire(IP, ("editbuffer",), label="live-test-suite", pid=1,
                      timeout=0)
    assert "agent-workflow" in str(e.value)
    assert p.exists()


def test_97_detached_lease_is_renewed_by_token_authenticated_use(root,
                                                                 monkeypatch):
    """A token-authenticated mutating call passes through the detached lease
    and renews its TTL — so an ACTIVE workflow keeps its lease indefinitely."""
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    age=200, ttl=locks.DEFAULT_DETACHED_TTL)
    before = json.loads(p.read_text())["acquired_at"]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with locks.acquire(IP, ("editbuffer",), label="device snapshot",
                       timeout=0):
        assert not lease_path(root, "editbuffer").exists()  # passed through
    after = json.loads(p.read_text())
    assert after["acquired_at"] > before
    assert after["kind"] == "detached" and after["pid"] is None


def test_97_abandoned_detached_lease_expires_on_its_ttl(root, capsys):
    """The other half: an ABANDONED detached lease must not brick the device.
    With no pid to probe, the (short) TTL is what reclaims it."""
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    age=locks.DEFAULT_DETACHED_TTL + 5,
                    ttl=locks.DEFAULT_DETACHED_TTL, label="abandoned-agent")
    assert locks.is_stale(locks.read_lease(p))
    with locks.acquire(IP, ("editbuffer",), label="contender", pid=1,
                       timeout=0):
        assert "breaking stale device lock" in capsys.readouterr().err
        assert not p.exists()


def test_cli_lock_detach_records_no_pid_and_a_short_ttl(root):
    res = run_cli("device", "lock", "--scope", "all", "--detach",
                  "--label", "agent-workflow", "--ip", IP)
    assert res.exit_code == 0, res.output
    data = json.loads(lease_path(root, "all").read_text())
    assert data["kind"] == "detached"
    assert data["pid"] is None
    assert data["ttl_seconds"] == locks.DEFAULT_DETACHED_TTL
    assert locks.DEFAULT_DETACHED_TTL < locks.DEFAULT_SESSION_TTL
    assert "HELIXGEN_LOCK_TOKEN=" in res.output


def test_cli_lock_detach_refuses_ttl_zero(root):
    """No pid AND no TTL = a lease only --force could ever clear."""
    res = run_cli("device", "lock", "--detach", "--ttl", "0", "--label", "s",
                  "--ip", IP)
    assert res.exit_code != 0
    assert "--detach" in res.output
    assert not lease_path(root, "all").exists()


def test_cli_lock_detach_relock_keeps_it_detached(root, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-x")
    assert run_cli("device", "lock", "--scope", "library", "--detach",
                   "--label", "s", "--ip", IP).exit_code == 0
    assert run_cli("device", "lock", "--scope", "library", "--detach",
                   "--label", "s2", "--ip", IP).exit_code == 0
    data = json.loads(lease_path(root, "library").read_text())
    assert data["kind"] == "detached" and data["pid"] is None
    assert data["label"] == "s2" and data["token"] == "tok-x"


def test_cli_unlock_releases_a_detached_lease_with_the_token(root, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-x")
    assert run_cli("device", "lock", "--scope", "all", "--detach", "--label",
                   "agent", "--ip", IP).exit_code == 0
    res = run_cli("device", "unlock", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert not lease_path(root, "all").exists()


def test_cli_lock_status_renders_detached_leases(root):
    assert run_cli("device", "lock", "--scope", "irs", "--detach", "--label",
                   "agent", "--ip", IP).exit_code == 0
    res = run_cli("device", "lock", "--status", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert "detached" in res.output and "pid None" not in res.output


# --------------------------------------------------------------------------
# #97b (Task 2): `--pid <pid>` — a lease bound to a process the CALLER names,
# one that spans the whole workflow (an agent passes $PPID). Liveness is
# decidable, so the dead-shell grace that let the 2026-07-27 contender in
# does not apply here.
# --------------------------------------------------------------------------

def test_cli_lock_pid_records_kind_pid_with_a_start_time(root):
    res = run_cli("device", "lock", "--scope", "all", "--pid", os.getpid(),
                  "--label", "agent-workflow", "--ip", IP)
    assert res.exit_code == 0, res.output
    data = json.loads(lease_path(root, "all").read_text())
    assert data["kind"] == "pid"
    assert data["pid"] == os.getpid()
    assert data["pid_start"] == locks._pid_start(os.getpid())
    assert "HELIXGEN_LOCK_TOKEN=" in res.output


def test_cli_lock_prints_the_guidance_that_matches_the_kind(root):
    """The post-lock stderr epilogue is agent-facing operating instruction:
    each kind must get ITS guidance. The legacy one ("run from your long-lived
    shell … dead parent gives contenders a reclaim path after 120s") is wrong
    for both new kinds — no grace applies to `pid`, and `detached` has no pid
    at all — and is precisely what #97b exists to retract."""
    pid_res = run_cli("device", "lock", "--scope", "editbuffer", "--pid",
                      os.getpid(), "--label", "a", "--ip", IP)
    assert f"bound to pid {os.getpid()}" in pid_res.output
    assert "long-lived shell" not in pid_res.output  # the retracted guidance

    det_res = run_cli("device", "lock", "--scope", "irs", "--detach",
                      "--label", "a", "--ip", IP)
    assert "DETACHED (no pid)" in det_res.output

    plain_res = run_cli("device", "lock", "--scope", "library", "--label",
                        "a", "--ip", IP)
    assert "--pid $PPID" in plain_res.output  # steers to the agent mechanism
    assert "DETACHED" not in plain_res.output


def test_cli_lock_relock_rotates_the_nonce(root, monkeypatch):
    """Re-locking REPLACES the lease, so it must take a new identity. Sharing
    the old nonce lets a still-running verb's LeaseSet.release() — which
    unlinks by nonce — delete the session lease that replaced its transient
    one, leaving the device silently unlocked."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-x")
    assert run_cli("device", "lock", "--scope", "library", "--label", "a",
                   "--ip", IP).exit_code == 0
    before = json.loads(lease_path(root, "library").read_text())["nonce"]
    assert run_cli("device", "lock", "--scope", "library", "--pid",
                   os.getpid(), "--label", "a", "--ip", IP).exit_code == 0
    after = json.loads(lease_path(root, "library").read_text())
    assert after["nonce"] != before
    assert after["kind"] == "pid" and after["token"] == "tok-x"


def test_97b_transient_release_cannot_delete_the_session_lease(root,
                                                               monkeypatch):
    """End to end for the nonce rotation: a verb holds a transient `auto`
    lease under the session token; the agent re-locks that scope with --pid
    mid-flight; the verb exiting must not take the new lease with it."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-x")
    leases = locks.acquire(IP, ("library",), label="a verb", token="tok-x")
    assert run_cli("device", "lock", "--scope", "library", "--pid",
                   os.getpid(), "--label", "agent", "--ip", IP).exit_code == 0
    leases.release()
    data = json.loads(lease_path(root, "library").read_text())
    assert data["kind"] == "pid" and data["label"] == "agent"


def test_cli_lock_pid_uses_the_session_ttl(root):
    """Settled decision 5: the TTL demotes to a backstop for a --pid lease
    (liveness is the real check), so it keeps DEFAULT_SESSION_TTL rather than
    the shorter detached default."""
    assert run_cli("device", "lock", "--scope", "all", "--pid", os.getpid(),
                   "--label", "a", "--ip", IP).exit_code == 0
    data = json.loads(lease_path(root, "all").read_text())
    assert data["ttl_seconds"] == locks.DEFAULT_SESSION_TTL


def test_cli_lock_pid_and_detach_are_mutually_exclusive(root):
    res = run_cli("device", "lock", "--pid", os.getpid(), "--detach",
                  "--label", "a", "--ip", IP)
    assert res.exit_code != 0
    assert "--pid" in res.output and "--detach" in res.output
    assert not lease_path(root, "all").exists()


def test_cli_lock_pid_refuses_a_dead_owner(root):
    """A lease for a dead pid is stale the moment it is written — always a
    caller bug, so it is refused at acquisition rather than written and
    reclaimed a beat later."""
    res = run_cli("device", "lock", "--pid", dead_pid(), "--label", "a",
                  "--ip", IP)
    assert res.exit_code != 0
    assert "not a live process" in res.output
    assert not lease_path(root, "all").exists()


def test_cli_lock_pid_relock_rebinds_the_pid(root, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-x")
    assert run_cli("device", "lock", "--scope", "library", "--detach",
                   "--label", "s", "--ip", IP).exit_code == 0
    assert run_cli("device", "lock", "--scope", "library", "--pid",
                   os.getpid(), "--label", "s2", "--ip", IP).exit_code == 0
    data = json.loads(lease_path(root, "library").read_text())
    assert data["kind"] == "pid" and data["pid"] == os.getpid()
    assert data["pid_start"] == locks._pid_start(os.getpid())
    assert data["token"] == "tok-x"


def test_97b_pid_lease_survives_the_shell_that_took_it(root):
    """The #97 case: the tool call's shell is long gone, but the pid the
    lease names (the agent process) is alive, so the lease still blocks well
    past SESSION_PID_GRACE_S."""
    # pid 1 stands in for the foreign agent process: alive, and not us
    p = write_lease(root, "all", pid=1, kind="pid",
                    pid_start=locks._pid_start(1),
                    age=locks.SESSION_PID_GRACE_S + 5,
                    ttl=locks.DEFAULT_SESSION_TTL, label="agent-workflow")
    assert not locks.is_stale(locks.read_lease(p))
    with pytest.raises(locks.LockHeld) as e:
        locks.acquire(IP, ("editbuffer",), label="contender", pid=1, timeout=0)
    assert "agent-workflow" in str(e.value)


def test_97b_dead_pid_lease_is_reclaimable_immediately(root, capsys):
    """Settled decision 4: no SESSION_PID_GRACE_S for kind 'pid'. The owner
    was chosen by the caller, so its death is conclusive — a fresh lease
    (age 0) whose pid is dead is stale right now."""
    p = write_lease(root, "all", pid=dead_pid(), kind="pid", age=0,
                    ttl=locks.DEFAULT_SESSION_TTL, label="crashed-agent")
    assert locks.is_stale(locks.read_lease(p))
    with locks.acquire(IP, ("editbuffer",), label="contender", pid=1,
                       timeout=0):
        assert "breaking stale device lock" in capsys.readouterr().err
        assert not p.exists()


def test_97b_dead_session_lease_keeps_its_grace(root):
    """...and the legacy kind is UNCHANGED: a session lease records whatever
    shell took it (possibly a short-lived wrapper), so a dead pid only counts
    after the grace."""
    p = write_lease(root, "all", pid=dead_pid(), kind="session", age=0,
                    ttl=locks.DEFAULT_SESSION_TTL, label="wrapper-session")
    assert not locks.is_stale(locks.read_lease(p))
    p = write_lease(root, "irs", pid=dead_pid(), kind="session",
                    age=locks.SESSION_PID_GRACE_S + 5,
                    ttl=locks.DEFAULT_SESSION_TTL, label="wrapper-session")
    assert locks.is_stale(locks.read_lease(p))


def test_97b_recycled_pid_lease_reads_dead(root):
    """The Task 1 guard applies to the kind it exists for: a --pid lease whose
    pid is alive but is now a DIFFERENT process is dead, not immortal."""
    p = write_lease(root, "all", pid=1, kind="pid", age=0,
                    pid_start="Thu Jan  1 00:00:00 1970",
                    ttl=locks.DEFAULT_SESSION_TTL, label="recycled")
    assert locks.is_stale(locks.read_lease(p))


def test_97b_pid_lease_is_renewed_by_token_authenticated_use(root, monkeypatch):
    p = write_lease(root, "all", pid=os.getpid(), kind="pid", token="tok-97b",
                    pid_start=locks._pid_start(os.getpid()), age=200,
                    ttl=locks.DEFAULT_SESSION_TTL)
    before = json.loads(p.read_text())["acquired_at"]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97b")
    with locks.acquire(IP, ("editbuffer",), label="device snapshot",
                       timeout=0):
        assert not lease_path(root, "editbuffer").exists()  # passed through
    after = json.loads(p.read_text())
    assert after["acquired_at"] > before
    assert after["kind"] == "pid" and after["pid"] == os.getpid()


# --------------------------------------------------------------------------
# #97b (Task 3): all three lease kinds must RENDER unambiguously — in
# `--status`, in its JSON, and in the error a blocked contender reads. "held
# by 'agent' (pid 50754, alive)" tells you to wait; "locked" tells you nothing.
# --------------------------------------------------------------------------

def test_97b_describe_renders_each_kind_unambiguously(root):
    live = locks.read_lease(write_lease(root, "all", pid=os.getpid(),
                                        kind="pid", label="agent",
                                        pid_start=locks._pid_start(os.getpid())))
    dead = locks.read_lease(write_lease(root, "irs", pid=dead_pid(),
                                        kind="pid", label="crashed"))
    session = locks.read_lease(write_lease(root, "library", pid=os.getpid(),
                                           kind="session", label="shell"))
    detached = locks.read_lease(write_lease(root, "globals", pid=None,
                                            kind="detached", label="cron"))
    assert f"pid {os.getpid()}, alive" in locks.describe(live)
    assert f"pid {dead['pid']}, dead" in locks.describe(dead)
    # the legacy kind is unchanged: pid, no liveness claim
    assert f"pid {os.getpid()}" in locks.describe(session)
    assert "alive" not in locks.describe(session)
    assert "detached" in locks.describe(detached)
    assert "pid" not in locks.describe(detached)


def test_97b_describe_claims_no_liveness_for_a_foreign_host_lease(root):
    """A pid on ANOTHER host is not probeable — the TTL is all we have there,
    so the line must not assert 'alive' or 'dead' about it."""
    lease = locks.read_lease(write_lease(root, "all", pid=os.getpid(),
                                         kind="pid", host="other-host",
                                         pid_start="whenever"))
    line = locks.describe(lease)
    assert f"pid {os.getpid()}" in line
    assert "alive" not in line and "dead" not in line


def test_97b_status_reports_pid_liveness(root):
    write_lease(root, "all", pid=os.getpid(), kind="pid",
                pid_start=locks._pid_start(os.getpid()))
    write_lease(root, "irs", pid=dead_pid(), kind="pid")
    write_lease(root, "library", pid=os.getpid(), kind="session")
    write_lease(root, "globals", pid=None, kind="detached")
    rows = {r["scope"]: r for r in locks.status(IP)}
    assert rows["all"]["pid_alive"] is True
    assert rows["irs"]["pid_alive"] is False
    # only kind 'pid' claims liveness; the other kinds report none
    assert rows["library"]["pid_alive"] is None
    assert rows["globals"]["pid_alive"] is None and rows["globals"]["pid"] is None


def test_97b_cli_status_shows_kind_and_liveness(root):
    write_lease(root, "all", pid=os.getpid(), kind="pid", label="agent",
                pid_start=locks._pid_start(os.getpid()))
    write_lease(root, "globals", pid=None, kind="detached", label="cron")
    res = run_cli("device", "lock", "--status", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert f"pid {os.getpid()} alive" in res.output
    assert "detached" in res.output


def test_97b_a_dead_pid_lease_of_ours_is_a_PROVEN_lost_session(root,
                                                              monkeypatch):
    """Reconciliation: a lapsed lease file of ours on disk is proof the scope
    WAS ours — and that holds for the pid kind too. A --pid lease whose owner
    died leaves exactly such a file, so the verb must get the full-strength
    'stop and re-establish' message, not a silent pass."""
    # the session is demonstrably alive (a live lease on another scope), so
    # only the lapsed file distinguishes "lost it" from "never held it"
    write_lease(root, "irs", pid=os.getpid(), token="tok-97b", kind="pid",
                pid_start=locks._pid_start(os.getpid()),
                ttl=locks.DEFAULT_SESSION_TTL, label="agent")
    write_lease(root, "library", pid=dead_pid(), token="tok-97b", kind="pid",
                age=0, ttl=locks.DEFAULT_SESSION_TTL, label="agent")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97b")
    with pytest.raises(locks.LockLost) as e:
        locks.check_session(IP, ("library",), strict=True)
    assert e.value.proven is True
    assert "do NOT retry" in str(e.value)


def test_97b_contender_error_names_the_kind_it_is_up_against(root):
    p = write_lease(root, "all", pid=1, kind="pid", label="agent",
                    pid_start=locks._pid_start(1),
                    age=locks.SESSION_PID_GRACE_S + 5,
                    ttl=locks.DEFAULT_SESSION_TTL)
    with pytest.raises(locks.LockHeld) as e:
        locks.acquire(IP, ("editbuffer",), label="contender", pid=1, timeout=0)
    assert "'agent' (pid 1, alive" in str(e.value)
    assert p.exists()


# --------------------------------------------------------------------------
# #97 (Task 3): a presented-but-dangling token must fail loudly — read-only
# verbs included. Setting $HELIXGEN_LOCK_TOKEN declares "I am in a held
# session"; when it opens no live lease for the scope a verb touches, that
# session is gone and proceeding unlocked is how the 2026-07-27 workflow got
# real-looking numbers for a snapshot it had been REFUSED permission to select.
# --------------------------------------------------------------------------

def test_97_check_session_is_a_noop_without_a_token(root):
    """No token = today's behavior exactly: unlocked reads stay free, even
    with a foreign lease held."""
    write_lease(root, "editbuffer", pid=1, label="other-agent")
    locks.check_session(IP, ("editbuffer",))


def test_97_check_session_passes_with_a_live_covering_lease(root, monkeypatch):
    write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                ttl=locks.DEFAULT_DETACHED_TTL, label="agent-workflow")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    locks.check_session(IP, ("editbuffer", "irs"))


def test_97_check_session_passes_for_a_scope_outside_a_narrow_lease(
        root, monkeypatch):
    """Holding `library` and touching `editbuffer` is NOT a lost session — the
    token demonstrably opens a live lease, the other scope is simply free.
    Raising here would reduce the whole scope vocabulary to `--scope all`:
    `device sync` (library+irs) could not run under a `library` lease, and
    every read of another scope would hard-fail with a message claiming a
    reclaim that never happened."""
    write_lease(root, "library", pid=None, token="tok-97", kind="detached",
                ttl=locks.DEFAULT_DETACHED_TTL, label="agent-workflow")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    locks.check_session(IP, ("editbuffer",))
    locks.check_session(IP, ("library", "irs"))


def test_97_narrow_lease_holder_still_acquires_other_scopes(root, fake_client,
                                                            monkeypatch):
    """End to end: a `library` lease holder runs a verb that locks
    `editbuffer`. It must take that scope transiently, not be refused."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-x")
    assert run_cli("device", "lock", "--scope", "library", "--detach",
                   "--label", "agent", "--ip", IP).exit_code == 0
    res = run_cli("device", "load", "1", "--ip", IP)
    assert res.exit_code == 0, res.output
    # and it really TOOK that scope — exiting 0 alone would also pass if the
    # verb had run with no lease at all
    assert fake_client["editbuffer_locked_during_call"] is True


def test_97_narrow_lease_holder_contends_normally_not_lockheld_as_lost(
        root, fake_client, monkeypatch):
    """And when that other scope IS held by someone else, it is ordinary
    contention (waited on, reported as locked) — not a lost session."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-x")
    assert run_cli("device", "lock", "--scope", "library", "--detach",
                   "--label", "agent", "--ip", IP).exit_code == 0
    write_lease(root, "editbuffer", pid=1, label="other-agent")
    monkeypatch.setenv("HELIXGEN_LOCK_TIMEOUT", "0")
    res = run_cli("device", "load", "1", "--ip", IP)
    assert res.exit_code != 0
    assert "is locked by" in res.output and "other-agent" in res.output
    assert "reclaimed" not in res.output


def test_97_check_session_renews_the_covering_lease(root, monkeypatch):
    """A read-only call is proof its session is alive, so it renews — the
    Task 1 root cause was that a measure-only stretch renewed nothing and
    lost the lease it was holding."""
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    age=200, ttl=locks.DEFAULT_DETACHED_TTL)
    before = json.loads(p.read_text())["acquired_at"]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    locks.check_session(IP, ("editbuffer",))
    assert json.loads(p.read_text())["acquired_at"] > before


def test_97_read_only_verb_renews_the_lease_it_holds(root, fake_client,
                                                     monkeypatch):
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    age=200, ttl=locks.DEFAULT_DETACHED_TTL)
    before = json.loads(p.read_text())["acquired_at"]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    assert run_cli("device", "blocks", "--ip", IP).exit_code == 0
    assert json.loads(p.read_text())["acquired_at"] > before


@pytest.mark.parametrize("ttl", [0, -1, -0.5])
def test_97_session_lock_refuses_a_detached_lease_with_no_expiry(root, ttl):
    """The invariant lives in the lock layer, not only in the CLI flag — and
    a NEGATIVE ttl is 'no expiry' to _remaining_ttl exactly like 0, so it
    would leave the same unclearable pid-less lease."""
    with pytest.raises(ValueError):
        locks.session_lock(IP, ["all"], label="agent", ttl=ttl, detach=True)
    assert not lease_path(root, "all").exists()


def test_97_check_session_raises_naming_the_holder_that_reclaimed_it(
        root, monkeypatch):
    write_lease(root, "editbuffer", pid=1, label="live-test-suite")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with pytest.raises(locks.LockLost) as e:
        locks.check_session(IP, ("editbuffer",))
    msg = str(e.value)
    assert "live-test-suite" in msg          # the current holder, named
    assert "reclaimed" in msg                # not merely "locked"
    assert "device lock" in msg              # actionable: re-establish state


def test_97_check_session_raises_when_no_lease_exists_at_all(root, monkeypatch):
    """The lease may have simply expired — no holder to name, still a lost
    session, still a refusal."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with pytest.raises(locks.LockLost) as e:
        locks.check_session(IP, ("library",))
    assert "reclaimed or expired" in str(e.value)


def test_97_check_session_rejects_a_stale_lease_of_our_own(root, monkeypatch):
    """A dead-pid session lease past the grace is reclaimable by anyone — it
    is no longer protection, so a token that only opens IT must not pass."""
    write_lease(root, "all", pid=dead_pid(), token="tok-97", kind="session",
                age=locks.SESSION_PID_GRACE_S + 5)
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with pytest.raises(locks.LockLost):
        locks.check_session(IP, ("editbuffer",))


# every networked read-only verb carrying @_reads — all of them, so removing
# a decorator can never pass unnoticed. "{out}" is filled with a scratch path.
# `device measure` here IS the observed 2026-07-27 scenario end to end: the
# agent's lease is gone, a contender holds `editbuffer`, and the read used to
# hand back a well-formed measurement of whatever snapshot happened to be
# active.
READ_ONLY_VERBS = [
    (("device", "measure"), "editbuffer"),
    (("device", "info"), "library"),
    (("device", "meters"), "editbuffer"),
    (("device", "tuner"), "editbuffer"),
    (("device", "watch", "--seconds", "0.1"), "editbuffer"),
    (("device", "blocks"), "editbuffer"),
    (("device", "params", "0", "0"), "editbuffer"),
    (("device", "active"), "editbuffer"),
    (("device", "list"), "library"),
    (("device", "setlists"), "library"),
    (("device", "read", "1"), "library"),
    (("device", "pull", "1", "{out}"), "library"),
    (("device", "backup", "--dir", "{out}"), "library"),
    (("device", "setlist", "export-hss", "HGTEST", "{out}"), "library"),
    (("device", "slots", "list", "--verify"), "library"),
    (("device", "list-irs"), "irs"),
    (("device", "pull-ir", "some.wav", "{out}"), "irs"),
    (("device", "settings", "list", "--values"), "globals"),
    (("device", "settings", "get", "global.tuner.type"), "globals"),
]


@pytest.mark.parametrize("argv,scope", READ_ONLY_VERBS)
def test_97_read_only_verbs_refuse_a_dangling_token(root, fake_client,
                                                    monkeypatch, tmp_path,
                                                    argv, scope):
    write_lease(root, scope, pid=1, label="other-agent")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    argv = tuple(a.format(out=tmp_path / "out") for a in argv)
    res = run_cli(*argv, "--ip", IP)
    assert res.exit_code != 0, res.output
    assert "other-agent" in res.output and "reclaimed" in res.output


def test_97_slots_list_stays_exempt_when_it_is_offline(root, monkeypatch):
    """`slots list` without --verify reads the local manifest only — the
    other `when()`-narrowing verb. An offline verb must stay usable under a
    dangling token: exactly the state you are in when you need to inspect."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    assert run_cli("device", "slots", "list", "--ip", IP).exit_code == 0


def test_97_settings_list_stays_exempt_when_it_is_offline(root, monkeypatch):
    """`settings list` without --values touches no network — an offline verb
    must not be locked out by a dangling token (recovery stays possible)."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    assert run_cli("device", "settings", "list", "--ip", IP).exit_code == 0


def test_97_dry_run_that_still_reads_the_device_refuses_a_dangling_token(
        root, fake_client, monkeypatch):
    """A verb whose `when()` narrows to NO lease (ir-prune without --yes) is
    still a networked read: it must not slip past the check that `device
    list-irs`, a strictly weaker read of the same data, is subject to."""
    write_lease(root, "irs", pid=1, label="other-agent")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    res = run_cli("device", "ir-prune", "--ip", IP)
    assert res.exit_code != 0, res.output
    assert "other-agent" in res.output and "reclaimed" in res.output


def test_97_read_only_verb_without_a_token_still_runs_unlocked(root,
                                                               fake_client):
    """Verbs invoked with NO token keep today's behavior exactly — read-only
    verbs must not start requiring locks."""
    write_lease(root, "editbuffer", pid=1, label="other-agent")
    res = run_cli("device", "blocks", "--ip", IP)
    assert res.exit_code == 0, res.output


def test_97_read_only_verb_with_a_live_lease_runs(root, fake_client,
                                                  monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    assert run_cli("device", "lock", "--scope", "all", "--detach", "--label",
                   "agent-workflow", "--ip", IP).exit_code == 0
    res = run_cli("device", "blocks", "--ip", IP)
    assert res.exit_code == 0, res.output


def test_97_mutating_verb_refuses_instead_of_locking_a_free_scope(
        root, fake_client, monkeypatch):
    """Before: a token that authenticated nothing silently acquired a fresh
    transient lease and proceeded. Now it is a lost session, and the verb
    stops."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    res = run_cli("device", "load", "1", "--ip", IP)
    assert res.exit_code != 0
    assert "reclaimed or expired" in res.output
    assert not lease_path(root, "editbuffer").exists()


def test_97_unlock_still_works_with_a_dangling_token(root, monkeypatch):
    """The recovery path must never be locked out by the new check."""
    write_lease(root, "editbuffer", pid=1, label="other-agent")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    assert run_cli("device", "unlock", "--ip", IP).exit_code == 0
    res = run_cli("device", "lock", "--status", "--ip", IP)
    assert res.exit_code == 0, res.output


# --------------------------------------------------------------------------
# #97 review round 2: renewal covers the whole session, and covers a verb
# that outruns its own TTL
# --------------------------------------------------------------------------

def test_97_use_renews_every_lease_the_token_owns_not_just_this_verbs_scope(
        root, monkeypatch):
    """A multi-scope agent lease is ONE session: a stretch of `library` verbs
    must not let the sibling `editbuffer` lease age out and be reclaimed
    under the agent while it is demonstrably active."""
    eb = write_lease(root, "editbuffer", pid=None, token="tok-97",
                     kind="detached", age=200,
                     ttl=locks.DEFAULT_DETACHED_TTL)
    lib = write_lease(root, "library", pid=None, token="tok-97",
                      kind="detached", age=200,
                      ttl=locks.DEFAULT_DETACHED_TTL)
    before = {p: json.loads(p.read_text())["acquired_at"] for p in (eb, lib)}
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    locks.check_session(IP, ("library",))
    for p in (eb, lib):
        assert json.loads(p.read_text())["acquired_at"] > before[p], p.name


def test_97_foreign_leases_are_never_renewed_by_our_use(root, monkeypatch):
    other = write_lease(root, "irs", pid=1, label="other-agent", age=100)
    write_lease(root, "library", pid=None, token="tok-97", kind="detached")
    before = json.loads(other.read_text())["acquired_at"]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    locks.check_session(IP, ("library",))
    assert json.loads(other.read_text())["acquired_at"] == before


def test_97_keep_alive_renews_a_lease_that_the_verb_outruns(root, monkeypatch):
    """Renewal at verb ENTRY alone loses the lease mid-flight for anything
    longer than its TTL (`normalize`, a long `--seconds` window). The
    heartbeat keeps an ACTIVE long verb's lease alive."""
    monkeypatch.setattr(locks, "HEARTBEAT_MAX_S", 0.05)
    # ... and the 1s floor, or HEARTBEAT_MAX_S alone changes nothing
    monkeypatch.setattr(locks, "HEARTBEAT_MIN_S", 0.02)
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    ttl=locks.DEFAULT_DETACHED_TTL)
    with locks.keep_alive(IP, token="tok-97"):
        # AFTER entry: keep_alive renews once on the way in, so a `before`
        # captured outside the block is satisfied without the thread ever
        # beating (the assertion would pass against a no-op heartbeat).
        before = json.loads(p.read_text())["acquired_at"]
        deadline = time.time() + 2.0
        while (json.loads(p.read_text())["acquired_at"] == before
               and time.time() < deadline):
            time.sleep(0.01)
        assert json.loads(p.read_text())["acquired_at"] > before
    # and the thread stops with the verb
    assert not any(t.name == "helixgen-lock-keepalive" and t.is_alive()
                   for t in threading.enumerate())


def test_97_keep_alive_warns_when_the_lease_goes_away_mid_call(root,
                                                               monkeypatch):
    """Losing the lease DURING a long verb is the #97 failure with positive
    proof attached: the heartbeat sees it and must say so, rather than let
    the verb print well-formed output for a device it no longer holds."""
    monkeypatch.setattr(locks, "HEARTBEAT_MAX_S", 0.05)
    monkeypatch.setattr(locks, "HEARTBEAT_MIN_S", 0.02)
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    ttl=locks.DEFAULT_DETACHED_TTL)
    with capture_err() as errlog, locks.keep_alive(IP, token="tok-97"):
        p.unlink()  # reclaimed / released by someone else mid-call
        deadline = time.time() + 2.0
        while "lapsed" not in errlog.text and time.time() < deadline:
            time.sleep(0.01)
    assert "lapsed DURING this call" in errlog.text, errlog.text


def test_97_keep_alive_warns_when_only_ONE_of_two_scopes_lapses(root,
                                                                monkeypatch):
    """A multi-scope session that drops ONE scope mid-call is the same
    untrustworthy output. Waiting for the count to reach zero would let a
    `--scope editbuffer --scope library` agent lose `editbuffer` in silence."""
    monkeypatch.setattr(locks, "HEARTBEAT_MAX_S", 0.05)
    monkeypatch.setattr(locks, "HEARTBEAT_MIN_S", 0.02)
    p = write_lease(root, "editbuffer", pid=None, token="tok-97",
                    kind="detached", ttl=locks.DEFAULT_DETACHED_TTL)
    write_lease(root, "library", pid=None, token="tok-97", kind="detached",
                ttl=locks.DEFAULT_DETACHED_TTL)
    with capture_err() as errlog, locks.keep_alive(IP, token="tok-97"):
        p.unlink()  # one scope reclaimed; `library` still ours
        deadline = time.time() + 2.0
        while "lapsed" not in errlog.text and time.time() < deadline:
            time.sleep(0.01)
    assert "lapsed DURING this call" in errlog.text, errlog.text


@pytest.mark.parametrize("exc", [RuntimeError("kaboom"), OSError("kaboom")])
def test_97_keep_alive_says_so_when_the_heartbeat_itself_dies(root,
                                                              monkeypatch,
                                                              exc):
    """An unexpected exception used to kill the daemon thread silently: no
    more renewals, no `lapsed` warning, lease gone by the next call — the
    silent loss this whole feature exists to prevent. OSError included: it is
    the likeliest one here (vanished lease dir) and used to return quietly."""
    monkeypatch.setattr(locks, "HEARTBEAT_MAX_S", 0.05)
    monkeypatch.setattr(locks, "HEARTBEAT_MIN_S", 0.02)
    write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                ttl=locks.DEFAULT_DETACHED_TTL)
    calls = {"n": 0}
    real = locks._renew_owned

    def boom(ip, token):
        calls["n"] += 1
        if calls["n"] == 1:  # the entry renewal must still succeed
            return real(ip, token)
        raise exc

    monkeypatch.setattr(locks, "_renew_owned", boom)
    with capture_err() as errlog, locks.keep_alive(IP, token="tok-97"):
        deadline = time.time() + 2.0
        while ("keep-alive stopped" not in errlog.text
               and time.time() < deadline):
            time.sleep(0.01)
    assert ("keep-alive stopped" in errlog.text
            and "kaboom" in errlog.text), errlog.text


def test_97_renewal_that_lands_on_a_reacquired_lease_is_not_held(root,
                                                                 monkeypatch):
    """`_renew` is nonce-guarded, so a lease broken + re-acquired under us
    silently no-ops the write. Counting that as a renewed, healthy lease
    would resurrect the exact silent-expiry failure #97 exists to prevent."""
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    ttl=locks.DEFAULT_DETACHED_TTL)

    real_rewrite = locks._rewrite

    def steal(path, payload, *, expect_nonce=None):
        # a contender broke and re-took the lease between our read and write
        data = json.loads(p.read_text())
        data.update(nonce="someone-else", token="tok-other",
                    label="other-agent", acquired_at=time.time())
        p.write_text(json.dumps(data))
        return real_rewrite(path, payload, expect_nonce=expect_nonce)

    monkeypatch.setattr(locks, "_rewrite", steal)
    assert locks._renew_owned(IP, "tok-97") == (0, None)
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    monkeypatch.setattr(locks, "_rewrite", real_rewrite)
    with pytest.raises(locks.LockLost):
        locks.check_session(IP, ("editbuffer",))


def test_97_a_failed_renewal_write_does_not_lose_a_lease_that_is_still_ours(
        root, monkeypatch):
    """The other no-op cause: the write itself failed (full disk, read-only
    FS). The lease file is untouched and still ours — refusing there would
    be a false alarm, so it stays held until the TTL genuinely runs out."""
    write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                ttl=locks.DEFAULT_DETACHED_TTL)
    monkeypatch.setattr(locks, "_rewrite",
                        lambda *a, **k: False)  # every write fails
    held, _ = locks._renew_owned(IP, "tok-97")
    assert held == 1
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    locks.check_session(IP, ("editbuffer",))  # no refusal


def test_97_token_held_on_another_device_address_is_not_a_lost_session(
        root, monkeypatch):
    """Leases are keyed by ADDRESS, so one session spans two Stadiums — or
    two spellings of one (`helix.local` here, `10.0.0.4` there). Owning
    nothing under THIS address is the pre-lease state, not proof of a
    reclaim; blaming a reclaim blocked reads outright (no `--no-lock`)."""
    write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                ttl=locks.DEFAULT_DETACHED_TTL, ip="198.51.100.7")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    locks.check_session(IP, ("library",), strict=True)  # no refusal
    assert not lease_path(root, "library").exists()  # and still takes nothing


def test_97_read_under_a_token_held_elsewhere_warns_that_it_is_unlocked(
        root, monkeypatch, capsys):
    """Not a refusal — but not silent either: the caller believes it holds a
    device it holds nothing on, which is #97 one address-spelling apart."""
    write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                ttl=locks.DEFAULT_DETACHED_TTL, ip="198.51.100.7")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    locks.check_session(IP, ("library",), strict=True)
    err = capsys.readouterr().err
    assert "198.51.100.7" in err and "UNLOCKED" in err


def test_97_mutating_verb_under_a_token_held_elsewhere_does_not_warn(
        root, monkeypatch, capsys):
    """It goes on to acquire this address transiently — it IS locked."""
    write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                ttl=locks.DEFAULT_DETACHED_TTL, ip="198.51.100.7")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    locks.check_session(IP, ("library",))
    assert capsys.readouterr().err == ""


def test_97_other_device_lease_does_not_excuse_a_scope_someone_else_holds(
        root, monkeypatch):
    """...but only where nothing else is driving THIS device: a live foreign
    lease over the scope we are about to read is still a refusal."""
    write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                ttl=locks.DEFAULT_DETACHED_TTL, ip="198.51.100.7")
    write_lease(root, "library", label="other-agent")  # live, not ours
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with pytest.raises(locks.LockLost) as e:
        locks.check_session(IP, ("library",), strict=True)
    assert "other-agent" in str(e.value)


def test_97_refusal_under_a_lease_held_elsewhere_is_never_called_a_reclaim(
        root, monkeypatch):
    """The refusal above must not tell an agent its session was reclaimed:
    the token opens nothing HERE, but it opens a live lease one address
    spelling over, and "re-take a lease, do NOT retry" throws that away.
    Name the address instead — that IS the fix. (3rd review pass.)"""
    write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                ttl=locks.DEFAULT_DETACHED_TTL, ip="198.51.100.7")
    write_lease(root, "library", label="other-agent")  # live, not ours
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with pytest.raises(locks.LockLost) as e:
        locks.check_session(IP, ("library",), strict=True)
    assert e.value.proven is False
    assert e.value.elsewhere == "198.51.100.7"
    assert "198.51.100.7" in str(e.value)
    assert "do NOT retry" not in str(e.value)
    assert "Wait for the holder" in str(e.value)


def test_97_a_stale_lease_elsewhere_does_not_excuse_a_dangling_token(
        root, monkeypatch):
    """The escape hatch is for a LIVE session on another address. An expired
    one everywhere means the session really is gone."""
    write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                ttl=60, age=120, ip="198.51.100.7")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with pytest.raises(locks.LockLost):
        locks.check_session(IP, ("library",))


def test_97_keep_alive_is_a_noop_when_no_lease_is_held(root):
    with locks.keep_alive(IP, token="tok-97"):
        pass
    assert not lease_path(root, "all").exists()


def test_97_long_verb_keeps_its_lease_alive(root, fake_client, monkeypatch):
    """End to end through the CLI wrapper: the lease is renewed DURING the
    verb body, not only before it."""
    monkeypatch.setattr(locks, "HEARTBEAT_MAX_S", 0.05)
    # ... and the 1s floor, or HEARTBEAT_MAX_S alone changes nothing
    monkeypatch.setattr(locks, "HEARTBEAT_MIN_S", 0.02)
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    ttl=locks.DEFAULT_DETACHED_TTL)
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    seen = {}

    import helixgen.device as device_mod
    real_blocks = device_mod.HelixClient.edit_buffer_blocks

    def slow_blocks(self):
        entry = json.loads(p.read_text())["acquired_at"]
        deadline = time.time() + 2.0
        while (json.loads(p.read_text())["acquired_at"] == entry
               and time.time() < deadline):
            time.sleep(0.02)
        seen["renewed_during_call"] = (
            json.loads(p.read_text())["acquired_at"] > entry)
        return real_blocks(self)

    monkeypatch.setattr(device_mod.HelixClient, "edit_buffer_blocks",
                        slow_blocks)
    assert run_cli("device", "blocks", "--ip", IP).exit_code == 0
    assert seen["renewed_during_call"] is True


def test_97_unlock_tells_you_to_unset_a_now_dangling_token(root, monkeypatch):
    """`device unlock` is the documented end of an agent workflow — but the
    token stays exported in the caller's shell, where it now opens nothing
    and makes every later device verb (reads included) fail."""
    res = run_cli("device", "lock", "--scope", "all", "--detach", "--label",
                  "agent-workflow", "--ip", IP)
    token = [ln.split("=", 1)[1] for ln in res.output.splitlines()
             if ln.startswith("HELIXGEN_LOCK_TOKEN=")][0]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", token)
    out = run_cli("device", "unlock", "--ip", IP)
    assert out.exit_code == 0, out.output
    assert "unset HELIXGEN_LOCK_TOKEN" in out.output


def test_97_unlock_says_nothing_while_the_token_still_opens_a_lease(
        root, monkeypatch):
    res = run_cli("device", "lock", "--scope", "editbuffer", "--scope",
                  "library", "--detach", "--label", "agent", "--ip", IP)
    token = [ln.split("=", 1)[1] for ln in res.output.splitlines()
             if ln.startswith("HELIXGEN_LOCK_TOKEN=")][0]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", token)
    out = run_cli("device", "unlock", "--scope", "library", "--ip", IP)
    assert out.exit_code == 0, out.output
    assert "unset HELIXGEN_LOCK_TOKEN" not in out.output




def test_97_unlock_with_no_token_never_suggests_unsetting_one(root):
    """The most common invocation of all: no token exported, nothing owned.
    Advising `unset HELIXGEN_LOCK_TOKEN` there is nonsense (and noise for an
    agent parsing stderr)."""
    out = run_cli("device", "unlock", "--ip", IP)
    assert out.exit_code == 0, out.output
    assert "unset HELIXGEN_LOCK_TOKEN" not in out.output


def test_97_unlock_without_the_token_cannot_release_a_detached_lease(root):
    """A detached lease has no pid to match, so the token is the ONLY proof
    of ownership: lose it and `unlock` keeps the lease (reporting it) —
    `--force` is the way out. Pinned because the exit code is 0 and an agent
    could otherwise read "unlocked" into a device still held."""
    run_cli("device", "lock", "--scope", "all", "--detach", "--label",
            "agent", "--ip", IP)
    bare = run_cli("device", "unlock", "--ip", IP)
    assert bare.exit_code == 0, bare.output   # nothing OF OURS to release
    assert "kept 'all'" in bare.output and "detached" in bare.output
    assert lease_path(root, "all").exists()
    explicit = run_cli("device", "unlock", "--scope", "all", "--ip", IP)
    assert explicit.exit_code != 0           # but ASKING for it errors
    assert run_cli("device", "unlock", "--scope", "all", "--force",
                   "--ip", IP).exit_code == 0
    assert not lease_path(root, "all").exists()


# --------------------------------------------------------------------------
# #97 review round 3: TTLs that can't be kept alive, partial lease loss
# --------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [("--detach",), ()])
@pytest.mark.parametrize("ttl", ["nan", "inf"])
def test_97_lock_refuses_a_non_finite_ttl(root, ttl, extra):
    """`--ttl nan` slipped past the `ttl <= 0` guard (nan compares False to
    everything) and wrote a pid-less lease with NO expiry — precisely what
    the --detach guard exists to refuse — plus a bare `NaN` value that is not
    valid JSON for any non-Python reader. (`-inf` was already refused by the
    no-expiry guard.)"""
    res = run_cli("device", "lock", "--ttl", ttl, "--label", "a",
                  "--ip", IP, *extra)
    assert res.exit_code != 0, res.output
    assert "finite" in res.output
    assert not lease_path(root, "all").exists()


@pytest.mark.parametrize("ttl", [1, 2, 3, 9])
@pytest.mark.parametrize("extra", [("--detach",), (), ("--pid", "SELF")])
def test_97_lock_refuses_a_ttl_too_short_to_renew(root, ttl, extra):
    """Renewal skips a lease within RENEW_MARGIN_S of expiry and the
    heartbeat sleeps at least HEARTBEAT_MIN_S, so a TTL this short expires
    mid-workflow no matter how actively it is used — silent lease loss, the
    failure #97 exists to prevent. Every kind, `--pid` included: that is the
    invocation agents are told to use."""
    extra = tuple(str(os.getpid()) if a == "SELF" else a for a in extra)
    res = run_cli("device", "lock", "--ttl", ttl, "--label", "a",
                  "--ip", IP, *extra)
    assert res.exit_code != 0, res.output
    assert "too short" in res.output
    assert not lease_path(root, "all").exists()


@pytest.mark.parametrize("extra", [("--detach",), ()])
def test_97_lock_refuses_a_negative_ttl(root, extra):
    """`--ttl -5` is the plausible typo for `--ttl 5` — which the too-short
    guard refuses instructively. Without this guard the typo slipped past it
    (`0 < ttl` is False for a negative) on a plain session lease and wrote
    `ttl_seconds: -5`, which `_remaining_ttl` reads as NO EXPIRY: the most
    fragile lease shape there is, reclaimable only by pid liveness, rendered
    by `--status` as `ttl -5s` (reads as already expired, not as no expiry).
    """
    res = run_cli("device", "lock", "--ttl", "-5", "--label", "a",
                  "--ip", IP, *extra)
    assert res.exit_code != 0, res.output
    assert "negative" in res.output
    assert not lease_path(root, "all").exists()


@pytest.mark.parametrize("extra", [("--detach",), (), ("--pid", "SELF")])
def test_97_lock_accepts_the_minimum_ttl(root, extra):
    extra = tuple(str(os.getpid()) if a == "SELF" else a for a in extra)
    assert run_cli("device", "lock", "--ttl", locks.MIN_SESSION_TTL,
                   "--label", "a", "--ip", IP, *extra).exit_code == 0


def test_97_renewal_leaves_a_lease_at_the_expiry_boundary_alone(root,
                                                                monkeypatch):
    """RENEW_MARGIN_S: renewing right at expiry can land on top of a waiter's
    legitimate re-acquisition (#72), so those are left to expire — and since
    NOTHING will renew such a lease again, entering a verb on it is entering
    on a lease certain to lapse mid-call. It counts as lost, loudly, instead
    of admitting the verb (silent unlocked work is the #97 failure)."""
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    ttl=60, age=59.5)  # 0.5s left — inside the margin
    before = json.loads(p.read_text())["acquired_at"]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with pytest.raises(locks.LockLost):
        locks.check_session(IP, ("editbuffer",))
    assert json.loads(p.read_text())["acquired_at"] == before


def test_97_a_lease_with_room_to_renew_is_not_treated_as_doomed(root,
                                                                monkeypatch):
    """The falsifying half of the test above: just OUTSIDE the margin the
    same lease passes and is renewed."""
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    ttl=60, age=55)  # 5s left — outside the margin
    before = json.loads(p.read_text())["acquired_at"]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    locks.check_session(IP, ("editbuffer",))
    assert json.loads(p.read_text())["acquired_at"] > before


def test_97_an_expired_lease_of_ours_is_reported_not_read_as_never_held(
        root, fake_client, monkeypatch):
    """Partial loss with the evidence still on disk: the agent holds
    editbuffer+irs, the `editbuffer` lease lapses (per-scope TTLs can
    diverge) and nobody has reclaimed it yet. The token still opens `irs`, so
    the session reads as alive and `device blocks` used to answer normally —
    while the scope it was reading was no longer held. The expired file is
    proof we lost it, so it errors."""
    write_lease(root, "irs", pid=None, token="tok-97", kind="detached",
                label="agent", ttl=locks.DEFAULT_DETACHED_TTL)
    write_lease(root, "editbuffer", pid=None, token="tok-97", kind="detached",
                label="agent", ttl=60, age=90)  # expired, still on disk
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    res = run_cli("device", "blocks", "--ip", IP)
    assert res.exit_code != 0, res.output
    assert "editbuffer" in res.output and "expired" in res.output


def test_97_a_reported_expired_lease_is_cleared_so_it_cannot_stick(
        root, monkeypatch):
    """Loud ONCE, then self-heal. Nothing renews an expired lease and no
    acquire path removes an owned lease that isn't its own target, so leaving
    the file on disk made every later verb over that scope refuse for
    good — mutating verbs included, against a scope nothing was contending,
    until `device unlock`. (2nd review pass.)"""
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    label="agent", ttl=60, age=90)  # expired, still on disk
    write_lease(root, "editbuffer", pid=None, token="tok-97", kind="detached",
                label="agent", ttl=locks.DEFAULT_DETACHED_TTL)
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with pytest.raises(locks.LockLost):
        locks.check_session(IP, ("library",))
    assert not p.exists()
    locks.check_session(IP, ("library",))          # the scope is free again
    locks.check_session(IP, ("library",), strict=True)


def test_97_a_scope_someone_else_holds_is_not_reported_as_a_reclaim(
        root, monkeypatch):
    """A strict read of a scope outside a narrow lease that a live FOREIGN
    lease holds right now still refuses — but "reclaimed from your session"
    and "never part of it" are indistinguishable there, and asserting the
    first tells an agent to tear a healthy workflow down when waiting for
    the holder is all it needs. (2nd review pass.)"""
    write_lease(root, "editbuffer", pid=None, token="tok-97", kind="detached",
                label="agent", ttl=locks.DEFAULT_DETACHED_TTL)
    write_lease(root, "library", pid=1, label="other-agent")  # live, not ours
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with pytest.raises(locks.LockLost) as e:
        locks.check_session(IP, ("library",), strict=True)
    assert e.value.proven is False
    assert "or never part of it" in str(e.value)
    assert "Wait for the holder" in str(e.value)
    assert "do NOT retry" not in str(e.value)


def test_97_a_dangling_token_still_gets_the_full_strength_message(
        root, monkeypatch):
    """The counterpart: the token opens NOTHING here, so the session really
    is gone and the strong stop-and-re-establish wording stands."""
    write_lease(root, "library", pid=1, label="other-agent")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    with pytest.raises(locks.LockLost) as e:
        locks.check_session(IP, ("library",), strict=True)
    assert e.value.proven is True
    assert "do NOT retry" in str(e.value)


def test_97_a_stale_transient_lease_of_ours_is_not_a_lost_session(
        root, fake_client, monkeypatch):
    """Counterpart: a per-verb (`kind: auto`) lease left behind by a killed
    verb is not part of the declared session, so it must not turn every
    later read of that scope into a refusal."""
    write_lease(root, "irs", pid=None, token="tok-97", kind="detached",
                label="agent", ttl=locks.DEFAULT_DETACHED_TTL)
    write_lease(root, "editbuffer", pid=None, token="tok-97", kind="auto",
                label="helixgen device load", ttl=60, age=90)
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    assert run_cli("device", "blocks", "--ip", IP).exit_code == 0


def test_97_read_refuses_when_a_contender_took_one_scope_of_a_narrow_lease(
        root, fake_client, monkeypatch):
    """The read/write asymmetry, reachable with a MULTI-SCOPE lease: the
    agent holds editbuffer+irs, a contender reclaims editbuffer, and
    `device blocks` used to hand back the edit buffer that contender is now
    driving (the token still opened the `irs` lease, so the session read as
    alive). A read has no contend-and-wait fallback, so it refuses."""
    write_lease(root, "irs", pid=None, token="tok-97", kind="detached",
                label="agent", ttl=locks.DEFAULT_DETACHED_TTL)
    write_lease(root, "editbuffer", pid=1, label="other-agent")
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    res = run_cli("device", "blocks", "--ip", IP)
    assert res.exit_code != 0, res.output
    assert "other-agent" in res.output and "reclaimed" in res.output


def test_97_read_of_a_free_scope_outside_a_narrow_lease_still_passes(
        root, fake_client, monkeypatch):
    """...but only when someone else actually holds it. A granular lease
    stays granular: holding `irs` and reading the (unheld) edit buffer is
    exactly as free as it was unlocked."""
    write_lease(root, "irs", pid=None, token="tok-97", kind="detached",
                label="agent", ttl=locks.DEFAULT_DETACHED_TTL)
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    assert run_cli("device", "blocks", "--ip", IP).exit_code == 0


def test_97_ir_prune_dry_run_heartbeats_its_lease(root, fake_client,
                                                  monkeypatch):
    """A dry run is a READ (it scans the whole pool) and can outrun a short
    TTL just like `watch` — it gets the same guard, not only the entry
    check."""
    monkeypatch.setattr(locks, "HEARTBEAT_MAX_S", 0.05)
    monkeypatch.setattr(locks, "HEARTBEAT_MIN_S", 0.02)
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    ttl=locks.DEFAULT_DETACHED_TTL)
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    seen = {}

    import helixgen.device.maintenance as mt

    def slow_prune(**kw):
        entry = json.loads(p.read_text())["acquired_at"]
        deadline = time.time() + 2.0
        while (json.loads(p.read_text())["acquired_at"] == entry
               and time.time() < deadline):
            time.sleep(0.01)
        seen["renewed"] = json.loads(p.read_text())["acquired_at"] > entry
        return {"ok": True, "dry_run": True, "device_irs": 0,
                "referenced": [], "protected": [], "orphans": [],
                "deleted": [], "errors": [], "warnings": []}

    monkeypatch.setattr(mt, "ir_prune", slow_prune)
    res = run_cli("device", "ir-prune", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert seen["renewed"] is True


# --------------------------------------------------------------------------
# re-locking switches a lease's kind (the `--detach` help text promises both
# directions)
# --------------------------------------------------------------------------

def _lock_and_export(monkeypatch, *extra):
    res = run_cli("device", "lock", "--scope", "all", "--label", "agent",
                  "--ip", IP, *extra)
    assert res.exit_code == 0, res.output
    token = [ln.split("=", 1)[1] for ln in res.output.splitlines()
             if ln.startswith("HELIXGEN_LOCK_TOKEN=")][0]
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", token)
    return token


def test_97_relock_with_detach_drops_the_pid_of_a_session_lease(root,
                                                                monkeypatch):
    _lock_and_export(monkeypatch)
    data = json.loads(lease_path(root, "all").read_text())
    assert data["pid"] == os.getppid()
    _lock_and_export(monkeypatch, "--detach")
    data = json.loads(lease_path(root, "all").read_text())
    assert (data["kind"], data["pid"]) == ("detached", None)


def test_97_plain_relock_rebinds_a_detached_lease_to_the_shell(root,
                                                               monkeypatch):
    """The other direction — a `session` lease with pid None would never be
    pid-stale and would render as literal "pid None" in --status."""
    _lock_and_export(monkeypatch, "--detach")
    _lock_and_export(monkeypatch)
    data = json.loads(lease_path(root, "all").read_text())
    assert (data["kind"], data["pid"]) == ("session", os.getppid())


# --------------------------------------------------------------------------
# review follow-ups: the heartbeat on the MUTATING path, its delay, the
# --no-lock escape hatch vs the session check, and stdout hygiene
# --------------------------------------------------------------------------

def test_97_long_mutating_verb_keeps_its_lease_alive(root, fake_client,
                                                     monkeypatch):
    """`normalize` and friends are the motivating case: the heartbeat has to
    run on the `_locked` path too, not just on read-only verbs."""
    monkeypatch.setattr(locks, "HEARTBEAT_MAX_S", 0.05)
    monkeypatch.setattr(locks, "HEARTBEAT_MIN_S", 0.02)
    p = write_lease(root, "all", pid=None, token="tok-97", kind="detached",
                    ttl=locks.DEFAULT_DETACHED_TTL)
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-97")
    seen = {}

    import helixgen.device as device_mod
    real_load = device_mod.HelixClient.load_preset

    def slow_load(self, *a, **kw):
        entry = json.loads(p.read_text())["acquired_at"]
        deadline = time.time() + 2.0
        while (json.loads(p.read_text())["acquired_at"] == entry
               and time.time() < deadline):
            time.sleep(0.01)
        seen["renewed"] = json.loads(p.read_text())["acquired_at"] > entry
        return real_load(self, *a, **kw)

    monkeypatch.setattr(device_mod.HelixClient, "load_preset", slow_load)
    res = run_cli("device", "load", "1", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert seen["renewed"] is True


def test_97_heartbeat_beats_on_a_third_of_the_shortest_ttl():
    """A beat per TTL/3 leaves two more chances before the lease lapses.
    Clamped both ways so a tiny TTL can't spin and a long one still beats."""
    assert locks._heartbeat_delay(30.0) == 10.0
    assert locks._heartbeat_delay(None) == locks.HEARTBEAT_MAX_S
    assert locks._heartbeat_delay(10_000.0) == locks.HEARTBEAT_MAX_S
    assert locks._heartbeat_delay(0.3) == locks.HEARTBEAT_MIN_S
    # the floor is why MIN_SESSION_TTL exists: any TTL a user may take must
    # still get a beat well clear of the renewal margin
    assert (locks._heartbeat_delay(locks.MIN_SESSION_TTL)
            < locks.MIN_SESSION_TTL - locks.RENEW_MARGIN_S)


def test_97_heartbeat_keeps_renewing_the_scopes_it_still_holds(root,
                                                               monkeypatch):
    """Losing one scope warns but must NOT stop the thread: the surviving
    sibling lease would then age out unrenewed mid-call — the same silent
    expiry, one scope over."""
    monkeypatch.setattr(locks, "HEARTBEAT_MAX_S", 0.05)
    monkeypatch.setattr(locks, "HEARTBEAT_MIN_S", 0.02)
    gone = write_lease(root, "editbuffer", pid=None, token="tok-97",
                       kind="detached", ttl=locks.DEFAULT_DETACHED_TTL)
    kept = write_lease(root, "library", pid=None, token="tok-97",
                       kind="detached", ttl=locks.DEFAULT_DETACHED_TTL)
    with capture_err() as errlog, locks.keep_alive(IP, token="tok-97"):
        gone.unlink()
        deadline = time.time() + 2.0
        while "lapsed" not in errlog.text and time.time() < deadline:
            time.sleep(0.01)
        after_warning = json.loads(kept.read_text())["acquired_at"]
        deadline = time.time() + 2.0
        while (json.loads(kept.read_text())["acquired_at"] == after_warning
               and time.time() < deadline):
            time.sleep(0.01)
        assert json.loads(kept.read_text())["acquired_at"] > after_warning
    assert "lapsed DURING this call" in errlog.text, errlog.text


def test_97_no_lock_skips_the_dangling_token_check(root, fake_client,
                                                   monkeypatch):
    """`--no-lock` is the documented (dangerous) opt-out of the whole lock
    layer, session check included — otherwise a dangling token would leave a
    mutating verb with no way past at all."""
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-dangling")
    assert run_cli("device", "load", "1", "--ip", IP).exit_code != 0
    res = run_cli("device", "load", "1", "--no-lock", "--ip", IP)
    assert res.exit_code == 0, res.output


def test_97_unlock_dangling_warning_keeps_json_stdout_parseable(root,
                                                                monkeypatch):
    """The advice goes to stderr: `device unlock --json` stdout must stay
    machine-readable for the agent that scripted the workflow."""
    token = _lock_and_export(monkeypatch, "--detach")
    assert token
    res = run_cli("device", "unlock", "--json", "--ip", IP)
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["released"] == ["all"]
    assert "unset HELIXGEN_LOCK_TOKEN" in res.stderr
