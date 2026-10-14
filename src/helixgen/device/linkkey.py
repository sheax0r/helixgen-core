"""One-command local capture of the firmware 1.4 ``hedit`` SFTP key.

Firmware/app 1.4 stopped bundling the editor's ``id_hedit`` key (it used to live
at ``Contents/Resources/sshKeys/id_hedit``) and rotated it, which broke
helixgen's IR transfer (hgc-6r5.11). The 1.4 app now *writes the key to a temp
file at connect time* and hands that path to libssh2/libcurl, so the only way to
get it is to watch the app authenticate once. This module automates that capture:

1. Re-sign a copy of the installed Stadium app with the entitlements Frida needs
   (macOS notarizes the shipping app with the hardened runtime, which blocks
   attach). The copy goes to ``~/Helix Stadium Debug.app``.
2. Launch the debug copy and attach Frida, hooking every SSH auth entry point.
3. The user imports one *new* IR in the app's Cab IR browser; the app connects,
   and the hook reveals the key file (or in-memory bytes).
4. Write it to ``~/.helixgen/id_hedit`` (``sftp.default_hedit_key`` finds it
   there first), ``chmod 600``.

The captured key stays **local to this machine**. It is never committed or
distributed: public redistribution of the ``hedit`` key is DMCA-1201 / EULA
exposure (decided 2026-10-13). Each user captures their own from their own
installed, licensed copy of the app.

macOS only (codesign + Frida). ``frida`` is the ``linkkey`` extra; it is imported
lazily so the rest of the ``device`` CLI never depends on it.
"""
from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from .client import HelixError

DEST_KEY = os.path.expanduser("~/.helixgen/id_hedit")
DEBUG_APP = os.path.expanduser("~/Helix Stadium Debug.app")

# Likely install locations of the shipping editor (source for the debug copy).
_APP_CANDIDATES = [
    "/Applications/Line6/Helix Stadium.app",
    "/Applications/Helix Stadium.app",
    os.path.expanduser("~/Applications/Line6/Helix Stadium.app"),
]

# Entitlements that let Frida attach: a hardened-runtime copy refuses otherwise.
_ENTITLEMENTS = {
    "com.apple.security.get-task-allow": True,
    "com.apple.security.cs.disable-library-validation": True,
    "com.apple.security.cs.allow-unsigned-executable-memory": True,
    "com.apple.security.cs.allow-dyld-environment-variables": True,
}

# Frida JS: hook every SSH auth entry point. Proven in tools/frida_sftp_auth.py.
_SCRIPT = r"""
var landed = [];
function findExp(name) {
  // Frida 17 removed Module.findExportByName(null, ...); use the new API first.
  if (typeof Module.findGlobalExportByName === 'function')
    return Module.findGlobalExportByName(name);
  if (typeof Module.findExportByName === 'function')
    return Module.findExportByName(null, name);
  return null;
}
function hook(name, fn) {
  var a = findExp(name);
  if (!a) return;
  Interceptor.attach(a, fn);
  landed.push(name);
}

// libssh2_userauth_publickey_fromfile_ex(session, user, ulen, pubpath, privpath, passphrase)
hook('libssh2_userauth_publickey_fromfile_ex', { onEnter: function (args) {
  var priv = null, pass = null;
  try { priv = args[4].readUtf8String(); } catch (e) {}
  try { pass = args[5].isNull() ? null : args[5].readUtf8String(); } catch (e) {}
  send({t:'keyfile', path: priv, passphrase: pass});
}});

// libssh2_userauth_publickey_frommemory(session, user, ulen, pub, publen, priv, privlen, passphrase)
hook('libssh2_userauth_publickey_frommemory', { onEnter: function (args) {
  var len = args[6].toInt32();
  var pass = null;
  try { pass = args[7].isNull() ? null : args[7].readUtf8String(); } catch (e) {}
  var bytes = null;
  try { bytes = args[5].readByteArray(len); } catch (e) {}
  send({t:'keymem', len: len, passphrase: pass}, bytes);
}});

// libssh2_userauth_password_ex(session, user, ulen, password, plen, cb)
hook('libssh2_userauth_password_ex', { onEnter: function (args) {
  var pw = null;
  try { pw = args[3].readUtf8String(args[4].toInt32()); } catch (e) {}
  send({t:'password', value: pw});
}});

// curl_easy_setopt(curl, opt, value) — SSH auth options, if SFTP goes via curl
var CURLOPT = {10153:'SSH_PRIVATE_KEYFILE', 10026:'KEYPASSWD',
               10005:'USERPWD', 10174:'PASSWORD', 10173:'USERNAME'};
hook('curl_easy_setopt', { onEnter: function (args) {
  var o = args[1].toInt32(); var n = CURLOPT[o]; if (!n) return;
  var v = null; try { v = args[2].readUtf8String(); } catch (e) {}
  send({t:'curl', name: n, value: v});
}});

send({t:'landed', hooks: landed});
"""


def find_app(explicit: Optional[str] = None) -> str:
    """Return the path to the installed Helix Stadium editor, or raise."""
    if explicit:
        if os.path.isdir(explicit):
            return explicit
        raise HelixError(f"no Helix Stadium app at: {explicit}")
    for cand in _APP_CANDIDATES:
        if os.path.isdir(cand):
            return cand
    raise HelixError(
        "could not find the Helix Stadium editor. Install it, or pass "
        "--app /path/to/Helix\\ Stadium.app.")


def app_version(app: str) -> Optional[str]:
    """Read CFBundleShortVersionString from an app bundle's Info.plist."""
    info = Path(app) / "Contents" / "Info.plist"
    try:
        with open(info, "rb") as fh:
            return plistlib.load(fh).get("CFBundleShortVersionString")
    except Exception:  # noqa: BLE001 - version is advisory only
        return None


def _require_frida():
    try:
        import frida  # noqa: F401
    except ImportError as exc:
        raise HelixError(
            "device link-key needs Frida. Install it with:\n"
            "    uv tool install --force 'helixgen[device,linkkey]'\n"
            "or: pip install frida") from exc
    import frida
    return frida


def resign_debug_app(src_app: str, dest_app: str = DEBUG_APP) -> str:
    """Copy ``src_app`` to ``dest_app`` and ad-hoc re-sign it debuggable.

    Mirrors tools/resign-debug-app.sh: re-signs nested dylibs/frameworks first,
    then the bundle with the Frida entitlements + ``--options=runtime``. Returns
    ``dest_app``. Raises :class:`HelixError` if codesign is missing or the
    get-task-allow entitlement does not stick.
    """
    if shutil.which("codesign") is None:
        raise HelixError("codesign not found — device link-key is macOS-only.")

    shutil.rmtree(dest_app, ignore_errors=True)
    shutil.copytree(src_app, dest_app, symlinks=True)

    with tempfile.NamedTemporaryFile(
            suffix=".plist", delete=False, mode="wb") as ent_fh:
        plistlib.dump(_ENTITLEMENTS, ent_fh)
        ent_path = ent_fh.name

    try:
        frameworks = Path(dest_app) / "Contents" / "Frameworks"
        if frameworks.is_dir():
            for item in frameworks.iterdir():
                if item.suffix in (".dylib", ".framework"):
                    subprocess.run(
                        ["codesign", "--force", "--sign", "-",
                         "--timestamp=none", str(item)],
                        check=False, capture_output=True)
        res = subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", "--timestamp=none",
             "--entitlements", ent_path, "--options=runtime", dest_app],
            check=False, capture_output=True, text=True)
        if res.returncode != 0:
            raise HelixError(f"codesign failed: {res.stderr.strip()}")
    finally:
        os.unlink(ent_path)

    check = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", dest_app],
        check=False, capture_output=True, text=True)
    if "get-task-allow" not in (check.stdout + check.stderr):
        raise HelixError(
            "re-signed app is missing get-task-allow — Frida cannot attach. "
            "Your macOS may enforce System Integrity Protection on this app; "
            "capture the key manually with tools/frida_sftp_auth.py.")
    return dest_app


_KEY_MARKERS = (b"-----BEGIN", b"openssh-key-v1", b"PuTTY-User-Key")


def _looks_like_private_key(data: bytes) -> bool:
    head = data.lstrip()[:64]
    return any(head.startswith(m) or m in data[:64] for m in _KEY_MARKERS)


def _store_key(dest: str, *, data: Optional[bytes] = None,
               from_path: Optional[str] = None) -> bool:
    """Write the captured key to ``dest`` with 0600 perms. Returns True on write.

    Exactly one of ``data`` (in-memory bytes) or ``from_path`` (a key file the
    app wrote) is used. Returns False — so the caller keeps waiting — when the
    temp file has already vanished (the app deletes it fast) or the bytes do not
    look like a private key (a stray non-key path on the auth hook).
    """
    if from_path is not None:
        try:
            with open(from_path, "rb") as fh:
                data = fh.read()
        except OSError:
            return False  # vanished between hook and read, or unreadable
    if not data or not _looks_like_private_key(data):
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # Create 0600 up front so there is never a world-readable window; O_TRUNC
    # replaces any prior file (and its mode) atomically on the open.
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(dest, 0o600)  # belt-and-suspenders if the file pre-existed
    return True


def _debug_pids(frida, debug_app: str) -> set:
    """PIDs whose executable lives inside ``debug_app`` (the re-signed copy).

    Matching by bundle path, not name, so a helper/XPC child or the user's own
    /Applications copy is never mistaken for the main debug process. Falls back
    to a name match if the device won't report process paths.
    """
    dev = frida.get_local_device()
    try:
        procs = dev.enumerate_processes(scope="full")
        hits = {p.pid for p in procs
                if (p.parameters or {}).get("path", "").startswith(debug_app)}
        if hits:
            return hits
    except Exception:  # noqa: BLE001 - scope="full" unsupported/denied
        pass
    return {p.pid for p in dev.enumerate_processes()
            if "helix" in p.name.lower() and "stadium" in p.name.lower()}


def capture(
    app: Optional[str] = None,
    dest: str = DEST_KEY,
    timeout: float = 180.0,
    keep_debug_app: bool = False,
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """Capture the ``hedit`` key end to end and write it to ``dest``.

    Returns a result dict ``{ok, dest, source, mode, hooks, fingerprint,
    passphrase_set, version}`` — the key itself and any passphrase are NEVER in
    the result (only whether a passphrase was present). ``mode`` is ``"keyfile"``
    or ``"keymem"``. Raises
    :class:`HelixError` on any setup failure; a *timeout* (user never imported an
    IR, or the app used an auth path we don't capture) returns ``ok=False``.

    ``log`` receives human-readable progress lines (the CLI passes ``click.echo``).
    """
    emit = log or (lambda _m: None)
    if platform.system() != "Darwin":
        raise HelixError("device link-key is macOS-only (needs codesign + Frida).")

    frida = _require_frida()
    src_app = find_app(app)
    ver = app_version(src_app)
    emit(f"source app: {src_app}" + (f" (v{ver})" if ver else ""))

    emit("re-signing a debuggable copy (needs your login keychain; may prompt)…")
    debug_app = resign_debug_app(src_app)
    emit(f"debug copy: {debug_app}")

    before = _debug_pids(frida, debug_app)  # stale instance from a prior run
    emit("launching the debug copy…")
    if subprocess.run(["open", "-n", debug_app], check=False).returncode != 0:
        raise HelixError(f"could not launch {debug_app}.")

    # Wait for the freshly launched instance to appear (a pid we didn't see).
    pid = None
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        new = _debug_pids(frida, debug_app) - before
        if new:
            pid = min(new)
            break
        time.sleep(0.5)
    if pid is None:
        if not keep_debug_app:
            shutil.rmtree(debug_app, ignore_errors=True)
        raise HelixError(
            "the debug app did not start. Launch it yourself with "
            f"`open -n '{debug_app}'` and retry, or capture manually with "
            "tools/frida_sftp_auth.py.")

    result: dict = {"ok": False, "dest": dest, "source": src_app,
                    "mode": None, "hooks": [], "fingerprint": None,
                    "passphrase_set": False, "version": ver}

    def on_message(msg, data):
        if msg.get("type") != "send" or result["ok"]:
            return  # ignore later auth attempts once we have the key
        p = msg["payload"]
        t = p.get("t")
        if t == "landed":
            result["hooks"] = p.get("hooks") or []
        elif t == "keyfile" and p.get("path"):
            if _store_key(dest, from_path=p["path"]):
                result.update(ok=True, mode="keyfile",
                              passphrase_set=bool(p.get("passphrase")))
        elif t == "keymem" and data:
            if _store_key(dest, data=bytes(data)):
                result.update(ok=True, mode="keymem",
                              passphrase_set=bool(p.get("passphrase")))

    session = script = None
    try:
        try:
            session = frida.get_local_device().attach(pid)
            script = session.create_script(_SCRIPT)
            script.on("message", on_message)
            script.load()
        except Exception as exc:  # noqa: BLE001 - any frida error -> clean degrade
            raise HelixError(f"Frida could not attach to the debug app: {exc}. "
                             "Retry, or capture manually with "
                             "tools/frida_sftp_auth.py.") from exc

        # script.load() returns before the async `landed` message arrives, and
        # the app may still be loading libssh2 — give the hooks a moment.
        hook_deadline = time.monotonic() + 10.0
        while not result["hooks"] and time.monotonic() < hook_deadline:
            time.sleep(0.2)
        if not result["hooks"]:
            raise HelixError(
                "no SSH auth hooks attached — the app is not debuggable "
                "(hardened runtime). This usually means the re-sign did not "
                "take; retry, or capture manually with tools/frida_sftp_auth.py.")
        emit(f"attached to pid {pid}; hooks: {', '.join(result['hooks'])}")
        emit("")
        emit(">>> In the Helix Stadium window that just opened, import ONE NEW IR")
        emit("    (Cab IR browser → Import) that the device does NOT already have.")
        emit("    The import makes the app connect and reveals the key.")
        emit("")

        deadline = time.monotonic() + timeout
        while not result["ok"] and time.monotonic() < deadline:
            time.sleep(0.25)
    finally:
        for teardown in (
                (script.unload if script else None),
                (session.detach if session else None)):
            if teardown:
                try:
                    teardown()
                except Exception:  # noqa: BLE001 - teardown best-effort
                    pass
        if not keep_debug_app:
            try:
                os.kill(pid, 15)  # stop the process before deleting its bundle
            except (ProcessLookupError, PermissionError):
                pass
            shutil.rmtree(debug_app, ignore_errors=True)

    if result["ok"]:
        result["fingerprint"] = _fingerprint(dest)
    return result


def _fingerprint(key_path: str) -> Optional[str]:
    """SHA256 fingerprint of the key (via ssh-keygen), for confirmation only."""
    if shutil.which("ssh-keygen") is None:
        return None
    res = subprocess.run(
        ["ssh-keygen", "-lf", key_path],
        check=False, capture_output=True, text=True)
    out = (res.stdout or "").strip()
    return out or None


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    r = capture(log=lambda m: print(m, file=sys.stderr))
    print(r)
    sys.exit(0 if r["ok"] else 1)
