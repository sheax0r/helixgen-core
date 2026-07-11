#!/usr/bin/env python3
"""Decode the Helix Stadium OSC-over-TCP framing + msgpack payloads.

Frame (observed):  <8-byte header> <OSC message>
  header: 01 08 <2 bytes ??> <4 bytes seq/len?>  -- refined empirically below
OSC message: address (NUL-padded to 4) + typetags ","... (NUL-padded to 4) + args
  args by tag:  i=int32be  f=float32be  s=string(padded)  b=blob(len32be+bytes,padded)
Blobs frequently contain msgpack maps.
"""
import struct, sys

try:
    import msgpack  # optional; we fall back to a tiny decoder note
    HAVE_MP = True
except Exception:
    HAVE_MP = False


def _pad4(n): return (n + 3) & ~3


def _pad(b):
    return b + b'\x00' * (_pad4(len(b)) - len(b) if len(b) % 4 else 0)


def _padz(s):
    """NUL-terminate then pad to 4-byte boundary."""
    b = s.encode('latin1') + b'\x00'
    while len(b) % 4:
        b += b'\x00'
    return b


def osc_encode(addr, args):
    """args: list of ('i', int) | ('b', bytes) | ('s', str) | ('f', float)."""
    out = _padz(addr)
    tags = ','
    body = b''
    for t, v in args:
        tags += t
        if t == 'i':
            body += struct.pack('>i', v)
        elif t == 'f':
            body += struct.pack('>f', v)
        elif t == 'h':
            body += struct.pack('>q', v)
        elif t == 's' or t == 'S':
            body += _padz(v)
        elif t == 'b':
            body += struct.pack('>i', len(v)) + v
            while len(body) % 4:
                body += b'\x00'
        else:
            raise ValueError('unknown tag ' + t)
    out += _padz(tags) + body
    return out


def parse_osc_message(buf, off=0):
    """Parse one OSC message starting at off. Returns (addr, args, next_off)."""
    # address
    end = buf.index(b'\x00', off)
    addr = buf[off:end].decode('latin1')
    p = _pad4(end + 1 - off) + off - (off - off)  # advance to padded boundary
    # recompute properly:
    p = off + _pad4(len(addr) + 1)
    if p >= len(buf) or buf[p:p+1] != b',':
        return addr, [], p
    tend = buf.index(b'\x00', p)
    tags = buf[p+1:tend].decode('latin1')
    # on-wire tag block = "," + tags + "\0", padded to 4 bytes
    q = p + _pad4(len(tags) + 2)
    args = []
    for t in tags:
        if t == 'i':
            args.append(('i', struct.unpack_from('>i', buf, q)[0])); q += 4
        elif t == 'f':
            args.append(('f', struct.unpack_from('>f', buf, q)[0])); q += 4
        elif t == 'h':
            args.append(('h', struct.unpack_from('>q', buf, q)[0])); q += 8
        elif t == 'd':
            args.append(('d', struct.unpack_from('>d', buf, q)[0])); q += 8
        elif t == 's' or t == 'S':
            se = buf.index(b'\x00', q); s = buf[q:se].decode('latin1')
            args.append(('s', s)); q += _pad4(len(s) + 1)
        elif t == 'b':
            blen = struct.unpack_from('>i', buf, q)[0]; q += 4
            blob = buf[q:q+blen]; q += _pad4(blen)
            args.append(('b', blob))
        elif t in 'TFN':
            args.append((t, None))
        else:
            args.append(('?' + t, None))
    return addr, args, q


def decode_blob(blob):
    # Observed: blob = <4-byte big-endian length> <msgpack map>
    body = blob
    if len(blob) >= 4:
        n = struct.unpack_from('>I', blob, 0)[0]
        if n == len(blob) - 4:
            body = blob[4:]
    if not body:
        return None
    if HAVE_MP:
        try:
            return msgpack.unpackb(body, raw=False, strict_map_key=False)
        except Exception:
            return '<msgpack? %d bytes: %r>' % (len(body), body[:60])
    return '<blob %d bytes: %r>' % (len(body), body[:60])


def try_frame(raw):
    """Best-effort: skip the binary header, find first OSC '/', parse from there."""
    i = raw.find(b'/')
    if i < 0:
        return None
    hdr = raw[:i]
    try:
        addr, args, nxt = parse_osc_message(raw, i)
    except Exception as e:
        return {'hdr': hdr.hex(), 'error': str(e), 'raw': raw[:64].hex()}
    out = {'hdr': hdr.hex(), 'addr': addr, 'args': []}
    for t, v in args:
        if t == 'b':
            out['args'].append(('blob', decode_blob(v)))
        else:
            out['args'].append((t, v))
    return out


if __name__ == '__main__':
    import json, base64
    path = sys.argv[1] if len(sys.argv) > 1 else 'captures/raw.jsonl'
    want = sys.argv[2] if len(sys.argv) > 2 else None
    seen = set()
    for line in open(path):
        r = json.loads(line)
        if want and want not in r.get('addr', ''):
            continue
        if r.get('addr') in ('/trigger', '/heartbeat', '/dspEvent') and not want:
            continue
        b = base64.b64decode(r['b64']) if r.get('b64') else b''
        key = (r['dir'].split('[')[0], r.get('addr'), r.get('len'), r['b64'][:24])
        if key in seen:
            continue
        seen.add(key)
        d = try_frame(b)
        print('%-5s %-6s %s' % (r['dir'].split('[')[0], r.get('port'), json.dumps(d, default=str)[:600]))
