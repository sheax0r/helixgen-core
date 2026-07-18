"""Reachability preflight for the telemetry verbs (#64c/#64f).

`device tuner` / `device meters` / `device measure` subscribe to the 2003
PUB stream, whose SUB socket connects lazily — an unreachable device used to
be indistinguishable from silence (a full --seconds wait, then "no meter
data"). The verbs now TCP-probe the RPC control port (--port) up front and
fail fast with an instructive error. No real device or socket is touched:
`discovery.probe_reachable` is monkeypatched, and a sentinel subscriber
asserts the stream is never opened when the preflight fails.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from helixgen.cli import cli

msgpack = pytest.importorskip("msgpack")

VERBS = ["tuner", "meters", "measure"]


class ExplodingSubscriber:
    """The preflight must fail BEFORE any subscribe happens."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("HelixSubscriber must not be constructed when "
                             "the reachability preflight fails")


@pytest.fixture(autouse=True)
def _configured_ip(monkeypatch):
    monkeypatch.setenv("HELIXGEN_HELIX_IP", "10.0.0.99")


def _patch_unreachable(monkeypatch):
    from helixgen.device import discovery
    from helixgen.device import subscribe as sub_mod

    probed = []

    def fake_probe(ip, port=2002, **kw):
        probed.append((ip, port))
        return False

    monkeypatch.setattr(discovery, "probe_reachable", fake_probe)
    monkeypatch.setattr(sub_mod, "HelixSubscriber", ExplodingSubscriber)
    return probed


@pytest.mark.parametrize("verb", VERBS)
def test_unreachable_device_fails_fast_with_clear_error(monkeypatch, verb):
    probed = _patch_unreachable(monkeypatch)
    result = CliRunner().invoke(
        cli, ["device", verb, "--seconds", "600"])
    assert result.exit_code == 1
    assert "no Helix Stadium reachable at 10.0.0.99:2002" in result.output
    assert "device discover" in result.output
    assert probed == [("10.0.0.99", 2002)]


@pytest.mark.parametrize("verb", VERBS)
def test_preflight_probes_the_port_option(monkeypatch, verb):
    # #64f: --port used to be accepted but ignored on these verbs; it now
    # drives the control-port reachability probe.
    probed = _patch_unreachable(monkeypatch)
    result = CliRunner().invoke(
        cli, ["device", verb, "--port", "2222", "--seconds", "600"])
    assert result.exit_code == 1
    assert probed == [("10.0.0.99", 2222)]
    assert "10.0.0.99:2222" in result.output


def test_probe_reachable_true_on_open_port():
    import socket
    import threading

    from helixgen.device import discovery

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    t = threading.Thread(target=lambda: srv.accept(), daemon=True)
    t.start()
    try:
        assert discovery.probe_reachable("127.0.0.1", port, timeout=2.0)
    finally:
        srv.close()


def test_probe_reachable_false_on_closed_port():
    import socket

    from helixgen.device import discovery

    # bind-then-close guarantees the port exists and is now closed
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert not discovery.probe_reachable("127.0.0.1", port, timeout=0.5)
