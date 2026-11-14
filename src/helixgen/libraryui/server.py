"""Local HTTP server for `helixgen library ui`. Stdlib only, localhost only.

Simpler than ``liveedit/server.py``: the hub fetches state on demand, so there
is no SSE / PUB-stream draining. The Host / Origin / Content-Type guards (anti
DNS-rebind, anti cross-origin) are the same, lifted from there. Action POST
routes are added by later tasks; unknown routes 404.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from helixgen.libraryui.session import LibraryError, LibrarySession

_PAGE = Path(__file__).with_name("page.html")
logger = logging.getLogger("helixgen.libraryui")

MAX_BODY = 256 * 1024
SOCKET_TIMEOUT = 20.0


def _route(session: LibrarySession, path: str, body: dict):
    """Mutating endpoints -> response dict, or ``None`` for an unknown route.
    (Setlist/IR routes land in Tasks 4–5.)"""
    if path == "/copy":
        return session.copy_to_device(body.get("hsp"), to=body.get("to"),
                                      pos=body.get("pos"))
    if path == "/pull":
        return session.pull_from_device(body.get("cid"))
    return None


def make_server(session: LibrarySession, host: str = "127.0.0.1",
                port: int = 0) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the library hub binds localhost only")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = SOCKET_TIMEOUT

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
            if self._bad_host():
                return self._json(403, {"error": "bad Host"})
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, _PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path in ("/state", "/refresh"):
                try:
                    self._json(200, session.public_state())
                except Exception as e:  # noqa: BLE001
                    logger.exception("library hub: state failed")
                    self._json(500, {"error": f"{type(e).__name__}: {e}"})
            else:
                self._json(404, {"error": "not found"})

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
            self.close_connection = True
            self._json(code, {"error": msg})

        def do_POST(self):  # noqa: N802
            bad = self._guard()
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
                    raise LibraryError("body must be a JSON object")
                out = _route(session, urlsplit(self.path).path, b)
            except LibraryError as e:
                return self._json(e.status, {"error": str(e)})
            except (ValueError, RecursionError) as e:
                return self._json(400, {"error": f"bad request: {e}"})
            except Exception as e:  # noqa: BLE001
                logger.exception("library hub: unexpected error")
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


def serve(session: LibrarySession, host: str = "127.0.0.1", port: int = 8766,
          on_ready=None) -> None:
    srv = make_server(session, host, port)
    if on_ready is not None:
        on_ready(f"http://127.0.0.1:{srv.server_address[1]}/", srv)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stopping = True
        srv.server_close()
