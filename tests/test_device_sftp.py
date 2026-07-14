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


def test_addcontent_hash_extracts_16byte_blob():
    from helixgen.device.sftp import _addcontent_hash
    # /addContent payload decodes to a dict with a 16-byte 'hash'
    args = [{"ccid": -11, "cctp": 1002, "cid_": 951,
             "hash": bytes.fromhex("0fbe090d975dd8f6e31b16c06a80e2ac")}]
    assert _addcontent_hash(args) == "0fbe090d975dd8f6e31b16c06a80e2ac"


def test_addcontent_hash_none_when_absent():
    from helixgen.device.sftp import _addcontent_hash
    assert _addcontent_hash([{"cid_": 1}]) is None
    assert _addcontent_hash([1, 2, "x"]) is None


def test_remove_ir_file_unlinks_under_ir_dir():
    class _RmSFTP(_FakeSFTP):
        def remove(self, remote):
            self.calls.append(("remove", remote))

    fake = _RmSFTP()
    _sftp_with(fake).remove_ir_file("ZZC-test.wav")
    assert fake.calls == [("remove", "/data/stadium-family-fw/ir/ZZC-test.wav")]


def test_remove_ir_file_rejects_path_traversal():
    fake = _FakeSFTP()
    s = _sftp_with(fake)
    with pytest.raises(HelixError, match="basename"):
        s.remove_ir_file("../presets/evil.wav")
    assert fake.calls == []


def test_remove_ir_file_wraps_error():
    class _BoomSFTP(_FakeSFTP):
        def remove(self, remote):
            raise IOError("nope")

    with pytest.raises(sftp.HelixError, match="remove"):
        _sftp_with(_BoomSFTP()).remove_ir_file("x.wav")


def test_remove_ir_file_tolerates_already_gone():
    """The device lazily GCs the file itself after /RemoveContent — an ENOENT
    means it beat us to it, which is success."""
    class _GoneSFTP(_FakeSFTP):
        def remove(self, remote):
            raise FileNotFoundError(2, "No such file")

    _sftp_with(_GoneSFTP()).remove_ir_file("x.wav")  # no raise
