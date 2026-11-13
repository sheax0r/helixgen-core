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


def _run(monkeypatch, args, client=None):
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
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    r = CliRunner().invoke(cli, ["device", "live", "--ip", "10.0.0.9", *args])
    return r, fake, seen, opened


def test_live_loads_once_and_serves_on_localhost(monkeypatch):
    r, fake, seen, opened = _run(monkeypatch, ["--no-open"])
    assert r.exit_code == 0, r.output
    assert fake.reads == 1                               # ONE edit-buffer read
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8765
    assert seen["session"].liveness is True
    assert _Sub.made == [("10.0.0.99", (2001, 2003))]    # the client's resolved ip
    assert "live editor: http://127.0.0.1:8765/" in r.output
    assert opened == []


def test_live_opens_the_browser_by_default_and_takes_a_port(monkeypatch):
    r, fake, seen, opened = _run(monkeypatch, ["--port-http", "9001"])
    assert r.exit_code == 0, r.output
    assert seen["port"] == 9001
    assert opened == ["http://127.0.0.1:9001/"]


def test_live_uses_the_write_lease_unless_no_lock(monkeypatch):
    r, _, seen, _ = _run(monkeypatch, ["--no-open"])
    assert seen["session"].lease.enabled is True
    r, _, seen, _ = _run(monkeypatch, ["--no-open", "--no-lock"])
    assert seen["session"].lease.enabled is False


def test_live_fails_cleanly_when_the_device_cannot_be_read(monkeypatch):
    c = FakeClient()
    c.fail = HelixError("no reply from 10.0.0.9")
    r, _, seen, _ = _run(monkeypatch, ["--no-open"], client=c)
    assert r.exit_code != 0
    assert "no reply" in r.output and "Traceback" not in r.output
    assert "session" not in seen                         # never started serving


def test_live_reports_a_port_in_use(monkeypatch):
    def serve(*a, **k):
        raise OSError(48, "Address already in use")
    fake = FakeClient()
    monkeypatch.setattr(cli_device, "_client", lambda: (lambda ip, port: _Ctx(fake), HelixError))
    monkeypatch.setattr("helixgen.liveedit.server.serve", serve)
    monkeypatch.setattr("helixgen.device.subscribe.HelixSubscriber", _Sub)
    r = CliRunner().invoke(cli, ["device", "live", "--ip", "10.0.0.9", "--no-open"])
    assert r.exit_code != 0 and "--port-http" in r.output
