"""Unit tests for device link-key (no Frida / codesign / app needed)."""
from __future__ import annotations

import plistlib
import stat

import pytest

from helixgen.device import linkkey
from helixgen.device.client import HelixError


def test_find_app_explicit_ok(tmp_path):
    app = tmp_path / "Helix Stadium.app"
    app.mkdir()
    assert linkkey.find_app(str(app)) == str(app)


def test_find_app_explicit_missing(tmp_path):
    with pytest.raises(HelixError, match="no Helix Stadium app"):
        linkkey.find_app(str(tmp_path / "nope.app"))


def test_find_app_candidates(tmp_path, monkeypatch):
    app = tmp_path / "Helix Stadium.app"
    app.mkdir()
    monkeypatch.setattr(linkkey, "_APP_CANDIDATES", [str(app)])
    assert linkkey.find_app() == str(app)


def test_find_app_none(monkeypatch):
    monkeypatch.setattr(linkkey, "_APP_CANDIDATES", ["/no/such.app"])
    with pytest.raises(HelixError, match="could not find"):
        linkkey.find_app()


def test_app_version_reads_plist(tmp_path):
    app = tmp_path / "Helix Stadium.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleShortVersionString": "1.4.0"}, fh)
    assert linkkey.app_version(str(app)) == "1.4.0"


def test_app_version_missing_is_none(tmp_path):
    assert linkkey.app_version(str(tmp_path / "x.app")) is None


_KEY = b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA...\n-----END OPENSSH PRIVATE KEY-----\n"


def test_store_key_from_bytes_is_0600(tmp_path):
    dest = tmp_path / ".helixgen" / "id_hedit"
    assert linkkey._store_key(str(dest), data=_KEY) is True
    assert dest.read_bytes() == _KEY
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_store_key_from_path_is_0600(tmp_path):
    src = tmp_path / "temp_key"
    src.write_bytes(_KEY)
    dest = tmp_path / "sub" / "id_hedit"
    assert linkkey._store_key(str(dest), from_path=str(src)) is True
    assert dest.read_bytes() == _KEY
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_store_key_overwrites_world_readable_file(tmp_path):
    # a pre-existing 0644 file must end up 0600, never leaking the new key.
    dest = tmp_path / "id_hedit"
    dest.write_bytes(b"old")
    dest.chmod(0o644)
    assert linkkey._store_key(str(dest), data=_KEY) is True
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_store_key_vanished_temp_returns_false(tmp_path):
    # the app deletes its temp before we copy -> keep waiting, no write.
    dest = tmp_path / "id_hedit"
    assert linkkey._store_key(str(dest), from_path=str(tmp_path / "gone")) is False
    assert not dest.exists()


def test_store_key_rejects_non_key_bytes(tmp_path):
    # a stray non-key path on the auth hook must not count as a capture.
    dest = tmp_path / "id_hedit"
    assert linkkey._store_key(str(dest), data=b"not a key at all") is False
    assert not dest.exists()


def test_capture_non_darwin_raises(monkeypatch):
    monkeypatch.setattr(linkkey.platform, "system", lambda: "Linux")
    with pytest.raises(HelixError, match="macOS-only"):
        linkkey.capture()


def test_require_frida_missing_hint(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "frida":
            raise ImportError("no frida")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(HelixError, match="needs Frida"):
        linkkey._require_frida()


def test_resign_missing_codesign(tmp_path, monkeypatch):
    src = tmp_path / "Helix Stadium.app"
    src.mkdir()
    monkeypatch.setattr(linkkey.shutil, "which", lambda _n: None)
    with pytest.raises(HelixError, match="codesign not found"):
        linkkey.resign_debug_app(str(src), str(tmp_path / "Debug.app"))
