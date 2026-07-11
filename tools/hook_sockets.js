'use strict';
// Frida hook: capture Helix Stadium <-> device OSC traffic (cleartext TCP).
// Parses the OSC address, filters the chatty /dspEvent + /heartbeat streams,
// and forwards full payloads (as raw data) for offline decoding.

const DEVICE_IP = '192.168.4.84';
const NOISE = ['/dspEvent', '/heartbeat', '/meter', '/vu'];  // suppressed unless SEND

const getpeername = new NativeFunction(
  Module.getGlobalExportByName('getpeername'), 'int', ['int', 'pointer', 'pointer']);

function peerForFd(fd) {
  try {
    const addr = Memory.alloc(128);
    const len = Memory.alloc(4); len.writeU32(128);
    if (getpeername(fd, addr, len) !== 0) return null;
    if (addr.add(1).readU8() !== 2) return null; // AF_INET
    const port = (addr.add(2).readU8() << 8) | addr.add(3).readU8();
    const ip = [4, 5, 6, 7].map(o => addr.add(o).readU8()).join('.');
    return { ip, port, s: ip + ':' + port };
  } catch (e) { return null; }
}

// Find the first OSC address ("/....\0") in the frame, skipping the binary header.
function oscAddr(bytes) {
  for (let i = 0; i < bytes.length && i < 32; i++) {
    if (bytes[i] === 0x2f) { // '/'
      let j = i;
      while (j < bytes.length && bytes[j] !== 0x00) j++;
      let s = '';
      for (let k = i; k < j; k++) s += String.fromCharCode(bytes[k]);
      if (/^[\x20-\x7e]+$/.test(s)) return s;
    }
  }
  return '?';
}

function forward(dir, peer, buf, len) {
  const n = Math.min(len, 8192);
  const raw = buf.readByteArray(n);
  const addr = oscAddr(new Uint8Array(raw));
  const isNoise = NOISE.indexOf(addr) !== -1;
  if (dir === 'RECV' && isNoise) return;           // suppress inbound telemetry
  send({ dir, peer: peer.s, port: peer.port, len, addr }, raw);
}

const installed = [];

// fd + linear (buf,len) at fixed arg indices: send/sendto/write and NOCANCEL variants.
function hookBufSend(name, bufIdx, lenIdx) {
  const p = Module.findGlobalExportByName(name); if (!p) return;
  Interceptor.attach(p, {
    onEnter(args) {
      const peer = peerForFd(args[0].toInt32());
      if (!peer || peer.ip !== DEVICE_IP) return;
      forward('SEND[' + name + ']', peer, args[bufIdx], args[lenIdx].toInt32());
    }
  });
  installed.push(name);
}
function hookBufRecv(name, bufIdx) {
  const p = Module.findGlobalExportByName(name); if (!p) return;
  Interceptor.attach(p, {
    onEnter(args) { this.fd = args[0].toInt32(); this.buf = args[bufIdx]; },
    onLeave(ret) {
      const n = ret.toInt32(); if (n <= 0) return;
      const peer = peerForFd(this.fd);
      if (!peer || peer.ip !== DEVICE_IP) return;
      forward('RECV[' + name + ']', peer, this.buf, n);
    }
  });
  installed.push(name);
}
// scatter-gather: writev / sendmsg (msghdr.msg_iov@16, msg_iovlen@24 on 64-bit macOS)
function hookIovSend(name, iovArg, cntArg, viaMsghdr) {
  const p = Module.findGlobalExportByName(name); if (!p) return;
  Interceptor.attach(p, {
    onEnter(args) {
      const peer = peerForFd(args[0].toInt32());
      if (!peer || peer.ip !== DEVICE_IP) return;
      let iov, cnt;
      if (viaMsghdr) { const m = args[1]; iov = m.add(16).readPointer(); cnt = m.add(24).readInt(); }
      else { iov = args[iovArg]; cnt = args[cntArg].toInt32(); }
      for (let i = 0; i < cnt && i < 32; i++) {
        const base = iov.add(i * 16).readPointer();
        const len = iov.add(i * 16 + 8).readU64().toNumber();
        if (len > 0) forward('SEND[' + name + ']', peer, base, len);
      }
    }
  });
  installed.push(name);
}

['send', 'send$NOCANCEL', 'write', 'write$NOCANCEL'].forEach(n => hookBufSend(n, 1, 2));
['sendto', 'sendto$NOCANCEL'].forEach(n => hookBufSend(n, 1, 2));
['recv', 'recv$NOCANCEL', 'recvfrom', 'read', 'read$NOCANCEL'].forEach(n => hookBufRecv(n, 1));
['writev', 'writev$NOCANCEL'].forEach(n => hookIovSend(n, 1, 2, false));
['sendmsg', 'sendmsg$NOCANCEL'].forEach(n => hookIovSend(n, 0, 0, true));

send({ dir: 'INFO', msg: 'OSC hooks installed for ' + DEVICE_IP + '; symbols=' + installed.join(',') });
