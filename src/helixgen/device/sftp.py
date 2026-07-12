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


def push_ir(ip: str, local_wav: str, *, key_path: Optional[str] = None,
            user: str = DEFAULT_USER, wait_timeout: float = 25.0) -> dict:
    """Import an IR onto the device the way the editor does.

    The editor does **not** upload the raw WAV — it uploads the *processed*
    IR (source truncated to the Stadium's 8192-sample form + fade), and the
    device registers it by MD5-ing that file's data chunk. That MD5 **is**
    helixgen's ``irhash``. So we render the same processed file
    (:func:`helixgen.ir.write_stadium_ir`) and SFTP it to ``ir/<stem>.wav``;
    the device then registers it under exactly the hash a preset references.

    Registration is confirmed via ``/IrPathForHashGet`` (the reliable check —
    the ``/GetContainerContents`` listing lags). Returns
    ``{ok, name, helixgen_hash, device_path, registered, remote, already}``.
    """
    import tempfile
    import time
    from helixgen.ir import write_stadium_ir
    from .client import HelixClient

    stem = Path(local_wav).stem
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        # render the device-canonical processed IR; its data-chunk MD5 == irhash
        hg_hash = write_stadium_ir(local_wav, tmp.name)

        with HelixClient(ip) as h:
            if h.ir_path_for_hash(hg_hash):
                return {"ok": True, "name": stem, "helixgen_hash": hg_hash,
                        "device_path": h.ir_path_for_hash(hg_hash),
                        "registered": True, "remote": None, "already": True}

        with HelixSFTP(ip, key_path=key_path, user=user) as s:
            remote = s.upload_ir(tmp.name, remote_name=f"{stem}.wav")
            on_disk = s.ir_file_exists(f"{stem}.wav")

        with HelixClient(ip) as h:
            deadline = time.time() + wait_timeout
            while time.time() < deadline:
                path = h.ir_path_for_hash(hg_hash)
                if path:
                    return {"ok": True, "name": stem, "helixgen_hash": hg_hash,
                            "device_path": path, "registered": True,
                            "remote": remote, "already": False}
                time.sleep(1.5)
        # uploaded but not yet registered — the device registers on its scan
        return {"ok": bool(on_disk), "name": stem, "helixgen_hash": hg_hash,
                "device_path": None, "registered": False, "remote": remote,
                "already": False,
                "note": "uploaded; not yet registered — open the editor's IR "
                        "librarian or retry `device list-irs` shortly"}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
