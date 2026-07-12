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
    """Records the SFTP call sequence."""

    def __init__(self):
        self.calls = []

    def put(self, local, remote):
        self.calls.append(("put", local, remote))


def _sftp_with(fake):
    s = sftp.HelixSFTP("1.2.3.4", key_path="/tmp/k")
    s._sftp = fake
    return s


def test_upload_ir_direct_write_to_final_path(tmp_path):
    """upload_ir mirrors the editor: a direct put straight to ir/<name>.wav
    (a rename lands as IN_MOVED_TO and does not trigger device registration)."""
    wav = tmp_path / "Cab 4x12.wav"
    wav.write_bytes(b"RIFFxxxx")
    fake = _FakeSFTP()
    remote = _sftp_with(fake).upload_ir(str(wav))
    assert remote == "/data/stadium-family-fw/ir/Cab 4x12.wav"
    assert fake.calls == [("put", str(wav), remote)]


def test_upload_ir_honors_remote_name(tmp_path):
    wav = tmp_path / "processed_tmp.wav"
    wav.write_bytes(b"RIFFxxxx")
    fake = _FakeSFTP()
    remote = _sftp_with(fake).upload_ir(str(wav), remote_name="My Cab.wav")
    assert remote == "/data/stadium-family-fw/ir/My Cab.wav"
    assert fake.calls[0][2] == remote


def test_upload_ir_wraps_transfer_error(tmp_path):
    wav = tmp_path / "Cab.wav"
    wav.write_bytes(b"RIFFxxxx")

    class _BoomSFTP(_FakeSFTP):
        def put(self, local, remote):
            raise IOError("connection dropped mid-transfer")

    with pytest.raises(sftp.HelixError, match="upload"):
        _sftp_with(_BoomSFTP()).upload_ir(str(wav))
