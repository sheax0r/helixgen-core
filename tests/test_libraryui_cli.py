"""Task 6: the `helixgen library ui` CLI verb."""
from __future__ import annotations

from click.testing import CliRunner

from helixgen.cli_library import library


def test_ui_help_advertises_flags():
    res = CliRunner().invoke(library, ["ui", "--help"])
    assert res.exit_code == 0
    assert "--port-http" in res.output
    assert "--no-open" in res.output


def test_ui_builds_session_and_serves_offline(monkeypatch):
    captured = {}

    def fake_serve(session, host, http_port, on_ready=None):
        captured["session"] = session
        captured["host"] = host
        if on_ready:
            on_ready(f"http://127.0.0.1:{http_port or 8766}/", None)

    # force offline: no device resolves, so no lease is taken and no real
    # device is touched by the test.
    import helixgen.device.discovery as discovery
    monkeypatch.setattr(discovery, "resolve_ip",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no device")))
    import helixgen.libraryui.server as server
    monkeypatch.setattr(server, "serve", fake_serve)

    res = CliRunner().invoke(library, ["ui", "--no-open", "--port-http", "0"])
    assert res.exit_code == 0, res.output
    assert "library hub:" in res.output
    assert captured["host"] == "127.0.0.1"
    from helixgen.libraryui.session import LibrarySession
    assert isinstance(captured["session"], LibrarySession)
