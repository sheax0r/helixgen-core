"""HelixSubscriber — live event subscriber for the Helix Stadium's PUB streams.

The device publishes state on two ZeroMQ ``PUB`` sockets that a client joins
with a ``SUB`` socket subscribed to all topics:

- **2001** — property-change notifications (``/setEditBuffer`` +
  ``/setPropertyValue`` firehose). Device→editor frames on this port carry a
  **12-byte binary header** (version / sequence / length) *before* the OSC
  packet.
- **2003** — DSP telemetry (``/dspEvent``, ``/trigger``, ``/meter``,
  ``/heartbeat``). No header — the frame *is* the OSC packet.

Both framings are handled uniformly: we locate the first ``/`` in the frame and
parse the OSC message from there, which naturally skips the 2001 header.

This module reuses helixgen's existing codecs (``osc``, ``content``) rather than
reimplementing OSC/msgpack.  ``pyzmq`` and ``msgpack`` are imported lazily (as in
``client.py``); a missing dependency raises ``HelixError``.
"""
from __future__ import annotations

import time
from typing import Iterable, Iterator, List, NamedTuple, Optional, Sequence, Tuple

from .osc import parse_osc_message
from . import content as _content
from .client import HelixError

# OSC addresses that are pure background chatter — a 1 Hz clock tick and a
# periodic keep-alive. Dropped by ``stream`` unless ``include_noise=True``.
NOISE_ADDRS = frozenset({"/trigger", "/heartbeat"})


class Event(NamedTuple):
    """One decoded PUB message: which port it came from, its OSC address, and
    its already-decoded argument values (blob args msgpack-decoded, except a
    ``_sbepgsm`` content blob which is left as raw ``bytes``)."""

    port: int
    addr: str
    args: list


class HelixSubscriber:
    """Subscribe to the Stadium's 2001/2003 PUB streams and yield ``Event``s.

    A single ZMQ ``Poller`` is registered on both ``SUB`` sockets so one
    ``poll`` call drains both ports.  Never blocks without a finite timeout.
    """

    def __init__(self, ip: str = "192.168.4.84",
                 ports: Sequence[int] = (2001, 2003)):
        self.ip = ip
        self.ports = tuple(ports)
        self._zmq = None
        self._ctx = None
        # {socket: port}
        self._socks: dict = {}
        self.poller = None
        # count of telemetry frames skipped because they failed to parse (a
        # single malformed PUB frame must not abort a live stream)
        self.skipped = 0

    # -- lazy deps ---------------------------------------------------------
    def _load_zmq(self):
        try:
            import zmq
        except ImportError as exc:
            raise HelixError(
                "the device feature needs pyzmq; install with "
                "`pip install 'helixgen[device]'`"
            ) from exc
        return zmq

    def _load_msgpack(self):
        try:
            import msgpack  # noqa: F401
        except ImportError as exc:
            raise HelixError(
                "the device feature needs msgpack; install with "
                "`pip install 'helixgen[device]'`"
            ) from exc
        return msgpack

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> "HelixSubscriber":
        """Open a SUB socket per port, subscribe to all topics, and register a
        shared Poller.  A lazily-connected SUB never errors on a dead host — it
        simply yields no events — so unlike the RPC client there is nothing to
        verify here."""
        zmq = self._load_zmq()
        try:
            self._zmq = zmq
            self._ctx = zmq.Context.instance()
            self.poller = zmq.Poller()
            for port in self.ports:
                sock = self._ctx.socket(zmq.SUB)
                sock.setsockopt(zmq.LINGER, 0)
                sock.setsockopt(zmq.SUBSCRIBE, b"")  # all topics
                sock.connect(f"tcp://{self.ip}:{port}")
                self.poller.register(sock, zmq.POLLIN)
                self._socks[sock] = port
        except zmq.ZMQError as exc:
            self.close()
            raise HelixError(f"could not open device SUB socket: {exc}") from exc
        return self

    def close(self) -> None:
        for sock in list(self._socks):
            try:
                sock.close()
            except Exception:  # noqa: BLE001 - closing must not raise
                pass
        self._socks = {}
        self.poller = None

    def __enter__(self) -> "HelixSubscriber":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- decode ------------------------------------------------------------
    @staticmethod
    def _decode_arg(tag: str, value):
        """Decode one OSC arg value; blobs become msgpack objects, except a
        ``_sbepgsm`` content blob which stays raw ``bytes``."""
        if tag != "b":
            return value
        if isinstance(value, (bytes, bytearray)) and bytes(value[:8]) == _content.MAGIC:
            return bytes(value)
        return _content.decode_blob(value)

    def _parse_frame(self, port: int, raw: bytes) -> Optional[Event]:
        """Turn one raw ZMQ frame into an ``Event`` (or ``None`` if it holds no
        OSC packet).  Finding the first ``/`` skips any binary header — the
        12-byte 2001 prefix or nothing on 2003."""
        i = raw.find(b"/")
        if i < 0:
            return None
        addr, rargs, _ = parse_osc_message(raw, i)
        args = [self._decode_arg(t, v) for t, v in rargs]
        return Event(port=port, addr=addr, args=args)

    # -- poll --------------------------------------------------------------
    def poll(self, timeout: float = 0.5) -> List[Event]:
        """Return every event available within a ``timeout``-second window,
        draining both ports.  The first ``poll`` waits up to the remaining
        window; once frames start arriving, subsequent polls use a 0 ms timeout
        so the socket queues drain quickly and the call returns promptly."""
        if self.poller is None:
            raise HelixError("subscriber is not connected; call connect() first")
        # zmq's exception type, or an empty tuple when a fake socket is injected
        zmq_error = getattr(self._zmq, "ZMQError", ()) if self._zmq else ()
        events: List[Event] = []
        deadline = time.monotonic() + max(timeout, 0.0)
        first = True
        while True:
            remaining = deadline - time.monotonic()
            wait_ms = int(max(remaining, 0.0) * 1000) if first else 0
            try:
                ready = dict(self.poller.poll(wait_ms))
            except zmq_error as exc:
                raise HelixError(f"device poll failed: {exc}") from exc
            if not ready:
                break
            for sock in ready:
                port = self._socks.get(sock)
                try:
                    raw = sock.recv()
                except zmq_error as exc:
                    raise HelixError(f"device recv failed: {exc}") from exc
                try:
                    ev = self._parse_frame(port, raw)
                except (ValueError, IndexError, KeyError, RuntimeError):
                    # a single malformed telemetry frame must not kill a live
                    # stream (tuner/meters/watch) — skip it and keep draining.
                    self.skipped += 1
                    continue
                if ev is not None:
                    events.append(ev)
            first = False
        return events

    # -- stream ------------------------------------------------------------
    def stream(self, duration: Optional[float] = None,
               filter_addrs: Optional[Iterable[str]] = None,
               include_noise: bool = False) -> Iterator[Event]:
        """Yield events until ``duration`` seconds elapse (or forever if
        ``None``).

        - ``filter_addrs`` — if given, only events whose address is in the set
          are yielded.
        - ``include_noise`` — when ``False`` (default), the pure-noise
          ``/trigger`` and ``/heartbeat`` addresses are dropped.

        Each underlying ``poll`` uses a finite timeout, so the generator never
        blocks indefinitely even when ``duration is None``.
        """
        allow = None if filter_addrs is None else set(filter_addrs)
        deadline = None if duration is None else time.monotonic() + duration
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                t = min(0.5, remaining)
            else:
                t = 0.5
            for ev in self.poll(timeout=t):
                if not include_noise and ev.addr in NOISE_ADDRS:
                    continue
                if allow is not None and ev.addr not in allow:
                    continue
                yield ev
