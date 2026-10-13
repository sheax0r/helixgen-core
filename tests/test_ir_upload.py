"""Tests for the shared per-tone IR-upload core (backlog #6).

`helixgen.device.ir_upload` is the single implementation behind three
call sites: CLI `device install --auto-irs` (`helixgen.cli._auto_upload_irs`),
`device copy` (the shared per-tone IR core), and the
`device install` path. These tests exercise the core directly, so
its behavior is pinned independent of any one caller's wrapper.
"""
from pathlib import Path

import pytest


class _FakeMapping:
    def __init__(self, table):
        self._t = table  # hash -> path

    def resolve_by_hash(self, hh):
        if hh not in self._t:
            raise KeyError(hh)
        return self._t[hh]


def _patch(monkeypatch, table, push_results):
    """Patch IrMapping.load + sftp.push_ir. `push_results` is popped in call
    order (one entry per hash actually pushed)."""
    import helixgen.ir as _ir
    from helixgen.device import sftp as _sftp

    monkeypatch.setattr(_ir.IrMapping, "load",
                        classmethod(lambda cls: _FakeMapping(table)))
    calls = []

    def fake_push(ip, path, **kw):
        calls.append((ip, str(path)))
        return push_results.pop(0)

    monkeypatch.setattr(_sftp, "push_ir", fake_push)
    return calls


# ---------------------------------------------------------------------------
# upload_missing_irs
# ---------------------------------------------------------------------------

def test_upload_missing_irs_imported(monkeypatch):
    from helixgen.device import ir_upload

    _patch(monkeypatch, {"aa11": Path("/irs/a.wav")},
          [{"ok": True, "registered": True, "hash_match": True,
            "device_hash": "aa11", "name": "a", "already": False}])
    results = ir_upload.upload_missing_irs("1.2.3.4", ["aa11"])
    assert results == [{
        "hash": "aa11", "ok": True, "outcome": "imported",
        "note": "imported IR a (aa11)", "path": "/irs/a.wav", "name": "a",
        "device_hash": "aa11", "hash_match": True,
    }]


def test_upload_missing_irs_already_on_device(monkeypatch):
    from helixgen.device import ir_upload

    _patch(monkeypatch, {"bb22": Path("/irs/b.wav")},
          [{"ok": True, "already": True}])
    results = ir_upload.upload_missing_irs("1.2.3.4", ["bb22"])
    assert results[0]["ok"] is True
    assert results[0]["outcome"] == "already"
    assert "already on device" in results[0]["note"]


def test_upload_missing_irs_hash_mismatch(monkeypatch):
    from helixgen.device import ir_upload

    _patch(monkeypatch, {"aa11": Path("/irs/a.wav")},
          [{"ok": True, "registered": True, "hash_match": False,
            "device_hash": "ZZZZ", "name": "a", "already": False}])
    results = ir_upload.upload_missing_irs("1.2.3.4", ["aa11"])
    assert results[0]["ok"] is False
    assert results[0]["outcome"] == "hash_mismatch"
    assert "won't resolve" in results[0]["note"]
    assert "ZZZZ" in results[0]["note"]


def test_upload_missing_irs_not_yet_registered(monkeypatch):
    from helixgen.device import ir_upload

    _patch(monkeypatch, {"aa11": Path("/irs/a.wav")},
          [{"ok": True, "registered": False, "name": "a", "already": False}])
    results = ir_upload.upload_missing_irs("1.2.3.4", ["aa11"])
    assert results[0]["ok"] is False
    assert results[0]["outcome"] == "not_yet_registered"
    assert "not yet" in results[0]["note"]


def test_upload_missing_irs_upload_failed(monkeypatch):
    from helixgen.device import ir_upload

    _patch(monkeypatch, {"aa11": Path("/irs/a.wav")},
          [{"ok": False, "already": False}])
    results = ir_upload.upload_missing_irs("1.2.3.4", ["aa11"])
    assert results[0]["ok"] is False
    assert results[0]["outcome"] == "upload_failed"


def test_upload_missing_irs_not_found_locally(monkeypatch):
    from helixgen.device import ir_upload

    _patch(monkeypatch, {}, [])
    results = ir_upload.upload_missing_irs("1.2.3.4", ["nope"])
    assert results == [{
        "hash": "nope", "ok": False, "outcome": "not_found_locally",
        "note": ("referenced IR nope not found locally; register it "
                 "(helixgen register-irs) — cab may be silent"),
    }]


def test_upload_missing_irs_no_mapping_applies_to_every_hash(monkeypatch):
    import helixgen.ir as _ir
    from helixgen.device import ir_upload

    def _broken_load(cls):
        raise OSError("boom")

    monkeypatch.setattr(_ir.IrMapping, "load", classmethod(_broken_load))
    results = ir_upload.upload_missing_irs("1.2.3.4", ["aa11", "bb22"])
    assert [r["hash"] for r in results] == ["aa11", "bb22"]
    assert all(r["outcome"] == "no_mapping" and r["ok"] is False for r in results)
    assert "mapping.json" in results[0]["note"]


def test_upload_missing_irs_push_ir_raises_helixerror_is_surfaced(monkeypatch):
    """push_ir raising HelixError is caught and turned into a per-hash entry
    rather than propagating (a defensive improvement shared by every
    caller — previously only the bulk path guarded this)."""
    import helixgen.ir as _ir
    from helixgen.device import ir_upload
    from helixgen.device.client import HelixError

    monkeypatch.setattr(_ir.IrMapping, "load",
                        classmethod(lambda cls: _FakeMapping(
                            {"aa11": Path("/irs/a.wav")})))

    from helixgen.device import sftp as _sftp

    def _raise(ip, path, **kw):
        raise HelixError("device unreachable")

    monkeypatch.setattr(_sftp, "push_ir", _raise)

    results = ir_upload.upload_missing_irs("1.2.3.4", ["aa11"])
    assert results[0]["ok"] is False
    assert results[0]["outcome"] == "upload_error"
    assert "device unreachable" in results[0]["note"]
    assert results[0]["error_type"] == "HelixError"


def test_upload_missing_irs_push_ir_raises_any_exception_is_surfaced(monkeypatch):
    """push_ir spans SFTP/paramiko/sockets, so its failure surface is much
    wider than HelixError — ANY exception (e.g. an OSError from a dropped
    socket) is caught per-hash and never propagates, and later hashes in the
    same call still get attempted."""
    import helixgen.ir as _ir
    from helixgen.device import ir_upload

    monkeypatch.setattr(_ir.IrMapping, "load",
                        classmethod(lambda cls: _FakeMapping(
                            {"aa11": Path("/irs/a.wav"),
                             "bb22": Path("/irs/b.wav")})))

    from helixgen.device import sftp as _sftp

    def _push(ip, path, **kw):
        if "a.wav" in str(path):
            raise OSError("connection reset by peer")
        return {"ok": True, "registered": True, "hash_match": True,
                "device_hash": "bb22", "name": "b", "already": False}

    monkeypatch.setattr(_sftp, "push_ir", _push)

    results = ir_upload.upload_missing_irs("1.2.3.4", ["aa11", "bb22"])
    assert results[0]["ok"] is False
    assert results[0]["outcome"] == "upload_error"
    assert results[0]["error_type"] == "OSError"
    assert "connection reset" in results[0]["note"]
    # the failure was contained: the second hash still uploaded fine
    assert results[1]["ok"] is True
    assert results[1]["outcome"] == "imported"


def test_upload_missing_irs_empty_hashes_returns_empty(monkeypatch):
    from helixgen.device import ir_upload

    _patch(monkeypatch, {}, [])
    assert ir_upload.upload_missing_irs("1.2.3.4", []) == []


# ---------------------------------------------------------------------------
# sync_preset_irs (diff + upload, the combined per-tone entry point)
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, have):
        self._have = have

    def device_ir_hashes(self):
        return set(self._have)


def test_sync_preset_irs_nothing_missing_returns_empty(monkeypatch):
    from helixgen.device import ir_upload

    monkeypatch.setattr("helixgen.device.bridge.check_irs",
                        lambda client, body: {"present": {"aa11"}, "missing": set()})
    assert ir_upload.sync_preset_irs(_FakeClient({"aa11"}), {"_hsp": "x"},
                                     "1.2.3.4") == []


def test_sync_preset_irs_uploads_missing_when_auto_irs_true(monkeypatch):
    from helixgen.device import ir_upload

    monkeypatch.setattr("helixgen.device.bridge.check_irs",
                        lambda client, body: {"present": set(), "missing": {"aa11"}})
    calls = []

    def _fake_upload(ip, hashes, **kw):
        calls.append((ip, list(hashes)))
        return [{"hash": h, "ok": True, "outcome": "imported"} for h in hashes]

    monkeypatch.setattr(ir_upload, "upload_missing_irs", _fake_upload)

    results = ir_upload.sync_preset_irs(_FakeClient(set()), {"_hsp": "x"},
                                        "9.9.9.9", auto_irs=True)
    assert calls == [("9.9.9.9", ["aa11"])]
    assert results == [{"hash": "aa11", "ok": True, "outcome": "imported"}]


def test_sync_preset_irs_auto_irs_false_skips_upload_but_reports(monkeypatch):
    from helixgen.device import ir_upload

    monkeypatch.setattr("helixgen.device.bridge.check_irs",
                        lambda client, body: {"present": set(), "missing": {"aa11"}})
    calls = []
    monkeypatch.setattr(ir_upload, "upload_missing_irs",
                        lambda ip, hashes: calls.append((ip, list(hashes))))

    results = ir_upload.sync_preset_irs(_FakeClient(set()), {"_hsp": "x"},
                                        "9.9.9.9", auto_irs=False)
    assert calls == []
    assert len(results) == 1
    assert results[0]["hash"] == "aa11"
    assert results[0]["ok"] is False
    assert results[0]["outcome"] == "skipped_auto_irs_off"


# ---------------------------------------------------------------------------
# firmware 1.4 IR-list cap (hgc-6r5.8)
# ---------------------------------------------------------------------------

class _CountClient:
    def __init__(self, n=0, error=None):
        self.n, self.error = n, error

    def list_irs(self, *, strict=False, settle=True, include_unusable=False):
        if self.error is not None:
            raise self.error
        return [{"hash": None, "cid_": i} for i in range(self.n)]


def test_upload_refused_whole_batch_when_it_would_exceed_cap(monkeypatch):
    from helixgen.device import ir_upload

    calls = _patch(monkeypatch, {"aa11": Path("/irs/a.wav"),
                                 "bb22": Path("/irs/b.wav")}, [])
    results = ir_upload.upload_missing_irs(
        "1.2.3.4", ["aa11", "bb22", "nope"], client=_CountClient(2047))
    assert calls == []  # nothing uploaded: no half-written tone
    by = {r["hash"]: r for r in results}
    assert by["aa11"]["outcome"] == by["bb22"]["outcome"] == "ir_cap"
    assert not by["aa11"]["ok"]
    assert "2048" in by["aa11"]["note"] and "1 more" in by["aa11"]["note"]
    assert by["nope"]["outcome"] == "not_found_locally"


def test_upload_proceeds_when_it_fits_exactly(monkeypatch):
    from helixgen.device import ir_upload

    ok = {"ok": True, "registered": True, "hash_match": True, "name": "a",
          "already": False}
    calls = _patch(monkeypatch, {"aa11": Path("/irs/a.wav")}, [dict(ok)])
    results = ir_upload.upload_missing_irs(
        "1.2.3.4", ["aa11"], client=_CountClient(2047))
    assert len(calls) == 1 and results[0]["ok"]


def test_unreadable_listing_does_not_block_upload(monkeypatch):
    from helixgen.device import ir_upload

    ok = {"ok": True, "registered": True, "hash_match": True, "name": "a",
          "already": False}
    calls = _patch(monkeypatch, {"aa11": Path("/irs/a.wav")}, [dict(ok)])
    ir_upload.upload_missing_irs(
        "1.2.3.4", ["aa11"], client=_CountClient(error=RuntimeError("x")))
    assert len(calls) == 1


def test_sync_preset_irs_passes_its_client_to_the_cap_check(monkeypatch):
    from helixgen.device import bridge, ir_upload

    monkeypatch.setattr(bridge, "check_irs",
                        lambda client, body: {"missing": ["aa11"]})
    _patch(monkeypatch, {"aa11": Path("/irs/a.wav")}, [])
    results = ir_upload.sync_preset_irs(_CountClient(2048), {}, "1.2.3.4")
    assert results[0]["outcome"] == "ir_cap"
    assert "0 more" in results[0]["note"]
