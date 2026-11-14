"""Task 3: reconcile actions — copy → device, pull ← device (with fakes)."""
from __future__ import annotations

import pytest

from helixgen.libraryui.session import LibraryError, LibrarySession
from helixgen.tone_meta import ToneMeta, Variant


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _meta(tmp_path, preset_name="Crunch - Prestige", write=True):
    hsp = tmp_path / "tones" / "a.hsp"
    hsp.parent.mkdir(parents=True, exist_ok=True)
    if write:
        hsp.write_text("{}")
    return ToneMeta(
        artist=None, song=None, descriptor="Crunch", tags=[],
        description_md=None,
        variants={"ibanez-prestige": Variant(hsp="tones/a.hsp",
                                              preset_name=preset_name)},
        created="2026-01-01", updated="2026-01-01")


def _session(tmp_path, *, metas=None, offline=False, copy_fn=None, pull_fn=None):
    kw = dict(tone_loader=lambda: (metas if metas is not None else []),
              guitar_loader=lambda: [], ir_local_loader=lambda: [],
              library_root=tmp_path)
    if copy_fn is not None:
        kw["copy_fn"] = copy_fn
    if pull_fn is not None:
        kw["pull_fn"] = pull_fn
    factory = None if offline else (lambda: _FakeClient())
    return LibrarySession(factory, **kw)


def test_copy_calls_device_copy_with_hsp(tmp_path):
    seen = {}

    def copy_fn(h, hsp_path, *, setlist, pos):
        seen.update(hsp=str(hsp_path), setlist=setlist, pos=pos)
        return {"ok": True, "action": "created", "name": "Crunch - Prestige"}

    st = _session(tmp_path, metas=[_meta(tmp_path)], copy_fn=copy_fn)
    res = st.copy_to_device("tones/a.hsp", to="Rock", pos=3)
    assert res["ok"] is True
    assert seen["hsp"].endswith("tones/a.hsp")
    assert seen["setlist"] == "Rock" and seen["pos"] == 3


def test_copy_missing_hsp_refused_before_push(tmp_path):
    called = []
    st = _session(tmp_path, metas=[_meta(tmp_path, write=False)],
                  copy_fn=lambda *a, **k: called.append(1) or {"ok": True})
    with pytest.raises(LibraryError) as ei:
        st.copy_to_device("tones/a.hsp")
    assert ei.value.status == 404
    assert not called  # nothing pushed


def test_copy_offline_refused(tmp_path):
    st = _session(tmp_path, metas=[_meta(tmp_path)], offline=True,
                  copy_fn=lambda *a, **k: {"ok": True})
    with pytest.raises(LibraryError) as ei:
        st.copy_to_device("tones/a.hsp")
    assert ei.value.status == 409


def test_ambiguous_device_name_refused(tmp_path):
    from helixgen.device.copy import AmbiguousName

    def copy_fn(h, hsp_path, *, setlist, pos):
        raise AmbiguousName("Crunch - Prestige",
                            [{"cid": 11}, {"cid": 22}])

    st = _session(tmp_path, metas=[_meta(tmp_path)], copy_fn=copy_fn)
    with pytest.raises(LibraryError) as ei:
        st.copy_to_device("tones/a.hsp")
    assert ei.value.status == 409
    assert "11" in str(ei.value) and "22" in str(ei.value)


def test_device_error_surfaced_not_crashing(tmp_path):
    def copy_fn(h, hsp_path, *, setlist, pos):
        raise OSError("device dropped")

    st = _session(tmp_path, metas=[_meta(tmp_path)], copy_fn=copy_fn)
    with pytest.raises(LibraryError) as ei:
        st.copy_to_device("tones/a.hsp")
    assert ei.value.status == 502
    # the session still serves state after a failed action
    assert st.public_state()["tones"][0]["name"] == "Crunch"


def test_pull_writes_hsp_and_registers(tmp_path):
    def pull_fn(h, cid, *, tones_dir):
        tones_dir.mkdir(parents=True, exist_ok=True)
        (tones_dir / "pulled.hsp").write_text("{}")
        return {"name": "Pulled Tone", "cid": cid}

    st = _session(tmp_path, pull_fn=pull_fn)
    res = st.pull_from_device(2037)
    assert res == {"name": "Pulled Tone", "cid": 2037}
    assert (tmp_path / "tones" / "pulled.hsp").exists()


def test_pull_bad_cid_refused(tmp_path):
    st = _session(tmp_path, pull_fn=lambda *a, **k: {"name": "x"})
    with pytest.raises(LibraryError) as ei:
        st.pull_from_device("not-a-number")
    assert ei.value.status == 400


def test_pull_collision_is_409(tmp_path):
    import click

    def pull_fn(h, cid, *, tones_dir):
        raise click.ClickException("a tone already exists at ...")

    st = _session(tmp_path, pull_fn=pull_fn)
    with pytest.raises(LibraryError) as ei:
        st.pull_from_device(2037)
    assert ei.value.status == 409
