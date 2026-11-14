"""Local HTTP + SSE server for `helixgen device live`. Stdlib only, localhost
only.

The page POSTs writes (``/param``, ``/bypass``, ``/save``, ``/reread``) and
holds one SSE connection (``/events``) on which it is pushed every change —
small ``param`` / ``bypass`` / ``meta`` messages, or the full ``state`` when
the structure changed. A background thread drains the device's PUB stream into
the session. All locking lives in :class:`LiveSession`.

The Host / Origin / Content-Type guards (anti DNS-rebind, anti cross-origin)
and the stream-draining loop are lifted from the unmerged `tweak-ui` branch's
``tweak/server.py``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from helixgen.liveedit.session import LiveError, LiveSession

_PAGE = Path(__file__).with_name("page.html")
logger = logging.getLogger("helixgen.liveedit")

SSE_KEEPALIVE = 15.0
MAX_BODY = 64 * 1024      # a write is ~100 bytes
SOCKET_TIMEOUT = 20.0     # a half-sent request must not park a thread forever


def _int(body: dict, key: str) -> int:
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise LiveError(f"field {key!r} must be an integer")
    return v


def _client(body: dict):
    """The writing page's self-chosen id (so it can skip its own change
    message); anything that is not a short string is ignored."""
    c = body.get("client")
    return c if isinstance(c, str) and 0 < len(c) <= 64 else None


def _rev(body: dict):
    """The structure revision the writing page is showing (optional)."""
    r = body.get("rev")
    return r if isinstance(r, int) and not isinstance(r, bool) else None


def _route(session: LiveSession, path: str, b: dict):
    """Mutating endpoints -> response dict, or ``None`` for an unknown route."""
    if path == "/param":
        if "value" not in b:
            raise LiveError("missing field 'value'")
        v = b["value"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise LiveError("field 'value' must be a number")
        return session.set_param(_int(b, "path"), _int(b, "block"),
                                 _int(b, "pid"), v, client=_client(b),
                                 rev=_rev(b))
    if path == "/bypass":
        if not isinstance(b.get("enabled"), bool):
            raise LiveError("field 'enabled' must be true or false")
        return session.set_bypass(_int(b, "path"), _int(b, "block"),
                                  b["enabled"], client=_client(b),
                                  rev=_rev(b))
    if path == "/snapshot":
        return session.activate_snapshot(_int(b, "index"))
    if path == "/preset":
        return session.load_preset(_int(b, "cid"))
    if path == "/save":
        return session.save()
    if path == "/reread":
        session.reread()
        return {}
    return None


def make_server(session: LiveSession, host: str = "127.0.0.1",
                port: int = 0) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the live editor binds localhost only")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = SOCKET_TIMEOUT

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # the page must never be framed by another site (clickjacking Save)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _hosts(self) -> set:
            p = self.server.server_address[1]
            return {f"127.0.0.1:{p}", f"localhost:{p}"}

        def _bad_host(self) -> bool:
            return self.headers.get("Host") not in self._hosts()

        def do_GET(self):  # noqa: N802
            if self._bad_host():  # anti DNS-rebind: guards reads too
                return self._json(403, {"error": "bad Host"})
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, _PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/state":
                self._json(200, session.public_state())
            elif path == "/presets":
                try:
                    self._json(200, session.list_presets())
                except LiveError as e:
                    self._json(e.status, {"error": str(e)})
            elif path == "/events":
                self._sse()
            else:
                self._json(404, {"error": "not found"})

        def _sse(self) -> None:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
            except OSError:
                return
            self.close_connection = True
            version = rev = -1          # a fresh page gets the full state first
            srv = self.server
            while not srv.stopping:
                version, rev, msgs = session.changes_since(version, rev)
                try:
                    if msgs:
                        for m in msgs:
                            self.wfile.write(f"data: {json.dumps(m)}\n\n".encode())
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                except OSError:
                    return              # the page went away
                session.wait(version, rev, SSE_KEEPALIVE)

        def _guard(self):
            if self._bad_host():
                return 403, "bad Host"
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {"http://" + h for h in self._hosts()}:
                return 403, "bad Origin"
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return 415, "Content-Type must be application/json"
            return None

        def _refuse(self, code: int, msg: str) -> None:
            """Answer and drop the connection WITHOUT reading the body: nothing
            a refused sender put on the wire may be parsed as a next request."""
            self.close_connection = True
            self._json(code, {"error": msg})

        def do_POST(self):  # noqa: N802
            bad = self._guard()            # before a single body byte is read
            if bad:
                return self._refuse(bad[0], bad[1])
            if self.headers.get("Transfer-Encoding"):
                return self._refuse(400, "chunked bodies are not accepted")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._refuse(400, "bad Content-Length")
            if n < 0 or n > MAX_BODY:
                return self._refuse(413, "request body too large")
            raw = self.rfile.read(n) if n else b""
            try:
                b = json.loads(raw or b"{}")
                if not isinstance(b, dict):
                    raise LiveError("body must be a JSON object")
                out = _route(session, urlsplit(self.path).path, b)
            except LiveError as e:
                body = {"error": str(e)}
                if e.value is not None:
                    body["value"] = e.value
                return self._json(e.status, body)
            except (ValueError, RecursionError) as e:      # malformed JSON
                return self._json(400, {"error": f"bad request: {e}"})
            except Exception as e:  # noqa: BLE001 — never leak a traceback
                logger.exception("live editor: unexpected error")
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            if out is None:
                return self._json(404, {"error": "not found"})
            self._json(200, {"ok": True, **out})

        def log_message(self, *a):  # quiet
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        stopping = False

    srv = Server((host, port), Handler)
    srv.session = session
    return srv


def run_subscriber(server, subscriber, *, session: LiveSession = None,
                   error_pause: float = 0.5) -> None:
    """Drain the device's PUB stream(s) into the session until the server
    stops. ``subscriber`` is a context manager exposing ``poll(timeout)`` (a
    :class:`HelixSubscriber`, or a fake in tests). A flaky stream must never
    kill the loop."""
    session = session if session is not None else server.session
    with subscriber as sub:
        while not server.stopping:
            try:
                events = sub.poll(0.5)
            except Exception as exc:  # noqa: BLE001
                logger.debug("live editor: poll failed: %r", exc)
                events = []
                if error_pause:
                    time.sleep(error_pause)
            try:
                session.handle_events(events)
                session.tick()
            except Exception:  # noqa: BLE001
                logger.exception("live editor: event handling failed")


def serve(session: LiveSession, host: str = "127.0.0.1", port: int = 8765,
          subscriber=None, on_ready=None) -> None:
    """Serve until interrupted. ``on_ready(url, srv)`` is called once
    listening; ``srv.shutdown()`` from another thread stops it (how the app
    window closing stops the server)."""
    srv = make_server(session, host, port)
    if subscriber is not None:
        threading.Thread(target=run_subscriber, args=(srv, subscriber),
                         daemon=True).start()
    if on_ready is not None:
        on_ready(f"http://127.0.0.1:{srv.server_address[1]}/", srv)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stopping = True
        with session.cond:
            session.cond.notify_all()
        srv.server_close()
