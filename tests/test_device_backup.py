"""Tests for the local preset-backup library (``helixgen.device.backup``).

Never touches a real device: a ``FakeClient`` returns canned presets and a
fixed edit-buffer blob.  Offline reads (``local_list`` / ``read_local``) work
with no client at all.
"""
import hashlib
import json

import pytest

from helixgen.device import backup as bk

FAKE_BLOB = b"_sbepgsm" + b"payload"

CANNED_PRESETS = [
    {"cid_": 101, "name": "Clean Machine", "cctp": 1000, "posi": 0},
    {"cid_": 102, "name": "Lead/Tone: Hot!", "cctp": 1000, "posi": 1},
    {"cid_": 103, "name": "Ambient Wash", "cctp": 1000, "posi": 5},
]


class FakeClient:
    """Stand-in for HelixClient. Records calls; returns canned data."""

    def __init__(self, blob=FAKE_BLOB, presets=None):
        self.blob = blob
        self.presets = presets if presets is not None else CANNED_PRESETS
        self.calls = []

    def list_presets(self, container=-2):
        self.calls.append(("list_presets", container))
        return self.presets

    def load_preset(self, cid):
        self.calls.append(("load_preset", cid))
        return True

    def get_edit_buffer(self):
        self.calls.append(("get_edit_buffer",))
        return self.blob


def test_backup_writes_files_and_manifest(tmp_path):
    client = FakeClient()
    entries = bk.backup_setlist(client, bk.USER, tmp_path, now="2026-07-11T10:00:00")

    assert len(entries) == 3

    # Files land with <NN-slot>-<safe-name>.sbe names, sanitized.
    names = {e["file"] for e in entries}
    assert "00-1A-Clean-Machine.sbe" in names
    assert "01-1B-Lead-Tone-Hot.sbe" in names       # slash/colon/space sanitized
    assert "05-2B-Ambient-Wash.sbe" in names        # posi 5 -> slot 2B

    for e in entries:
        blob = (tmp_path / e["file"]).read_bytes()
        assert blob == FAKE_BLOB
        assert e["sha256"] == hashlib.sha256(FAKE_BLOB).hexdigest()
        assert e["bytes"] == len(FAKE_BLOB)
        assert e["fmt"] == "sbe"
        assert e["setlist"] == "user"
        assert e["saved_at"] == "2026-07-11T10:00:00"

    # Manifest on disk mirrors the returned entries.
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["version"] == bk.MANIFEST_VERSION
    assert len(manifest["entries"]) == 3
    assert {e["cid"] for e in manifest["entries"]} == {101, 102, 103}


def test_backup_loads_each_preset(tmp_path):
    client = FakeClient()
    bk.backup_setlist(client, bk.USER, tmp_path, now="t")
    loaded = [cid for (name, cid) in
              ((c[0], c[1]) for c in client.calls if c[0] == "load_preset")]
    # each preset loaded, plus a best-effort restore to the first cid
    assert loaded[:3] == [101, 102, 103]
    assert loaded[-1] == 101  # restore


def test_saved_at_omitted_when_now_none(tmp_path):
    entries = bk.backup_setlist(FakeClient(), bk.USER, tmp_path, now=None)
    assert all("saved_at" not in e for e in entries)


def test_local_list_reads_offline(tmp_path):
    bk.backup_setlist(FakeClient(), bk.USER, tmp_path, now="t")
    # No client involved — pure disk read.
    entries = bk.local_list(tmp_path)
    assert len(entries) == 3
    assert {e["name"] for e in entries} == {
        "Clean Machine", "Lead/Tone: Hot!", "Ambient Wash"}


def test_local_list_empty_without_manifest(tmp_path):
    assert bk.local_list(tmp_path) == []


def test_read_local_returns_blob(tmp_path):
    entries = bk.backup_setlist(FakeClient(), bk.USER, tmp_path, now="t")
    path = tmp_path / entries[0]["file"]
    assert bk.read_local(path) == FAKE_BLOB


def test_manifest_merge_replaces_same_file_keeps_others(tmp_path):
    # First backup of the full setlist.
    bk.backup_setlist(FakeClient(), bk.USER, tmp_path, now="t1")
    # Second backup of just one preset with a new blob -> its entry updates,
    # the other two survive.
    one = [{"cid_": 101, "name": "Clean Machine", "cctp": 1000, "posi": 0}]
    bk.backup_setlist(FakeClient(blob=b"_sbepgsmNEW", presets=one),
                      bk.USER, tmp_path, now="t2")

    entries = {e["file"]: e for e in bk.local_list(tmp_path)}
    assert len(entries) == 3
    updated = entries["00-1A-Clean-Machine.sbe"]
    assert updated["saved_at"] == "t2"
    assert updated["sha256"] == hashlib.sha256(b"_sbepgsmNEW").hexdigest()


def test_sanitize_name():
    assert bk.sanitize_name("Lead/Tone: Hot!") == "Lead-Tone-Hot"
    assert bk.sanitize_name("../../etc/passwd") == "etc-passwd"
    assert bk.sanitize_name("  spaced  out  ") == "spaced-out"
    assert bk.sanitize_name("") == "untitled"
    assert bk.sanitize_name("///") == "untitled"
    assert bk.sanitize_name("Keep_This.v2") == "Keep_This.v2"


def test_default_backup_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_DEVICE_BACKUPS", str(tmp_path / "custom"))
    assert bk.default_backup_dir() == tmp_path / "custom"


def test_default_backup_dir_default(monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEVICE_BACKUPS", raising=False)
    got = bk.default_backup_dir()
    assert got.name == "device-backups"
    assert got.parent.name == ".helixgen"


def test_backup_uses_default_dir_when_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_DEVICE_BACKUPS", str(tmp_path / "dflt"))
    entries = bk.backup_setlist(FakeClient(), bk.USER, now="t")
    assert len(entries) == 3
    assert (tmp_path / "dflt" / "manifest.json").exists()


def test_setlist_name_mapping(tmp_path):
    entries = bk.backup_setlist(FakeClient(), bk.THROWAWAY, tmp_path, now="t")
    assert all(e["setlist"] == "throwaway" for e in entries)
