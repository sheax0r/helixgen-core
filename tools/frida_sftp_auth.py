#!/usr/bin/env python3
"""Reveal how the Helix Stadium 1.4 editor authenticates its SFTP (`hedit`) login.

App 1.4 dropped the bundled Contents/Resources/sshKeys/id_hedit and the device
rejects the old key, breaking helixgen's IR transfer (hgc-6r5.11). The 1.4 app
drives SFTP through libcurl; libcurl's SSH backend takes the private key as a
FILE PATH (CURLOPT_SSH_PRIVATE_KEYFILE) or uses a password — so hooking
curl_easy_setopt at connect time shows exactly which, and where the key is.

This hooks the auth-relevant curl_easy_setopt options and prints them when you
import ONE IR in the app's Cab IR browser. If it reports a PRIVATE_KEYFILE path,
copy that file immediately (the app may delete it after the transfer) and point
helixgen at it via $HELIXGEN_HELIX_SSH_KEY.

Attaching needs Frida and a debuggable copy of the app (hardened-runtime apps
reject attach; use a re-signed "Helix Stadium Debug.app" as in the 1.3.2 RE).

    pip install frida
    python3 tools/frida_sftp_auth.py            # attaches to a running editor

Prints option name + value for the auth options only. File bytes / other traffic
are never touched.
"""
import sys
import frida

# libcurl CURLOPT_* numbers for the SSH/auth options we care about.
OPTS = {
    10005: "USERPWD",
    10026: "KEYPASSWD",            # private-key passphrase
    10152: "SSH_PUBLIC_KEYFILE",
    10153: "SSH_PRIVATE_KEYFILE",  # <-- the key's file path, if key-auth
    10162: "SSH_HOST_PUBLIC_KEY_MD5",
    10173: "USERNAME",
    10174: "PASSWORD",
    151:   "SSH_AUTH_TYPES",       # int: 1=PUBLICKEY 2=PASSWORD 4=HOST 8=KBD
}

SCRIPT = r"""
var OPTS = %s;
var setopt = Module.findExportByName(null, 'curl_easy_setopt');
if (!setopt) { send({t:'info', msg:'curl_easy_setopt NOT FOUND'}); }
else {
  Interceptor.attach(setopt, {
    onEnter: function (args) {
      var opt = args[1].toInt32();
      var name = OPTS[opt];
      if (!name) return;
      var val;
      if (opt === 151) { val = '' + args[2].toInt32(); }         // int bitmask
      else { try { val = args[2].readUtf8String(); } catch (e) { val = '<ptr>'; } }
      send({t:'opt', opt: opt, name: name, val: val});
    }
  });
  send({t:'info', msg:'curl_easy_setopt hooked @' + setopt});
}
// Also catch libssh2 in-memory key auth, in case curl is bypassed.
var mem = Module.findExportByName(null, 'libssh2_userauth_publickey_frommemory');
if (mem) {
  Interceptor.attach(mem, { onEnter: function () {
    send({t:'opt', opt: 0, name: 'libssh2_frommemory', val: '<in-memory key blob>'});
  }});
}
""" % (__import__("json").dumps(OPTS),)


def main():
    try:
        dev = frida.get_local_device()
        procs = [p for p in dev.enumerate_processes()
                 if "helix" in p.name.lower() and "stadium" in p.name.lower()]
        if not procs:
            print("No running 'Helix Stadium' process. Launch the editor first.",
                  file=sys.stderr)
            return 1
        pid = procs[0].pid
        print(f"attaching to {procs[0].name} (pid {pid}) ...")
        session = dev.attach(pid)
    except Exception as exc:  # noqa: BLE001
        print(f"attach failed: {exc}\n"
              "Hardened-runtime apps reject attach — use a re-signed debuggable\n"
              "copy of the 1.4 app (as with the 1.3.2 RE).", file=sys.stderr)
        return 2

    def on_message(msg, _data):
        if msg.get("type") == "send":
            p = msg["payload"]
            if p.get("t") == "info":
                print(f"[frida] {p['msg']}")
            elif p.get("t") == "opt":
                print(f"  curl {p['name']} ({p['opt']}) = {p['val']}")

    script = session.create_script(SCRIPT)
    script.on("message", on_message)
    script.load()
    print("hooked. Now import ONE IR in the app's Cab IR browser. "
          "Ctrl-C when done.\n"
          "A SSH_PRIVATE_KEYFILE path => copy that file at once (may be a temp "
          "the app deletes); a PASSWORD/USERPWD => key-auth is gone, it's a "
          "password now.")
    try:
        sys.stdin.read()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
