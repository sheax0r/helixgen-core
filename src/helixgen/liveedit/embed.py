"""Start the live chain editor IN-PROCESS and return a handle instead of
blocking — the library hub's "Edit →" handoff (libraryui phase 2).

Mirrors the ``device live`` bootstrap (``cli_device.device_live``) but hands
back a running server rather than serving in the foreground: its own
``HelixClient`` + ``editbuffer`` lease + PUB subscriber + server thread, all
independent of the hub's ``library``/``irs`` lease, so the two coexist (the
hub deliberately leaves ``editbuffer`` free for exactly this). ``stop()`` tears
it all down. No subprocess, no shelling out to the CLI.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class EmbeddedEditor:
    url: str
    _stop: Callable[[], None]

    def stop(self) -> None:
        self._stop()


def start(ip: str, port: int, *, http_port: int = 0,
          ready_timeout: float = 15.0) -> EmbeddedEditor:
    """Open the live editor on the device's ACTIVE tone and return a handle.

    Raises ``HelixError`` if the device is unreachable, below firmware 1.4, or
    the server does not come up within ``ready_timeout``. The caller loads the
    tone it wants edited BEFORE calling this, so the editor's initial read sees
    it (a later preset switch is followed live via the subscriber)."""
    from helixgen.device.client import HelixClient, HelixError
    from helixgen.device.subscribe import HelixSubscriber
    from helixgen.liveedit import server as _server
    from helixgen.liveedit.lease import EditLease
    from helixgen.liveedit.session import LiveSession

    client = HelixClient(ip, port).connect()
    dev_ip = getattr(client, "ip", None) or ip
    try:
        fw = (client.product_info() or {}).get("firmware") or ""
        parts = tuple(int(x) for x in fw.split(".")[:2])
    except Exception:  # noqa: BLE001 — unknown firmware: let it try
        parts = ()
    if parts and parts < (1, 4):
        client.close()
        raise HelixError("the live editor needs Stadium firmware 1.4 or later "
                         f"(this device reports {fw})")

    lease = EditLease(dev_ip, enabled=True)
    session = LiveSession(client, lease=lease, liveness=True)
    session.load()
    subscriber = HelixSubscriber(dev_ip, ports=(2001, 2003))

    ready = threading.Event()
    box: dict[str, Any] = {}

    def _ready(url: str, srv=None) -> None:
        box["url"], box["srv"] = url, srv
        ready.set()

    def _run() -> None:
        try:
            _server.serve(session, "127.0.0.1", http_port,
                          subscriber=subscriber, on_ready=_ready)
        finally:
            ready.set()  # unblock the waiter even if serve() never got ready

    threading.Thread(target=_run, daemon=True).start()
    if not ready.wait(ready_timeout) or "url" not in box:
        try:
            lease.release(final=True)
        finally:
            client.close()
        raise HelixError("the live editor did not start in time")

    srv = box.get("srv")

    def _stop() -> None:
        try:
            if srv is not None:
                srv.shutdown()
        finally:
            try:
                lease.release(final=True)
            finally:
                client.close()

    return EmbeddedEditor(box["url"], _stop)
