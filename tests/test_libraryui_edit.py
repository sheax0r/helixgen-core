"""Phase 2: the "Edit →" handoff — load the tone, hand back the live-editor URL.

Seam-level with a fake client + fake launcher (the real launcher boots an
in-process liveedit server, exercised against hardware, not here)."""
from __future__ import annotations

import pytest

from helixgen.libraryui.session import LibraryError, LibrarySession

_SENTINEL = object()


class _FakeClient:
    def __init__(self, load_ok=True, dirty=False):
        self.loaded = None
        self._ok = load_ok
        self._dirty = dirty

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def preset_edited(self):
        return self._dirty

    def load_preset(self, cid):
        self.loaded = cid
        return self._ok


def _session(*, offline=False, launcher=_SENTINEL, load_ok=True, dirty=False):
    client = _FakeClient(load_ok, dirty)
    kw = dict(tone_loader=lambda: [], guitar_loader=lambda: [],
              ir_local_loader=lambda: [])
    if launcher is not _SENTINEL:
        kw["live_launcher"] = launcher
    factory = None if offline else (lambda: client)
    return LibrarySession(factory, **kw), client


def test_edit_loads_tone_then_returns_url():
    st, client = _session(launcher=lambda: "http://127.0.0.1:9123/")
    assert st.edit_handoff(2084) == {"cid": 2084,
                                     "url": "http://127.0.0.1:9123/"}
    assert client.loaded == 2084  # made active before the editor opened


def test_edit_without_launcher_is_409():
    st, _ = _session(launcher=None)
    with pytest.raises(LibraryError) as ei:
        st.edit_handoff(1)
    assert ei.value.status == 409


def test_edit_bad_cid_is_400():
    st, _ = _session(launcher=lambda: "x")
    with pytest.raises(LibraryError) as ei:
        st.edit_handoff("not-a-number")
    assert ei.value.status == 400


def test_edit_refused_load_is_502_and_skips_editor():
    called = []
    st, client = _session(load_ok=False,
                          launcher=lambda: called.append(1) or "x")
    with pytest.raises(LibraryError) as ei:
        st.edit_handoff(7)
    assert ei.value.status == 502
    assert not called  # editor never opened when the load failed


def test_edit_launcher_failure_is_502():
    def boom():
        raise RuntimeError("editor would not boot")
    st, _ = _session(launcher=boom)
    with pytest.raises(LibraryError) as ei:
        st.edit_handoff(7)
    assert ei.value.status == 502


def test_edit_offline_is_409():
    st, _ = _session(offline=True, launcher=lambda: "x")
    with pytest.raises(LibraryError) as ei:
        st.edit_handoff(7)
    assert ei.value.status == 409


def test_edit_refuses_when_editor_has_unsaved_edits():
    # loading a preset discards the edit buffer — a second Edit must not throw
    # away unsaved tweaks silently.
    called = []
    st, client = _session(dirty=True,
                          launcher=lambda: called.append(1) or "x")
    with pytest.raises(LibraryError) as ei:
        st.edit_handoff(7)
    assert ei.value.status == 409
    assert client.loaded is None and not called  # neither loaded nor opened
