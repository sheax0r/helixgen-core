"""Unit tests for the optional progress-callback seam on the sync engine
(device/setlist_sync.py :func:`sync_setlists`).

Reuses the device-free fake-client harness from ``test_setlist_sync`` (the
``FakeClient`` / ``_stub_bridge`` / ``_manifest`` / ``_seed_pool`` helpers,
which patch ``read_hsp``, the transcoder, ``bridge.check_irs`` and
``_upload_missing_irs`` so nothing hits disk or the network). These tests
assert the *event stream* a scripted sync produces when a ``progress``
callback is supplied — the engine's only new observable behavior.
"""
from __future__ import annotations

from helixgen.device import setlist_sync as ss
from helixgen.device.client import HelixError

from tests.test_setlist_sync import (
    FakeClient,
    _manifest,
    _seed_pool,
    _stub_bridge,
)


def _of_phase(events, phase):
    return [e for e in events if e.phase == phase]


# ---------------------------------------------------------------------------
# ProgressEvent shape
# ---------------------------------------------------------------------------

def test_progress_event_is_frozen_with_documented_fields():
    ev = ss.ProgressEvent("install", label="X", index=1, total=2,
                          status="ok", detail="d")
    assert (ev.phase, ev.label, ev.index, ev.total, ev.status, ev.detail) == (
        "install", "X", 1, 2, "ok", "d")
    d = ss.ProgressEvent("plan")
    assert d.label is None and d.index is None and d.total is None
    assert d.status is None and d.detail is None
    import dataclasses
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.phase = "gc"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# full fresh sync stream
# ---------------------------------------------------------------------------

def test_full_fresh_sync_event_stream(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A", "Tone B"]},
                  hashes={"Tone A": "sha256:a", "Tone B": "sha256:b"})
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    events = []
    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"],
                           progress=events.append)

    assert res["ok"] is True

    plans = _of_phase(events, "plan")
    assert len(plans) == 1
    assert plans[0].total == 2

    installs = _of_phase(events, "install")
    assert [(e.index, e.total, e.label, e.status) for e in installs] == [
        (1, 2, "Tone A", "ok"),
        (2, 2, "Tone B", "ok"),
    ]

    refs = _of_phase(events, "references")
    assert len(refs) == 1
    assert refs[0].label == "helixgen"
    assert refs[0].status == "ok"
    assert refs[0].index == 1
    assert refs[0].total == 1


# ---------------------------------------------------------------------------
# per-tone install FAILURE still emits its install event with status=error
# ---------------------------------------------------------------------------

def test_install_failure_still_emits_error_event(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A", "Tone B"]},
                  hashes={"Tone A": "sha256:a", "Tone B": "sha256:b"})

    class FailA(FakeClient):
        def install_into_pool(self, blob, name, *, template_blob=None, pos=None):
            if name == "Tone A":
                raise HelixError("boom installing A")
            return super().install_into_pool(blob, name,
                                             template_blob=template_blob, pos=pos)

    client = FailA(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    events = []
    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"],
                           progress=events.append)

    assert res["ok"] is False
    assert any("Tone A" in e for e in res["errors"])
    assert res["pool"]["installed"] == ["Tone B"]

    installs = _of_phase(events, "install")
    a = next(e for e in installs if e.label == "Tone A")
    b = next(e for e in installs if e.label == "Tone B")
    assert a.status == "error"
    assert a.detail and "boom installing A" in a.detail
    assert a.index == 1 and a.total == 2
    assert b.status == "ok"
    assert b.index == 2 and b.total == 2


def test_install_returns_no_cid_emits_error_event(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})

    class NoCid(FakeClient):
        def install_into_pool(self, blob, name, *, template_blob=None, pos=None):
            return None

    client = NoCid(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    events = []
    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"],
                           progress=events.append)

    assert res["ok"] is False
    installs = _of_phase(events, "install")
    assert len(installs) == 1
    assert installs[0].label == "Tone A"
    assert installs[0].status == "error"
    assert installs[0].detail and "no cid" in installs[0].detail.lower()


# ---------------------------------------------------------------------------
# update events
# ---------------------------------------------------------------------------

def test_update_events_emitted(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:NEW"})
    _seed_pool("Tone A", 5000, 0, "sha256:OLD")
    client = FakeClient(setlists={"helixgen": 42}, pool=[("Tone A", 5000, 0)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    events = []
    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"],
                           progress=events.append)

    assert res["pool"]["updated"] == ["Tone A"]
    assert _of_phase(events, "plan")[0].total == 1
    updates = _of_phase(events, "update")
    assert [(e.index, e.total, e.label, e.status) for e in updates] == [
        (1, 1, "Tone A", "ok")]
    assert _of_phase(events, "install") == []


def test_repush_emits_update_events(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})
    _seed_pool("Tone A", 5000, 0, "sha256:a")
    client = FakeClient(setlists={"helixgen": 42}, pool=[("Tone A", 5000, 0)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    events = []
    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"],
                           repush=True, progress=events.append)

    assert res["pool"]["updated"] == ["Tone A"]
    updates = _of_phase(events, "update")
    assert len(updates) == 1
    assert updates[0].label == "Tone A"
    assert updates[0].status == "ok"


# ---------------------------------------------------------------------------
# IR events
# ---------------------------------------------------------------------------

def test_ir_events_ok_and_error(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    monkeypatch.setattr(
        ss.bridge, "check_irs",
        lambda client, body: {"present": set(), "missing": {"aa11", "bb22"}})

    def fake_upload(ip, hashes):
        return [
            {"hash": "aa11", "name": "cab-A.wav", "ok": True, "note": "registered"},
            {"hash": "bb22", "ok": False, "note": "not found locally"},
        ]

    monkeypatch.setattr(ss, "_upload_missing_irs", fake_upload)

    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    events = []
    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"],
                           progress=events.append)

    assert len(res["irs"]) == 2
    irs = _of_phase(events, "irs")
    assert [(e.index, e.total, e.label, e.status) for e in irs] == [
        (1, 2, "cab-A.wav", "ok"),
        (2, 2, "bb22", "error"),
    ]
    assert irs[1].detail == "not found locally"


# ---------------------------------------------------------------------------
# callback-exception safety
# ---------------------------------------------------------------------------

def test_callback_exception_never_breaks_sync_and_warns_once(
        tmp_path, monkeypatch, capsys):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A", "Tone B"]},
                  hashes={"Tone A": "sha256:a", "Tone B": "sha256:b"})
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    def boom(ev):
        raise RuntimeError("callback kaboom")

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"], progress=boom)

    assert res["ok"] is True
    assert res["pool"]["installed"] == ["Tone A", "Tone B"]
    assert "helixgen" in res["references"]

    err = capsys.readouterr().err
    assert err.count("sync progress callback raised") == 1


def test_callback_exception_result_matches_progress_none(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)

    def build():
        mm = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A", "Tone B"]},
                       hashes={"Tone A": "sha256:a", "Tone B": "sha256:b"})
        cl = FakeClient(setlists={"helixgen": 42})
        return mm, cl

    m1, c1 = build()
    monkeypatch.setattr(ss, "HelixClient", lambda **k: c1)
    res_none = ss.sync_setlists(m1, ip="1.2.3.4", setlists=["helixgen"])

    m2, c2 = build()
    monkeypatch.setattr(ss, "HelixClient", lambda **k: c2)

    def boom(ev):
        raise RuntimeError("nope")

    res_boom = ss.sync_setlists(m2, ip="1.2.3.4", setlists=["helixgen"],
                                progress=boom)

    assert res_boom == res_none


# ---------------------------------------------------------------------------
# progress=None unchanged
# ---------------------------------------------------------------------------

def test_progress_none_no_events_and_normal_result(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A", "Tone B"]},
                  hashes={"Tone A": "sha256:a", "Tone B": "sha256:b"})
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["ok"] is True
    assert res["pool"]["installed"] == ["Tone A", "Tone B"]
    assert client.mirror_calls == [(42, [5000, 5001])]


# ---------------------------------------------------------------------------
# delete + gc events
# ---------------------------------------------------------------------------

def test_delete_and_skip_events(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    from tests.test_setlist_sync import _add_slot_only_tone
    from helixgen.device.client import Cctp

    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    _seed_pool("Keep", 5000, 0, "sha256:k")
    _add_slot_only_tone(m, "Gone", slot=None, content_hash="sha256:g")
    _seed_pool("Gone", 5001, 1, "sha256:g")
    m.tones["Gone"]["slot"] = None
    _add_slot_only_tone(m, "Shared", slot=None, content_hash="sha256:s")
    _seed_pool("Shared", 5002, 2, "sha256:s")
    m.tones["Shared"]["slot"] = None
    client = FakeClient(setlists={"helixgen": 42, "other": 43},
                        pool=[("Keep", 5000, 0), ("Gone", 5001, 1),
                              ("Shared", 5002, 2)])
    client._refs[43] = [dict(cctp=Cctp.REFERENCE, rcid=5002, cid_=9500, posi=0)]
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    events = []
    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"],
                           progress=events.append)

    assert res["pool"]["deleted"] == ["Gone"]
    assert res["pool"]["delete_skipped"] == ["Shared"]
    dels = _of_phase(events, "delete")
    by_label = {e.label: e for e in dels}
    assert by_label["Gone"].status == "ok"
    assert by_label["Shared"].status == "skip"
    assert all(e.total == 2 for e in dels)


def test_gc_events(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    _seed_pool("Keep", 5000, 0, "sha256:k")
    client = FakeClient(
        setlists={"helixgen": 42},
        pool=[("Keep", 5000, 0), ("Orphan", 5001, 1)],
    )
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    events = []
    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=None, gc=True,
                           progress=events.append)

    assert res["gc"]["deleted"] == ["Orphan"]
    gcs = _of_phase(events, "gc")
    assert len(gcs) == 1
    assert gcs[0].label == "Orphan"
    assert gcs[0].status == "ok"
    assert gcs[0].total == 1
