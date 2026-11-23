"""Task 5: IR view (presence diff by hash) + push/delete/rename actions."""
from __future__ import annotations

import pytest

from helixgen.libraryui.session import LibraryError, LibrarySession


class _FakeClient:
    def __init__(self, irs=None):
        self.ip = "10.0.0.9"
        self._irs = irs or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_presets(self, container=-2, **kw):
        return []

    def list_setlists(self, **kw):
        return []

    def list_irs(self, **kw):
        return list(self._irs)


def _session(*, client=None, computer_irs=None, offline=False, push_ir_fn=None,
             delete_ir_fn=None, rename_ir_fn=None, usage_fn=None,
             remove_everywhere_fn=None):
    kw = dict(tone_loader=lambda: [], guitar_loader=lambda: [],
              ir_local_loader=lambda: (computer_irs or []))
    for k, v in (("push_ir_fn", push_ir_fn), ("delete_ir_fn", delete_ir_fn),
                 ("rename_ir_fn", rename_ir_fn), ("usage_fn", usage_fn),
                 ("remove_everywhere_fn", remove_everywhere_fn)):
        if v is not None:
            kw[k] = v
    factory = None if offline else (lambda: client)
    return LibrarySession(factory, **kw)


def test_ir_presence_diff_by_hash():
    comp = [{"hash": "aaa", "name": "Both IR", "wav": "irs/a.wav"},
            {"hash": "bbb", "name": "Local Only", "wav": "irs/b.wav"}]
    dev = [{"hash": "aaa", "name": "Both IR"},
           {"hash": "ccc", "name": "Device Only"}]
    st = _session(client=_FakeClient(irs=dev), computer_irs=comp)
    irs = {r["hash"]: r for r in st.public_state()["irs"]}
    assert irs["aaa"]["presence"] == "both"
    assert irs["bbb"]["presence"] == "computer"
    assert irs["ccc"]["presence"] == "device"
    assert irs["ccc"]["wav"] is None


def test_ir_view_empty_offline():
    st = _session(offline=True, computer_irs=[{"hash": "x", "name": "n",
                                               "wav": "irs/x.wav"}])
    irs = st.public_state()["irs"]
    assert [r["presence"] for r in irs] == ["computer"]


def test_ir_headroom_counts_device_irs():
    from helixgen.device.ir_upload import IR_LIST_CAP
    dev = [{"hash": "aaa", "name": "A"}, {"hash": "bbb", "name": "B"}]
    st = _session(client=_FakeClient(irs=dev), computer_irs=[])
    assert st.public_state()["ir_headroom"] == {"used": 2, "cap": IR_LIST_CAP}


def test_ir_headroom_none_when_offline():
    # Offline we don't know the device's IR count, so headroom is omitted
    # rather than guessed (the drawer hides the N/cap line).
    st = _session(offline=True, computer_irs=[{"hash": "x", "name": "n",
                                               "wav": "irs/x.wav"}])
    assert st.public_state()["ir_headroom"] is None


def test_push_ir_calls_through(tmp_path):
    wav = tmp_path / "irs" / "cab.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"RIFF....WAVE")
    seen = {}

    def push_fn(h, p):
        seen["path"] = str(p)
        return {"ok": True, "name": "cab.wav", "registered": True}

    st = LibrarySession(lambda: _FakeClient(), tone_loader=lambda: [],
                        guitar_loader=lambda: [], ir_local_loader=lambda: [],
                        push_ir_fn=push_fn, library_root=tmp_path)
    res = st.push_ir("irs/cab.wav")
    assert res["ok"] is True
    assert seen["path"].endswith("irs/cab.wav")


def test_push_missing_wav_refused(tmp_path):
    called = []
    st = LibrarySession(lambda: _FakeClient(), tone_loader=lambda: [],
                        guitar_loader=lambda: [], ir_local_loader=lambda: [],
                        push_ir_fn=lambda *a: called.append(1) or {"ok": True},
                        library_root=tmp_path)
    with pytest.raises(LibraryError) as ei:
        st.push_ir("irs/nope.wav")
    assert ei.value.status == 404
    assert not called


def test_push_ir_cap_surfaces_as_error(tmp_path):
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"RIFF")

    def push_fn(h, p):
        raise RuntimeError("device IR list is full (2048) — free space first")

    st = LibrarySession(lambda: _FakeClient(), tone_loader=lambda: [],
                        guitar_loader=lambda: [], ir_local_loader=lambda: [],
                        push_ir_fn=push_fn, library_root=tmp_path)
    with pytest.raises(LibraryError) as ei:
        st.push_ir("x.wav")
    assert ei.value.status == 502
    assert "2048" in str(ei.value)


def test_delete_ir_calls_through():
    seen = {}

    def del_fn(h, name_or_hash):
        seen["t"] = name_or_hash
        return {"ok": True, "name": "Old", "hash": name_or_hash}

    _session(client=_FakeClient(), delete_ir_fn=del_fn).delete_ir("deadbeef")
    assert seen["t"] == "deadbeef"


def test_rename_ir_calls_through():
    seen = {}

    def ren_fn(h, name_or_hash, new_name):
        seen.update(t=name_or_hash, n=new_name)
        return {"ok": True}

    _session(client=_FakeClient(), rename_ir_fn=ren_fn).rename_ir("abc", "Tele")
    assert seen == {"t": "abc", "n": "Tele"}


def test_rename_ir_needs_new_name():
    with pytest.raises(LibraryError) as ei:
        _session(client=_FakeClient()).rename_ir("abc", "")
    assert ei.value.status == 400


# -- IR folders (library-first sidecar) ------------------------------------

def _folder_session(tmp_path, *, computer_irs=None, device_irs=None, offline=False):
    factory = None if offline else (lambda: _FakeClient(irs=device_irs or []))
    return LibrarySession(factory, tone_loader=lambda: [],
                          guitar_loader=lambda: [],
                          ir_local_loader=lambda: (computer_irs or []),
                          library_root=tmp_path)


def test_ir_view_carries_folder_from_sidecar(tmp_path):
    from helixgen import ir_folders
    ir_folders.set_folder("aaa", "Marshall", library_dir=tmp_path)
    st = _folder_session(tmp_path, computer_irs=[
        {"hash": "aaa", "name": "A", "wav": "irs/a.wav"},
        {"hash": "bbb", "name": "B", "wav": "irs/b.wav"}])
    rows = {r["hash"]: r for r in st.public_state()["irs"]}
    assert rows["aaa"]["folder"] == "Marshall"
    assert rows["bbb"]["folder"] is None


def test_set_ir_folder_assigns_and_persists(tmp_path):
    from helixgen import ir_folders
    st = _folder_session(tmp_path, offline=True)
    out = st.set_ir_folder("aaa", "Fender cabs")
    assert out == {"hash": "aaa", "folder": "Fender cabs"}
    assert ir_folders.load(library_dir=tmp_path) == {"aaa": "Fender cabs"}


def test_set_ir_folder_unfiles_on_blank(tmp_path):
    from helixgen import ir_folders
    st = _folder_session(tmp_path, offline=True)
    st.set_ir_folder("aaa", "X")
    out = st.set_ir_folder("aaa", "")
    assert out["folder"] is None
    assert ir_folders.load(library_dir=tmp_path) == {}


def test_set_ir_folder_blank_hash_is_400(tmp_path):
    st = _folder_session(tmp_path, offline=True)
    with pytest.raises(LibraryError) as ei:
        st.set_ir_folder("   ", "X")
    assert ei.value.status == 400


def test_set_ir_folder_non_string_folder_is_400(tmp_path):
    st = _folder_session(tmp_path, offline=True)
    with pytest.raises(LibraryError) as ei:
        st.set_ir_folder("aaa", 3)
    assert ei.value.status == 400


# -- ir_usage + the fail-closed delete guard -------------------------------

def test_ir_usage_wires_through():
    canned = {"hash": "aaa",
              "tones": [{"cid": 11, "name": "A", "setlists": ["Rock"]},
                        {"cid": 12, "name": "B", "setlists": []}],
              "indeterminate": [99]}
    out = _session(client=_FakeClient(),
                   usage_fn=lambda h, nh, ci: dict(canned)).ir_usage("MyCab")
    assert out == canned


def test_ir_usage_offline_raises():
    with pytest.raises(LibraryError) as ei:
        _session(offline=True).ir_usage("x")
    assert ei.value.status == 409


def test_delete_ir_blocked_by_referencing_tone():
    called = []
    st = _session(
        client=_FakeClient(),
        usage_fn=lambda h, nh, ci: {"hash": "aaa", "indeterminate": [],
                                    "tones": [{"cid": 11, "name": "A",
                                               "setlists": []}]},
        delete_ir_fn=lambda h, nh: called.append(nh) or {"ok": True})
    with pytest.raises(LibraryError) as ei:
        st.delete_ir("aaa")
    assert ei.value.status == 409
    assert not called          # the IR is NOT deleted while a tone uses it


def test_delete_ir_blocked_by_indeterminate():
    st = _session(
        client=_FakeClient(),
        usage_fn=lambda h, nh, ci: {"hash": "aaa", "tones": [],
                                    "indeterminate": [99]},
        delete_ir_fn=lambda h, nh: (_ for _ in ()).throw(
            AssertionError("must not delete")))
    with pytest.raises(LibraryError) as ei:
        st.delete_ir("aaa")
    assert ei.value.status == 409


def test_delete_ir_force_skips_sweep_and_deletes():
    called = []
    st = _session(
        client=_FakeClient(),
        usage_fn=lambda h, nh, ci: (_ for _ in ()).throw(
            AssertionError("force must not sweep")),
        delete_ir_fn=lambda h, nh: called.append(nh) or {"ok": True,
                                                          "hash": nh})
    st.delete_ir("aaa", force=True)
    assert called == ["aaa"]


def test_remove_tone_device_wires_through():
    seen = {}
    st = _session(client=_FakeClient(),
                  remove_everywhere_fn=lambda h, cid: seen.update(cid=cid)
                  or {"ok": True, "removed_pool": cid, "removed_refs": [],
                      "errors": []})
    out = st.remove_tone_device(11)
    assert seen["cid"] == 11 and out["ok"] is True


def test_remove_tone_device_raises_on_device_refusal():
    """A device-refused removal (ok:False) must raise, not report success — so
    the page's error path fires and a referencing IR isn't falsely cleared."""
    st = _session(
        client=_FakeClient(),
        remove_everywhere_fn=lambda h, cid: {
            "ok": False, "errors": ["still referenced by 'Studio'"],
            "removed_refs": [], "removed_pool": None})
    with pytest.raises(LibraryError) as ei:
        st.remove_tone_device(9)
    assert ei.value.status == 502
    assert "still referenced" in str(ei.value)


# -- _default_ir_usage: enumeration + fail-closed resolution ----------------

class _SweepClient:
    def __init__(self, *, device_irs=None, presets=None, body=None):
        self._irs = device_irs or []
        self._presets = presets or []
        self._body = body

    def list_irs(self, strict=False):
        return list(self._irs)

    def list_setlists(self, strict=False):
        return []

    def list_container(self, cid, strict=False):
        return []

    def list_presets(self, container=-2, strict=False):
        return list(self._presets)

    def get_content(self, cid):
        return b"blob"


def test_default_ir_usage_unresolved_name_fails_closed():
    """A *name* that resolves to no hash raises (so the guard blocks) rather
    than sweeping for the raw name, which never matches a 32-hex irhash."""
    from helixgen.libraryui.session import _default_ir_usage
    with pytest.raises(ValueError):
        _default_ir_usage(_SweepClient(), "MyCab", [])


def test_default_ir_usage_matches_any_slot(monkeypatch):
    """The sweep uses bridge.hsp_ir_hashes (every slot of every block), so a
    hash carried in slot[1] of a non-HX2 block (dual-cab / Cab+IR) is caught —
    ir.extract_ir_hashes (slot[0], HX2 only) would miss it and fail OPEN."""
    from helixgen.libraryui import session as S
    h32 = "a" * 32
    body = {"preset": {"flow": [{"b0": {"slot": [
        {"model": "HX2_Cab_WhateverCab", "irhash": None},
        {"model": "HX2_Cab_SecondLane", "irhash": h32}]}}]}}
    monkeypatch.setattr(S, "_semantic_canon_blob", lambda blob, lib: body)
    c = _SweepClient(device_irs=[{"hash": h32, "name": "Dual"}],
                     presets=[{"cid_": 5, "name": "P"}])
    out = S._default_ir_usage(c, h32, [])
    assert [t["cid"] for t in out["tones"]] == [5]
    assert out["indeterminate"] == []
