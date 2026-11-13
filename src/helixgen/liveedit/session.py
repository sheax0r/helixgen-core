"""LiveSession — the state behind `helixgen device live`.

Holds ONE structure parsed from the device's live edit buffer
(:mod:`helixgen.liveedit.chain`) and keeps it true: page writes go straight to
the device (fast-ack, clamped, never queued here), and the device's 2001 change
stream updates it in place. The device is the only source of truth — there is
no `.hsp` in this session.

Locking (lock order: ``devlock`` then ``cond``, never the reverse):

* ``devlock`` serializes device I/O — the client is not thread-safe.
* ``cond`` guards the in-memory state and the change log; it is only ever held
  for microseconds, so a slow device read never stalls the page's event stream.

Changes reach the page as a log of small messages (``param`` / ``bypass`` /
``meta``); a structural change (preset or snapshot switch, re-read) bumps
``rev`` and the page gets the full state instead.
"""
from __future__ import annotations

import collections
import contextlib
import math
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from helixgen.device import content as _content
from helixgen.device.client import HelixError
from helixgen.liveedit import chain as _chain
from helixgen.liveedit.events import DIRTY_KEY, decode_event

# A snapshot recall is announced as /activateSnapshot followed by the batch of
# values it applied. When our re-read for the first is slow, the second is
# still queued when it finishes — and what it describes is already in that
# read. Within this window after a snapshot re-read, the batch is not news.
SNAPSHOT_SETTLE = 1.0
DEAD_AFTER = 6.0      # s of total silence on the PUB streams = disconnected
RETRY_EVERY = 3.0     # s between reconnect attempts while disconnected
_OWN_MAX = 256        # own write echoes remembered


class LiveError(Exception):
    """A request the session refuses or could not complete. ``status`` is the
    HTTP status the server answers with; ``value`` (when set) is what the
    control should show instead — the device's last known value."""

    def __init__(self, msg: str, *, status: int = 400, value: Any = None):
        super().__init__(msg)
        self.status = status
        self.value = value


def decode_blob(blob: bytes) -> Any:
    """A pushed ``/setEditBuffer`` blob -> the same dict ``read_edit_buffer``
    returns."""
    return _content.decode_any(blob)


class _NoLease:
    def ensure(self) -> None:
        pass

    def done(self) -> None:
        pass

    def transient(self, scopes):
        return contextlib.nullcontext()

    def tick(self) -> None:
        pass


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-4 * max(1.0, abs(a), abs(b))


class LiveSession:
    LOG_MAX = 512

    def __init__(self, client, *, lease=None, liveness: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.lease = lease if lease is not None else _NoLease()
        self.liveness = liveness
        self._clock = clock
        self.cond = threading.Condition()
        self.devlock = threading.Lock()
        self.version = 0          # bumps on every message
        self.rev = 0              # bumps on every structural change
        self._log: Deque[Tuple[int, dict]] = collections.deque(maxlen=self.LOG_MAX)
        self._chain: dict = {"snapshot": None, "snapshots": [], "dirty": False, "paths": []}
        self._params: Dict[Tuple[int, int, int], dict] = {}
        self._blocks: Dict[Tuple[int, int], dict] = {}
        self._preset: dict = {"cid": None, "name": None}
        self._dirty = False
        self._connected = True
        self._awaiting_ref = False
        # own writes awaiting their 2001 echo: request id -> (control, value),
        # and per control the request id of our NEWEST write to it
        self._own: "collections.OrderedDict[int, tuple]" = collections.OrderedDict()
        self._latest: Dict[tuple, int] = {}
        # the control a write is on the wire for, and the events for it that
        # arrived meanwhile (its echo can beat its ack to us)
        self._writing: Optional[tuple] = None
        self._deferred: List[tuple] = []
        self._gen = 0             # bumps on every install (stale-read guard)
        self._need_reread = False
        self._snapshot_reread_at = -math.inf
        self._last_event: Optional[float] = None
        self._last_retry = -math.inf

    # --- state (all under cond) ---------------------------------------------
    def _meta(self) -> dict:
        snap = self._chain.get("snapshot")
        names = self._chain.get("snapshots") or []
        name = names[snap] if isinstance(snap, int) and 0 <= snap < len(names) else None
        return {"dirty": self._dirty, "connected": self._connected,
                "preset": dict(self._preset), "snapshot": snap,
                "snapshot_name": name}

    def _emit(self, msg: dict) -> None:
        self.version += 1
        self._log.append((self.version, msg))
        self.cond.notify_all()

    def _emit_meta(self) -> None:
        self._emit({"t": "meta", **self._meta()})

    def _set_dirty(self, dirty: bool) -> None:
        if dirty != self._dirty:
            self._dirty = dirty
            self._emit_meta()

    def _set_connected(self, up: bool) -> None:
        with self.cond:
            if up != self._connected:
                self._connected = up
                self._emit_meta()

    def _install(self, eb: Any, preset: Optional[dict],
                 dirty: Optional[bool] = False, *,
                 expect_gen: Optional[int] = None,
                 awaiting_ref: bool = False) -> bool:
        """Swap in a freshly parsed buffer. ``preset`` / ``dirty`` of ``None``
        keep what the session has. With ``expect_gen``, the swap only happens
        if no OTHER install landed since that generation was read (a pushed
        buffer is newer than a read that was in flight when it arrived);
        returns whether it happened."""
        built = _chain.build_chain(eb)
        params, blocks = _chain.param_index(built), _chain.block_index(built)
        with self.cond:
            if expect_gen is not None and self._gen != expect_gen:
                return False
            self._chain, self._params, self._blocks = built, params, blocks
            if preset is not None:
                self._preset = {"cid": preset.get("cid"), "name": preset.get("name")}
            if dirty is not None:
                self._dirty = dirty
            self._connected = True
            self._awaiting_ref = awaiting_ref
            self._need_reread = False
            self._own.clear()
            self._latest.clear()
            self._deferred = []       # they addressed the buffer just replaced
            self._gen += 1
            self.rev += 1
            self.version += 1
            self.cond.notify_all()
        return True

    def public_state(self) -> dict:
        with self.cond:
            return {"rev": self.rev, "version": self.version, **self._meta(),
                    "snapshots": list(self._chain.get("snapshots") or []),
                    "paths": self._chain["paths"]}

    def param(self, path: int, block: int, pid: int) -> dict:
        with self.cond:
            return dict(self._params[(path, block, pid)])

    def block(self, path: int, block: int) -> dict:
        with self.cond:
            return dict(self._blocks[(path, block)])

    # --- change log -----------------------------------------------------------
    def changes_since(self, version: int, rev: int) -> Tuple[int, int, List[dict]]:
        """Messages a page at ``(version, rev)`` is missing, as
        ``(version, rev, msgs)``. A structural change, or a page too far behind
        for the bounded log, gets one full ``state`` message instead."""
        with self.cond:
            if rev == self.rev and version == self.version:
                return self.version, self.rev, []
            overflow = not self._log or self._log[0][0] > version + 1
            if rev != self.rev or overflow:
                state = {"t": "state", "rev": self.rev, "version": self.version,
                         **self._meta(),
                         "snapshots": list(self._chain.get("snapshots") or []),
                         "paths": self._chain["paths"]}
                return self.version, self.rev, [state]
            return self.version, self.rev, [m for v, m in self._log if v > version]

    def wait(self, version: int, rev: int, timeout: float) -> None:
        """Block until something changed past ``(version, rev)`` or ``timeout``."""
        with self.cond:
            self.cond.wait_for(
                lambda: self.version != version or self.rev != rev, timeout)

    # --- device reads -----------------------------------------------------------
    def load(self, *, identity: bool = True) -> None:
        """ONE edit-buffer read -> state. With ``identity`` (startup, reconnect,
        an explicit re-read) the active preset's identity and the unsaved flag
        are read too; a snapshot switch or an in-place change of the SAME
        preset passes ``identity=False`` and costs only the buffer read.

        Raises :class:`LiveError` (502) and marks the session disconnected when
        the device cannot be read. If a pushed buffer is installed while the
        read is in flight, the read is stale and is taken again."""
        with self.devlock:
            for attempt in range(3):
                with self.cond:
                    gen = self._gen
                try:
                    eb = self.client.read_edit_buffer(fast=True)
                    preset = self.client.active_preset() if identity else None
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"could not read the device: {e}", status=502) from e
                dirty: Optional[bool] = None
                if identity:
                    try:  # the unsaved-edits flag is a device property, not in the buffer
                        dirty = bool(self.client.get_property(DIRTY_KEY).value)
                    except Exception:  # noqa: BLE001 — cosmetic; never block a load on it
                        dirty = False
                try:
                    if self._install(eb, preset, dirty,
                                     expect_gen=gen if attempt < 2 else None):
                        return
                except LiveError:
                    raise
                except Exception as e:  # noqa: BLE001 — an unparseable buffer
                    raise LiveError(f"could not parse the edit buffer: {e!r}",
                                    status=502) from e

    def reread(self) -> None:
        """An explicit, full re-read (the page's Re-read control)."""
        self.load()

    def _reread_quiet(self, *, identity: bool = True) -> None:
        try:
            self.load(identity=identity)
        except LiveError:
            pass          # already marked disconnected; tick() keeps retrying

    # --- writes -------------------------------------------------------------------
    def _require_up(self) -> None:
        if not self._connected:
            raise LiveError("the device is disconnected — re-read to retry",
                            status=503)

    def _remember(self, rid: Optional[int], key: tuple, value: Any) -> None:
        if rid is None:
            return
        self._own[rid] = (key, value)
        self._latest[key] = rid
        while len(self._own) > _OWN_MAX:
            old, (k, _v) = self._own.popitem(last=False)
            if self._latest.get(k) == old:
                del self._latest[k]

    def _ensure_lease(self, current: Any) -> None:
        """Take the write lease for the write about to happen; the caller MUST
        call ``self.lease.done()`` when the write is over."""
        try:
            self.lease.ensure()
        except LiveError as e:
            e.value = current       # the page puts the control back
            raise

    def _begin_write(self, key: tuple) -> None:
        """(under ``cond``) Events for ``key`` are held from now until
        :meth:`_end_write`: the echo of the write we are about to send can
        reach the subscriber thread before its ack reaches us, i.e. before we
        know the request id that identifies it as ours."""
        self._writing = key

    def _end_write(self) -> None:
        """(under ``cond``) Release the held events, in arrival order."""
        held, self._deferred, self._writing = self._deferred, [], None
        for action in held:
            self._apply_locked(action)

    def _stale_commit(self, rev0: int, lookup) -> None:
        """Called under ``cond`` with the ack in hand. If the structure changed
        while the write was on the wire (a pushed preset does not wait for
        ``devlock``), the value we sent belongs to the OLD tone: do not write
        it into the new one's state, tell the page, and go read the truth."""
        if self.rev != rev0:
            self._need_reread = True
            now = lookup()
            raise LiveError("the tone changed while this change was on its way "
                            "— re-reading the device", status=409, value=now)

    def _require_rev(self, rev: Optional[int], current: Any) -> None:
        """Refuse a write from a page still showing an OLDER structure: its
        control may now sit over a different param (the preset, the snapshot or
        a model changed between its render and this request)."""
        if rev is not None and rev != self.rev:
            raise LiveError("the tone changed on the device — this control "
                            "was refreshed instead of written", status=409,
                            value=current)

    def set_param(self, path: int, block: int, pid: int, value: Any, *,
                  client: Optional[str] = None,
                  rev: Optional[int] = None) -> dict:
        """Write one param to the live edit buffer. The value is clamped to the
        range the model advertises (the device clamps nothing); returns
        ``{"value": <what was written>}``. ``client`` (the writing page's id)
        is echoed on the change message so that page can skip its own write."""
        try:
            v = float(value)
        except (TypeError, ValueError, OverflowError):
            raise LiveError("value must be a number") from None
        if not math.isfinite(v):
            raise LiveError("value must be a finite number")
        key = (int(path), int(block), int(pid))
        with self.devlock:
            with self.cond:
                self._require_up()
                p = self._params.get(key)
                self._require_rev(rev, p["value"] if p else None)
                if p is None:
                    raise LiveError("no such control on the loaded tone "
                                    "(did the preset change?)", status=404)
                v = _chain.clamp(p, v)
                current, ptype, rev0 = p["value"], p["type"], self.rev
            self._ensure_lease(current)
            with self.cond:
                self._begin_write(("param",) + key)
            committed = False
            try:
                try:
                    ok = self.client.set_param(key[0], key[1], key[2], v,
                                               force=True, fast=True, ptype=ptype)
                    rid = getattr(self.client, "last_request_id", None)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device write failed: {e}", status=502,
                                    value=current) from e
                finally:
                    self.lease.done()
                if not ok:
                    raise LiveError("the device did not acknowledge the write",
                                    status=502, value=current)
                with self.cond:
                    self._stale_commit(
                        rev0, lambda: (self._params.get(key) or {}).get("value"))
                    self._remember(rid, ("param",) + key, v)
                    live = self._params.get(key)
                    if live is not None:
                        live["value"] = v
                    msg = {"t": "param", "path": key[0], "block": key[1],
                           "pid": key[2], "value": v}
                    if client:
                        msg["client"] = client
                    self._emit(msg)
                    self._set_dirty(True)
                    self._end_write()
                    committed = True
            finally:
                if not committed:
                    with self.cond:
                        self._end_write()
        return {"value": v}

    def set_bypass(self, path: int, block: int, enabled: Any, *,
                   client: Optional[str] = None,
                   rev: Optional[int] = None) -> dict:
        """Enable/bypass one block in the live edit buffer."""
        key = (int(path), int(block))
        on = bool(enabled)
        with self.devlock:
            with self.cond:
                self._require_up()
                b = self._blocks.get(key)
                self._require_rev(rev, b["enabled"] if b else None)
                if b is None:
                    raise LiveError("no such block on the loaded tone "
                                    "(did the preset change?)", status=404)
                current, rev0 = b["enabled"], self.rev
            self._ensure_lease(current)
            with self.cond:
                self._begin_write(("bypass",) + key)
            committed = False
            try:
                try:
                    ok = self.client.set_block_enable(key[0], key[1], on, fast=True)
                    rid = getattr(self.client, "last_request_id", None)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"device write failed: {e}", status=502,
                                    value=current) from e
                finally:
                    self.lease.done()
                if not ok:
                    raise LiveError("the device did not acknowledge the write",
                                    status=502, value=current)
                with self.cond:
                    self._stale_commit(
                        rev0, lambda: (self._blocks.get(key) or {}).get("enabled"))
                    self._remember(rid, ("bypass",) + key, on)
                    live = self._blocks.get(key)
                    if live is not None:
                        live["enabled"] = on
                    msg = {"t": "bypass", "path": key[0], "block": key[1],
                           "enabled": on}
                    if client:
                        msg["client"] = client
                    self._emit(msg)
                    self._set_dirty(True)
                    self._end_write()
                    committed = True
            finally:
                if not committed:
                    with self.cond:
                        self._end_write()
        return {"enabled": on}

    def save(self) -> dict:
        """Persist the edit buffer into the ACTIVE tone's preset, in place — the
        hardware's own Save. The target is the user-pool preset that holds the
        tone (for one loaded from a setlist, the preset its setlist entry
        points at — so the save shows in every setlist holding that preset);
        a factory preset or an unresolvable id is refused. Returns the pool
        ``cid`` that was written."""
        with self.devlock:
            with self.cond:
                self._require_up()
            with self.lease.transient(("editbuffer", "library")):
                try:
                    pre = self.client.active_preset()
                    active = pre.get("cid")
                    # a tone loaded from a setlist is active under its setlist
                    # REFERENCE; the content lives in the pool preset behind it
                    cid = (self.client.resolve_pool_cid(active, pre.get("ccid"))
                           if active is not None else None)
                    if cid is None:
                        raise LiveError(
                            "only a user preset can be saved in place (active "
                            f"preset: {pre.get('name') or active!r} is a "
                            "factory preset or does not resolve). Use "
                            "`helixgen device save` to store the edit buffer "
                            "as a new preset.", status=409)
                    ok = self.client.save_edit_buffer(cid)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"save failed: {e}", status=502) from e
            if not ok:
                raise LiveError("the device refused the save", status=502)
            with self.cond:
                self._preset = {"cid": active, "name": pre.get("name")}
                self._dirty = False
                self._emit_meta()
        return {"saved": pre.get("name"), "cid": cid}

    # --- 2001 events ----------------------------------------------------------------
    def _own_echo(self, seq: int, key: tuple, value: Any) -> Optional[str]:
        """Is this event the echo of one of OUR writes?

        ``None``     — no: someone else's change.
        ``"stale"``  — the echo of an own write that a NEWER own write to the
                       same control has since replaced: drop it, or a late echo
                       drags the control backwards mid-gesture.
        ``"latest"`` — the echo of our newest write to that control. The
                       stream is the device's own ordering, so this is what the
                       device holds NOW — apply it like any event (it only
                       changes anything if a foreign change slipped in between
                       our ack and here).

        A remembered request id is consumed by its echo: ids are small
        counters that every helixgen process starts from the same number."""
        own = self._own.get(seq)
        if own is None or own[0] != key:
            return None
        if isinstance(value, bool) or isinstance(own[1], bool):
            same = bool(value) == bool(own[1])
        else:
            same = _close(float(value), float(own[1]))
        if not same:
            return None
        del self._own[seq]
        if self._latest.get(key) == seq:
            del self._latest[key]
            return "latest"
        return "stale"

    def _apply(self, action: tuple) -> None:
        with self.cond:
            kind = action[0]
            if kind == "param":
                target = ("param", action[2], action[3], action[4])
            elif kind == "bypass":
                target = ("bypass", action[2], action[3])
            else:
                target = None
            if target is not None and target == self._writing:
                self._deferred.append(action)   # judged once the write's ack is in
                return
            self._apply_locked(action)

    def _apply_locked(self, action: tuple) -> None:
        kind = action[0]
        if kind == "param":
            _, seq, path, block, pid, value = action
            key = (path, block, pid)
            if self._own_echo(seq, ("param",) + key, value) == "stale":
                return
            p = self._params.get(key)
            if p is None or _close(p["value"], value):
                return
            p["value"] = value
            self._emit({"t": "param", "path": path, "block": block,
                        "pid": pid, "value": value})
        elif kind == "bypass":
            _, seq, path, block, on = action
            key = (path, block)
            if self._own_echo(seq, ("bypass",) + key, on) == "stale":
                return
            b = self._blocks.get(key)
            if b is None or b["enabled"] == on:
                return
            b["enabled"] = on
            self._emit({"t": "bypass", "path": path, "block": block,
                        "enabled": on})
        elif kind == "dirty":
            self._set_dirty(action[1])

    def handle_events(self, events: list) -> None:
        """Digest one poll batch from the device's PUB stream(s). Value changes
        apply in place; however many structural events a batch holds, at most
        ONE re-read follows (the slower the device, the more they coalesce).
        Never raises: a failed re-read marks the session disconnected."""
        if events:
            self._last_event = self._clock()
        blob = ref = None
        reread = snapshot = identity = False
        late: List[tuple] = []
        for ev in events:
            if getattr(ev, "port", 2001) != 2001:
                continue
            action = decode_event(ev.addr, ev.args)
            if action is None:
                continue
            kind = action[0]
            if kind == "editbuffer":
                blob, ref, reread, late = action[1], None, False, []
                snapshot = False
            elif kind == "presetref":
                ref = (action[1], action[2])
            elif kind == "snapshot":
                reread = snapshot = True
            elif kind == "reread":
                straggler = (action[1] == "/setBatchedParamVals" and
                             self._clock() - self._snapshot_reread_at < SNAPSHOT_SETTLE)
                if not straggler:
                    reread = True
            else:
                late.append(action)

        installed = False
        if blob is not None:
            try:
                eb = decode_blob(blob)
                preset = ({"cid": ref[0], "name": ref[1]} if ref is not None
                          else {"cid": None, "name": None})
                self._install(eb, preset, awaiting_ref=ref is None)
                installed = True
            except Exception:  # noqa: BLE001 — an undecodable push: go read it
                reread = identity = True
        if ref is not None and not installed:
            with self.cond:
                same = self._awaiting_ref or ref[0] == self._preset.get("cid")
                if same:
                    self._preset = {"cid": ref[0], "name": ref[1]}
                    self._awaiting_ref = False
                    self._emit_meta()
            if not same:
                reread = identity = True   # another preset, and its blob never came
        if reread:
            self._reread_quiet(identity=identity)
            if snapshot:
                self._snapshot_reread_at = self._clock()
            # every value event in this batch was emitted BEFORE that read, so
            # the read already holds it — replaying one would put a pre-switch
            # value back on top of the new state
            late = [a for a in late if a[0] == "dirty"]
        for action in late:
            self._apply(action)

    # --- housekeeping ---------------------------------------------------------------
    def tick(self) -> None:
        """Called periodically by the server loop: release an idle write lease,
        notice a silent device, retry a lost connection."""
        self.lease.tick()
        with self.cond:
            pending = self._need_reread and self._connected
        if pending:
            self._reread_quiet()
        now = self._clock()
        heard = self._last_event is not None and now - self._last_event <= DEAD_AFTER
        if (self.liveness and self._connected and self._last_event is not None
                and not heard):
            self._set_connected(False)
        if (not self._connected and now - self._last_retry >= RETRY_EVERY
                and (heard or not self.liveness)):
            self._last_retry = now
            self._reread_quiet()
