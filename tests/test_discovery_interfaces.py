"""Per-interface discovery (hgc-094 / backlog #77a).

A VPN holding the default route used to blind discovery to a LAN Stadium:
mDNS + the probe looked only at the tunnel. Discovery now enumerates every
UP interface with an RFC 1918 address (tunnels only when nothing physical
exists). Parsing/selection is pinned against captured ifconfig output.
"""
from __future__ import annotations

import ipaddress

from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device import discovery

# Captured on the maintainer's Mac 2026-10-13 (trimmed): LAN on en0 (/22),
# VPN on utun4 holding the default route, a link-local en7.
MAC_IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
	inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	ether aa:bb:cc:dd:ee:ff
	inet 192.168.4.87 netmask 0xfffffc00 broadcast 192.168.7.255
	status: active
en7: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet 169.254.3.173 netmask 0xffff0000 broadcast 169.254.255.255
en5: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500
	inet 192.168.99.2 netmask 0xffffff00 broadcast 192.168.99.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
	inet 10.5.0.2 --> 10.5.0.2 netmask 0xffff0000
"""

LINUX_IP_O = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 192.168.1.20/24 brd 192.168.1.255 scope global eth0\\       valid_lft forever preferred_lft forever
5: tun0    inet 10.8.0.6/24 scope global tun0\\       valid_lft forever preferred_lft forever
"""


def test_parse_mac_ifconfig():
    got = discovery.parse_interfaces(MAC_IFCONFIG)
    by_name = {name: (ip, plen, tun) for name, ip, plen, tun in got}
    assert by_name["en0"] == ("192.168.4.87", 22, False)
    assert by_name["utun4"] == ("10.5.0.2", 16, True)
    assert "en5" not in by_name  # not UP


def test_parse_linux_ip_o():
    got = discovery.parse_interfaces(LINUX_IP_O)
    by_name = {name: (ip, plen, tun) for name, ip, plen, tun in got}
    assert by_name["eth0"] == ("192.168.1.20", 24, False)
    assert by_name["tun0"] == ("10.8.0.6", 24, True)


def test_targets_prefer_physical_over_vpn(monkeypatch):
    monkeypatch.setattr(discovery, "local_interfaces",
                        lambda: discovery.parse_interfaces(MAC_IFCONFIG))
    assert discovery.probe_targets() == [
        ("192.168.4.87", ipaddress.ip_network("192.168.4.0/22"))]


def test_targets_fall_back_to_tunnel_when_nothing_physical(monkeypatch):
    only_vpn = "\n".join(MAC_IFCONFIG.splitlines()[-2:])
    monkeypatch.setattr(discovery, "local_interfaces",
                        lambda: discovery.parse_interfaces(only_vpn))
    # /16 is capped to the 1024-host block around our address
    assert discovery.probe_targets() == [
        ("10.5.0.2", ipaddress.ip_network("10.5.0.0/22"))]


def test_targets_without_enumeration_use_default_route(monkeypatch):
    monkeypatch.setattr(discovery, "local_interfaces", lambda: [])
    monkeypatch.setattr(discovery, "local_ipv4", lambda: "192.168.1.5")
    monkeypatch.setattr(discovery, "local_prefixlen", lambda ip: 24)
    assert discovery.probe_targets() == [
        ("192.168.1.5", ipaddress.ip_network("192.168.1.0/24"))]
    monkeypatch.setattr(discovery, "local_ipv4", lambda: "203.0.113.5")
    assert discovery.probe_targets() == []


def test_targets_multiple_physical_dedup_same_subnet(monkeypatch):
    monkeypatch.setattr(discovery, "local_interfaces", lambda: [
        ("en0", "192.168.4.87", 22, False),
        ("en1", "192.168.4.90", 22, False),
        ("en2", "10.0.0.5", 24, False)])
    assert discovery.probe_targets() == [
        ("192.168.4.87", ipaddress.ip_network("192.168.4.0/22")),
        ("10.0.0.5", ipaddress.ip_network("10.0.0.0/24"))]


def test_discover_probes_and_names_every_range(monkeypatch):
    from tests.test_discovery import _FakeClient

    nets = [("192.168.4.87", ipaddress.ip_network("192.168.4.0/22")),
            ("10.0.0.5", ipaddress.ip_network("10.0.0.0/24"))]
    monkeypatch.setattr(discovery, "probe_targets", lambda **kw: nets)
    seen = {}

    def fake_mdns(**kw):
        seen["bind_ips"] = kw.get("bind_ips")
        return []
    probed = []

    def fake_probe(**kw):
        probed.append((kw["subnet_ip"], kw["prefixlen"]))
        return []
    monkeypatch.setattr(discovery, "mdns_discover", fake_mdns)
    monkeypatch.setattr(discovery, "probe_subnet", fake_probe)
    monkeypatch.setattr("helixgen.cli_device._client",
                        lambda: (_FakeClient, discovery.IPResolutionError))
    r = CliRunner().invoke(cli, ["device", "discover"])
    assert r.exit_code != 0
    assert seen["bind_ips"] == ["192.168.4.87", "10.0.0.5"]
    assert probed == [("192.168.4.87", 22), ("10.0.0.5", 24)]
    assert r.output.count("192.168.4.0/22, 10.0.0.0/24") == 2
