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
   a bounded concurrent TCP *connect* probe of the local /24 on the
   Stadium's RPC port 2002 (the device ignores ICMP, so ping is useless).
   Strictly limited to the machine's own /24 — never probes beyond the
   local subnet — with short per-connect timeouts and bounded concurrency.
   The probe additionally refuses to run when the machine's own address is
   not in a private (RFC 1918) range: connect-scanning 253 hosts of a
   PUBLIC /24 is a port scan of strangers, not LAN discovery (backlog #77).

Known limitations (backlog #77):

* **Default-route interface blindness.** :func:`local_ipv4` picks the
  interface that carries the default route. With a VPN up, that is usually
  the tunnel — so both the mDNS query and the /24 probe can look at the
  wrong network and miss a LAN-attached Stadium. Workaround: disconnect
  the VPN for the one-shot ``device discover``, or skip discovery entirely
  and pass ``--ip`` / set ``$HELIXGEN_HELIX_IP``. (Enumerating candidate
  interfaces needs per-interface addresses, which pure stdlib does not
  expose portably.)
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

import ipaddress
import os
import socket
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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
                  service: str = MDNS_SERVICE) -> List[Candidate]:
    """One-shot mDNS browse for ``service``; collects responses for up to
    ``timeout`` seconds (re-asking once halfway through for reliability).
    Returns unique candidates (by IP). Socket errors return ``[]``."""
    query = build_mdns_query(service)
    found: Dict[str, Candidate] = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 0))
        sock.settimeout(0.25)
        deadline = time.monotonic() + max(0.5, timeout)
        resend_at = time.monotonic() + max(0.5, timeout) / 2
        sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
        while time.monotonic() < deadline:
            if time.monotonic() >= resend_at:
                resend_at = float("inf")
                try:
                    sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
                except OSError:
                    pass
            try:
                data, _addr = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            for cand in candidates_from_records(
                    parse_mdns_response(data), service):
                found.setdefault(cand.ip, cand)
    except OSError:
        pass
    finally:
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


def probe_reachable(ip: str, port: int = RPC_PORT, *,
                    timeout: float = 2.0) -> bool:
    """One cheap TCP connect probe of ``ip:port`` — the canonical "is the
    device up?" check (the Stadium ignores ICMP ping; an open RPC port is
    the reliable reachability signal — the same test :func:`probe_subnet`
    and the live suite's gate use). Never raises."""
    try:
        with socket.create_connection((str(ip), int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def probe_subnet(port: int = RPC_PORT, *, connect_timeout: float = 0.35,
                 max_workers: int = 64,
                 subnet_ip: Optional[str] = None) -> List[str]:
    """TCP connect-probe of the **local /24 only** for an open Stadium RPC
    port. Etiquette: never probes beyond the machine's own /24 (254 hosts),
    short per-connect timeouts, bounded thread pool, skips our own address.
    Returns the responding IPs (unconfirmed candidates).

    Private ranges only (backlog #77): when the machine's own address is
    not RFC 1918-private, the probe refuses to run (stderr warning, empty
    result) — connect-scanning a public /24 would be a port scan of 253
    strangers' hosts, not LAN discovery. Use ``--ip`` /
    ``$HELIXGEN_HELIX_IP`` to reach a device on an exotic network.
    """
    me = subnet_ip or local_ipv4()
    if not me:
        return []
    if not _is_rfc1918(me):
        sys.stderr.write(
            f"warning: this machine's address {me} is not in a private "
            "(RFC 1918) range — refusing to connect-scan a non-private "
            "/24. Pass --ip (or set $HELIXGEN_HELIX_IP) to target the "
            "device directly instead.\n")
        return []
    base = me.rsplit(".", 1)[0]
    targets = [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != me]

    def _try(ip: str) -> Optional[str]:
        try:
            with socket.create_connection((ip, port), timeout=connect_timeout):
                return ip
        except OSError:
            return None

    hits: List[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_try, ip) for ip in targets]
        for fut in as_completed(futures):
            ip = fut.result()
            if ip:
                hits.append(ip)
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
