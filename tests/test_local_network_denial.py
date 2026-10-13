"""macOS Local Network privacy denial must be visible (hgc-7he).

When macOS denies a process Local Network access, connect()/sendto() to a
LAN address fails immediately with EHOSTUNREACH (sometimes EPERM). ZeroMQ
retries that silently, so every device verb used to end in a misleading
"no Helix Stadium answered" timeout. A plain-TCP preflight now names the
real cause; discovery reports the same hint instead of "found nothing".
All sockets are mocked.
"""
from __future__ import annotations

import errno
import socket

import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device import discovery


def _raiser(exc):
    def fake_connect(addr, timeout=None):
        raise exc
    return fake_connect


@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(discovery.sys, "platform", "darwin")


class TestConnectError:
    @pytest.mark.parametrize("code", [errno.EHOSTUNREACH, errno.EPERM])
    def test_denial_on_darwin_names_the_setting(self, monkeypatch, darwin,
                                                code):
        monkeypatch.setattr(discovery.socket, "create_connection",
                            _raiser(OSError(code, "No route to host")))
        why = discovery.connect_error("192.168.4.84", 2002)
        assert "Privacy & Security > Local Network" in why
        assert "restart" in why

    def test_ehostunreach_off_darwin_is_not_blamed_on_privacy(
            self, monkeypatch):
        monkeypatch.setattr(discovery.sys, "platform", "linux")
        monkeypatch.setattr(discovery.socket, "create_connection",
                            _raiser(OSError(errno.EHOSTUNREACH, "no route")))
        why = discovery.connect_error("192.168.4.84", 2002)
        assert "Local Network" not in why

    def test_refused_is_distinct(self, monkeypatch, darwin):
        monkeypatch.setattr(discovery.socket, "create_connection",
                            _raiser(ConnectionRefusedError(
                                errno.ECONNREFUSED, "refused")))
        why = discovery.connect_error("192.168.4.84", 2002)
        assert "refused" in why and "Remote Access" in why
        assert "Local Network" not in why

    def test_timeout_is_distinct(self, monkeypatch, darwin):
        monkeypatch.setattr(discovery.socket, "create_connection",
                            _raiser(socket.timeout("timed out")))
        why = discovery.connect_error("192.168.4.84", 2002, timeout=2.0)
        assert "timed out" in why and "device discover" in why
        assert "Local Network" not in why

    def test_success_is_none(self, monkeypatch):
        class _S:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return None
        monkeypatch.setattr(discovery.socket, "create_connection",
                            lambda addr, timeout=None: _S())
        assert discovery.connect_error("192.168.4.84", 2002) is None
        assert discovery.probe_reachable("192.168.4.84", 2002)


class TestClientPreflight:
    def test_connect_fails_fast_before_zmq(self, monkeypatch, darwin):
        from helixgen.device.client import HelixClient, HelixError

        monkeypatch.setattr(discovery.socket, "create_connection",
                            _raiser(OSError(errno.EHOSTUNREACH, "no route")))
        h = HelixClient("192.168.4.84")

        def boom():
            raise AssertionError("ZMQ socket must not open after a failed "
                                 "preflight")
        monkeypatch.setattr(h, "_open_socket", boom)
        with pytest.raises(HelixError, match="Local Network"):
            h.connect()


class TestDiscoveryDenial:
    def test_probe_all_denied_raises(self, monkeypatch, darwin):
        monkeypatch.setattr(discovery.socket, "create_connection",
                            _raiser(OSError(errno.EHOSTUNREACH, "no route")))
        with pytest.raises(discovery.LocalNetworkDenied,
                           match="Local Network"):
            discovery.probe_subnet(subnet_ip="192.168.4.87", prefixlen=24)

    def test_probe_mixed_failures_is_just_empty(self, monkeypatch, darwin):
        def fake_connect(addr, timeout=None):
            if addr[0].endswith(".1"):
                raise socket.timeout("timed out")
            raise OSError(errno.EHOSTUNREACH, "no route")
        monkeypatch.setattr(discovery.socket, "create_connection",
                            fake_connect)
        assert discovery.probe_subnet(
            subnet_ip="192.168.4.87", prefixlen=24) == []

    def test_mdns_all_sends_denied_raises(self, monkeypatch, darwin):
        class _Sock:
            def __init__(self, *a):
                pass

            def setsockopt(self, *a):
                pass

            def bind(self, addr):
                pass

            def settimeout(self, t):
                pass

            def sendto(self, data, addr):
                raise OSError(errno.EHOSTUNREACH, "No route to host")

            def close(self):
                pass
        monkeypatch.setattr(discovery.socket, "socket", _Sock)
        with pytest.raises(discovery.LocalNetworkDenied):
            discovery.mdns_discover(timeout=0.5, bind_ips=["192.168.4.87"])

    def test_discover_cli_reports_the_hint(self, monkeypatch):
        def denied(**kw):
            raise discovery.LocalNetworkDenied(discovery.LOCAL_NETWORK_HINT)
        monkeypatch.setattr(discovery, "probe_targets", lambda **kw: [],
                            raising=False)
        monkeypatch.setattr(discovery, "mdns_discover", denied)
        r = CliRunner().invoke(cli, ["device", "discover"])
        assert r.exit_code != 0
        assert "Privacy & Security > Local Network" in r.output
        assert "no Helix Stadium found" not in r.output
