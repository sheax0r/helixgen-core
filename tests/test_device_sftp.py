"""Unit tests for the device SFTP helper's key location (no paramiko / device)."""
from __future__ import annotations

import pytest

from helixgen.device import sftp
from helixgen.device.client import HelixError


def test_default_hedit_key_env_override(tmp_path, monkeypatch):
    key = tmp_path / "id_hedit"
    key.write_text("dummy")
    monkeypatch.setenv("HELIXGEN_HELIX_SSH_KEY", str(key))
    assert sftp.default_hedit_key() == str(key)


def test_default_hedit_key_env_missing_file(monkeypatch):
    monkeypatch.setenv("HELIXGEN_HELIX_SSH_KEY", "/no/such/id_hedit")
    with pytest.raises(HelixError, match="missing file"):
        sftp.default_hedit_key()


def test_default_hedit_key_not_found(monkeypatch):
    monkeypatch.delenv("HELIXGEN_HELIX_SSH_KEY", raising=False)
    monkeypatch.setattr(sftp, "_KEY_CANDIDATES", ["/definitely/not/here"])
    with pytest.raises(HelixError, match="could not find"):
        sftp.default_hedit_key()


def test_ir_dir_path():
    s = sftp.HelixSFTP("1.2.3.4", key_path="/tmp/k")
    assert s.ir_dir == "/data/stadium-family-fw/ir"


def test_module_imports_without_paramiko():
    # importing the module must not require paramiko (it's a lazy dep)
    import importlib
    importlib.reload(sftp)
    assert hasattr(sftp, "HelixSFTP") and hasattr(sftp, "push_ir")


class _FakeSFTP:
    """Records the SFTP call sequence so we can assert atomic upload order."""

    def __init__(self, posix=True):
        self.calls = []
        self._posix = posix
        if not posix:
            # simulate a server without posix-rename@openssh.com
            self.posix_rename = self._no_posix

    def _no_posix(self, *a):
        raise AttributeError("no posix_rename")

    def put(self, local, remote):
        self.calls.append(("put", local, remote))

    def posix_rename(self, src, dst):
        self.calls.append(("posix_rename", src, dst))

    def rename(self, src, dst):
        self.calls.append(("rename", src, dst))

    def remove(self, path):
        self.calls.append(("remove", path))


def _sftp_with(fake):
    s = sftp.HelixSFTP("1.2.3.4", key_path="/tmp/k")
    s._sftp = fake
    return s


def test_upload_ir_is_atomic_stage_then_rename(tmp_path):
    """The final `<name>.wav` must never be written directly — we stage to a
    temp name the device's *.wav watcher ignores, then atomically rename."""
    wav = tmp_path / "Cab 4x12.wav"
    wav.write_bytes(b"RIFFxxxx")
    fake = _FakeSFTP(posix=True)
    remote = _sftp_with(fake).upload_ir(str(wav))

    assert remote == "/data/stadium-family-fw/ir/Cab 4x12.wav"
    staging = "/data/stadium-family-fw/ir/.Cab 4x12.wav.uploading"
    # put lands on the staging path (not the final .wav), then rename into place
    assert fake.calls[0] == ("put", str(wav), staging)
    assert fake.calls[1] == ("posix_rename", staging, remote)
    # the final path is never a `put` target
    assert not any(c[0] == "put" and c[2] == remote for c in fake.calls)


def test_upload_ir_falls_back_to_plain_rename(tmp_path):
    wav = tmp_path / "Cab.wav"
    wav.write_bytes(b"RIFFxxxx")
    fake = _FakeSFTP(posix=False)
    remote = _sftp_with(fake).upload_ir(str(wav))
    staging = "/data/stadium-family-fw/ir/.Cab.wav.uploading"
    ops = [c[0] for c in fake.calls]
    assert ops[0] == "put"
    # remove-any-existing-target then plain rename
    assert ("remove", remote) in fake.calls
    assert ("rename", staging, remote) in fake.calls


def test_upload_ir_cleans_up_staging_on_failure(tmp_path):
    wav = tmp_path / "Cab.wav"
    wav.write_bytes(b"RIFFxxxx")

    class _BoomSFTP(_FakeSFTP):
        def put(self, local, remote):
            self.calls.append(("put", local, remote))
            raise IOError("connection dropped mid-transfer")

    fake = _BoomSFTP(posix=True)
    with pytest.raises(sftp.HelixError, match="upload"):
        _sftp_with(fake).upload_ir(str(wav))
    staging = "/data/stadium-family-fw/ir/.Cab.wav.uploading"
    # a failed transfer must not leave the staging turd behind
    assert ("remove", staging) in fake.calls
