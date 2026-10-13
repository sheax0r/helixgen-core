"""LAN discovery + IP resolution for Helix Stadium devices (workspace #74).

Pure stdlib — no pyzmq/msgpack/zeroconf. Two mechanisms, used by
``helixgen device discover``:

1. **mDNS (primary).** The Stadium advertises the DNS-SD service
   ``_stadiumserver._tcp.local.`` and answers a one-shot multicast PTR
   query itself (verified live 2026-07-16 against a Stadium XL, fw 1.3.2:
   one datagram from the device carries PTR + SRV + A — instance ``p35x1``,
   target ``p35x1.local.``, A ``192.168.x.x``; the SRV port is 2001, the
   change-stream port — the RPC port is still 2002). :func:`mdns_discover`
   sends the query to ``224.0.0.251:5353`` with the QU (unicast-response)
   bit set and parses every response received within the timeout.

2. **Subnet TCP probe (fallback).** For networks that block multicast:
   a bounded concurrent TCP *connect* probe of the local subnet on the
   Stadium's RPC port 2002 (the device ignores ICMP, so ping is useless).
   The range comes from the interface's own NETMASK — not an assumed /24,
   which silently under-scanned every wider network (a /22 hides three
   quarters of its hosts; hc-3qw) — capped at :data:`MAX_PROBE_HOSTS`
   addresses anchored on our own address, so a /16 probes a bounded slice
   rather than 65k hosts. Strictly limited to the machine's own subnet —
   never probes beyond the local subnet — with short per-connect timeouts
   and bounded concurrency. The probe additionally refuses to run when the
   range is not in a private (RFC 1918) range: connect-scanning a PUBLIC
   subnet is a port scan of strangers, not LAN discovery (backlog #77).

Known limitations (backlog #77):

* **Default-route interface blindness — FIXED (hgc-094).** Discovery used
  to look only at the interface carrying the default route, so a VPN
  (``utun``) holding it hid a LAN-attached Stadium. :func:`probe_targets`
  now enumerates every UP interface with an RFC 1918 address (from the
  same ``ip``/``ifconfig`` output the netmask lookup already reads),
  preferring physical interfaces and using tunnels only when nothing else
  exists; mDNS queries go out bound to each of those addresses and the
  probe sweeps each range (1024-host cap per interface).
* **The mDNS listener is unicast-reply-only.** :func:`mdns_discover` sends
  a QU (unicast-response) query from an ephemeral port and never joins the
  224.0.0.251 multicast group, so responders that ignore the QU bit and
  reply only via multicast (an RFC 6762 §5.4 allowance) are invisible.
  The Stadium (fw 1.3.2, verified live) replies unicast, so this works
  against real hardware; other firmware would fall through to the probe.

Both mechanisms only *find candidates*; the CLI confirms each candidate
with the cheap read-only ``/ProductInfoGet`` handshake before trusting or
persisting it (community prior art: the Stadium desktop app's discovery
layer is flaky but direct-to-IP sessions are stable — so helixgen uses
discovery exactly once, persists the result into the per-device record
(``~/.helixgen/devices/<serial>.json``), and keeps every session
direct-to-IP).

This module also owns :func:`resolve_ip` — the single IP resolution chain
used everywhere a device IP is needed::

    --ip flag  >  $HELIXGEN_HELIX_IP  >  persisted device record  >  error

There is **no hardcoded default IP** anywhere anymore (the old baked-in
``192.168.x.x`` literal was the maintainer's own DHCP lease — a
guaranteed-wrong default for anyone else that failed as a long stall).
"""
from __future__ import annotations

import errno
import ipaddress
import os
import re
import select
import socket
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

#: The DNS-SD service type the Stadium advertises (verified live).
MDNS_SERVICE = "_stadiumserver._tcp.local."

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353

#: The Stadium's RPC port — used by the connect-probe fallback and the
#: reachability confirmation. (The mDNS SRV record advertises 2001, the
#: property-change PUB stream; sessions talk RPC on 2002.)
RPC_PORT = 2002

_TYPE_A = 1
_TYPE_PTR = 12
_TYPE_SRV = 33


class IPResolutionError(RuntimeError):
    """No device IP could be resolved (no flag, no env, no persisted record)."""


#: macOS Local Network privacy (hgc-7he). A process denied Local Network
#: access gets connect()/sendto() to LAN addresses failing IMMEDIATELY with
#: EHOSTUNREACH (sometimes EPERM) — ZeroMQ retries that silently, so without
#: this hint the only symptom was a misleading "no Helix Stadium answered".
LOCAL_NETWORK_HINT = (
    "macOS blocked the connection before it left this machine — this "
    "process is most likely denied Local Network access. Enable your "
    "terminal app under System Settings > Privacy & Security > Local "
    "Network, then QUIT and restart the terminal (the grant only reaches "
    "newly launched processes). If access is already granted, the host may "
    "simply be down — check the device is on and the IP is right.")


class LocalNetworkDenied(RuntimeError):
    """Every discovery socket failed the way a macOS Local Network privacy
    denial does (see :data:`LOCAL_NETWORK_HINT`)."""


def is_local_network_denial(exc: BaseException) -> bool:
    """True when ``exc`` looks like a macOS Local Network privacy denial
    (EHOSTUNREACH / EPERM on darwin). Never true off macOS."""
    return sys.platform == "darwin" and getattr(exc, "errno", None) in (
        errno.EHOSTUNREACH, errno.EPERM)


#: The RFC 1918 private ranges — the only address space the /24 probe will
#: scan (backlog #77). Checked explicitly rather than via
#: ``ipaddress.is_private`` because is_private's answer changed across
#: Python versions (3.12.4+ counts documentation/CGNAT special-purpose
#: ranges too); the probe's etiquette gate must be deterministic.
_RFC1918_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_rfc1918(ip: str) -> bool:
    """True when ``ip`` parses as IPv4 inside an RFC 1918 private range.
    Unparseable input is False (fail closed)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return isinstance(addr, ipaddress.IPv4Address) and any(
        addr in net for net in _RFC1918_NETS)


#: The change-stream port the Stadium advertises in its mDNS SRV record
#: (observed live: SRV 2001, RPC 2002 — the RPC port sits one above the
#: advertised stream port). A device on a nonstandard SRV port is assumed
#: to keep that +1 offset for its RPC port. (backlog #77)
SRV_PORT = 2001


@dataclass
class Candidate:
    """A discovery hit — an address that *looks like* a Stadium (mDNS
    advertisement or open RPC port); not yet confirmed by a handshake."""

    ip: str
    hostname: Optional[str] = None   # e.g. "p35x1.local." (mDNS only)
    instance: Optional[str] = None   # e.g. "p35x1" (mDNS only)
    via: str = "mdns"                # "mdns" | "probe"
    # Nonstandard RPC port derived from the mDNS SRV record (None = the
    # standard 2002; probe hits are always standard). backlog #77.
    rpc_port: Optional[int] = None


# ---------------------------------------------------------------------------
# DNS wire helpers (offline-testable)
# ---------------------------------------------------------------------------

def encode_dns_name(name: str) -> bytes:
    """``foo.bar.local.`` -> DNS label wire form (no compression)."""
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_mdns_query(service: str = MDNS_SERVICE) -> bytes:
    """A one-shot mDNS PTR question for ``service`` with the QU bit set
    (unicast-response requested; RFC 6762 §5.4)."""
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    question = encode_dns_name(service) + struct.pack(">HH", _TYPE_PTR, 0x8001)
    return header + question


def _parse_name(data: bytes, off: int) -> Tuple[str, int]:
    """Decode a (possibly compressed) DNS name at ``off``; returns
    ``(dotted_name, next_offset)``. Loop-safe on malicious pointers."""
    labels: List[str] = []
    next_off = off
    jumped = False
    seen: set = set()
    while True:
        if off >= len(data):
            break
        length = data[off]
        if length == 0:
            off += 1
            if not jumped:
                next_off = off
            break
        if length & 0xC0 == 0xC0:
            if off + 2 > len(data):
                break
            ptr = struct.unpack(">H", data[off:off + 2])[0] & 0x3FFF
            if not jumped:
                next_off = off + 2
            if ptr in seen:  # compression loop — bail
                break
            seen.add(ptr)
            off = ptr
            jumped = True
            continue
        labels.append(data[off + 1:off + 1 + length].decode("utf-8", "replace"))
        off += 1 + length
    name = ".".join(labels)
    return (name + "." if name else "."), next_off


def parse_mdns_response(data: bytes) -> List[Tuple[str, str, object]]:
    """Parse one mDNS response datagram into ``(name, type, value)`` records.

    Only the record types discovery needs: ``PTR`` (value = target name),
    ``SRV`` (value = ``(port, target)``), ``A`` (value = dotted IPv4).
    Malformed packets yield ``[]`` rather than raising.
    """
    records: List[Tuple[str, str, object]] = []
    try:
        qd, an, ns, ar = struct.unpack(">HHHH", data[4:12])
        off = 12
        for _ in range(qd):
            _, off = _parse_name(data, off)
            off += 4
        for _ in range(an + ns + ar):
            name, off = _parse_name(data, off)
            if off + 10 > len(data):
                break
            rtype, _rclass, _ttl, rdlen = struct.unpack(
                ">HHIH", data[off:off + 10])
            off += 10
            if off + rdlen > len(data):
                break
            rdata = data[off:off + rdlen]
            if rtype == _TYPE_PTR:
                target, _ = _parse_name(data, off)
                records.append((name, "PTR", target))
            elif rtype == _TYPE_SRV and rdlen >= 6:
                _pri, _wt, port = struct.unpack(">HHH", rdata[:6])
                target, _ = _parse_name(data, off + 6)
                records.append((name, "SRV", (port, target)))
            elif rtype == _TYPE_A and rdlen == 4:
                records.append((name, "A", socket.inet_ntoa(rdata)))
            off += rdlen
    except (struct.error, OSError, ValueError):
        return []
    return records


def candidates_from_records(
        records: List[Tuple[str, str, object]],
        service: str = MDNS_SERVICE) -> List[Candidate]:
    """Join PTR -> SRV -> A records into :class:`Candidate` hits for
    ``service``. Case-insensitive name matching (mDNS names are)."""
    svc = service.lower()
    instances: List[str] = []          # PTR targets (instance names)
    srv: Dict[str, Tuple[int, str]] = {}   # instance -> (port, host)
    addrs: Dict[str, str] = {}         # host -> ipv4
    for name, rtype, value in records:
        if rtype == "PTR" and name.lower() == svc:
            instances.append(str(value))
        elif rtype == "SRV":
            srv[name.lower()] = (int(value[0]), str(value[1]))  # type: ignore[index]
        elif rtype == "A":
            addrs[name.lower()] = str(value)
    out: List[Candidate] = []
    for inst in instances:
        entry = srv.get(inst.lower())
        if not entry:
            continue
        srv_port, host = entry
        ip = addrs.get(host.lower())
        if not ip:
            continue
        label = inst[:-len("." + service)] if inst.lower().endswith(
            "." + svc) else inst.rstrip(".")
        # A nonstandard advertised stream port implies a nonstandard RPC port
        # one above it; the standard 2001 leaves rpc_port None (default 2002).
        rpc_port = srv_port + 1 if srv_port and srv_port != SRV_PORT else None
        out.append(Candidate(ip=ip, hostname=host, instance=label,
                             via="mdns", rpc_port=rpc_port))
    return out


# ---------------------------------------------------------------------------
# live mechanisms
# ---------------------------------------------------------------------------

def mdns_discover(timeout: float = 3.0,
                  service: str = MDNS_SERVICE, *,
                  bind_ips: Optional[Sequence[str]] = None) -> List[Candidate]:
    """One-shot mDNS browse for ``service``; collects responses for up to
    ``timeout`` seconds (re-asking once halfway through for reliability).
    Returns unique candidates (by IP). Socket errors return ``[]``.

    ``bind_ips`` (hgc-094): one query socket per local address, bound to it
    with ``IP_MULTICAST_IF`` so the query leaves THAT interface — a VPN
    holding the default route no longer swallows the LAN query. Empty /
    ``None`` = one unbound socket (the OS picks the interface).

    When EVERY send is refused the way a macOS Local Network denial refuses
    it, raises :class:`LocalNetworkDenied` (hgc-7he) instead of returning
    ``[]``."""
    query = build_mdns_query(service)
    found: Dict[str, Candidate] = {}
    addrs = list(bind_ips or [""])
    socks: List[socket.socket] = []
    denied = 0
    for ip in addrs:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((ip, 0))
            if ip:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                socket.inet_aton(ip))
            sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
        except OSError as e:
            denied += is_local_network_denial(e)
            sock.close()
            continue
        socks.append(sock)
    if not socks:
        if denied == len(addrs):
            raise LocalNetworkDenied(LOCAL_NETWORK_HINT)
        return []
    try:
        deadline = time.monotonic() + max(0.5, timeout)
        resend_at = time.monotonic() + max(0.5, timeout) / 2
        while socks and time.monotonic() < deadline:
            if time.monotonic() >= resend_at:
                resend_at = float("inf")
                for sock in socks:
                    try:
                        sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
                    except OSError:
                        pass
            try:
                ready, _, _ = select.select(socks, [], [], 0.25)
            except (OSError, ValueError):
                break
            for sock in ready:
                try:
                    data, _addr = sock.recvfrom(9000)
                except OSError:
                    socks.remove(sock)  # a dead socket must not spin select
                    sock.close()
                    continue
                for cand in candidates_from_records(
                        parse_mdns_response(data), service):
                    found.setdefault(cand.ip, cand)
    finally:
        for sock in socks:
            sock.close()
    return sorted(found.values(), key=lambda c: c.ip)


def local_ipv4() -> Optional[str]:
    """This machine's primary outbound IPv4 (UDP connect trick — no packet
    is actually sent). ``None`` when undeterminable or loopback/link-local."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1; connect() sends nothing
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None
    if ip.startswith("127.") or ip.startswith("169.254."):
        return None
    return ip


#: Ceiling on how many addresses one connect-probe sweep may touch. A /22
#: (1024 addresses) takes ~6 s at the default timeout/concurrency; a /16
#: would be 65k hosts — a scan, not a discovery. When the interface's
#: network is wider than this, the probe sweeps the cap-sized block
#: containing our own address instead (hc-3qw).
MAX_PROBE_HOSTS = 1024


def netmask_to_prefixlen(mask: str) -> Optional[int]:
    """``0xfffffc00`` / ``255.255.252.0`` -> ``22``. ``None`` for anything
    that is not a legal contiguous IPv4 netmask (fail closed)."""
    try:
        if mask.lower().startswith("0x"):
            mask = str(ipaddress.IPv4Address(int(mask, 16)))
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except ValueError:
        return None


def parse_prefixlen(output: str, ip: str) -> Optional[int]:
    """Prefix length of the interface carrying ``ip``, read out of
    ``ifconfig`` or ``ip -o -4 addr`` output. Pure — the offline half of
    :func:`local_prefixlen`.

    Handles both spellings: iproute2's ``inet 192.168.4.98/22`` and
    BSD/macOS's ``inet 192.168.4.98 netmask 0xfffffc00``. ``None`` when the
    address does not appear (callers then assume /24, the old behavior).
    """
    quoted = re.escape(ip)
    m = re.search(rf"\binet\s+{quoted}/(\d{{1,2}})(?!\d)", output)
    if m:
        return int(m.group(1))
    m = re.search(rf"\binet\s+{quoted}\s+netmask\s+(\S+)", output)
    return netmask_to_prefixlen(m.group(1)) if m else None


def local_prefixlen(ip: str) -> Optional[int]:
    """The netmask width of the local interface holding ``ip``, or ``None``
    when it can't be determined (no ``ip``/``ifconfig`` on PATH — Windows,
    say — or the address belongs to no local interface).

    Shelling out is the portable way in: stdlib exposes no per-interface
    netmask (``socket`` gives addresses only, and ``fcntl``'s
    ``SIOCGIFNETMASK`` is Linux-only and wants an interface name).
    """
    for cmd in (["ip", "-o", "-4", "addr"], ["ifconfig", "-a"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found = parse_prefixlen(out or "", ip)
        if found is not None:
            return found
    return None


def probe_network(ip: str, prefixlen: Optional[int],
                  *, max_hosts: int = MAX_PROBE_HOSTS
                  ) -> ipaddress.IPv4Network:
    """The network the probe will actually sweep: ``ip``'s network at
    ``prefixlen`` (``None`` = the historical /24 assumption), narrowed to at
    most ``max_hosts`` addresses around ``ip``. Pure; raises ``ValueError``
    on anything that is not a dotted IPv4 address (an IPv6 one included —
    the probe is IPv4-only)."""
    width = 24 if prefixlen is None else max(0, min(32, int(prefixlen)))
    floor = 32 - (max(1, int(max_hosts)).bit_length() - 1)
    return ipaddress.IPv4Network(f"{ip}/{max(width, floor)}", strict=False)


#: Interface-name prefixes treated as VPN/tunnel links (alongside the
#: POINTOPOINT flag). Scanned only when no physical private interface exists.
_TUNNEL_PREFIXES = ("utun", "tun", "ppp", "ipsec", "wg", "gif", "stf")


def _is_tunnel(name: str, p2p: bool) -> bool:
    return p2p or name.lower().startswith(_TUNNEL_PREFIXES)


def parse_interfaces(output: str
                     ) -> List[Tuple[str, str, Optional[int], bool]]:
    """``(name, ipv4, prefixlen, is_tunnel)`` for every IPv4 address on an
    UP interface, read from ``ifconfig -a`` (BSD/macOS or net-tools) or
    ``ip -o -4 addr show up`` output. Pure — the offline half of
    :func:`local_interfaces`. ``prefixlen`` is ``None`` for an unparseable
    netmask; down interfaces (ifconfig flags without ``UP``) are dropped."""
    out: List[Tuple[str, str, Optional[int], bool]] = []
    name: Optional[str] = None
    up = p2p = False
    for line in output.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)",
                     line)
        if m:  # iproute2 one-line form (already filtered to UP links)
            out.append((m[1], m[2], int(m[3]), _is_tunnel(m[1], False)))
            continue
        m = re.match(r"^(\S+?):\s+flags=\S*<([^>]*)>", line)
        if m:  # ifconfig interface header
            flags = set(m[2].split(","))
            name, up, p2p = m[1], "UP" in flags, "POINTOPOINT" in flags
            continue
        m = re.match(
            r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)\b.*?\bnetmask\s+(\S+)", line)
        if m and name and up:
            out.append((name, m[1], netmask_to_prefixlen(m[2]),
                        _is_tunnel(name, p2p)))
    return out


def local_interfaces() -> List[Tuple[str, str, Optional[int], bool]]:
    """Every UP interface's IPv4 addresses (see :func:`parse_interfaces`),
    or ``[]`` when neither ``ip`` nor ``ifconfig`` is available (Windows).
    Shelling out because stdlib exposes no portable per-interface address
    + netmask listing."""
    for cmd in (["ip", "-o", "-4", "addr", "show", "up"], ["ifconfig", "-a"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found = parse_interfaces(out or "")
        if found:
            return found
    return []


def probe_targets(*, max_hosts: int = MAX_PROBE_HOSTS
                  ) -> List[Tuple[str, ipaddress.IPv4Network]]:
    """``(our address, range to sweep)`` for every interface discovery
    should look at (hgc-094): each UP interface with an RFC 1918 address —
    physical ones only, unless there are none, in which case VPN/tunnel
    links are used (a VPN holding the default route used to blind discovery
    to the LAN). Each range is the interface's netmask capped at
    ``max_hosts`` (:func:`probe_network`); interfaces sharing one range are
    swept once. Without interface enumeration (no ``ip``/``ifconfig``) it
    falls back to the default-route address (:func:`local_ipv4`). ``[]``
    when nothing private is probeable."""
    private = [i for i in local_interfaces() if _is_rfc1918(i[1])]
    chosen = [i for i in private if not i[3]] or private
    if not chosen:
        me = local_ipv4()
        if not me or not _is_rfc1918(me):
            return []
        chosen = [("", me, local_prefixlen(me), False)]
    out: List[Tuple[str, ipaddress.IPv4Network]] = []
    for _name, ip, plen, _tun in chosen:
        net = probe_network(ip, plen, max_hosts=max_hosts)
        if all(net != seen for _, seen in out):
            out.append((ip, net))
    return out


_REDISCOVER = ("wrong IP, device off, or a stale device record? Re-run "
               "`helixgen device discover`, or pass --ip / set "
               "$HELIXGEN_HELIX_IP.")


def connect_error(ip: str, port: int = RPC_PORT, *,
                  timeout: float = 2.0) -> Optional[str]:
    """One cheap TCP connect probe of ``ip:port`` — the canonical "is the
    device up?" check (the Stadium ignores ICMP ping; an open RPC port is
    the reliable reachability signal). ``None`` when the connect succeeds,
    else a short actionable reason that keeps the failure modes distinct:
    a macOS Local Network denial (hgc-7he), a refusal (host up, port
    closed), a timeout, or any other socket error. Never raises."""
    try:
        with socket.create_connection((str(ip), int(port)), timeout=timeout):
            return None
    except (socket.timeout, TimeoutError):
        return f"TCP connect timed out after {timeout:g}s — {_REDISCOVER}"
    except ConnectionRefusedError:
        return ("connection refused — the host is up but nothing listens on "
                "that port: Remote Access disabled on the Stadium, a wrong "
                "--port, or that address is not a Stadium.")
    except OSError as e:
        if is_local_network_denial(e):
            return (f"TCP connect failed: {e.strerror or e} "
                    f"(errno {e.errno}). {LOCAL_NETWORK_HINT}")
        return f"TCP connect failed ({e}) — {_REDISCOVER}"


def probe_reachable(ip: str, port: int = RPC_PORT, *,
                    timeout: float = 2.0) -> bool:
    """Boolean form of :func:`connect_error`. Never raises."""
    return connect_error(ip, port, timeout=timeout) is None


def probe_subnet(port: int = RPC_PORT, *, connect_timeout: float = 0.35,
                 max_workers: int = 64,
                 subnet_ip: Optional[str] = None,
                 prefixlen: Optional[int] = None,
                 max_hosts: int = MAX_PROBE_HOSTS) -> List[str]:
    """TCP connect-probe of the **local subnet only** for an open Stadium
    RPC port. The range comes from the interface's netmask
    (:func:`local_prefixlen`; ``prefixlen`` overrides it, ``None`` on both
    falls back to /24) capped at ``max_hosts`` addresses around our own
    address — see :func:`probe_network`. Etiquette: never probes beyond the
    machine's own subnet, short per-connect timeouts, bounded thread pool,
    skips our own address. Returns the responding IPs (unconfirmed).

    Private ranges only (backlog #77): when the range is not RFC
    1918-private, the probe refuses to run (stderr warning naming the
    range, empty result) — connect-scanning a public subnet would be a port
    scan of strangers' hosts, not LAN discovery. Use ``--ip`` /
    ``$HELIXGEN_HELIX_IP`` to reach a device on an exotic network.
    """
    me = subnet_ip or local_ipv4()
    if not me:
        return []
    try:
        net = probe_network(
            me, local_prefixlen(me) if prefixlen is None else prefixlen,
            max_hosts=max_hosts)
    except ValueError:
        net = None
    # Both gates matter: our own address must be private, and so must every
    # address the (possibly wider-than-/24) sweep would touch.
    if net is None or not any(net.subnet_of(n) for n in _RFC1918_NETS):
        sys.stderr.write(
            f"warning: {net or me} is not in a private (RFC 1918) range — "
            "refusing to connect-scan it. Pass --ip (or set "
            "$HELIXGEN_HELIX_IP) to target the device directly instead.\n")
        return []
    targets = [str(host) for host in net.hosts() if str(host) != me]

    def _try(ip: str) -> Tuple[Optional[str], bool]:
        try:
            with socket.create_connection((ip, port), timeout=connect_timeout):
                return ip, False
        except OSError as e:
            return None, is_local_network_denial(e)

    hits: List[str] = []
    denied = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_try, ip) for ip in targets]
        for fut in as_completed(futures):
            ip, was_denied = fut.result()
            if ip:
                hits.append(ip)
            denied += was_denied
    # A real LAN mixes timeouts/refusals; EVERY connect failing instantly
    # with EHOSTUNREACH/EPERM is the macOS privacy denial (hgc-7he).
    if not hits and targets and denied == len(targets):
        raise LocalNetworkDenied(LOCAL_NETWORK_HINT)
    return sorted(hits)


# ---------------------------------------------------------------------------
# the resolution chain
# ---------------------------------------------------------------------------

_NO_IP_MSG = (
    "no Helix device IP configured. Resolution is: --ip flag > "
    "$HELIXGEN_HELIX_IP > the device record persisted by `helixgen device "
    "discover` — none is set. Run `helixgen device discover` once to find "
    "and persist your Stadium's address, or pass --ip / set "
    "$HELIXGEN_HELIX_IP explicitly.")


def resolve_ip(explicit: Optional[str] = None, *, warn: bool = True) -> str:
    """The single device-IP resolution chain: ``explicit`` (the --ip flag /
    a caller-supplied address) > ``$HELIXGEN_HELIX_IP`` > the most recently
    discovered persisted device record. Raises :class:`IPResolutionError`
    **immediately** (no network, no stall) when none is available.

    When several persisted records carry different IPs (multiple Stadiums
    discovered), the most recently discovered wins deterministically
    (``ip_updated_at`` desc, then serial desc) and a warning names the
    chosen device — pass ``--ip`` to target another.
    """
    if explicit:
        return explicit
    env = os.environ.get("HELIXGEN_HELIX_IP")
    if env:
        return env
    from helixgen.device import observations

    recorded = observations.devices_with_ips()
    if not recorded:
        raise IPResolutionError(_NO_IP_MSG)
    chosen = recorded[0]
    if warn and len({r["ip"] for r in recorded}) > 1:
        sys.stderr.write(
            f"warning: {len(recorded)} discovered devices on record; using "
            f"serial {chosen['serial']} at {chosen['ip']} (most recently "
            "discovered) — pass --ip to target another\n")
    return str(chosen["ip"])


def resolve_port(ip: Optional[str] = None, *,
                 explicit: Optional[int] = None) -> int:
    """The RPC control port for the resolved device: an ``explicit`` ``--port``
    wins; else the nonstandard port persisted in the record for ``ip`` (or,
    when ``ip`` is None, the record :func:`resolve_ip` would pick); else the
    standard :data:`RPC_PORT`.

    Keeps the port half of the resolution chain aligned with the IP half so a
    device discovered on a nonstandard port is reached automatically, without
    the user re-passing ``--port`` every verb (backlog #77)."""
    if explicit is not None:
        return int(explicit)
    from helixgen.device import observations

    target_ip = ip
    if not target_ip:
        try:
            target_ip = resolve_ip(None, warn=False)
        except IPResolutionError:
            return RPC_PORT
    for rec in observations.devices_with_ips():
        if rec.get("ip") == target_ip and rec.get("port"):
            return int(rec["port"])
    return RPC_PORT
