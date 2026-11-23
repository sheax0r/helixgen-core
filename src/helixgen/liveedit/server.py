"""Local HTTP + SSE server for `helixgen device live`. Stdlib only, localhost
only.

The page POSTs writes (``/param``, ``/bypass``, ``/save``, ``/reread``) and
holds one SSE connection (``/events``) on which it is pushed every change —
small ``param`` / ``bypass`` / ``meta`` messages, or the full ``state`` when
the structure changed. A background thread drains the device's PUB stream into
the session. All locking lives in :class:`LiveSession`.

The Host / Origin / Content-Type guards (anti DNS-rebind, anti cross-origin)
and the stream-draining loop are lifted from an earlier abandoned prototype's
server.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from helixgen import localguard
from helixgen.liveedit import intents
from helixgen.liveedit.drawer import ACTION_SCOPES
from helixgen.liveedit.events import decode_content_event
from helixgen.liveedit.session import LiveError, LiveSession
from helixgen.libraryui.session import LibraryError

_PAGE = Path(__file__).with_name("page.html")
logger = logging.getLogger("helixgen.liveedit")

SSE_KEEPALIVE = 15.0
MAX_BODY = 256 * 1024     # a write is ~100 bytes; library payloads are larger
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
    """Mutating endpoints -> response dict, or ``None`` for an unknown route.

    Signal-flow routes are flat (unprefixed). A follow-on editor pane namespaces
    its POSTs under ``/<pane>/…`` to avoid clashing — the library drawer already
    does (``/library/*``), and 81v.5 Device settings reserves ``/device/*``. Add
    those handlers in the pane's own PR; see the pane-host design spec.
    """
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
    if path == "/swap":
        return session.swap_model(_int(b, "path"), _int(b, "block"),
                                  _int(b, "model_id"))
    if path == "/add-block":
        return session.add_block(_int(b, "path"), _int(b, "model_id"))
    if path == "/second-cab":
        return session.set_second_cab(_int(b, "path"), _int(b, "block"),
                                      _int(b, "model_id"))
    if path == "/add-split":
        return session.split_block(_int(b, "path"), _int(b, "block"))
    if path == "/move-block":
        return session.move_block(_int(b, "path"), _int(b, "src"), _int(b, "dst"))
    if path == "/remove-block":
        return session.remove_block(_int(b, "path"), _int(b, "block"))
    if path == "/snapshot":
        return session.activate_snapshot(_int(b, "index"))
    if path == "/snapshot/rename":
        name = b.get("name")
        if not isinstance(name, str):
            raise LiveError("field 'name' must be a string")
        return session.rename_snapshot(_int(b, "index"), name)
    if path == "/snapshot/color":
        if "color" not in b:
            raise LiveError("missing field 'color'")
        return session.set_snapshot_color(_int(b, "index"), b["color"])
    if path == "/snapshot/copy":
        return session.copy_snapshot(_int(b, "src"), _int(b, "dst"))
    if path == "/snapshot/swap":
        return session.swap_snapshot(_int(b, "a"), _int(b, "b"))
    if path == "/preset":
        return session.load_preset(_int(b, "cid"))
    if path == "/save":
        return session.save()
    if path == "/reread":
        session.reread()
        return {}
    # Device pane (81v.5): device-global settings over /device/*. Reads live
    # values for one catalog page, or writes one key. The offline catalog
    # (page structure) is GET /device/state in do_GET.
    if path == "/device/values":
        page = b.get("page")
        if not isinstance(page, str):
            raise LiveError("field 'page' must be a string")
        return session.device_values(page)
    if path == "/device/set":
        key = b.get("key")
        if not isinstance(key, str):
            raise LiveError("field 'key' must be a string")
        if "value" not in b:
            raise LiveError("missing field 'value'")
        return session.device_write(key, b["value"])
    # Controllers pane (81v.3): assign / clear a MIDI-CC controller on a param.
    # Read of the current assignments is GET /controllers/state (do_GET).
    if path == "/controllers/assign":
        cc = b.get("cc")
        if isinstance(cc, bool) or not isinstance(cc, int):
            raise LiveError("field 'cc' must be an integer")
        return session.assign_midi_cc(_int(b, "path"), _int(b, "block"),
                                      _int(b, "pid"), cc, rev=_rev(b))
    if path == "/controllers/clear":
        return session.clear_param_controllers(_int(b, "path"), _int(b, "block"),
                                               _int(b, "pid"), rev=_rev(b))
    return None


def make_server(session: LiveSession, host: str = "127.0.0.1",
                port: int = 0, *, drawer=None) -> ThreadingHTTPServer:
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

        def _bad_host(self) -> bool:
            return localguard.bad_host(self.headers, self.server.server_address[1])

        def do_GET(self):  # noqa: N802
            if self._bad_host():  # anti DNS-rebind: guards reads too
                return self._json(403, {"error": "bad Host"})
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, _PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/state":
                self._json(200, session.public_state())
            elif path == "/intents":
                # static adjective -> affected-knob map + alias list for the
                # search box; the page does the role-matching client-side
                self._json(200, intents.payload())
            elif path == "/models":
                # static swap catalog {category: [{id,model,name}]} the model
                # picker reads once (hgc-cc4)
                from helixgen.device import defs as _defs
                self._json(200, _defs.models_by_category())
            elif path == "/presets":
                try:
                    self._json(200, session.list_presets())
                except LiveError as e:
                    self._json(e.status, {"error": str(e)})
            elif path == "/snapshot/globals":
                # the snapshot panel's slice of Global Settings (Discard-Edits +
                # read-only tempo/MIDI context). Read-only; writes reuse
                # POST /device/set. (81v.2 §8.5)
                try:
                    self._json(200, session.snapshot_globals())
                except LiveError as e:
                    self._json(e.status, {"error": str(e)})
            elif path == "/events":
                self._sse()
            elif path == "/library/state" and drawer is not None:
                try:
                    self._json(200, drawer.public_state())
                except Exception as e:  # noqa: BLE001
                    logger.exception("live editor: library state failed")
                    self._json(500, {"error": f"{type(e).__name__}: {e}"})
            # Device pane (81v.5): the offline Global-Settings catalog (page
            # structure) — live values are POST /device/values, writes POST
            # /device/set (see _route). No device I/O, so no connection needed.
            elif path == "/device/state":
                self._json(200, session.device_pages())
            # Controllers pane (81v.3): current FS/EXP/MIDI assignments projected
            # off the live buffer. Read-only, no device I/O (cached buffer).
            elif path == "/controllers/state":
                try:
                    self._json(200, session.controllers())
                except Exception as e:  # noqa: BLE001 — a bad buffer must not 500 raw
                    logger.exception("live editor: controllers state failed")
                    self._json(500, {"error": f"{type(e).__name__}: {e}"})
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
            return localguard.guard_post(self.headers, self.server.server_address[1])

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
                p = urlsplit(self.path).path
                if p.startswith("/library/") and drawer is not None:
                    name = p[len("/library/"):]
                    scopes = ACTION_SCOPES.get(name, ())
                    # a device-mutating action holds ONLY its own scope, and
                    # only for the moment it runs — so an agent pushing tones
                    # while the page is open is not locked out. A foreign
                    # holder makes transient() raise LiveError(409) below.
                    if scopes:
                        with session.lease.transient(scopes):
                            out = drawer.action(name, b)
                    else:
                        out = drawer.action(name, b)
                else:
                    out = _route(session, p, b)
            except LibraryError as e:
                return self._json(e.status, {"error": str(e)})
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


def _dispatch_content(drawer, ev, push, schedule) -> None:
    """One PUB event -> the library drawer. ``apply_event`` is pure (no device
    I/O), so it runs inline; a ``save`` only SCHEDULES the diff re-verify —
    ``resolve_diff`` hits the shared, mutate-locked library client and must
    never run on this thread, or a busy library action would stall the live
    chain stream."""
    ce = decode_content_event(ev)
    if ce is None:
        return
    delta = drawer.apply_event(ce)
    if delta and push is not None:
        push(delta)
    if ce.kind == "save" and ce.cid is not None and schedule is not None:
        schedule(ce.cid)


def run_subscriber(server, subscriber, *, session: LiveSession = None,
                   drawer=None, push=None, schedule=None,
                   error_pause: float = 0.5) -> None:
    """Drain the device's PUB stream(s) into the session until the server
    stops. ``subscriber`` is a context manager exposing ``poll(timeout)`` (a
    :class:`HelixSubscriber`, or a fake in tests). A flaky stream must never
    kill the loop.

    When ``drawer`` is given, each event is ALSO offered to the library drawer:
    ``push(delta)`` sends a drawer delta to the page (over the same SSE stream),
    and ``schedule(cid)`` runs a save's diff re-verify off-thread."""
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
                if drawer is not None:
                    for ev in events:
                        _dispatch_content(drawer, ev, push, schedule)
            except Exception:  # noqa: BLE001
                logger.exception("live editor: event handling failed")


def run_discovery(discover, connect, *, connected, should_stop,
                  interval: float = 2.0, sleep=time.sleep) -> None:
    """Poll for a device and connect on the first sighting.

    ``discover()`` returns a device ip (or ``None``); ``connect(ip)`` does the
    bring-up and is expected to flip ``connected()`` to True. While
    ``connected()`` is True nothing is attempted, so ``connect`` runs at most
    once per connection — when the device later drops and ``connected()`` goes
    back to False, polling resumes and reconnects. A ``discover`` or
    ``connect`` error is logged and the loop keeps going (the device network
    is flaky)."""
    while not should_stop():
        if not connected():
            ip = None
            try:
                ip = discover()
            except Exception as exc:  # noqa: BLE001 — a flaky scan is normal
                logger.debug("live editor: discovery failed: %r", exc)
            if ip:
                try:
                    connect(ip)
                except Exception:  # noqa: BLE001 — unreachable/asleep: retry
                    logger.debug("live editor: connect to %s failed", ip,
                                 exc_info=True)
        sleep(interval)


def serve(session: LiveSession, host: str = "127.0.0.1", port: int = 8765,
          subscriber=None, on_ready=None, *, drawer=None) -> None:
    """Serve until interrupted. ``on_ready(url, srv)`` is called once
    listening; ``srv.shutdown()`` from another thread stops it (how the app
    window closing stops the server). ``drawer`` (when given) mounts the
    library hub on ``/library/*``."""
    srv = make_server(session, host, port, drawer=drawer)
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
