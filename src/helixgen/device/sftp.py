"""SFTP file transfer to a Helix Stadium (impulse-response upload/download).

The Stadium stores IR `.wav` files under ``/data/stadium-family-fw/ir/`` and
auto-registers new files it sees there (it watches the directory, computes the
hash, writes its own db rows, and broadcasts ``/addContent``). So **uploading an
IR is just an SFTP put into ``ir/``** — we never touch the device's database.

The editor authenticates as the ``hedit`` user with an RSA key it bundles as
``id_hedit``. helixgen does **not** ship that key (it's a credential); it locates
the key from your installed Helix Stadium editor at runtime, or from
``$HELIXGEN_HELIX_SSH_KEY``.

Pure-Python: uses ``paramiko`` (+ ``cryptography``), both wheel-distributed — no
system SSH binary needed. Part of the ``device`` extra; imported lazily.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from .client import HelixError

DEFAULT_USER = "hedit"
DEFAULT_REMOTE_ROOT = "/data/stadium-family-fw"
IR_SUBDIR = "ir"

# Where the editor's id_hedit key lives, in likely install locations.
_KEY_CANDIDATES = [
    "/Applications/Line6/Helix Stadium.app/Contents/Resources/sshKeys/id_hedit",
    "/Applications/Helix Stadium.app/Contents/Resources/sshKeys/id_hedit",
    os.path.expanduser("~/Applications/Line6/Helix Stadium.app/Contents/Resources/sshKeys/id_hedit"),
    os.path.expanduser("~/Helix Stadium Debug.app/Contents/Resources/sshKeys/id_hedit"),
]


def default_hedit_key() -> str:
    """Locate the editor's ``id_hedit`` private key. Honors
    ``$HELIXGEN_HELIX_SSH_KEY``. Raises :class:`HelixError` if none is found."""
    env = os.environ.get("HELIXGEN_HELIX_SSH_KEY")
    if env:
        if os.path.exists(env):
            return env
        raise HelixError(f"$HELIXGEN_HELIX_SSH_KEY points at a missing file: {env}")
    for cand in _KEY_CANDIDATES:
        if os.path.exists(cand):
            return cand
    raise HelixError(
        "could not find the Helix editor's SFTP key (id_hedit). Install the "
        "Helix Stadium editor, or set $HELIXGEN_HELIX_SSH_KEY to its "
        "Contents/Resources/sshKeys/id_hedit path.")


def _paramiko():
    try:
        import paramiko
    except ImportError as exc:
        raise HelixError(
            "IR file transfer needs paramiko; install with "
            "`pip install 'helixgen[device]'`") from exc
    return paramiko


def _load_key(path: str):
    """Load an SSH private key, handling the PKCS#8 (`BEGIN PRIVATE KEY`) form
    the editor ships (paramiko's typed loaders want traditional PEM)."""
    paramiko = _paramiko()
    try:
        return paramiko.PKey.from_path(path)  # paramiko >= 3.4 generic loader
    except Exception:
        pass
    for kt in ("RSAKey", "Ed25519Key", "ECDSAKey"):
        try:
            return getattr(paramiko, kt).from_private_key_file(path)
        except Exception:
            continue
    # cryptography fallback: re-serialize PKCS#8 -> traditional PEM
    from io import StringIO
    from cryptography.hazmat.primitives import serialization
    with open(path, "rb") as f:
        pk = serialization.load_pem_private_key(f.read(), password=None)
    pem = pk.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return paramiko.RSAKey.from_private_key(StringIO(pem))


class HelixSFTP:
    """SFTP session to a Helix Stadium (context manager)."""

    def __init__(self, ip: str, *, key_path: Optional[str] = None,
                 user: str = DEFAULT_USER, port: int = 22,
                 remote_root: str = DEFAULT_REMOTE_ROOT):
        self.ip = ip
        self.port = port
        self.user = user
        self.remote_root = remote_root.rstrip("/")
        self.key_path = key_path or default_hedit_key()
        self._t = None
        self._sftp = None

    @property
    def ir_dir(self) -> str:
        return f"{self.remote_root}/{IR_SUBDIR}"

    def connect(self) -> "HelixSFTP":
        paramiko = _paramiko()
        key = _load_key(self.key_path)
        try:
            self._t = paramiko.Transport((self.ip, self.port))
            self._t.connect(username=self.user, pkey=key)
            self._sftp = paramiko.SFTPClient.from_transport(self._t)
        except Exception as exc:  # paramiko.SSHException, socket errors, …
            self.close()
            raise HelixError(f"SFTP connect to {self.user}@{self.ip} failed: {exc}") from exc
        return self

    def close(self) -> None:
        if self._sftp is not None:
            try: self._sftp.close()
            except Exception: pass
            self._sftp = None
        if self._t is not None:
            try: self._t.close()
            except Exception: pass
            self._t = None

    def __enter__(self) -> "HelixSFTP":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- read (safe) -------------------------------------------------------
    def list_ir_files(self) -> List[str]:
        """Basenames of the `.wav` files in the device's IR directory."""
        try:
            return sorted(f for f in self._sftp.listdir(self.ir_dir)
                          if f.lower().endswith(".wav"))
        except Exception as exc:
            raise HelixError(f"listing {self.ir_dir} failed: {exc}") from exc

    def download_ir(self, remote_name: str, local_path: str) -> str:
        """Download one IR `.wav` (by basename) to ``local_path``."""
        remote = f"{self.ir_dir}/{remote_name}"
        try:
            self._sftp.get(remote, local_path)
        except Exception as exc:
            raise HelixError(f"download {remote} failed: {exc}") from exc
        return local_path

    def ir_file_exists(self, remote_name: str) -> bool:
        try:
            self._sftp.stat(f"{self.ir_dir}/{remote_name}")
            return True
        except IOError:
            return False

    # -- write (device filesystem — the device auto-registers the file) ----
    def upload_ir(self, local_path: str, *, remote_name: Optional[str] = None) -> str:
        """Upload a local `.wav` straight into the device's IR directory.

        Mirrors the editor's import exactly: a direct ``open(write|create|trunc)``
        write to ``ir/<name>.wav``. (The editor writes directly, not via a
        temp+rename — a rename lands as ``IN_MOVED_TO`` which does **not** trigger
        the device's registration; a direct create does.) **Caller must upload
        the device-canonical *processed* IR** (``helixgen.ir.write_stadium_ir``),
        not a raw source WAV — the device registers an IR by MD5-ing the file's
        data chunk, and only the processed 8192-sample file hashes to the
        ``irhash`` a preset references. See :func:`push_ir`.

        Returns the remote path. **This writes to the device filesystem.**
        """
        local = Path(local_path)
        if not local.is_file():
            raise HelixError(f"no such IR file: {local_path}")
        name = remote_name or local.name
        remote = f"{self.ir_dir}/{name}"
        try:
            self._sftp.put(str(local), remote)
        except Exception as exc:
            raise HelixError(f"upload {local} -> {remote} failed: {exc}") from exc
        return remote


def _addcontent_hash(args: list) -> Optional[str]:
    """Pull the 32-hex IR hash out of an ``/addContent`` event's decoded args
    (the payload is a msgpack dict carrying a 16-byte ``hash``)."""
    for a in args:
        if isinstance(a, dict) and "hash" in a:
            h = a["hash"]
            if isinstance(h, (bytes, bytearray)) and len(h) == 16:
                return h.hex()
            if isinstance(h, str) and len(h) == 32:
                return h
    return None


def push_ir(ip: str, local_wav: str, *, key_path: Optional[str] = None,
            user: str = DEFAULT_USER, wait_timeout: float = 20.0) -> dict:
    """Import an IR onto the device — **instantly**.

    The device only registers a new IR file promptly while a client is
    subscribed to its 2001 change stream (that's what activates its watched-dir
    monitor; without a subscriber, an external upload waits on the device's slow
    ~15-20 min scan). So we open a :class:`HelixSubscriber` on 2001 **first**,
    then SFTP the IR into ``ir/`` and wait for the device's ``/addContent``
    broadcast — which lands in ~0.1-1 s and carries the hash the device
    registered the IR under.

    We upload the device-canonical processed IR (``helixgen.ir.write_stadium_ir``),
    which embeds a ``HASH`` chunk holding helixgen's ``irhash`` — exactly as the
    editor's own upload does. The device reads that chunk and registers the IR
    under **helixgen's hash** (rather than recomputing a different one), so the
    preset that references the ``irhash`` resolves. ``hash_match`` confirms it.
    Returns ``{ok, name, helixgen_hash, device_hash, hash_match, registered, cid,
    device_path, remote, already}``.
    """
    import tempfile
    import time
    from helixgen.ir import write_stadium_ir
    from .client import HelixClient
    from .subscribe import HelixSubscriber

    stem = Path(local_wav).stem
    fname = f"{stem}.wav"
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        hg_hash = write_stadium_ir(local_wav, tmp.name)

        with HelixClient(ip) as h:
            already = h.ir_path_for_hash(hg_hash)
            if already:
                return {"ok": True, "name": stem, "helixgen_hash": hg_hash,
                        "device_hash": hg_hash, "hash_match": True,
                        "registered": True, "cid": None, "device_path": already,
                        "remote": None, "already": True}

        # Subscribe to 2001 FIRST — this activates the device's watched-dir
        # monitor so it registers our upload immediately (the delay fix).
        with HelixSubscriber(ip, ports=(2001,)) as sub:
            time.sleep(0.6)  # let the SUB subscription reach the device
            with HelixSFTP(ip, key_path=key_path, user=user) as s:
                remote = s.upload_ir(tmp.name, remote_name=fname)
                on_disk = s.ir_file_exists(fname)

            saw_our_file = False
            dev_hash = None
            deadline = time.time() + wait_timeout
            while time.time() < deadline and dev_hash is None:
                for ev in sub.poll(0.5):
                    if ev.addr == "/observeWatchedDirChange" and \
                            any(fname in str(a) for a in ev.args):
                        saw_our_file = True
                    elif ev.addr == "/addContent":
                        hh = _addcontent_hash(ev.args)
                        if hh is not None:
                            dev_hash = hh  # our upload is the only dir change
            if dev_hash is not None:
                with HelixClient(ip) as h:
                    path = h.ir_path_for_hash(dev_hash)
                return {"ok": True, "name": stem, "helixgen_hash": hg_hash,
                        "device_hash": dev_hash,
                        "hash_match": dev_hash == hg_hash,
                        "registered": True, "cid": None,
                        "device_path": path, "remote": remote, "already": False,
                        "saw_watched_change": saw_our_file}

        # No /addContent within the window — fell back to the slow path.
        return {"ok": bool(on_disk), "name": stem, "helixgen_hash": hg_hash,
                "device_hash": None, "hash_match": None, "registered": False,
                "cid": None, "device_path": None, "remote": remote,
                "already": False,
                "note": "uploaded; the device did not register it promptly — it "
                        "will on its own scan (~15-20 min), or re-import via the "
                        "editor. (Expected instant registration via the 2001 "
                        "subscription; the device may not have been watching.)"}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
