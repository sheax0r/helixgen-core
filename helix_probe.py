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


# Well-known virtual setlist container slots (discovered by reverse-engineering).
SETLIST_SLOTS = [(-1, 'FACTORY'), (-2, 'USER'), (-5, 'Throwaway')]
SLOT_LABELS = 'ABCD'


def slot_label(posi):
    """Device 'posi' -> Helix bank/slot label, e.g. 0 -> '1A', 5 -> '2B'."""
    if posi is None:
        return ''
    return '%d%s' % (posi // 4 + 1, SLOT_LABELS[posi % 4])


def main():
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.DEALER)
    sock.setsockopt(zmq.LINGER, 0)
    print('connecting DEALER -> tcp://%s:%d ...' % (DEVICE, CMD_PORT))
    sock.connect('tcp://%s:%d' % (DEVICE, CMD_PORT))
    time.sleep(0.6)  # let ZMTP handshake settle before first send

    any_reply = False
    for slot, label in SETLIST_SLOTS:
        presets = [m for m in get_container_contents(sock, slot) if m.get('cctp') == 1000]
        if not presets:
            continue
        any_reply = True
        presets.sort(key=lambda m: (m.get('posi') if m.get('posi') is not None else 1e9))
        print('\n=== Setlist %s (container %d, %d presets) ===' % (label, slot, len(presets)))
        for m in presets:
            print('  %-4s cid=%-6s %s' % (
                slot_label(m.get('posi')), m.get('cid_'), m.get('name', '?')))

    if not any_reply:
        print('NO REPLY — device unreachable, or protocol/port changed.')
    sock.close()


if __name__ == '__main__':
    main()
