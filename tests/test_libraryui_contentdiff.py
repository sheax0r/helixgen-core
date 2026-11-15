"""Phase 2: content-diff "changed" badge + stale-after-save reconcile.

Seam-level tests with fakes (the real projection is HW-validated — spike
2026-11-14), plus a pinned test of the default projection's same/changed/
indeterminate branches and the in-place update's write + auto-commit.
"""
from __future__ import annotations

import pytest

from helixgen.libraryui import session as S
from helixgen.libraryui.session import LibraryError, LibrarySession
from helixgen.tone_meta import ToneMeta, Variant


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _meta(tmp_path, preset_name="Crunch - Prestige"):
    hsp = tmp_path / "tones" / "a.hsp"
    hsp.parent.mkdir(parents=True, exist_ok=True)
    hsp.write_text("{}")
    return ToneMeta(
        artist=None, song=None, descriptor="Crunch", tags=[],
        description_md=None,
        variants={"ibanez-prestige": Variant(hsp="tones/a.hsp",
                                             preset_name=preset_name)},
        created="2026-01-01", updated="2026-01-01")


def _session(tmp_path, *, offline=False, diff_fn=None, update_local_fn=None):
    meta = _meta(tmp_path)  # eager: writes tones/a.hsp so _resolve_hsp sees it
    kw = dict(tone_loader=lambda: [meta], guitar_loader=lambda: [],
              ir_local_loader=lambda: [], library_root=tmp_path)
    if diff_fn is not None:
        kw["diff_fn"] = diff_fn
    if update_local_fn is not None:
        kw["update_local_fn"] = update_local_fn
    factory = None if offline else (lambda: _FakeClient())
    return LibrarySession(factory, **kw)


# -- content_diff seam -----------------------------------------------------

def test_diff_returns_state_with_cid(tmp_path):
    seen = {}

    def diff_fn(h, cid, hsp_path):
        seen.update(cid=cid, hsp=str(hsp_path))
        return "changed"

    st = _session(tmp_path, diff_fn=diff_fn)
    assert st.content_diff(2037, "tones/a.hsp") == {"cid": 2037,
                                                    "state": "changed"}
    assert seen["cid"] == 2037 and seen["hsp"].endswith("tones/a.hsp")


def test_diff_cid_required(tmp_path):
    st = _session(tmp_path, diff_fn=lambda *a: "same")
    with pytest.raises(LibraryError) as ei:
        st.content_diff(None, "tones/a.hsp")
    assert ei.value.status == 400


def test_diff_missing_hsp_is_404(tmp_path):
    st = _session(tmp_path, diff_fn=lambda *a: "same")
    with pytest.raises(LibraryError) as ei:
        st.content_diff(1, "tones/does-not-exist.hsp")
    assert ei.value.status == 404


def test_diff_offline_refused(tmp_path):
    st = _session(tmp_path, offline=True, diff_fn=lambda *a: "same")
    with pytest.raises(LibraryError) as ei:
        st.content_diff(1, "tones/a.hsp")
    assert ei.value.status == 409


def test_diff_device_error_is_502(tmp_path):
    def diff_fn(h, cid, hsp_path):
        raise OSError("device dropped mid-read")

    st = _session(tmp_path, diff_fn=diff_fn)
    with pytest.raises(LibraryError) as ei:
        st.content_diff(1, "tones/a.hsp")
    assert ei.value.status == 502


# -- update_local seam -----------------------------------------------------

def test_update_local_calls_fn_and_returns(tmp_path):
    seen = {}

    def up(h, cid, hsp_path):
        seen.update(cid=cid, hsp=str(hsp_path))
        return {"name": "Crunch - Prestige", "hsp": str(hsp_path)}

    st = _session(tmp_path, update_local_fn=up)
    res = st.update_local(2037, "tones/a.hsp")
    assert res["name"] == "Crunch - Prestige"
    assert seen["cid"] == 2037 and seen["hsp"].endswith("tones/a.hsp")


def test_update_local_missing_hsp_is_404(tmp_path):
    st = _session(tmp_path, update_local_fn=lambda *a: {})
    with pytest.raises(LibraryError) as ei:
        st.update_local(1, "tones/nope.hsp")
    assert ei.value.status == 404


# -- the default projection (pinned via stubbed canon helpers) -------------
# ``_semantic_canon_*`` are module globals in ``session`` so patching on ``S``
# works; ``read_hsp`` / ``Library`` / ``default_library_path`` are imported
# locally inside ``_default_diff``, so they are patched at their source module.

def _stub_lib(monkeypatch):
    import helixgen.hsp as hspmod
    import helixgen.library as libmod
    monkeypatch.setattr(hspmod, "read_hsp", lambda p: {})
    monkeypatch.setattr(libmod, "Library", lambda p=None: object())
    monkeypatch.setattr(libmod, "default_library_path", lambda: "/x")


def _client():
    return type("C", (), {"get_content": lambda self, cid: b"blob"})()


def test_default_diff_same(monkeypatch):
    _stub_lib(monkeypatch)
    monkeypatch.setattr(S, "_semantic_canon_blob", lambda blob, lib: {"x": 1})
    monkeypatch.setattr(S, "_semantic_canon_body", lambda body, lib: {"x": 1})
    assert S._default_diff(_client(), 1, "/x.hsp") == "same"


def test_default_diff_changed(monkeypatch):
    _stub_lib(monkeypatch)
    monkeypatch.setattr(S, "_semantic_canon_blob", lambda blob, lib: {"x": 1})
    monkeypatch.setattr(S, "_semantic_canon_body", lambda body, lib: {"x": 2})
    assert S._default_diff(_client(), 1, "/x.hsp") == "changed"


def test_default_diff_indeterminate_when_transcode_raises(monkeypatch):
    _stub_lib(monkeypatch)

    def boom(*a, **k):
        raise ValueError("can't round-trip this structure")
    monkeypatch.setattr(S, "_semantic_canon_blob", boom)
    assert S._default_diff(_client(), 1, "/x.hsp") == "indeterminate"


def test_default_update_local_writes_in_place_and_commits(monkeypatch, tmp_path):
    hsp = tmp_path / "a.hsp"
    hsp.write_text("{}")
    import helixgen.cli_device as clidev
    import helixgen.device.untranscode as unt
    import helixgen.gitops as gitops
    import helixgen.hsp as hspmod
    import helixgen.library as libmod
    monkeypatch.setattr(libmod, "Library", lambda p=None: object())
    monkeypatch.setattr(libmod, "default_library_path", lambda: "/x")
    monkeypatch.setattr(clidev, "_preset_name_for_cid", lambda h, c: "Dev Name")
    monkeypatch.setattr(unt, "sbe_bytes_to_hsp",
                        lambda blob, name, library: {"meta": {"name": name}})
    written, committed = {}, {}
    monkeypatch.setattr(hspmod, "write_hsp",
                        lambda p, body: written.update(p=str(p), body=body))
    monkeypatch.setattr(gitops, "auto_commit",
                        lambda home, msg: committed.update(msg=msg))
    res = S._default_update_local(_client(), 5, hsp)
    assert res["name"] == "Dev Name"
    assert written["p"] == str(hsp)              # overwrote in place
    assert "update" in committed["msg"].lower()  # durability preserved
