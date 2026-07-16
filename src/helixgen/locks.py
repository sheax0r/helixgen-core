"""Machine-local advisory device locks (0.22.0; workspace backlog #71).

Any helixgen process that is about to MUTATE a Helix device first takes a
**lease** — a small JSON file under ``~/.helixgen/locks/<device-ip>/<scope>.lock``
(root overridable via ``$HELIXGEN_LOCKS``) — so concurrent helixgen processes
on the same machine (including agents nobody is orchestrating) never collide
on the device. The lease FILE is the source of truth: no fcntl handle must be
held across processes, so shell-agent flows (every CLI call a fresh pid) work.

Scopes (one lease file each; ``all`` is exclusive against everything):

* ``editbuffer`` — live-ops verbs that mutate the ACTIVE tone
* ``library``    — pool / setlist / preset-content writes
* ``irs``        — device IR writes
* ``globals``    — Global Settings / Global EQ writes
* ``all``        — an exclusive session lease over the whole device

A lease is a JSON object: ``{pid, hostname, acquired_at, ttl_seconds, label,
token?, kind, nonce}``. Ownership (a holder's OTHER processes passing through
its lease instead of deadlocking against it) is established by either:

* **token** — ``$HELIXGEN_LOCK_TOKEN`` matching the lease's ``token`` (the
  explicit, robust mechanism for shell-agent flows; ``device lock`` prints
  the token to export), or
* **pid** — the lease's recorded pid equals this process's pid *or parent
  pid* (``device lock`` records the invoking shell's pid, so sibling CLI
  calls from the same shell pass through automatically).

Staleness: a lease is stale when its TTL has expired, or when its recorded
pid is dead on this host. Stale leases are broken with a stderr warning; a
LIVE lease is never broken (use ``device unlock --force`` deliberately).

Advisory + machine-local ONLY: direct-protocol clients on other hosts and
the Stadium desktop editor are not covered.

Pure stdlib; no device/network dependency.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import uuid
from pathlib import Path

#: The granular scopes (one lease file each). ``ALL`` is the exclusive scope.
SCOPES = ("editbuffer", "globals", "irs", "library")
ALL = "all"
VALID_SCOPES = SCOPES + (ALL,)

#: Default seconds a contended acquire waits before giving up
#: (override per-process with $HELIXGEN_LOCK_TIMEOUT; 0 = fail fast).
DEFAULT_TIMEOUT = 30.0
#: Default TTL for `device lock` session leases (renewed by covered verbs).
DEFAULT_SESSION_TTL = 900
#: TTL for a verb's transient auto-acquired lease (released on verb exit;
#: the TTL only matters if the process dies AND pid-liveness can't see it).
AUTO_TTL = 900

#: Age below which an unparseable lease file is assumed to be a concurrent
#: writer mid-write (wait) rather than junk (break).
_CORRUPT_GRACE_S = 30.0


class LockError(Exception):
    """Base class for lock-layer errors."""


class LockHeld(LockError):
    """A needed scope is held by a live foreign lease."""

    def __init__(self, ip: str, scope: str, holder: dict, waited: float):
        self.ip = ip
        self.scope = scope
        self.holder = holder
        self.waited = waited
        super().__init__(
            f"device {ip} scope '{scope}' is locked by {describe(holder)}"
            + (f"; gave up after waiting {waited:.0f}s" if waited else
               "; not waiting (timeout 0)"))


def locks_root() -> Path:
    """The lease-file root: $HELIXGEN_LOCKS or ~/.helixgen/locks."""
    env = os.environ.get("HELIXGEN_LOCKS")
    return Path(env) if env else Path.home() / ".helixgen" / "locks"


def lock_dir(ip: str) -> Path:
    return locks_root() / re.sub(r"[^A-Za-z0-9._-]", "_", ip or "default")


def lock_path(ip: str, scope: str) -> Path:
    return lock_dir(ip) / f"{scope}.lock"


def lock_timeout() -> float:
    """$HELIXGEN_LOCK_TIMEOUT (seconds; 0 = fail fast), default 30."""
    raw = os.environ.get("HELIXGEN_LOCK_TIMEOUT")
    if raw is None:
        return DEFAULT_TIMEOUT
    try:
        return max(0.0, float(raw))
    except ValueError:
        print(f"warning: ignoring invalid HELIXGEN_LOCK_TIMEOUT={raw!r} "
              f"(using {DEFAULT_TIMEOUT:g}s)", file=sys.stderr)
        return DEFAULT_TIMEOUT


def env_token() -> str | None:
    return os.environ.get("HELIXGEN_LOCK_TOKEN") or None


def hostname() -> str:
    return socket.gethostname()


def describe(lease: dict) -> str:
    """Human line naming a lease's holder: label, pid, host, age, ttl."""
    age = time.time() - lease.get("acquired_at", time.time())
    ttl = lease.get("ttl_seconds")
    ttl_s = f", ttl {ttl:g}s" if isinstance(ttl, (int, float)) else ""
    return (f"{lease.get('label', '?')!r} (pid {lease.get('pid', '?')} on "
            f"{lease.get('hostname', '?')}, age {max(0.0, age):.0f}s{ttl_s})")


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. PermissionError: someone else's live process
    return True


def read_lease(path: Path) -> dict | None:
    """Read a lease file. A missing file is None; an unparseable file is
    surfaced as a synthetic lease that goes stale after a short grace
    (a concurrent writer mid-write must not be broken instantly)."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("lease is not a JSON object")
        return data
    except ValueError:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        return {"label": "unreadable lease (corrupt file)", "pid": None,
                "hostname": "?", "acquired_at": mtime,
                "ttl_seconds": _CORRUPT_GRACE_S, "corrupt": True}


def is_stale(lease: dict) -> bool:
    """TTL expired, or recorded pid dead on this host. Never true for a
    live foreign-host lease inside its TTL."""
    now = time.time()
    ttl = lease.get("ttl_seconds")
    acquired = lease.get("acquired_at")
    if (isinstance(ttl, (int, float)) and ttl > 0
            and isinstance(acquired, (int, float)) and now > acquired + ttl):
        return True
    if lease.get("corrupt"):
        return False  # only the TTL path above can expire a corrupt file
    pid = lease.get("pid")
    if lease.get("hostname") == hostname() and isinstance(pid, int):
        return not _pid_alive(pid)
    return False


def owned(lease: dict, token: str | None = None) -> bool:
    """Is this lease ours (this process / this shell / this token)?"""
    if lease.get("corrupt"):
        return False
    tok = token if token is not None else env_token()
    if tok and lease.get("token") == tok:
        return True
    if (lease.get("hostname") == hostname()
            and lease.get("pid") in (os.getpid(), os.getppid())):
        return True
    return False


# --------------------------------------------------------------------------
# lease file primitives (atomic create / renew / remove)
# --------------------------------------------------------------------------

def _write_new(path: Path, payload: dict) -> bool:
    """Atomically create ``path`` with ``payload``; False if it now exists.

    Writes a fully-formed temp file first, then links it into place
    (readers never observe a partial lease). Falls back to O_EXCL direct
    write on filesystems without hard links.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(body)
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass  # hard links unsupported here — O_EXCL fallback below
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            f.write(body)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _renew(path: Path, lease: dict) -> None:
    """Refresh a lease's acquired_at (TTL renewal). Atomic replace;
    best-effort — a renewal never fails the verb."""
    fresh = dict(lease)
    fresh["acquired_at"] = time.time()
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)


def _break_stale(path: Path, lease: dict) -> None:
    """Remove a stale lease, re-verifying content immediately before the
    unlink so a lease renewed/replaced mid-decision is left alone."""
    current = read_lease(path)
    if current is None:
        return
    if current != lease and not is_stale(current):
        return  # it changed under us and the new lease is live — keep it
    print(f"warning: breaking stale device lock {path.name} held by "
          f"{describe(current)}", file=sys.stderr)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# acquisition
# --------------------------------------------------------------------------

class LeaseSet:
    """Leases acquired by one :func:`acquire` call. Context manager;
    ``release()`` removes only the files this call created (nonce-checked),
    never a covering session lease it merely passed through."""

    def __init__(self, ip: str, created: list[tuple[Path, str]],
                 passthrough: list[Path]):
        self.ip = ip
        self._created = created
        self.passthrough = passthrough

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def release(self) -> None:
        for path, nonce in self._created:
            lease = read_lease(path)
            if lease is not None and lease.get("nonce") == nonce:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        self._created = []


def _conflict_paths(ip: str, scope: str) -> list[Path]:
    """The lease files whose LIVE foreign presence blocks acquiring `scope`."""
    if scope == ALL:
        return [lock_path(ip, s) for s in VALID_SCOPES]
    return [lock_path(ip, scope), lock_path(ip, ALL)]


def _normalize_scopes(scopes) -> tuple[str, ...]:
    seen = []
    for s in scopes:
        if s not in VALID_SCOPES:
            raise ValueError(
                f"unknown lock scope {s!r} (valid: {', '.join(VALID_SCOPES)})")
        if s not in seen:
            seen.append(s)
    if ALL in seen:
        return (ALL,)
    return tuple(sorted(seen))  # fixed global order — no deadlock cycles


def acquire(ip: str, scopes, *, label: str, ttl: float = AUTO_TTL,
            timeout: float | None = None, token: str | None = None,
            pid: int | None = None, kind: str = "auto") -> LeaseSet:
    """Acquire one lease per scope for device ``ip``; returns a
    :class:`LeaseSet` (usable as a context manager).

    Scopes already covered by a lease we own (token or pid/parent-pid match
    — e.g. a ``device lock`` session lease) are passed through and their
    TTL renewed instead of re-acquired. On contention, waits up to
    ``timeout`` seconds (default $HELIXGEN_LOCK_TIMEOUT, else 30; 0 = fail
    fast) with polling backoff, breaking stale leases (expired TTL / dead
    same-host pid) with a stderr warning, then raises :class:`LockHeld`
    naming the holder. All-or-nothing: a failure releases anything this
    call had already created.
    """
    want = _normalize_scopes(scopes)
    tok = token if token is not None else env_token()
    budget = lock_timeout() if timeout is None else max(0.0, float(timeout))
    deadline = time.monotonic() + budget
    created: list[tuple[Path, str]] = []
    passthrough: list[Path] = []
    try:
        for scope in want:
            _acquire_one(ip, scope, label=label, ttl=ttl, token=tok, pid=pid,
                         kind=kind, deadline=deadline, budget=budget,
                         created=created, passthrough=passthrough)
    except BaseException:
        LeaseSet(ip, created, passthrough).release()
        raise
    return LeaseSet(ip, created, passthrough)


def _acquire_one(ip: str, scope: str, *, label: str, ttl: float,
                 token: str | None, pid: int | None, kind: str,
                 deadline: float, budget: float,
                 created: list, passthrough: list) -> None:
    attempt = 0
    while True:
        # 1. Pass through (and renew) a covering lease we already own.
        covered = False
        for cover in ({scope, ALL} if scope != ALL else {ALL}):
            cpath = lock_path(ip, cover)
            lease = read_lease(cpath)
            if lease is not None and not is_stale(lease) and owned(lease, token):
                _renew(cpath, lease)
                passthrough.append(cpath)
                covered = True
                break
        if covered:
            return

        # 2. Find the blocking lease, breaking stale ones.
        blocker = None
        for cpath in _conflict_paths(ip, scope):
            lease = read_lease(cpath)
            if lease is None:
                continue
            if owned(lease, token):
                continue  # ours (e.g. our own just-created sibling scope)
            if is_stale(lease):
                _break_stale(cpath, lease)
                continue
            blocker = lease
            break

        # 3. Free? Try the atomic create (a racer may still beat us).
        if blocker is None:
            payload = {
                "pid": os.getpid() if pid is None else int(pid),
                "hostname": hostname(),
                "acquired_at": time.time(),
                "ttl_seconds": ttl,
                "label": label,
                "kind": kind,
                "nonce": uuid.uuid4().hex,
            }
            if token:
                payload["token"] = token
            path = lock_path(ip, scope)
            if _write_new(path, payload):
                created.append((path, payload["nonce"]))
                return
            continue  # lost the create race — reassess immediately

        # 4. Blocked by a live foreign lease: wait (or fail fast).
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LockHeld(ip, scope, blocker, budget)
        time.sleep(min(remaining, min(1.0, 0.1 * (2 ** min(attempt, 4)))))
        attempt += 1


# --------------------------------------------------------------------------
# inspection + release (the `device lock --status` / `device unlock` engine)
# --------------------------------------------------------------------------

def status(ip: str, token: str | None = None) -> list[dict]:
    """One row per existing lease for ``ip``: scope, holder fields, age,
    live/stale state, and whether it's ours."""
    rows = []
    now = time.time()
    for scope in VALID_SCOPES:
        path = lock_path(ip, scope)
        lease = read_lease(path)
        if lease is None:
            continue
        acquired = lease.get("acquired_at")
        age = (now - acquired) if isinstance(acquired, (int, float)) else None
        rows.append({
            "scope": scope,
            "label": lease.get("label"),
            "pid": lease.get("pid"),
            "hostname": lease.get("hostname"),
            "acquired_at": acquired,
            "ttl_seconds": lease.get("ttl_seconds"),
            "age_seconds": round(age, 1) if age is not None else None,
            "kind": lease.get("kind"),
            "state": "stale" if is_stale(lease) else "live",
            "ours": owned(lease, token),
            "path": str(path),
        })
    return rows


def release_scopes(ip: str, scopes=None, *, token: str | None = None,
                   force: bool = False) -> dict:
    """Release leases for ``ip`` (the ``device unlock`` engine).

    With explicit ``scopes``: each named lease must be ours (or stale, or
    ``force=True``) — a live foreign lease raises :class:`LockHeld`-free
    ``LockError`` naming the holder. Without ``scopes``: frees every lease
    we own (plus, with ``force``, everything else); foreign leases are
    reported, not an error. Returns {"released": [...], "kept": [...]}.
    """
    explicit = scopes is not None
    targets = _normalize_scopes(scopes) if explicit else VALID_SCOPES
    released, kept = [], []
    for scope in targets:
        path = lock_path(ip, scope)
        lease = read_lease(path)
        if lease is None:
            continue
        ours = owned(lease, token)
        stale = is_stale(lease)
        if ours or stale or force:
            if not ours and not stale and force:
                print(f"warning: force-releasing live foreign lock "
                      f"'{scope}' held by {describe(lease)}", file=sys.stderr)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            released.append(scope)
        elif explicit:
            raise LockError(
                f"device {ip} scope '{scope}' is held by {describe(lease)} — "
                f"not yours (set HELIXGEN_LOCK_TOKEN, or use --force to "
                f"break it deliberately)")
        else:
            kept.append({"scope": scope, "holder": describe(lease)})
    return {"released": released, "kept": kept}


def new_token() -> str:
    return uuid.uuid4().hex
