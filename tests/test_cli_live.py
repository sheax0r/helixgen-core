"""`helixgen device live` — the verb that serves the live chain editor
(hgc-58a). The server and device are faked; this pins the wiring."""
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


class _Sub:
    made = []

    def __init__(self, ip, ports):
        _Sub.made.append((ip, tuple(ports)))


def _run(monkeypatch, args, client=None, window_launch=None):
    fake = client or FakeClient()
    seen = {}
    _Sub.made = []

    def serve(session, host, port, subscriber=None, on_ready=None):
        seen.update(session=session, host=host, port=port, sub=subscriber)
        if on_ready:
            on_ready(f"http://127.0.0.1:{port or 54321}/")

    monkeypatch.setattr(cli_device, "_client", lambda: (lambda ip, port: _Ctx(fake), HelixError))
    monkeypatch.setattr("helixgen.liveedit.server.serve", serve)
    monkeypatch.setattr("helixgen.device.subscribe.HelixSubscriber", _Sub)
    opened = []
    launched = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    def _launch(url, *, on_exit=None, binary=None):
        launched.append(url)
        # default stub: an app window opened, so no browser fallback
        return object() if window_launch is None else window_launch(url)
    monkeypatch.setattr("helixgen.liveedit.window.launch", _launch)
    r = CliRunner().invoke(cli, ["device", "live", "--ip", "10.0.0.9", *args])
    return r, fake, seen, opened, launched


def test_live_loads_once_and_serves_on_localhost(monkeypatch):
    r, fake, seen, opened, launched = _run(monkeypatch, ["--no-open"])
    assert r.exit_code == 0, r.output
    assert fake.reads == 1                               # ONE edit-buffer read
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8765
    assert seen["session"].liveness is True
    assert _Sub.made == [("10.0.0.99", (2001, 2003))]    # the client's resolved ip
    assert "live editor: http://127.0.0.1:8765/" in r.output
    assert opened == [] and launched == []               # --no-open opens nothing


def test_live_opens_an_app_window_by_default_and_takes_a_port(monkeypatch):
    r, fake, seen, opened, launched = _run(monkeypatch, ["--port-http", "9001"])
    assert r.exit_code == 0, r.output
    assert seen["port"] == 9001
    assert launched == ["http://127.0.0.1:9001/"]        # app window, not a tab
    assert opened == []                                  # default browser untouched


def test_live_browser_flag_forces_the_default_browser(monkeypatch):
    r, fake, seen, opened, launched = _run(monkeypatch, ["--browser"])
    assert r.exit_code == 0, r.output
    assert opened == ["http://127.0.0.1:8765/"] and launched == []


def test_live_falls_back_to_the_browser_without_a_chromium(monkeypatch):
    r, fake, seen, opened, launched = _run(
        monkeypatch, [], window_launch=lambda url: None)  # no app window available
    assert r.exit_code == 0, r.output
    assert launched == ["http://127.0.0.1:8765/"]         # tried the app window
    assert opened == ["http://127.0.0.1:8765/"]           # then fell back


def test_live_uses_the_write_lease_unless_no_lock(monkeypatch):
    r, _, seen, _, _ = _run(monkeypatch, ["--no-open"])
    assert seen["session"].lease.enabled is True
    r, _, seen, _, _ = _run(monkeypatch, ["--no-open", "--no-lock"])
    assert seen["session"].lease.enabled is False


def test_live_fails_cleanly_when_the_device_cannot_be_read(monkeypatch):
    c = FakeClient()
    c.fail = HelixError("no reply from 10.0.0.9")
    r, _, seen, _, _ = _run(monkeypatch, ["--no-open"], client=c)
    assert r.exit_code != 0
    assert "no reply" in r.output and "Traceback" not in r.output
    assert "session" not in seen                         # never started serving


def _run_with_serve(monkeypatch, serve, args=("--no-open",), client=None):
    fake = client or FakeClient()
    monkeypatch.setattr(cli_device, "_client", lambda: (lambda ip, port: _Ctx(fake), HelixError))
    monkeypatch.setattr("helixgen.liveedit.server.serve", serve)
    monkeypatch.setattr("helixgen.device.subscribe.HelixSubscriber", _Sub)
    return CliRunner().invoke(cli, ["device", "live", "--ip", "10.0.0.9", *args]), fake


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


def test_live_rejects_an_impossible_port_before_touching_the_device(monkeypatch):
    def serve(*a, **k):
        raise AssertionError("served")
    r, fake = _run_with_serve(monkeypatch, serve, args=("--no-open", "--port-http", "70000"))
    assert r.exit_code == 2 and "70000" in r.output and fake.reads == 0


def test_live_refuses_firmware_older_than_1_4(monkeypatch):
    # older firmware does not acknowledge a live bypass, so every bypass on
    # the page would be reported as failed
    c = FakeClient()
    c.firmware = "1.3.2"
    r, _ = _run_with_serve(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("served")), client=c)
    assert r.exit_code != 0 and "1.4" in r.output and "1.3.2" in r.output


def test_live_releases_the_lease_for_good_on_every_exit(monkeypatch):
    import errno
    from helixgen.liveedit import lease as lease_mod
    made = []

    class Spy(lease_mod.EditLease):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.released = []
            made.append(self)

        def release(self, *, final=False):
            self.released.append(final)
            super().release(final=final)
    monkeypatch.setattr(lease_mod, "EditLease", Spy)
    _run_with_serve(monkeypatch, lambda *a, **k: None)                       # normal stop
    assert made[-1].released == [True]

    def boom(*a, **k):
        raise OSError(errno.EADDRINUSE, "in use")
    _run_with_serve(monkeypatch, boom)                                       # failed start
    assert made[-1].released == [True]


def test_live_stops_cleanly_on_sigterm_and_sighup(monkeypatch):
    import signal
    seen = {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: seen.__setitem__(sig, fn))
    _run_with_serve(monkeypatch, lambda *a, **k: None)
    assert set(seen) >= {signal.SIGTERM, signal.SIGHUP}
    try:
        seen[signal.SIGTERM](signal.SIGTERM, None)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("SIGTERM must unwind the server like Ctrl-C")
