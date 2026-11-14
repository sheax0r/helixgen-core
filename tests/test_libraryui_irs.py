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
             delete_ir_fn=None, rename_ir_fn=None):
    kw = dict(tone_loader=lambda: [], guitar_loader=lambda: [],
              ir_local_loader=lambda: (computer_irs or []))
    for k, v in (("push_ir_fn", push_ir_fn), ("delete_ir_fn", delete_ir_fn),
                 ("rename_ir_fn", rename_ir_fn)):
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
