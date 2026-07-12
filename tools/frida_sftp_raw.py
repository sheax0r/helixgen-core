#!/usr/bin/env python3
"""Hook libssh2_channel_write_ex on the running Helix editor to capture the RAW
SFTP wire protocol (before encryption) — reveals any SFTP EXTENDED / FSETSTAT /
non-standard request the API-level hooks (open/close/rename) can't see.

WRITE (type 6) data packets are skipped in the hook (file bytes = noise); every
other SFTP packet is parsed here for its type, request id, and — for EXTENDED —
the extension name (which is how a vendor would add a "register this IR" verb).
"""
import sys, time, struct, base64
import frida

SFTP = {1:"INIT",2:"VERSION",3:"OPEN",4:"CLOSE",5:"READ",6:"WRITE",7:"LSTAT",
        8:"FSTAT",9:"SETSTAT",10:"FSETSTAT",11:"OPENDIR",12:"READDIR",13:"REMOVE",
        14:"MKDIR",15:"RMDIR",16:"REALPATH",17:"STAT",18:"RENAME",19:"READLINK",
        20:"SYMLINK",21:"LINK",22:"BLOCK",23:"UNBLOCK",101:"STATUS",102:"HANDLE",
        103:"DATA",104:"NAME",105:"ATTRS",200:"EXTENDED",201:"EXTENDED_REPLY"}

SCRIPT = r"""
// The editor's SFTP layer uses the INTERNAL _libssh2_channel_write, not the
// public libssh2_channel_write_ex. Resolve it from libssh2's symbol table.
var target = null;
Process.enumerateModules().forEach(function(m){
  if (!/libssh2/i.test(m.name)) return;
  try {
    m.enumerateSymbols().forEach(function(s){
      if (s.name === '_libssh2_channel_write') target = s.address;
    });
  } catch(e){}
});
if (target) {
  Interceptor.attach(target, {
    onEnter: function(args) {
      try {
        // _libssh2_channel_write(channel, stream_id, buf, buflen)
        var buf = args[2];
        var len = args[3].toInt32();
        if (len < 5) return;
        var type = buf.add(4).readU8();      // SFTP packet type byte
        if (type === 6) return;              // WRITE data = noise, skip
        var n = len < 200 ? len : 200;
        send({t:'pkt', type:type, len:len}, buf.readByteArray(n));
      } catch (e) {}
    }
  });
  send({t:'info', msg:'_libssh2_channel_write hooked @' + target});
} else {
  send({t:'info', msg:'_libssh2_channel_write NOT FOUND'});
}
"""

def parse(type_, data):
    """Return a human string for a captured SFTP request packet."""
    try:
        # data = uint32 length + byte type + payload
        payload = data[5:]
        if type_ in (3,):  # OPEN: uint32 reqid, string filename
            reqid = struct.unpack(">I", payload[:4])[0]
            slen = struct.unpack(">I", payload[4:8])[0]
            name = payload[8:8+slen].decode("utf-8","replace")
            return f"reqid={reqid} path={name!r}"
        if type_ == 200:  # EXTENDED: uint32 reqid, string extension-name, ...
            reqid = struct.unpack(">I", payload[:4])[0]
            slen = struct.unpack(">I", payload[4:8])[0]
            ext = payload[8:8+slen].decode("utf-8","replace")
            rest = payload[8+slen:8+slen+64]
            return f"reqid={reqid} EXTENSION={ext!r} rest={rest!r}"
        if type_ in (4,5,8,10):  # CLOSE/READ/FSTAT/FSETSTAT: reqid, handle
            reqid = struct.unpack(">I", payload[:4])[0]
            return f"reqid={reqid} handle+attrs={payload[4:20].hex()}"
        if type_ in (7,9,17,13,16):  # path-based
            reqid = struct.unpack(">I", payload[:4])[0]
            slen = struct.unpack(">I", payload[4:8])[0]
            name = payload[8:8+slen].decode("utf-8","replace")
            return f"reqid={reqid} path={name!r}"
        reqid = struct.unpack(">I", payload[:4])[0] if len(payload)>=4 else "?"
        return f"reqid={reqid} raw={payload[:48].hex()}"
    except Exception as e:
        return f"(parse err {e}) {data[:32].hex()}"

def main():
    pid = next((p.pid for p in frida.get_local_device().enumerate_processes()
                if p.name.startswith("Helix")), None)
    if not pid:
        raise SystemExit("Helix not running")
    print(f"{time.time():.3f} ATTACH pid={pid}", flush=True)
    session = frida.attach(pid)
    script = session.create_script(SCRIPT)
    def on_msg(m, data):
        if m["type"] == "error":
            print("ERROR", m); return
        p = m["payload"]
        if p.get("t") == "info":
            print(f"{time.time():.3f} INFO {p['msg']}", flush=True); return
        if p.get("t") == "pkt":
            t = p["type"]; name = SFTP.get(t, f"?{t}")
            print(f"{time.time():.3f} SFTP {name:14} len={p['len']:6}  {parse(t, data)}", flush=True)
    script.on("message", on_msg)
    script.load()
    print(f"{time.time():.3f} capturing (Ctrl-C to stop)", flush=True)
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
