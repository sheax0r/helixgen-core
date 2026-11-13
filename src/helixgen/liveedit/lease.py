"""The live editor's use of the machine-local advisory device locks.

`device live` is long-lived, so it must NOT sit on the ``editbuffer`` scope for
its whole life — that would lock the agent that launched it out of `device
load` / `normalize` / every other live-ops verb. Instead it takes the lease
when a write actually happens and gives it back once the page has been idle
for a moment. If another helixgen process holds the device, the write is
refused immediately (no waiting): the page shows who is driving.
"""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Callable, Optional

from helixgen import locks
from helixgen.liveedit.session import LiveError

LABEL = "device live"


class EditLease:
    def __init__(self, ip: str, *, enabled: bool = True, idle: float = 2.0,
                 max_hold: float = 300.0,
                 clock: Callable[[], float] = time.monotonic):
        self.ip, self.enabled = ip, enabled
        self.idle, self.max_hold = idle, max_hold
        self._clock = clock
        self._mu = threading.Lock()
        self._held: Optional[locks.LeaseSet] = None
        self._since = 0.0
        self._used = 0.0

    def _acquire(self, scopes) -> locks.LeaseSet:
        try:
            return locks.acquire(self.ip, scopes, label=LABEL, timeout=0)
        except locks.LockError as e:
            raise LiveError(str(e), status=409) from e

    def _drop(self) -> None:
        held, self._held = self._held, None
        if held is not None:
            held.release()

    def ensure(self) -> None:
        """Hold ``editbuffer`` for the write about to happen (no-op if already
        held). Raises :class:`LiveError` (409) naming the holder when another
        process has the device."""
        if not self.enabled:
            return
        with self._mu:
            now = self._clock()
            # a lease has a TTL; a very long unbroken session re-takes it
            # rather than let it go stale underneath us
            if self._held is not None and now - self._since > self.max_hold:
                self._drop()
            if self._held is None:
                self._held = self._acquire(("editbuffer",))
                self._since = now
            self._used = now

    def tick(self) -> None:
        """Release the lease once no write has needed it for ``idle`` seconds."""
        with self._mu:
            if self._held is not None and self._clock() - self._used > self.idle:
                self._drop()

    @contextlib.contextmanager
    def transient(self, scopes):
        """Hold ``scopes`` for the duration of one operation (Save)."""
        if not self.enabled:
            yield
            return
        held = self._acquire(tuple(scopes))
        try:
            yield
        finally:
            held.release()

    def release(self) -> None:
        with self._mu:
            self._drop()
