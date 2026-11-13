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
from helixgen.device.client import Container, HelixError
from helixgen.liveedit import chain as _chain
from helixgen.liveedit.events import decode_event

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
        self._own: "collections.OrderedDict[int, tuple]" = collections.OrderedDict()
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

    def _install(self, eb: Any, preset: Optional[dict]) -> None:
        built = _chain.build_chain(eb)
        with self.cond:
            self._chain = built
            self._params = _chain.param_index(built)
            self._blocks = _chain.block_index(built)
            if preset is not None:
                self._preset = {"cid": preset.get("cid"), "name": preset.get("name")}
            self._dirty = built["dirty"]
            self._connected = True
            self._own.clear()
            self.rev += 1
            self.version += 1
            self.cond.notify_all()

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
    def load(self) -> None:
        """ONE edit-buffer read (plus the active preset's identity) -> state.
        Raises :class:`LiveError` (502) and marks the session disconnected when
        the device cannot be read."""
        with self.devlock:
            try:
                eb = self.client.read_edit_buffer()
                preset = self.client.active_preset()
            except (HelixError, OSError) as e:
                self._set_connected(False)
                raise LiveError(f"could not read the device: {e}", status=502) from e
            self._awaiting_ref = False
            self._install(eb, preset)

    reread = load

    def _reread_quiet(self) -> None:
        try:
            self.load()
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
        while len(self._own) > _OWN_MAX:
            self._own.popitem(last=False)

    def _ensure_lease(self, current: Any) -> None:
        try:
            self.lease.ensure()
        except LiveError as e:
            e.value = current       # the page puts the control back
            raise

    def set_param(self, path: int, block: int, pid: int, value: Any, *,
                  client: Optional[str] = None) -> dict:
        """Write one param to the live edit buffer. The value is clamped to the
        range the model advertises (the device clamps nothing); returns
        ``{"value": <what was written>}``. ``client`` (the writing page's id)
        is echoed on the change message so that page can skip its own write."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise LiveError("value must be a number") from None
        if not math.isfinite(v):
            raise LiveError("value must be a finite number")
        key = (int(path), int(block), int(pid))
        with self.devlock:
            with self.cond:
                self._require_up()
                p = self._params.get(key)
                if p is None:
                    raise LiveError("no such control on the loaded tone "
                                    "(did the preset change?)", status=404)
                v = _chain.clamp(p, v)
                current = p["value"]
            self._ensure_lease(current)
            try:
                ok = self.client.set_param(key[0], key[1], key[2], v,
                                           force=True, fast=True)
                rid = getattr(self.client, "last_request_id", None)
            except (HelixError, OSError) as e:
                self._set_connected(False)
                raise LiveError(f"device write failed: {e}", status=502,
                                value=current) from e
            if not ok:
                raise LiveError("the device did not acknowledge the write",
                                status=502, value=current)
            with self.cond:
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
        return {"value": v}

    def set_bypass(self, path: int, block: int, enabled: Any, *,
                   client: Optional[str] = None) -> dict:
        """Enable/bypass one block in the live edit buffer."""
        key = (int(path), int(block))
        on = bool(enabled)
        with self.devlock:
            with self.cond:
                self._require_up()
                b = self._blocks.get(key)
                if b is None:
                    raise LiveError("no such block on the loaded tone "
                                    "(did the preset change?)", status=404)
                current = b["enabled"]
            self._ensure_lease(current)
            try:
                ok = self.client.set_block_enable(key[0], key[1], on, fast=True)
                rid = getattr(self.client, "last_request_id", None)
            except (HelixError, OSError) as e:
                self._set_connected(False)
                raise LiveError(f"device write failed: {e}", status=502,
                                value=current) from e
            if not ok:
                raise LiveError("the device did not acknowledge the write",
                                status=502, value=current)
            with self.cond:
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
        return {"enabled": on}

    def save(self) -> dict:
        """Persist the edit buffer into the ACTIVE preset, in place — the
        hardware's own Save. Only a preset in the user pool can be saved this
        way; anything else (a factory preset, an unresolvable id) is refused."""
        with self.devlock:
            with self.cond:
                self._require_up()
            with self.lease.transient(("editbuffer", "library")):
                try:
                    pre = self.client.active_preset()
                    cid, ccid = pre.get("cid"), pre.get("ccid")
                    if cid is None or ccid != int(Container.POOL):
                        raise LiveError(
                            "only a preset in the user pool can be saved in "
                            f"place (active preset: {pre.get('name') or cid!r}). "
                            "Use `helixgen device save` to store the edit "
                            "buffer as a new preset.", status=409)
                    ok = self.client.save_edit_buffer(cid)
                except (HelixError, OSError) as e:
                    self._set_connected(False)
                    raise LiveError(f"save failed: {e}", status=502) from e
            if not ok:
                raise LiveError("the device refused the save", status=502)
            with self.cond:
                self._preset = {"cid": cid, "name": pre.get("name")}
                self._dirty = False
                self._emit_meta()
        return {"saved": pre.get("name"), "cid": cid}

    # --- 2001 events ----------------------------------------------------------------
    def _is_own_echo(self, seq: int, key: tuple, value: Any) -> bool:
        own = self._own.get(seq)
        if own is None or own[0] != key:
            return False
        if isinstance(value, bool) or isinstance(own[1], bool):
            return bool(value) == bool(own[1])
        return _close(float(value), float(own[1]))

    def _apply(self, action: tuple) -> None:
        kind = action[0]
        with self.cond:
            if kind == "param":
                _, seq, path, block, pid, value = action
                key = (path, block, pid)
                if self._is_own_echo(seq, ("param",) + key, value):
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
                if self._is_own_echo(seq, ("bypass",) + key, on):
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
        reread = False
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
            elif kind == "presetref":
                ref = (action[1], action[2])
            elif kind in ("snapshot", "reread"):
                reread = True
            else:
                late.append(action)

        installed = False
        if blob is not None:
            try:
                eb = decode_blob(blob)
                preset = ({"cid": ref[0], "name": ref[1]} if ref is not None
                          else {"cid": None, "name": None})
                self._install(eb, preset)
                self._awaiting_ref = ref is None
                installed = True
            except Exception:  # noqa: BLE001 — an undecodable push: go read it
                reread = True
        if ref is not None and not installed:
            with self.cond:
                same = self._awaiting_ref or ref[0] == self._preset.get("cid")
                if same:
                    self._preset = {"cid": ref[0], "name": ref[1]}
                    self._awaiting_ref = False
                    self._emit_meta()
            if not same:
                reread = True          # another preset, and its blob never came
        if reread:
            self._reread_quiet()
        for action in late:
            self._apply(action)

    # --- housekeeping ---------------------------------------------------------------
    def tick(self) -> None:
        """Called periodically by the server loop: release an idle write lease,
        notice a silent device, retry a lost connection."""
        self.lease.tick()
        now = self._clock()
        heard = self._last_event is not None and now - self._last_event <= DEAD_AFTER
        if (self.liveness and self._connected and self._last_event is not None
                and not heard):
            self._set_connected(False)
        if (not self._connected and now - self._last_retry >= RETRY_EVERY
                and (heard or not self.liveness)):
            self._last_retry = now
            self._reread_quiet()
