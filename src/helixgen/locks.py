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

**Detached leases** (``device lock --detach``; #97): a session lease with
``pid: None`` and ``kind: "detached"``, so nothing binds it to the invoking
shell's lifetime — an agent takes it from one tool call and the shell that
exits when that call returns no longer makes the lease reclaimable. Only
the TTL can expire it, so it defaults to the shorter
:data:`DEFAULT_DETACHED_TTL` and a no-expiry ``--ttl 0`` is refused (no pid
AND no TTL = a lease nothing but ``--force`` can clear). Every
token-authenticated lock-taking call renews it, so an active workflow keeps
its lease and an abandoned one expires on its own.

**A presented token must open a live lease** (:func:`check_session`, #97):
``$HELIXGEN_LOCK_TOKEN`` set but opening nothing on this device raises
:class:`LockLost` rather than proceeding unlocked. The policy is applied at
the CLI layer (read-only verbs included — see ``cli_device._reads``), not
inside :func:`acquire`. Every such call also RENEWS every lease the token
owns, and :func:`keep_alive` keeps renewing from a daemon thread for the
duration of a long verb, so only an IDLE stretch longer than the TTL loses
a lease.

Advisory + machine-local ONLY: direct-protocol clients on other hosts and
the Stadium desktop editor are not covered.

Pure stdlib; no device/network dependency.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import socket
import sys
import threading
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
#: Default TTL for a DETACHED session lease (`device lock --detach`, #97).
#: Materially shorter than :data:`DEFAULT_SESSION_TTL`: a detached lease has
#: no pid, so the TTL is the ONLY thing that reclaims an abandoned one.
#: Covered verbs renew it, so an active workflow never notices the shorter TTL.
DEFAULT_DETACHED_TTL = 300
#: TTL for a verb's transient auto-acquired lease (released on verb exit;
#: the TTL only matters if the process dies AND pid-liveness can't see it).
AUTO_TTL = 900
#: Shortest TTL `device lock` accepts. Renewal skips a lease within
#: :data:`RENEW_MARGIN_S` of expiry and the heartbeat sleeps at least
#: :data:`HEARTBEAT_MIN_S`, so anything shorter than this is a lease that
#: ACTIVE use cannot keep alive — it would expire mid-workflow in silence,
#: the very failure #97 exists to prevent. (0 / negative still means "no
#: expiry", which is a deliberate choice, not a too-short one.)
MIN_SESSION_TTL = 10.0

#: Age below which an unparseable lease file is assumed to be a concurrent
#: writer mid-write (wait) rather than junk (break).
_CORRUPT_GRACE_S = 30.0

#: A lease within this many seconds of TTL expiry is NOT passed
#: through/renewed (it is re-acquired fresh instead): renewing exactly at
#: the boundary is when a waiter may legitimately break + re-acquire, and
#: an in-place renewal there could resurrect the lease on top of the
#: waiter's (2026-07-16 review finding 5).
RENEW_MARGIN_S = 2.0

#: A SESSION lease whose recorded pid is dead is only stale after this
#: grace since its last acquisition/renewal: `device lock` records the
#: invoking shell's pid, and a lock taken via a short-lived wrapper
#: (script/make/`sh -c`) would otherwise be reclaimable instantly (review
#: finding 4). Covered verbs renew the lease, so an ACTIVE wrapper-based
#: session survives; an idle one is reclaimable after the grace.
SESSION_PID_GRACE_S = 120.0

#: A crashed breaker's mutex file older than this is cleared.
_BREAK_MUTEX_TTL_S = 10.0

#: A crashed acquirer's meta-lock older than this is reclaimed. Acquisition
#: (scan->create->verify) is brief; a meta-lock held longer means the holder
#: died mid-section, so it is broken and the wait retried.
_ACQUIRE_META_TTL_S = 10.0


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


class LockLost(LockError):
    """``$HELIXGEN_LOCK_TOKEN`` is set but opens no live lease covering a
    scope the caller is about to touch (#97): the session it declares was
    reclaimed or has expired, so proceeding would be unlocked work under the
    belief that the device is held. ``partial=True`` when the token still
    opens SOME lease but not one over this scope, and the scope is held by
    someone else — the same untrustworthy read, narrower cause."""

    def __init__(self, ip: str, scope: str, holder: dict | None,
                 partial: bool = False):
        self.ip = ip
        self.scope = scope
        self.holder = holder
        self.partial = partial
        who = (f"{describe(holder)} holds it now"
               if holder is not None else "nothing holds it now")
        lost = ("no longer opens a live lease over this scope — that part "
                "of your session was reclaimed or expired" if partial else
                "opens no live lease — the session lease was reclaimed or "
                "expired")
        super().__init__(
            f"device {ip} scope '{scope}': your $HELIXGEN_LOCK_TOKEN {lost}, "
            f"and {who}. Stop and re-establish device state: re-take a lease "
            f"(`helixgen device lock --scope all --detach --label <who>`) and "
            f"re-read whatever you were about to act on — do NOT retry this "
            f"call and continue, the device may have been driven by someone "
            f"else since. To work unlocked deliberately, unset "
            f"$HELIXGEN_LOCK_TOKEN.")


def locks_root() -> Path:
    """The lease-file root: ``$HELIXGEN_LOCKS`` or
    ``helixgen_home()/"locks"`` (i.e. ``~/.helixgen/locks``, following
    ``$HELIXGEN_HOME`` like every other home subarea). ``locks/`` is in the
    home repo's gitignore — leases are transient coordination state, never
    committed."""
    env = os.environ.get("HELIXGEN_LOCKS")
    if env:
        return Path(env).expanduser()
    from helixgen.home import helixgen_home

    return helixgen_home() / "locks"


def lock_dir(ip: str) -> Path:
    """Per-device lease directory. The device identity is sanitized into a
    filesystem-safe name that stays readable for the common case (IPv4 /
    hostnames pass through unchanged) but is INJECTIVE: distinct identities
    never share a directory (#72). The naive ``re.sub`` alone is many-to-one
    — every disallowed char maps to ``_`` and ``_`` is itself allowed, so
    ``fe80::1`` and ``fe80:_1`` both became ``fe80__1``. Whenever
    sanitization actually altered the identity we append a short digest of
    the ORIGINAL string, so any two identities that sanitize to the same
    base still get distinct directories."""
    key = ip or "default"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
    if safe != key:
        # some char had to be replaced — disambiguate with a stable digest of
        # the original so distinct identities can't collide on `safe`.
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe}-{digest}"
    return locks_root() / safe


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
    who = ("detached" if lease.get("kind") == "detached"
           else f"pid {lease.get('pid', '?')}")
    return (f"{lease.get('label', '?')!r} ({who} on "
            f"{lease.get('hostname', '?')}, age {max(0.0, age):.0f}s{ttl_s})")


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) TERMINATES the target on Windows (TerminateProcess
        # for any non-CTRL signal) — never probe there; TTL staleness only.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. PermissionError: someone else's live process
    return True


def _synthetic(path: Path, label: str, *, ttl: float | None) -> dict:
    """A placeholder lease for a file we couldn't read/parse: blocks like a
    real lease; with a ttl it goes stale after that grace (from mtime)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = time.time()
    return {"label": label, "pid": None, "hostname": "?",
            "acquired_at": mtime,
            "ttl_seconds": _CORRUPT_GRACE_S if ttl is None else ttl,
            "corrupt": True}


def read_lease(path: Path) -> dict | None:
    """Read a lease file. Missing → None. Unparseable / structurally
    invalid → a synthetic blocking lease that goes stale after a short
    grace (a concurrent writer mid-write must not be broken instantly).
    Unreadable (permissions) → a synthetic lease that never goes stale by
    itself (we can't verify what we can't read)."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except PermissionError:
        return _synthetic(path, "unreadable lease (permission denied)",
                          ttl=0)  # ttl 0 = never TTL-stale
    except OSError:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("lease is not a JSON object")
        if not isinstance(data.get("acquired_at"), (int, float)):
            # field-less/foreign JSON ({} etc): reclaimable after the grace,
            # renderable by --status (review finding 9)
            raise ValueError("lease has no acquired_at")
        return data
    except ValueError:
        return _synthetic(path, "unreadable lease (corrupt file)", ttl=None)


def _remaining_ttl(lease: dict) -> float | None:
    """Seconds until TTL expiry; None when the lease has no expiry
    (ttl_seconds absent or <= 0)."""
    ttl = lease.get("ttl_seconds")
    acquired = lease.get("acquired_at")
    if (isinstance(ttl, (int, float)) and ttl > 0
            and isinstance(acquired, (int, float))):
        return acquired + ttl - time.time()
    return None


def is_stale(lease: dict) -> bool:
    """TTL expired, or recorded pid dead on this host (session leases get
    :data:`SESSION_PID_GRACE_S` before pid-death counts — the recorded pid
    is the locking shell's and may be a short-lived wrapper). Never true
    for a live foreign-host lease inside its TTL. ttl_seconds <= 0 means
    no TTL expiry. A DETACHED lease records no pid, so only the TTL path
    can expire it (#97)."""
    remaining = _remaining_ttl(lease)
    if remaining is not None and remaining <= 0:
        return True
    if lease.get("corrupt"):
        return False  # only the TTL path above can expire a synthetic lease
    pid = lease.get("pid")
    if lease.get("hostname") == hostname() and isinstance(pid, int):
        if _pid_alive(pid):
            return False
        if lease.get("kind") == "session":
            acquired = lease.get("acquired_at")
            return (isinstance(acquired, (int, float))
                    and time.time() > acquired + SESSION_PID_GRACE_S)
        return True
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


def covering_lease(ip: str, scope: str,
                   token: str | None = None) -> tuple[Path, dict] | None:
    """``(path, lease)`` of the LIVE lease we own that covers ``scope`` — its
    own file or the exclusive ``all`` lease — or None when we hold no such
    lease."""
    tok = token if token is not None else env_token()
    for cover in ({scope, ALL} if scope != ALL else {ALL}):
        path = lock_path(ip, cover)
        lease = read_lease(path)
        if lease is not None and not is_stale(lease) and owned(lease, tok):
            return path, lease
    return None


def _unrenewable(lease: dict) -> bool:
    """Is this (live) lease inside :data:`RENEW_MARGIN_S` of expiry? Nothing
    renews such a lease (see :func:`_renew_owned`), so it is going to lapse
    within seconds whatever the caller does — treated as already lost rather
    than handed to a verb that would then run unlocked believing otherwise."""
    remaining = _remaining_ttl(lease)
    return remaining is not None and remaining <= RENEW_MARGIN_S


def _expired_own_session(ip: str, scope: str, token: str | None) -> bool:
    """Is there an EXPIRED session/detached lease of ours still on disk for
    ``scope``? That file is proof the scope was ours and lapsed — the one
    case where "I lost it" IS distinguishable from "I never held it", so it
    is reported instead of silently passing as a scope outside a narrow
    lease. Transient per-verb leases (``kind: "auto"``) are ignored: one left
    behind by a killed verb is not part of the declared session."""
    for cover in ({scope, ALL} if scope != ALL else {ALL}):
        lease = read_lease(lock_path(ip, cover))
        if (lease is not None and is_stale(lease) and owned(lease, token)
                and lease.get("kind") in ("session", "detached")):
            return True
    return False


def _foreign_holder(ip: str, scope: str, token: str | None) -> dict | None:
    """The live lease NOT ours that blocks ``scope``, if any."""
    for cpath in _conflict_paths(ip, scope):
        lease = read_lease(cpath)
        if (lease is not None and not is_stale(lease)
                and not owned(lease, token)):
            return lease
    return None


def _owned_on_other_device(ip: str, token: str | None) -> str | None:
    """The OTHER device address where ``token`` opens a live lease, if any.

    Leases are keyed by address string, so one session legitimately spans
    several lease dirs: two Stadiums, or the SAME device reached as
    ``helix.local`` here and ``10.0.0.4`` there (`--ip` vs
    ``$HELIXGEN_HELIX_IP`` vs the persisted record). Owning nothing on THIS
    address is then not evidence that the session was reclaimed — it is the
    pre-lease state for this address, which is what an untokened caller has
    too. Without this, a healthy lease on one address turned every verb
    against another into a hard refusal blaming a reclaim that never
    happened (reads have no ``--no-lock``, so there was no way past it).
    """
    if not token:
        return None
    here = lock_dir(ip)
    try:
        dirs = [d for d in locks_root().iterdir() if d.is_dir() and d != here]
    except OSError:
        return None
    return next(
        (d.name for d in sorted(dirs) for scope in VALID_SCOPES
         if (lease := read_lease(d / f"{scope}.lock")) is not None
         and not is_stale(lease) and lease.get("token") == token),
        None,
    )


def check_session(ip: str, scopes, *, token: str | None = None,
                  strict: bool = False) -> None:
    """Fail fast when a presented token no longer owns the device (#97).

    Exporting ``$HELIXGEN_LOCK_TOKEN`` is an explicit declaration of "I am
    in a held session". If it opens no live lease AT ALL on this device,
    that session is GONE — reclaimed by a contender or expired — and the
    work would proceed unlocked while still believing it is protected. That
    asymmetry is what made the 2026-07-27 workflow dangerous: the mutating
    call was refused (visibly) while the read-only one returned real-looking
    numbers for a device someone else was now driving. Raises
    :class:`LockLost` naming the current holder of the scope in question.

    A scope simply OUTSIDE a narrow lease (holding ``library`` and touching
    ``editbuffer``) is not a lost session and never raises: the session is
    demonstrably alive, so a mutating verb acquires that scope transiently
    (contending normally, with the usual wait) and a read stays as free as
    it was unlocked.

    ``strict=True`` (READS — they have no transient-acquire fallback) adds
    one case: a scope outside our lease that a live FOREIGN lease holds
    right now. A mutating verb contends for it and is refused visibly; a
    read would otherwise return well-formed numbers for a scope someone
    else is driving — the exact 2026-07-27 asymmetry, reachable with a
    multi-scope narrow lease that lost one scope.

    A lost scope whose EXPIRED lease file is still on disk raises for both
    kinds of caller (:func:`_expired_own_session`): there the file itself
    tells "I lost it" apart from "I never held it". Once the file is gone or
    re-acquired the two are indistinguishable, and only the ``strict``
    foreign-holder case above catches it.

    A token whose only live lease sits under ANOTHER device address is not a
    lost session (:func:`_owned_on_other_device`) — leases are keyed by
    address, and one session may span two Stadiums or two spellings of one.

    A live lease within :data:`RENEW_MARGIN_S` of expiry counts as MISSING:
    nothing renews it, so admitting it would let a long verb start on a
    lease certain to lapse mid-call (:func:`_renew_owned`).

    EVERY lease the token owns is RENEWED, not only the ones covering this
    verb's scopes — a read-only verb is proof its session is still active,
    which is what the grace/TTL clock should be measuring. Without this, a
    `device measure`-only stretch performs zero renewals and loses the very
    lease it is holding (the Task 1 root cause), and a stretch of `library`
    verbs silently ages out a sibling `editbuffer` lease of the same session.
    Renewal at entry only covers a verb that OUTRUNS its TTL — see
    :func:`keep_alive` for the in-flight half.

    NO token set → no-op. Unlocked callers, read-only verbs included, keep
    behaving exactly as before; this never makes a read start taking a lock.
    """
    tok = token if token is not None else env_token()
    if not tok:
        return
    missing = [s for s in _normalize_scopes(scopes)
               if (c := covering_lease(ip, s, tok)) is None
               or _unrenewable(c[1])]
    # Renew EVERY lease this token owns, not just the ones this verb touches:
    # a token is one session, and running any verb is proof the whole session
    # is alive. Renewing only the verb's scopes let a multi-scope agent lease
    # (`--scope editbuffer --scope library`) drift — a stretch of `library`
    # verbs never renewed `editbuffer`, which expired and was reclaimed by a
    # contender while the session was demonstrably active.
    held, _ = _renew_owned(ip, tok)
    if not missing:
        return
    # name the scope someone else actually holds, when there is one
    blocked = next(((s, h) for s in missing
                    if (h := _foreign_holder(ip, s, tok)) is not None), None)
    # an EXPIRED lease of ours still on disk is positive proof we lost that
    # scope — the one partial loss we can tell apart from "never held it"
    expired = next((s for s in missing if _expired_own_session(ip, s, tok)),
                   None)
    if strict and blocked is not None:
        scope, holder = blocked
    elif expired is not None:
        scope, holder = expired, _foreign_holder(ip, expired, tok)
    elif held:
        return  # session alive, just narrower than this verb — not our problem
    elif (elsewhere := _owned_on_other_device(ip, tok)) is not None:
        # The session is alive on ANOTHER address — never held here, so this
        # is not a reclaim and must not refuse. But a read carrying a token
        # that holds nothing HERE is running unlocked while its caller
        # believes otherwise: the #97 failure, one address-spelling apart
        # (`helix.local` vs the dotted quad). Mutating verbs need no warning
        # — they go on to acquire this address transiently.
        if strict:
            print(f"warning: your $HELIXGEN_LOCK_TOKEN holds a lease under "
                  f"device address '{elsewhere}', not '{ip}' — leases are "
                  f"keyed by address, so this read is UNLOCKED. Take a lease "
                  f"under the address you are driving and use one spelling "
                  f"of it for the whole session.", file=sys.stderr)
        return
    else:
        scope, holder = blocked or (missing[0], None)
    raise LockLost(ip, scope, holder, partial=bool(held))


# --------------------------------------------------------------------------
# lease file primitives (atomic create / renew / remove)
# --------------------------------------------------------------------------

def _write_new(path: Path, payload: dict) -> bool:
    """Atomically create ``path`` with ``payload``; False if it now exists.

    Writes a fully-formed 0600 temp file first, then links it into place
    (readers never observe a partial lease). Falls back to O_EXCL direct
    write on filesystems without hard links.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(body)
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass  # hard links unsupported here — O_EXCL fallback below
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            f.write(body)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _rewrite(path: Path, payload: dict, *,
             expect_nonce: str | None = None) -> bool:
    """Atomically replace an existing lease (renewal / re-label). The lease
    carries the private token, so the file stays 0600. Best-effort — a
    renewal never fails the verb; returns True on success, False on a no-op.

    When ``expect_nonce`` is given, the on-disk lease is re-read immediately
    before the replace and the write is SKIPPED unless its ``nonce`` still
    matches — so a renewal that raced a concurrent break+re-acquire can never
    clobber the lease now owned by someone else (#72 TOCTOU)."""
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if expect_nonce is not None:
            current = read_lease(path)
            if current is None or current.get("nonce") != expect_nonce:
                tmp.unlink(missing_ok=True)
                return False  # lease vanished or was re-acquired — do not clobber
        os.replace(tmp, path)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


def _renew(path: Path, lease: dict) -> bool:
    """Refresh a lease's acquired_at (TTL renewal). Callers only renew
    leases with a comfortable TTL margin (:data:`RENEW_MARGIN_S`), so this
    replace cannot land on top of a waiter's legitimately re-acquired
    lease (waiters only act after expiry). Nonce-guarded regardless: if the
    file was broken + re-acquired since we read it, the renewal no-ops rather
    than resurrecting our lease on top of the new owner's (#72 TOCTOU).

    Returns whether the lease is STILL OURS afterwards — not whether the
    write happened. A no-op write has two very different causes: the lease
    was broken + re-acquired under us (we lost it, and reporting it as held
    is the silent-expiry failure #97 exists to prevent) or the write itself
    failed (the lease file is untouched and still ours, just not refreshed —
    refusing there would be a false alarm). Re-read to tell them apart.
    """
    fresh = dict(lease)
    fresh["acquired_at"] = time.time()
    if _rewrite(path, fresh, expect_nonce=lease.get("nonce")):
        return True
    current = read_lease(path)
    return (current is not None and not is_stale(current)
            and current.get("nonce") == lease.get("nonce"))


def _renew_owned(ip: str, token: str | None) -> tuple[int, float | None]:
    """Renew every RENEWABLE lease on ``ip`` owned by ``token`` (or by this
    process, when no token is set). Returns ``(how many we hold, shortest
    remaining TTL)`` — None when nothing owned has a TTL at all.

    Margin-guarded exactly like ``_acquire_one``'s passthrough renewal: at
    the expiry boundary a renewal could land on a waiter's legitimate
    re-acquisition, so a lease within :data:`RENEW_MARGIN_S` of expiry is
    left to expire — and, because nothing will ever renew it again, it is
    NOT counted as held either. Counting it would let a caller enter a long
    verb on a lease guaranteed to lapse seconds later (silent unlocked work,
    the #97 failure); not counting it makes :func:`check_session` refuse
    while the doomed lease is still nominally live. A renewal that finds the
    lease re-acquired under us is likewise not held (:func:`_renew`).
    """
    held = 0
    shortest: float | None = None
    for scope in VALID_SCOPES:
        path = lock_path(ip, scope)
        lease = read_lease(path)
        if lease is None or is_stale(lease) or not owned(lease, token):
            continue
        remaining = _remaining_ttl(lease)
        if remaining is not None and remaining <= RENEW_MARGIN_S:
            continue  # unrenewable at the boundary — already lost, in effect
        if not _renew(path, lease):
            continue  # gone or re-acquired under us — not ours any more
        held += 1
        if remaining is not None:
            shortest = remaining if shortest is None else min(shortest,
                                                              remaining)
    return held, shortest


#: Longest a :func:`keep_alive` heartbeat sleeps between renewals. Shorter
#: TTLs renew proportionally sooner (a third of the shortest owned TTL).
HEARTBEAT_MAX_S = 30.0
#: Floor on that sleep, so a tiny TTL can't spin the heartbeat thread. It is
#: why :data:`MIN_SESSION_TTL` exists: a lease this short would beat once and
#: still find itself inside :data:`RENEW_MARGIN_S` of expiry.
HEARTBEAT_MIN_S = 1.0


def _heartbeat_delay(shortest: float | None) -> float:
    """Sleep between renewals: a third of the shortest owned TTL, clamped to
    [:data:`HEARTBEAT_MIN_S`, :data:`HEARTBEAT_MAX_S`]. A third leaves two
    further chances to renew before the lease lapses; no TTL at all (a lease
    that never expires) just beats at the ceiling."""
    if shortest is None:
        return HEARTBEAT_MAX_S
    return max(HEARTBEAT_MIN_S, min(HEARTBEAT_MAX_S, shortest / 3))


@contextlib.contextmanager
def keep_alive(ip: str, *, token: str | None = None):
    """Renew owned leases in the background for one verb's duration (#97).

    Renewing only at verb ENTRY means a verb that runs LONGER than its TTL
    loses its lease mid-flight to a contender while it is still driving the
    device — reachable today with `device normalize` (human-paced, one
    `--seconds` window per snapshot) and the telemetry verbs' unbounded
    `--seconds`, against the 300 s default of a detached lease. A daemon
    thread re-renews on a third of the shortest owned TTL so an ACTIVE
    workflow genuinely cannot time out, whether the activity is many short
    calls or one long one.

    No lease owned at entry → no thread (an unlocked verb costs nothing).
    Best-effort throughout: a renewal failure never fails the verb.

    Losing ANY of the leases held at entry warns, not just the last one: a
    multi-scope session that drops one scope mid-call is the same #97
    untrustworthy output, and waiting for the count to reach zero would let
    a `--scope editbuffer --scope library` agent lose `editbuffer` to a
    contender in silence.
    """
    tok = token if token is not None else env_token()
    held, shortest = _renew_owned(ip, tok)
    if not held:
        yield
        return
    stop = threading.Event()

    def _beat() -> None:
        nonlocal shortest, held
        while True:
            if stop.wait(_heartbeat_delay(shortest)):
                return
            try:
                alive, shortest = _renew_owned(ip, tok)
            except OSError:
                return  # lease dir vanished — nothing useful left to renew
            except Exception as exc:  # noqa: BLE001
                # A daemon thread that dies quietly stops renewing, and the
                # lease then lapses mid-call with none of the warning below
                # — exactly the silent loss #97 exists to prevent. Say so.
                print(f"warning: device {ip} lock keep-alive stopped "
                      f"({type(exc).__name__}: {exc}) — the lease is no "
                      f"longer being renewed and may expire during this "
                      f"call. Check with `helixgen device lock --status`.",
                      file=sys.stderr)
                return
            if alive < held:
                # Losing a lease mid-call is exactly the #97 failure, and
                # here we have positive proof of it — say so rather than let
                # the verb print well-formed output for a device someone else
                # may now be driving. The call is not aborted (the body may be
                # mid-write); the next call refuses outright via LockLost.
                print(f"warning: device {ip} lock lease lapsed DURING this "
                      f"call (reclaimed, released, or expired) — this "
                      f"output may not describe a device you still hold. "
                      f"Stop and re-establish device state: re-take a lease "
                      f"and re-read what you were acting on.", file=sys.stderr)
                if not alive:
                    return  # nothing owned left to renew
                # keep beating for the scopes we DO still hold: stopping here
                # would let a surviving sibling lease (`library` when
                # `editbuffer` was reclaimed) age out unrenewed mid-call —
                # the same silent expiry, one scope over. A further loss
                # warns again.
                held = alive

    beat = threading.Thread(target=_beat, name="helixgen-lock-keepalive",
                            daemon=True)
    beat.start()
    try:
        yield
    finally:
        stop.set()
        beat.join(timeout=1.0)


def _break_stale(path: Path, lease: dict) -> bool:
    """Remove a stale lease. Serialized by a break-mutex file and
    re-verified UNDER the mutex, so two waiters can't both 'break' it and
    the slower one can never unlink the faster one's fresh live lease
    (2026-07-16 review finding 3). Returns True when the path is (now)
    free to create, False when a live lease remains."""
    mpath = path.with_name(path.name + ".break")
    payload = {"pid": os.getpid(), "t": time.time()}
    if not _write_new(mpath, payload):
        # another breaker is active; if it crashed, clear its old mutex
        try:
            if time.time() - mpath.stat().st_mtime > _BREAK_MUTEX_TTL_S:
                mpath.unlink(missing_ok=True)
        except OSError:
            pass
        return False  # let the next poll re-assess
    try:
        current = read_lease(path)
        if current is None:
            return True
        if not is_stale(current):
            return False  # renewed/replaced under us — a live lease stays
        print(f"warning: breaking stale device lock {path.name} held by "
              f"{describe(current)}", file=sys.stderr)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return True
    finally:
        mpath.unlink(missing_ok=True)


def _meta_lock_path(ip: str) -> Path:
    """The per-device acquisition meta-lock. A process holds it across the
    whole scan->create->verify critical section so two racers taking
    CONFLICTING scopes (e.g. ``all`` vs ``library``) can't both slip through
    the create->rescan gap and double-commit (#88). One meta-lock per DEVICE,
    not per scope, because ``all`` conflicts with every granular scope."""
    return lock_dir(ip) / ".acquire.lock"


def _read_meta(path: Path) -> dict | None:
    """Read the acquisition meta-lock JSON (``{pid, t, nonce}``). Missing /
    unreadable / corrupt → None. The meta-lock's shape differs from a scope
    lease (no ``acquired_at``/``ttl_seconds``/``hostname``), so it cannot reuse
    :func:`read_lease`/:func:`is_stale`."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _meta_is_stale(path: Path) -> bool:
    """A meta-lock is stale ONLY when its mtime is past :data:`_ACQUIRE_META_TTL_S`
    AND its owner process is dead/unknown. The pid-liveness gate (consistent
    with how scope leases judge staleness) means a live-but-overrunning holder
    whose critical section stalled past the TTL is NOT broken on mtime alone
    (#88 W1). A meta-lock is always same-host (machine-local), so the recorded
    pid can be probed directly."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False  # gone
    if time.time() - mtime <= _ACQUIRE_META_TTL_S:
        return False  # within TTL — a live holder
    data = _read_meta(path)
    pid = data.get("pid") if data is not None else None
    return not (isinstance(pid, int) and _pid_alive(pid))


def _break_stale_meta(path: Path) -> None:
    """Remove a stale acquisition meta-lock the race-free way :func:`_break_stale`
    removes a stale scope lease: serialized by a break-mutex file and
    re-verified STILL-stale UNDER that mutex, immediately before the unlink.

    This closes the check-then-unlink TOCTOU (#88 W2): two acquirers can no
    longer both stat the same stale meta and unlink across a gap — only the
    breaker holding the mutex unlinks, and it unlinks only the exact stale file
    it re-observes, so the slower reclaimer can never delete the FRESH meta the
    faster one recreated. No fresh recreate here — return and let the caller's
    next poll re-create + re-verify (mirrors ``_break_stale``'s reclaim)."""
    mpath = path.with_name(path.name + ".break")
    if not _write_new(mpath, {"pid": os.getpid(), "t": time.time()}):
        # another breaker is active; if it crashed, clear its old mutex
        try:
            if time.time() - mpath.stat().st_mtime > _BREAK_MUTEX_TTL_S:
                mpath.unlink(missing_ok=True)
        except OSError:
            pass
        return  # let the next poll re-assess
    try:
        if _meta_is_stale(path):  # re-verify STILL stale under the mutex
            path.unlink(missing_ok=True)
    finally:
        mpath.unlink(missing_ok=True)


def _acquire_meta(path: Path) -> str | None:
    """Atomically grab the acquisition meta-lock; returns the nonce written
    into it when held, else None. The nonce lets the holder verify at release
    that it still owns the file (:func:`_release_meta`): if a holder stalls
    past :data:`_ACQUIRE_META_TTL_S` and a second acquirer reclaims and
    re-creates the meta-lock, the first must not delete that fresh holder's
    file — the same #72 TOCTOU guard the rest of this module uses.

    A genuinely stale meta-lock (mtime past the TTL AND owner-dead, per
    :func:`_meta_is_stale`) is reclaimed through :func:`_break_stale_meta` —
    mutex-serialized and re-verified, never a bare stat-then-unlink — and None
    is returned so the caller re-assesses on the next poll. Never
    unlink-and-recreate in one breath, which could delete a fresh holder's
    file (#88 W2)."""
    nonce = uuid.uuid4().hex
    if _write_new(path, {"pid": os.getpid(), "t": time.time(), "nonce": nonce}):
        return nonce
    if _meta_is_stale(path):
        _break_stale_meta(path)  # crashed holder — reclaim next pass
    return None


def _release_meta(path: Path, nonce: str) -> None:
    """Release the acquisition meta-lock only if we still own it. If our
    critical section overran the TTL and another acquirer reclaimed and
    re-created the meta-lock, its nonce differs from ours: leave it alone
    rather than delete a fresh holder's file (#72 pattern, mirrors
    :meth:`LeaseSet.release`)."""
    data = _read_meta(path)
    if data is not None and data.get("nonce") == nonce:
        path.unlink(missing_ok=True)


def _post_create_conflict(ip: str, scope: str, payload: dict,
                          token: str | None):
    """After atomically creating our ``scope`` lease, re-scan the OTHER
    conflicting files: the pre-create scan and the create are not one
    atomic step, so a racer may have created a conflicting lease (e.g.
    `all` vs a granular scope) in between (review finding 2). Deterministic
    tiebreak — the OLDER (acquired_at, nonce) lease wins; both racers run
    the same rule, so exactly one backs off. Returns the winning foreign
    lease when WE must back off, else None."""
    ours = (payload.get("acquired_at"), payload.get("nonce") or "")
    own_path = lock_path(ip, scope)
    for cpath in _conflict_paths(ip, scope):
        if cpath == own_path:
            continue
        lease = read_lease(cpath)
        if lease is None or owned(lease, token) or is_stale(lease):
            continue
        theirs = (lease.get("acquired_at"), lease.get("nonce") or "")
        try:
            we_lose = theirs < ours
        except TypeError:
            we_lose = True  # unorderable foreign lease: yield
        if we_lose:
            return lease
    return None


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
    meta = _meta_lock_path(ip)
    while True:
        # 1. Pass through (and renew) a covering LIVE lease we own — unless
        #    it's within RENEW_MARGIN_S of expiry (then re-acquire fresh:
        #    renewing at the boundary could resurrect it on top of a
        #    waiter's legitimate re-acquisition; review finding 5).
        covered = False
        for cover in ({scope, ALL} if scope != ALL else {ALL}):
            cpath = lock_path(ip, cover)
            lease = read_lease(cpath)
            if lease is None or not owned(lease, token):
                continue
            remaining_ttl = _remaining_ttl(lease)
            if (not is_stale(lease)
                    and (remaining_ttl is None
                         or remaining_ttl > RENEW_MARGIN_S)):
                if not _renew(cpath, lease):
                    continue  # broken + re-acquired under us — not ours to
                    #           pass through; fall through and acquire fresh
                passthrough.append(cpath)
                covered = True
                break
        if covered:
            return

        # 2+3 run under the per-device acquisition meta-lock so the whole
        #     scan -> create -> verify critical section is serialized across
        #     processes: two racers taking CONFLICTING scopes (`all` vs a
        #     granular scope) can no longer both slip through the
        #     create->rescan gap and double-commit (#88). The meta-lock is
        #     held only for this brief section, never across the step-4 wait.
        blocker = None
        meta_contended = False
        meta_nonce = _acquire_meta(meta)
        if meta_nonce is not None:
            try:
                # 2. Find the blocking lease, clearing stale ones (mutex-
                #    serialized) and our OWN expired/nearly-expired leases (a
                #    stale owned lease must be reclaimed here, never spun on —
                #    review finding 1).
                for cpath in _conflict_paths(ip, scope):
                    lease = read_lease(cpath)
                    if lease is None:
                        continue
                    if owned(lease, token):
                        remaining_ttl = _remaining_ttl(lease)
                        if (cpath == lock_path(ip, scope)
                                and (is_stale(lease)
                                     or (remaining_ttl is not None
                                         and remaining_ttl <= RENEW_MARGIN_S))):
                            try:  # our own expired/expiring TARGET lease: clear
                                cpath.unlink()  # and re-create fresh below
                            except FileNotFoundError:
                                pass
                        # an owned covering/sibling lease never blocks us (a
                        # stale one is left for its owner or a breaker; never
                        # unlinked here — a session lease siblings rely on)
                        continue
                    if is_stale(lease):
                        if not _break_stale(cpath, lease):
                            lease = read_lease(cpath)  # live / breaker busy
                            if lease is not None:
                                blocker = lease
                                break
                        continue
                    blocker = lease
                    break

                # 3. Free? Atomic create, then re-verify the OTHER conflicting
                #    files (the scan and the create are not one atomic step —
                #    review finding 2). The older lease wins the tiebreak; the
                #    younger backs off, so both racers never proceed.
                if blocker is None:
                    payload = {
                        # a DETACHED lease records no pid at all (#97): the
                        # invoking shell's lifetime must not bound it.
                        "pid": None if kind == "detached" else (
                            os.getpid() if pid is None else int(pid)),
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
                        loser_of = _post_create_conflict(ip, scope, payload,
                                                         token)
                        if loser_of is None:
                            created.append((path, payload["nonce"]))
                            return
                        # we lost the cross-file tiebreak: back off and wait
                        lease = read_lease(path)
                        if (lease is not None
                                and lease.get("nonce") == payload["nonce"]):
                            try:
                                path.unlink()
                            except FileNotFoundError:
                                pass
                        blocker = loser_of
                    else:
                        # lost the same-file create race — reassess after a beat
                        blocker = read_lease(path)
            finally:
                _release_meta(meta, meta_nonce)
        else:
            meta_contended = True  # another acquirer holds it — retry soon

        # 4. Blocked (or racing): wait with backoff, or fail fast.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if (meta_contended or blocker is None
                    or owned(blocker, token) or is_stale(blocker)):
                # a race artifact, not a live foreign holder — one more
                # immediate pass costs nothing and avoids a bogus error
                attempt += 1
                if attempt < 50:
                    time.sleep(0.02)
                    continue
                blocker = blocker or {"label": "create race", "pid": None,
                                      "hostname": hostname(),
                                      "acquired_at": time.time()}
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
    if explicit:
        # validate but do NOT collapse to `all` — for release, every
        # explicitly named scope must be freed (review finding 10)
        for s in scopes:
            if s not in VALID_SCOPES:
                raise ValueError(f"unknown lock scope {s!r} "
                                 f"(valid: {', '.join(VALID_SCOPES)})")
        targets = tuple(dict.fromkeys(scopes))
    else:
        targets = VALID_SCOPES
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
            if not force:
                # TOCTOU guard: a concurrent breaker may have re-acquired this
                # lease since we judged it. Never unlink a live lease now owned
                # by someone else (#72); only remove the one we actually saw.
                current = read_lease(path)
                if (current is not None
                        and current.get("nonce") != lease.get("nonce")
                        and not owned(current, token) and not is_stale(current)):
                    if explicit:
                        raise LockError(
                            f"device {ip} scope '{scope}' is held by "
                            f"{describe(current)} — not yours (re-acquired "
                            f"since; use --force to break it deliberately)")
                    kept.append({"scope": scope, "holder": describe(current)})
                    continue
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


def session_lock(ip: str, scopes, *, label: str, ttl: float,
                 pid: int | None = None, detach: bool = False,
                 timeout: float | None = None) -> tuple[str, list]:
    """The ``device lock`` engine: per requested scope, RENEW an existing
    owned covering lease in place (applying the new label/ttl — and
    keeping/reporting its STORED token, so the token printed to the user
    always actually opens the lease; review finding 7) or acquire a fresh
    session lease. Returns ``(token, [(scope, "locked"|"renewed"), ...])``.
    All-or-nothing for the freshly-locked scopes: on contention, leases
    this call created are released (renewed ones are left).

    ``detach=True`` writes a DETACHED lease instead (``kind: "detached"``,
    no pid — #97): nothing ties it to the caller's process, so only the TTL
    (renewed by every covered verb) or an explicit ``device unlock``
    releases it. Re-locking an owned scope switches it to/from detached.
    ``detach=True`` with a non-positive ``ttl`` (0, negative — both mean "no
    expiry" to :func:`is_stale`) is refused: no pid AND no expiry leaves a
    lease nothing but ``unlock --force`` could ever clear."""
    if ttl is not None and not math.isfinite(ttl):
        raise ValueError(
            f"ttl must be a finite number of seconds (got {ttl!r}): "
            "nan/inf are not an expiry at all, and are not valid JSON")
    if detach and (ttl is None or ttl <= 0):
        raise ValueError(
            "--detach needs a positive --ttl: a detached lease records no "
            "pid, so with no expiry either nothing but `device unlock "
            "--force` could ever release it (covered verbs renew it)")
    if ttl is not None and 0 < ttl < MIN_SESSION_TTL:
        raise ValueError(
            f"ttl {ttl:g}s is too short to keep alive: renewal skips a lease "
            f"within {RENEW_MARGIN_S:g}s of expiry, so it would lapse "
            f"mid-workflow even while you are using it. Use "
            f"--ttl {MIN_SESSION_TTL:g} or more.")
    want = _normalize_scopes(scopes)
    kind = "detached" if detach else "session"
    tok = env_token()
    # Adopt the stored token of a live owned covering lease first — the
    # printed token must be the one that opens the lease.
    for scope in want:
        found = None
        for cover in ({scope, ALL} if scope != ALL else {ALL}):
            lease = read_lease(lock_path(ip, cover))
            if (lease is not None and not is_stale(lease)
                    and owned(lease, tok) and lease.get("token")):
                found = lease["token"]
                break
        if found:
            tok = found
            break
    tok = tok or new_token()
    outcomes: list[tuple[str, str]] = []
    fresh: list[LeaseSet] = []
    try:
        for scope in want:
            path = lock_path(ip, scope)
            lease = read_lease(path)
            remaining = _remaining_ttl(lease) if lease is not None else None
            if (lease is not None and not is_stale(lease)
                    and owned(lease, tok)
                    and (remaining is None or remaining > RENEW_MARGIN_S)):
                renewed = dict(lease)
                renewed.update(label=label, ttl_seconds=ttl, token=tok,
                               acquired_at=time.time(), kind=kind,
                               pid=None if detach else (
                                   os.getppid() if pid is None else int(pid)))
                if not _rewrite(path, renewed, expect_nonce=lease.get("nonce")):
                    # broken + re-acquired since we read it — acquire fresh
                    fresh.append(acquire(ip, (scope,), label=label, ttl=ttl,
                                         token=tok, pid=pid, kind=kind,
                                         timeout=timeout))
                    outcomes.append((scope, "locked"))
                    continue
                outcomes.append((scope, "renewed"))
            else:
                fresh.append(acquire(ip, (scope,), label=label, ttl=ttl,
                                     token=tok, pid=pid, kind=kind,
                                     timeout=timeout))
                outcomes.append((scope, "locked"))
    except BaseException:
        for ls in fresh:
            ls.release()
        raise
    return tok, outcomes
