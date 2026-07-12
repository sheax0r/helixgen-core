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

// libssh2 SFTP file operations — reveals which device files the editor writes
// during an IR/song import (ir/ only? also db/?).
{
  const so = Module.findGlobalExportByName('libssh2_sftp_open_ex');
  if (so) Interceptor.attach(so, {
    onEnter(args) {
      try {
        const fn = args[1].readUtf8String(args[2].toInt32());
        const flags = args[3].toInt32();
        const w = (flags & 0x2) ? 'W' : '';   // LIBSSH2_FXF_WRITE
        const c = (flags & 0x8) ? 'C' : '';    // CREAT
        send({ dir: 'INFO', msg: 'SFTP_OPEN [' + w + c + ' 0x' + flags.toString(16) + '] ' + fn });
      } catch (e) {}
    }
  });
  const su = Module.findGlobalExportByName('libssh2_sftp_unlink_ex');
  if (su) Interceptor.attach(su, {
    onEnter(args) {
      try { send({ dir: 'INFO', msg: 'SFTP_UNLINK ' + args[1].readUtf8String(args[2].toInt32()) }); } catch (e) {}
    }
  });
  const sr = Module.findGlobalExportByName('libssh2_sftp_rename_ex');
  if (sr) Interceptor.attach(sr, {
    onEnter(args) {
      try { send({ dir: 'INFO', msg: 'SFTP_RENAME ' + args[1].readUtf8String(args[2].toInt32()) + ' -> ' + args[3].readUtf8String(args[4].toInt32()) }); } catch (e) {}
    }
  });
  // close / fsync / fsetstat / fstat — prime suspects for a post-write
  // registration trigger the editor uses that a plain paramiko put does not.
  const scl = Module.findGlobalExportByName('libssh2_sftp_close_handle');
  if (scl) Interceptor.attach(scl, {
    onEnter() { try { send({ dir: 'INFO', msg: 'SFTP_CLOSE handle' }); } catch (e) {} }
  });
  const sfs = Module.findGlobalExportByName('libssh2_sftp_fsync');
  if (sfs) Interceptor.attach(sfs, {
    onEnter() { try { send({ dir: 'INFO', msg: 'SFTP_FSYNC' }); } catch (e) {} }
  });
  const sst = Module.findGlobalExportByName('libssh2_sftp_fstat_ex');
  if (sst) Interceptor.attach(sst, {
    onEnter(args) { try { send({ dir: 'INFO', msg: 'SFTP_FSTAT setstat=' + args[2].toInt32() }); } catch (e) {} }
  });
  // channel exec / process startup — in case the editor runs a remote command
  // (e.g. a rescan/register helper) rather than pure SFTP.
  const cps = Module.findGlobalExportByName('libssh2_channel_process_startup');
  if (cps) Interceptor.attach(cps, {
    onEnter(args) {
      try {
        const req = args[1].readUtf8String(args[2].toInt32());
        let msg = '';
        try { msg = args[3].readUtf8String(args[4].toInt32()); } catch (e) {}
        send({ dir: 'INFO', msg: 'SSH_CHANNEL ' + req + ' :: ' + msg });
      } catch (e) {}
    }
  });
  send({ dir: 'INFO', msg: 'libssh2 SFTP hooks: open=' + !!so + ' unlink=' + !!su
        + ' close=' + !!scl + ' fsync=' + !!sfs + ' fstat=' + !!sst + ' exec=' + !!cps });
}

send({ dir: 'INFO', msg: 'OSC hooks installed for ' + DEVICE_IP + '; symbols=' + installed.join(',') });
