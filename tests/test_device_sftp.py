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


# -- push_ir vs the stale -11 listing cache and the wedged state (#93) -------
#
# Hardware-observed 2026-07-27 (hw-validation run, fw 1.3.2 b1340): a
# watched-dir import registers the IR (row + path index + /addContent) but
# does NOT invalidate the device's -11 container-listing cache — the listing
# stayed stale for 11+ min and refreshed the instant a row was renamed. So:
# (a) push_ir nudges the cache with a same-name rename after registration;
# (b) an "already on device" point-lookup hit is only trusted as genuinely
#     registered if the hash is listed AFTER such a nudge — a hash still
#     unlisted then is a wedged orphan file (#93: e.g. a client killed
#     between /RemoveContent and the file removal) and gets removed +
#     re-imported.

HG_HASH = "aa" * 16


class _FakeClient:
    def __init__(self, path=None, listed=(), listed_after_nudge=None,
                 listing_error=None, rename_ok=True, rename_error=None):
        self.path = path
        self.listed = list(listed)
        self.listed_after_nudge = listed_after_nudge
        self.listing_error = listing_error
        self.rename_ok = rename_ok
        self.rename_error = rename_error
        self.renames = []

    def __call__(self, ip):  # stands in for the HelixClient class
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def ir_path_for_hash(self, hh, strict=False):
        return self.path

    def rename(self, cid, name):
        self.renames.append((cid, name))
        if self.rename_error is not None:
            raise self.rename_error
        if not self.rename_ok:
            return False
        if self.listed_after_nudge is not None:
            self.listed = list(self.listed_after_nudge)
        return True

    def list_irs(self, *, strict=False, settle=True, include_unusable=False):
        if self.listing_error is not None:
            raise self.listing_error
        rows = []
        for i, h in enumerate(self.listed):
            # a dict entry is a verbatim row (tests shaping name/cid_ edge
            # cases); a None entry is a row whose hash failed to normalize —
            # visible (hash None) only when the caller asks for unusable rows
            if isinstance(h, dict):
                rows.append(dict(h))
            elif h is None:
                if include_unusable:
                    rows.append({"hash": None, "cid_": 100 + i,
                                 "name": f"ir{i}"})
            else:
                rows.append({"hash": h, "cid_": 100 + i, "name": f"ir{i}"})
        return rows


class _FakeSubscriber:
    """Emits one /addContent event carrying HG_HASH + cid, then goes quiet."""

    def __init__(self, ip, ports=()):
        self.events = [type("Ev", (), {
            "addr": "/addContent",
            "args": [{"hash": HG_HASH, "cid_": 1465, "name": "HGTEST-ir"}]})()]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def poll(self, timeout):
        evs, self.events = self.events, []
        return evs


class _FakePushSFTP:
    """Class-shaped fake for sftp.HelixSFTP; records calls across instances."""

    calls: list = []

    def __init__(self, ip, key_path=None, user=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def upload_ir(self, local, *, remote_name=None):
        _FakePushSFTP.calls.append(("upload", remote_name))
        return f"/data/stadium-family-fw/ir/{remote_name}"

    def ir_file_exists(self, name):
        return True

    def remove_ir_file(self, name):
        _FakePushSFTP.calls.append(("remove", name))


def _patched_push_ir(monkeypatch, tmp_path, client,
                     subscriber=_FakeSubscriber):
    import time as _time
    import helixgen.ir as _hir
    from helixgen.device import client as _client_mod
    from helixgen.device import subscribe as _sub_mod

    monkeypatch.setattr(_hir, "write_stadium_ir", lambda src, dst: HG_HASH)
    monkeypatch.setattr(_client_mod, "HelixClient", client)
    monkeypatch.setattr(_sub_mod, "HelixSubscriber", subscriber)
    monkeypatch.setattr(sftp, "HelixSFTP", _FakePushSFTP)
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    # monkeypatch-scoped reset: the recorder is a class attribute, so restore
    # it after the test rather than leaking state across tests
    monkeypatch.setattr(_FakePushSFTP, "calls", [])
    wav = tmp_path / "HGTEST-ir.wav"
    wav.write_bytes(b"RIFFxxxx")
    return sftp.push_ir("1.2.3.4", str(wav))


def test_push_ir_fresh_import_nudges_listing_and_returns_cid(monkeypatch,
                                                             tmp_path):
    """The normal import path must nudge the -11 listing cache (same-name
    rename of the cid /addContent reported) and surface that cid."""
    client = _FakeClient(path=None)
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["ok"] and res["registered"] and res["hash_match"]
    assert res["cid"] == 1465
    assert client.renames == [(1465, "HGTEST-ir")]
    assert ("upload", "HGTEST-ir.wav") in _FakePushSFTP.calls


def test_push_ir_already_registered_and_listed(monkeypatch, tmp_path):
    """Point lookup resolves AND the -11 listing has the hash: genuinely
    already on device — no SFTP traffic, no nudge needed."""
    client = _FakeClient(path="/data/stadium-family-fw/ir/HGTEST-ir.wav",
                         listed=(HG_HASH,))
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["ok"] and res["already"] is True
    assert _FakePushSFTP.calls == []
    assert client.renames == []


def test_push_ir_stale_listing_healed_by_nudge_is_already(monkeypatch,
                                                          tmp_path):
    """Point lookup resolves, hash unlisted, but a nudge (same-name rename of
    a listed row) refreshes the cache and the hash appears: the IR is
    genuinely registered — 'already', and the backing file must NOT be
    touched."""
    client = _FakeClient(path="/data/stadium-family-fw/ir/HGTEST-ir.wav",
                         listed=("bb" * 16,),
                         listed_after_nudge=("bb" * 16, HG_HASH))
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["ok"] and res["already"] is True
    assert _FakePushSFTP.calls == []
    assert client.renames == [(100, "ir0")]


def test_push_ir_wedged_file_removed_and_reimported(monkeypatch, tmp_path):
    """Point lookup resolves and the hash stays unlisted even after the nudge
    refreshed the cache: the file is a wedged orphan (#93). push_ir must
    remove it and run the normal import instead of false-positiving 'already
    on device'."""
    client = _FakeClient(path="/data/stadium-family-fw/ir/HGTEST-ir.wav",
                         listed=("bb" * 16,),
                         listed_after_nudge=("bb" * 16,))
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["already"] is False
    assert res["ok"] and res["registered"] and res["hash_match"]
    assert ("remove", "HGTEST-ir.wav") in _FakePushSFTP.calls
    assert ("upload", "HGTEST-ir.wav") in _FakePushSFTP.calls
    # remove the orphan BEFORE re-uploading
    assert _FakePushSFTP.calls.index(("remove", "HGTEST-ir.wav")) < \
        _FakePushSFTP.calls.index(("upload", "HGTEST-ir.wav"))


def test_push_ir_wedge_check_listing_failure_trusts_already(monkeypatch,
                                                            tmp_path):
    """A failed -11 listing must NOT be read as 'wedged' — flaky transport is
    the common case, and deleting the backing file on a bad read would break a
    genuinely-registered IR. Fall back to trusting the point lookup."""
    client = _FakeClient(path="/data/stadium-family-fw/ir/HGTEST-ir.wav",
                         listing_error=HelixError("dropped listing"))
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["ok"] and res["already"] is True
    assert _FakePushSFTP.calls == []


def test_push_ir_nudge_rename_failure_trusts_already(monkeypatch, tmp_path):
    """A dropped nudge-rename reply surfaces as rename() returning False (not
    an exception). Re-listing a cache that was never refreshed would read as
    'wedged' and DELETE a healthy IR's backing file — so an unconfirmed nudge
    must keep the trusting path, same as a failed listing."""
    client = _FakeClient(path="/data/stadium-family-fw/ir/HGTEST-ir.wav",
                         listed=("bb" * 16,), rename_ok=False)
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["ok"] and res["already"] is True
    assert _FakePushSFTP.calls == []
    assert client.renames == [(100, "ir0")]  # the nudge WAS attempted


def test_push_ir_empty_listing_trusts_already(monkeypatch, tmp_path):
    """Zero listed rows leave nothing to nudge, so the cache was never
    refreshed — a stale-EMPTY cache (only IR imported watched-dir by a
    non-nudging client) is indistinguishable from a wedge here. No refresh,
    no wedge verdict: trust the point lookup, touch nothing."""
    client = _FakeClient(path="/data/stadium-family-fw/ir/HGTEST-ir.wav",
                         listed=())
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["ok"] and res["already"] is True
    assert _FakePushSFTP.calls == []
    assert client.renames == []


def test_push_ir_unusable_listed_hash_vetoes_wedge_verdict(monkeypatch,
                                                           tmp_path):
    """A listing row whose hash list_irs could not normalize may BE the IR
    being pushed — declaring a wedge over it and deleting the backing file
    would break a genuinely-registered IR. Any unusable row keeps the
    trusting path (no nudge, no delete) — even when other, nudgeable rows
    are listed."""
    client = _FakeClient(path="/data/stadium-family-fw/ir/HGTEST-ir.wav",
                         listed=("bb" * 16, None))
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["ok"] and res["already"] is True
    assert _FakePushSFTP.calls == []
    assert client.renames == []


def test_push_ir_nudge_skips_rows_without_usable_name_or_cid(monkeypatch,
                                                             tmp_path):
    """The nudge rename must never write a device row's missing/None name
    back onto it (that would blank a real user IR's display name) — pick the
    first row with a truthy str name AND a cid."""
    rows = ({"hash": "bb" * 16, "cid_": 7, "name": None},
            {"hash": "cc" * 16, "name": "orphan-no-cid"},
            {"hash": "dd" * 16, "cid_": 9, "name": "usable"})
    client = _FakeClient(path="/data/stadium-family-fw/ir/HGTEST-ir.wav",
                         listed=rows,
                         listed_after_nudge=rows + ({"hash": HG_HASH,
                                                     "cid_": 10,
                                                     "name": "HGTEST-ir"},))
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["ok"] and res["already"] is True
    assert client.renames == [(9, "usable")]
    assert _FakePushSFTP.calls == []


def test_push_ir_no_nudgeable_row_trusts_already(monkeypatch, tmp_path):
    """Rows listed but none with a usable (name, cid) pair: the cache cannot
    be refreshed, so the wedge verdict is unearned — trust the point lookup,
    attempt no rename."""
    client = _FakeClient(path="/data/stadium-family-fw/ir/HGTEST-ir.wav",
                         listed=({"hash": "bb" * 16, "cid_": 7,
                                  "name": None},))
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["ok"] and res["already"] is True
    assert client.renames == []
    assert _FakePushSFTP.calls == []


def test_push_ir_fresh_import_nonstring_name_nudges_with_stem(monkeypatch,
                                                              tmp_path):
    """/addContent payloads are device-controlled msgpack: a non-str name
    must not be written back by the cache nudge — fall back to the wav
    stem."""
    class _BytesNameSubscriber(_FakeSubscriber):
        def __init__(self, ip, ports=()):
            self.events = [type("Ev", (), {
                "addr": "/addContent",
                "args": [{"hash": HG_HASH, "cid_": 1465,
                          "name": b"HGTEST-ir"}]})()]

    client = _FakeClient(path=None)
    res = _patched_push_ir(monkeypatch, tmp_path, client,
                           subscriber=_BytesNameSubscriber)
    assert res["ok"] and res["registered"]
    assert client.renames == [(1465, "HGTEST-ir")]  # the stem, as str


def test_push_ir_post_registration_nudge_failure_is_advisory(monkeypatch,
                                                             tmp_path):
    """The fresh-import cache nudge is advisory: the IR is registered either
    way, so a rename raising must not fail the push."""
    client = _FakeClient(path=None,
                         rename_error=HelixError("dropped rename"))
    res = _patched_push_ir(monkeypatch, tmp_path, client)
    assert res["ok"] and res["registered"] and res["hash_match"]
    assert client.renames == [(1465, "HGTEST-ir")]


class _NoCidSubscriber(_FakeSubscriber):
    """An /addContent event carrying a hash but no cid_."""

    def __init__(self, ip, ports=()):
        self.events = [type("Ev", (), {
            "addr": "/addContent", "args": [{"hash": HG_HASH}]})()]


def test_push_ir_addcontent_without_cid_skips_nudge(monkeypatch, tmp_path):
    """No cid_ in the /addContent payload: registration is still confirmed,
    but there is no row to nudge — cid is None and no rename is attempted."""
    client = _FakeClient(path=None)
    res = _patched_push_ir(monkeypatch, tmp_path, client,
                           subscriber=_NoCidSubscriber)
    assert res["ok"] and res["registered"] and res["hash_match"]
    assert res["cid"] is None
    assert client.renames == []


class _DecoySubscriber(_FakeSubscriber):
    """A multi-dict /addContent payload: a decoy dict with an INVALID hash but
    a cid, then the real registration with a 16-byte blob hash."""

    def __init__(self, ip, ports=()):
        self.events = [type("Ev", (), {
            "addr": "/addContent",
            "args": [{"hash": "junk", "cid_": 9999, "name": "decoy"},
                     {"hash": bytes.fromhex(HG_HASH), "cid_": 1465,
                      "name": "HGTEST-ir"}]})()]


def test_push_ir_cid_comes_from_the_dict_the_hash_was_accepted_from(
        monkeypatch, tmp_path):
    """cid/name must be read from the SAME arg dict whose hash validated —
    a decoy dict with an invalid hash must not supply the cid — and a real
    16-byte blob hash must flow through push_ir end to end."""
    client = _FakeClient(path=None)
    res = _patched_push_ir(monkeypatch, tmp_path, client,
                           subscriber=_DecoySubscriber)
    assert res["ok"] and res["registered"] and res["hash_match"]
    assert res["cid"] == 1465
    assert client.renames == [(1465, "HGTEST-ir")]
