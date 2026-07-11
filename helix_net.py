#!/usr/bin/env python3
"""helix_net — a minimal network client for the Line 6 Helix Stadium.

Speaks the editor's own protocol directly over the LAN: OSC messages carried on
ZeroMQ (ZMTP 3.0), msgpack blob payloads. No hardware editor required.

Ports (all cleartext TCP on the device):
    2002  ROUTER  RPC command/response      <- we connect a DEALER here
    2001  PUB     property-change stream     (not needed for CRUD)
    2003  PUB     DSP telemetry stream       (not needed for CRUD)

Container model: presets live in "containers" addressed by CID. Setlists are
virtual containers at negative slots: -1 FACTORY, -2 USER, -5 Throwaway.
Content-type (`cctp`): 1000 preset, 1001 setlist, 1002 template/IR.

RPC vocabulary (reverse-engineered):
    LIST    /GetContainerContents (reqid, containerCID) -> [ {cid_,name,cctp,posi}, ... ]
    READ    /GetContentRef        (reqid, cid)          -> {name, cid_, ccid, blck, ...}
    LOAD    /LoadPresetWithCID    (reqid, cid)
    CREATE  /AddContentsToContainer(reqid, container, [srcCIDs], pos, 0, 0)
    RENAME  /SetContentAttrs      (reqid, cid, {name: "..."})
    DELETE  /RemoveContent        (reqid, container, [cids])
Every write replies /status [reqid, code, n]  (code 0 = OK).
"""
import time, itertools
import zmq
import msgpack
from tools.osc import osc_encode, parse_osc_message

USER = -2
FACTORY = -1
THROWAWAY = -5
CT_PRESET, CT_SETLIST, CT_TEMPLATE = 1000, 1001, 1002


def _decode_blob(v):
    if not v:
        return None
    for start in (0, 4):
        try:
            return msgpack.unpackb(v[start:], raw=False, strict_map_key=False)
        except Exception:
            continue
    return v


class HelixClient:
    def __init__(self, ip='192.168.4.84', port=2002):
        self.ip, self.port = ip, port
        self._rid = itertools.count(1000)
        self.ctx = zmq.Context.instance()
        self.sock = None

    def connect(self, settle=0.6):
        self.sock = self.ctx.socket(zmq.DEALER)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.connect('tcp://%s:%d' % (self.ip, self.port))
        time.sleep(settle)  # let the ZMTP handshake complete
        self.poller = zmq.Poller()
        self.poller.register(self.sock, zmq.POLLIN)
        return self

    def close(self):
        if self.sock:
            self.sock.close()

    def __enter__(self): return self.connect()
    def __exit__(self, *a): self.close()

    def _rpc(self, addr, args, first_wait=2.0, settle=0.4):
        """Send a command and collect all reply frames until a quiet period.

        Returns a list of (addr, decoded_args) frames whose reqid matches ours.
        """
        rid = next(self._rid)
        self.sock.send(osc_encode(addr, [('i', rid)] + args))
        replies = []
        got = False
        while True:
            if self.poller.poll(int((settle if got else first_wait) * 1000)):
                raw = self.sock.recv()
                i = raw.find(b'/')
                if i < 0:
                    continue
                raddr, rargs, _ = parse_osc_message(raw, i)
                dec = [_decode_blob(v) if t == 'b' else v for t, v in rargs]
                if dec and dec[0] == rid:
                    replies.append((raddr, dec))
                got = True
            else:
                break
        return replies

    # ---- reads ----
    def list_container(self, cid):
        items = []
        for _, args in self._rpc('/GetContainerContents', [('i', cid)]):
            for a in args:
                if isinstance(a, list):
                    items.extend(x for x in a if isinstance(x, dict))
        return items

    def list_presets(self, container=USER):
        ps = [m for m in self.list_container(container) if m.get('cctp') == CT_PRESET]
        ps.sort(key=lambda m: m.get('posi', 1 << 30))
        return ps

    def get_ref(self, cid):
        for _, args in self._rpc('/GetContentRef', [('i', cid)]):
            for a in args:
                if isinstance(a, dict):
                    return a
        return None

    # ---- writes ----
    def _ok(self, replies):
        for addr, args in replies:
            if addr == '/status' and len(args) >= 2:
                return args[1] == 0
        return False

    def load_preset(self, cid):
        return self._ok(self._rpc('/LoadPresetWithCID', [('i', cid)]))

    def copy_into(self, container, src_cids, pos):
        """CREATE: copy preset(s) by CID into `container` at slot `pos`."""
        return self._ok(self._rpc('/AddContentsToContainer',
                                   [('i', container), ('b', msgpack.packb(list(src_cids))),
                                    ('i', pos), ('i', 0), ('i', 0)]))

    def set_name(self, cid, name):
        """RENAME (a SetContentAttrs with {name})."""
        return self._ok(self._rpc('/SetContentAttrs',
                                   [('i', cid), ('b', msgpack.packb({'name': name}))]))

    def remove(self, container, cids):
        """DELETE preset(s) by CID from a container."""
        return self._ok(self._rpc('/RemoveContent',
                                   [('i', container), ('b', msgpack.packb(list(cids)))]))

    # ---- convenience ----
    def find_by_pos(self, container, pos):
        for m in self.list_container(container):
            if m.get('posi') == pos:
                return m
        return None


if __name__ == '__main__':
    import sys
    ip = sys.argv[1] if len(sys.argv) > 1 else '192.168.4.84'
    with HelixClient(ip) as h:
        for m in h.list_presets(USER):
            print('%2d  cid=%-5s %s' % (m.get('posi'), m.get('cid_'), m.get('name')))
