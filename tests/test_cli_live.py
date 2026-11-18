"""`helixgen device live` — the verb that serves the live chain editor
(hgc-58a) and, since the cold-start rework (task 8/9), binds + serves with NO
device and connects one from a background discovery loop. The server, device
and discovery are faked; this pins the wiring."""
import threading
import time

from click.testing import CliRunner

from helixgen import cli_device
from helixgen.cli import cli
from helixgen.device.client import HelixError
from tests.test_liveedit_session import FakeClient


class _Ctx:
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self.c

    def __exit__(self, *a):
        return False

    def connect(self, *a, **k):          # clients are built with .connect()
        return self.c


class _FakeLease:
    enabled = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def release(self, *a, **k):
        pass


def _no_locks(monkeypatch):
    """Keep the advisory library+irs lease off the real filesystem."""
    monkeypatch.setattr("helixgen.locks.acquire", lambda *a, **k: _FakeLease())
    monkeypatch.setattr("helixgen.locks.keep_alive", lambda *a, **k: _FakeLease())


class _Sub:
    made = []

    def __init__(self, ip, ports):
        _Sub.made.append((ip, tuple(ports)))


class _Srv:
    """Minimal stand-in for the ThreadingHTTPServer on_ready hands back."""
    def shutdown(self):
        pass


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _env(monkeypatch, fake, serve, *, discover_ip=None):
    """Wire the fakes. ``discover_ip`` None = no device ever found (offline);
    a string = discovery returns that ip."""
    _no_locks(monkeypatch)
    _Sub.made = []
    opened, launched = [], []

    monkeypatch.setattr(cli_device, "_client",
                        lambda: (lambda ip, port: _Ctx(fake), HelixError))
    monkeypatch.setattr("helixgen.liveedit.server.serve", serve)
    monkeypatch.setattr("helixgen.device.subscribe.HelixSubscriber", _Sub)
    monkeypatch.setattr("helixgen.device.discovery.mdns_discover",
                        lambda *a, **k: [])

    def _resolve(explicit=None, *, warn=True):
        if discover_ip is None:
            raise cli_device_discovery_error()
        return discover_ip
    monkeypatch.setattr("helixgen.device.discovery.resolve_ip", _resolve)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    def _launch(url, *, on_exit=None, binary=None):
        launched.append(url)
        return object() if _env.window else None
    monkeypatch.setattr("helixgen.liveedit.window.launch", _launch)
    return opened, launched


_env.window = True   # default: an app window is available


def cli_device_discovery_error():
    from helixgen.device.discovery import IPResolutionError
    return IPResolutionError("no device on record")


def _run(monkeypatch, args, fake=None, serve=None, discover_ip=None):
    fake = fake or FakeClient()
    seen = {}

    def default_serve(session, host, port, subscriber=None, on_ready=None,
                      *, drawer=None):
        seen.update(session=session, host=host, port=port, drawer=drawer)
        if on_ready:
            on_ready(f"http://127.0.0.1:{port or 54321}/", _Srv())
    serve = serve or default_serve
    opened, launched = _env(monkeypatch, fake, serve, discover_ip=discover_ip)
    r = CliRunner().invoke(cli, ["device", "live", "--ip", "10.0.0.9", *args])
    return r, fake, seen, opened, launched


# --- offline cold-start ------------------------------------------------------

def test_live_serves_immediately_with_no_device(monkeypatch):
    r, fake, seen, opened, launched = _run(monkeypatch, ["--no-open"],
                                           discover_ip=None)
    assert r.exit_code == 0, r.output
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8765
    assert seen["session"].connected is False        # bound with no device
    assert seen["session"].liveness is True
    assert seen["drawer"] is not None                # library drawer mounted
    assert fake.reads == 0                            # NO eager device read
    assert "live editor: http://127.0.0.1:8765/" in r.output
    assert opened == [] and launched == []


def test_live_opens_an_app_window_by_default_and_takes_a_port(monkeypatch):
    r, fake, seen, opened, launched = _run(monkeypatch, ["--port-http", "9001"])
    assert r.exit_code == 0, r.output
    assert seen["port"] == 9001
    assert launched == ["http://127.0.0.1:9001/"]     # app window, not a tab
    assert opened == []


def test_live_browser_flag_forces_the_default_browser(monkeypatch):
    r, fake, seen, opened, launched = _run(monkeypatch, ["--browser"])
    assert r.exit_code == 0, r.output
    assert opened == ["http://127.0.0.1:8765/"] and launched == []


def test_live_falls_back_to_the_browser_without_a_chromium(monkeypatch):
    _env.window = False
    try:
        r, fake, seen, opened, launched = _run(monkeypatch, [])
    finally:
        _env.window = True
    assert r.exit_code == 0, r.output
    assert launched == ["http://127.0.0.1:8765/"]     # tried the app window
    assert opened == ["http://127.0.0.1:8765/"]       # then fell back


# --- discovery connects at runtime -------------------------------------------

def _blocking_serve(seen, go):
    def serve(session, host, port, subscriber=None, on_ready=None, *, drawer=None):
        seen.update(session=session, drawer=drawer)
        if on_ready:
            on_ready(f"http://127.0.0.1:{port or 54321}/", _Srv())
        go.wait(5)                                    # block like serve_forever
    return serve


def test_discovery_connects_and_reconciles(monkeypatch):
    fake = FakeClient()
    subs, recon = [], []
    monkeypatch.setattr("helixgen.liveedit.server.run_subscriber",
                        lambda srv, sub, **k: subs.append((sub, k)))
    monkeypatch.setattr("helixgen.liveedit.reconcile.run_reconcile",
                        lambda drawer, **k: recon.append(True))
    seen = {}
    go = threading.Event()
    opened, launched = _env(monkeypatch, fake, _blocking_serve(seen, go),
                            discover_ip="10.0.0.9")
    done = []
    t = threading.Thread(
        target=lambda: done.append(
            CliRunner().invoke(cli, ["device", "live", "--ip", "10.0.0.9",
                                     "--no-open"])),
        daemon=True)
    t.start()
    try:
        assert _wait(lambda: seen.get("session") and seen["session"].connected)
        assert fake.reads >= 1                         # connect_device read it
        assert _wait(lambda: subs and recon)           # subscriber + reconcile up
        assert _Sub.made == [("10.0.0.99", (2001, 2003))]
        # the subscriber is wired to route content events to the drawer over SSE
        assert subs[0][1]["drawer"] is seen["drawer"]
        assert callable(subs[0][1]["push"]) and callable(subs[0][1]["schedule"])
        assert seen["session"].lease.enabled is True   # editbuffer lease armed
    finally:
        go.set()
        t.join(5)
    assert done and done[0].exit_code == 0, done and done[0].output


def test_no_lock_disarms_the_editbuffer_lease_on_connect(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr("helixgen.liveedit.server.run_subscriber",
                        lambda *a, **k: None)
    monkeypatch.setattr("helixgen.liveedit.reconcile.run_reconcile",
                        lambda *a, **k: None)
    seen = {}
    go = threading.Event()
    _env(monkeypatch, fake, _blocking_serve(seen, go), discover_ip="10.0.0.9")
    t = threading.Thread(
        target=lambda: CliRunner().invoke(
            cli, ["device", "live", "--ip", "10.0.0.9", "--no-open",
                  "--no-lock"]),
        daemon=True)
    t.start()
    try:
        assert _wait(lambda: seen.get("session") and seen["session"].connected)
        assert seen["session"].lease.enabled is False
    finally:
        go.set()
        t.join(5)


def test_live_does_not_hold_a_lifetime_library_lease(monkeypatch):
    """Regression (hgc): the drawer must NOT sit on library+irs for its whole
    life — that locked out agents pushing tones while the page was open. At
    rest (connected, idle) it holds no library/irs lease; each drawer write
    takes its scope transiently instead (see the server library-route tests)."""
    fake = FakeClient()
    acquired = []
    reconciled = threading.Event()
    monkeypatch.setattr("helixgen.liveedit.server.run_subscriber",
                        lambda *a, **k: None)
    monkeypatch.setattr("helixgen.liveedit.reconcile.run_reconcile",
                        lambda *a, **k: reconciled.set())
    seen = {}
    go = threading.Event()
    _env(monkeypatch, fake, _blocking_serve(seen, go), discover_ip="10.0.0.9")
    # record every acquire's scopes AFTER _env (its _no_locks set a bare fake);
    # the reconcile thread starts only after the (removed) lifetime acquire, so
    # waiting on it guarantees any such acquire would already have happened.
    monkeypatch.setattr(
        "helixgen.locks.acquire",
        lambda ip, scopes, **k: (acquired.append(tuple(scopes)), _FakeLease())[1])
    t = threading.Thread(
        target=lambda: CliRunner().invoke(
            cli, ["device", "live", "--ip", "10.0.0.9", "--no-open"]),
        daemon=True)
    t.start()
    try:
        assert _wait(lambda: reconciled.is_set())
        assert ("library", "irs") not in acquired
        assert acquired == []          # nothing acquired at rest
    finally:
        go.set()
        t.join(5)


def test_old_firmware_is_ignored_and_the_page_still_serves(monkeypatch):
    # below 1.4 the device cannot ack a live bypass — it is skipped, NOT a hard
    # failure: the page keeps serving and the drawer works offline.
    fake = FakeClient()
    fake.firmware = "1.3.2"
    monkeypatch.setattr("helixgen.liveedit.server.run_subscriber",
                        lambda *a, **k: None)
    seen = {}
    go = threading.Event()
    _env(monkeypatch, fake, _blocking_serve(seen, go), discover_ip="10.0.0.9")
    out = []
    t = threading.Thread(
        target=lambda: out.append(CliRunner().invoke(
            cli, ["device", "live", "--ip", "10.0.0.9", "--no-open"])),
        daemon=True)
    t.start()
    try:
        assert _wait(lambda: seen.get("session") is not None)
        time.sleep(0.2)                                # let one discovery cycle run
        assert seen["session"].connected is False      # never connected
    finally:
        go.set()
        t.join(5)
    assert out and "1.3.2" in out[0].output            # the skip was reported


# --- error handling (serve binds in the foreground) --------------------------

def _run_with_serve(monkeypatch, serve, args=("--no-open",), fake=None):
    return _run(monkeypatch, list(args), fake=fake, serve=serve)[0], None


def test_live_reports_a_port_in_use(monkeypatch):
    import errno

    def serve(*a, **k):
        raise OSError(errno.EADDRINUSE, "Address already in use")
    r, _ = _run_with_serve(monkeypatch, serve)
    assert r.exit_code != 0 and "--port-http" in r.output


def test_live_does_not_blame_the_port_for_other_os_errors(monkeypatch):
    import errno

    def serve(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    r, _ = _run_with_serve(monkeypatch, serve)
    assert r.exit_code != 0
    assert "No space left" in r.output and "--port-http" not in r.output


def test_live_rejects_an_impossible_port_before_serving(monkeypatch):
    def serve(*a, **k):
        raise AssertionError("served")
    r, _ = _run_with_serve(monkeypatch, serve,
                           args=("--no-open", "--port-http", "70000"))
    assert r.exit_code == 2 and "70000" in r.output


def test_live_stops_cleanly_on_sigterm_and_sighup(monkeypatch):
    import signal
    seen = {}
    monkeypatch.setattr(signal, "signal",
                        lambda sig, fn: seen.__setitem__(sig, fn))
    _run_with_serve(monkeypatch, lambda *a, **k: None)
    assert set(seen) >= {signal.SIGTERM, signal.SIGHUP}
    try:
        seen[signal.SIGTERM](signal.SIGTERM, None)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("SIGTERM must unwind the server like Ctrl-C")
