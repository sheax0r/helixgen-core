#!/usr/bin/env python3
"""Helix Stadium network POC — list presets on the device over the LAN.

The Line 6 Helix Stadium editor talks to the hardware over ZeroMQ (ZMTP 3.0):
  * tcp://<device>:2002  ROUTER  — RPC command/response  (we connect as DEALER)
  * tcp://<device>:2001  PUB     — property-change stream (subscribe)
  * tcp://<device>:2003  PUB     — DSP telemetry stream   (subscribe)

Messages are OSC (address + typetags + args); blob args carry a 4-byte
big-endian length followed by a msgpack map. Preset/setlist enumeration is
`/GetContainerContents(reqid, containerCID)`, which streams one reply per
child item: {cid_: <CID>, name: "...", cctp: <type>, posi: <slot>, ...}.

Usage:  python helix_probe.py [device_ip]
"""
import sys, time, struct, itertools
import zmq
import msgpack
sys.path.insert(0, __file__.rsplit('/', 1)[0] + '/tools')
from osc import osc_encode, parse_osc_message

DEVICE = sys.argv[1] if len(sys.argv) > 1 else '192.168.4.84'
CMD_PORT = 2002
ROOT = -1

_reqid = itertools.count(1000)


def decode_blob(blob):
    """Blobs are msgpack — sometimes raw, sometimes with a 4-byte length prefix."""
    if not blob:
        return None
    try:
        return msgpack.unpackb(blob, raw=False, strict_map_key=False)
    except Exception:
        pass
    if len(blob) >= 4:
        try:
            return msgpack.unpackb(blob[4:], raw=False, strict_map_key=False)
        except Exception:
            pass
    return blob


def parse_reply(raw):
    """Return (addr, [decoded args]) for one OSC reply frame."""
    i = raw.find(b'/')
    if i < 0:
        return None, []
    addr, args, _ = parse_osc_message(raw, i)
    out = []
    for t, v in args:
        out.append(decode_blob(v) if t == 'b' else v)
    return addr, out


def get_container_contents(sock, container_cid, first_wait=2.0, settle=0.4):
    """Send one enumeration request and collect the reply array(s)."""
    rid = next(_reqid)
    msg = osc_encode('/GetContainerContents', [('i', rid), ('i', container_cid)])
    import os
    if os.environ.get('DBG'):
        print('  [dbg] send reqid=%d cid=%d len=%d hex=%s' % (rid, container_cid, len(msg), msg[:32].hex()))
    sock.send(msg)
    items = []
    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)
    got_any = False
    while True:
        wait = settle if got_any else first_wait
        ev = poller.poll(int(wait * 1000))
        if os.environ.get('DBG'):
            print('  [dbg] poll ->', bool(ev))
        if ev:
            raw = sock.recv()
            addr, args = parse_reply(raw)
            if os.environ.get('DBG'):
                print('  [dbg] recv len=%d addr=%s argtypes=%s' % (
                    len(raw), addr, [type(a).__name__ for a in args]))
            if addr != '/GetContainerContents':
                continue
            got_any = True
            for a in args:  # args: [reqid, arrayOfChildMaps, trailingInt]
                if isinstance(a, list):
                    items.extend(x for x in a if isinstance(x, dict))
                elif isinstance(a, dict):
                    items.append(a)
        else:
            break  # quiet period => done
    return items


def main():
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.DEALER)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, 2000)
    print('connecting DEALER -> tcp://%s:%d' % (DEVICE, CMD_PORT))
    sock.connect('tcp://%s:%d' % (DEVICE, CMD_PORT))
    time.sleep(0.6)  # let ZMTP handshake settle before first send

    # Enumerate the root container to discover setlists.
    print('enumerating root container (%d)...' % ROOT)
    roots = get_container_contents(sock, ROOT)
    if not roots:
        print('NO REPLY — device may require a handshake command first, or wrong port.')
        return

    def name(m): return m.get('name', '?')
    def cid(m):  return m.get('cid_')
    def ctp(m):  return m.get('cctp')

    print('\n=== root items (%d) ===' % len(roots))
    for m in roots:
        print('  cid=%-6s cctp=%-5s %s' % (cid(m), ctp(m), name(m)))

    # Recurse one level: list each container's presets.
    print('\n=== preset lists ===')
    for m in roots:
        c = cid(m)
        if c is None:
            continue
        kids = get_container_contents(sock, c)
        presets = [k for k in kids if k.get('name')]
        if not presets:
            continue
        print('\n[%s]  (cid=%s, %d entries)' % (name(m), c, len(presets)))
        for k in presets:
            pos = k.get('posi')
            print('   %3s  cid=%-6s %s' % (pos if pos is not None else '', cid(k), name(k)))

    sock.close()


if __name__ == '__main__':
    main()
