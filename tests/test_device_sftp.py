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
