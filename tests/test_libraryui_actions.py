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


def _session(tmp_path, *, metas=None, offline=False, copy_fn=None, pull_fn=None,
             import_fn=None):
    kw = dict(tone_loader=lambda: (metas if metas is not None else []),
              guitar_loader=lambda: [], ir_local_loader=lambda: [],
              library_root=tmp_path)
    if copy_fn is not None:
        kw["copy_fn"] = copy_fn
    if pull_fn is not None:
        kw["pull_fn"] = pull_fn
    if import_fn is not None:
        kw["import_fn"] = import_fn
    factory = None if offline else (lambda: _FakeClient())
    return LibrarySession(factory, **kw)


# -- import a .hsp FILE into the library (library import over an upload) -----

def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


_HSP_BYTES = b"rpshnosj" + b'{"meta": {"name": "Imported"}}'


def test_import_file_decodes_and_calls_import(tmp_path):
    seen = {}

    def import_fn(filename, data, *, guitar=None):
        seen.update(filename=filename, data=data, guitar=guitar)
        return {"name": filename}

    st = _session(tmp_path, import_fn=import_fn)
    out = st.import_file("Crunch.hsp", _b64(_HSP_BYTES), guitar="Tele")
    assert out == {"name": "Crunch.hsp"}
    assert seen["data"] == _HSP_BYTES and seen["guitar"] == "Tele"


def test_import_file_works_offline(tmp_path):
    # importing a FILE into the on-disk library needs no device.
    called = []
    st = _session(tmp_path, offline=True,
                  import_fn=lambda f, d, *, guitar=None: called.append(f) or {"name": f})
    st.import_file("x.hsp", _b64(_HSP_BYTES))
    assert called == ["x.hsp"]


def test_import_file_strips_client_path(tmp_path):
    seen = {}
    st = _session(tmp_path,
                  import_fn=lambda f, d, *, guitar=None: seen.update(f=f) or {"name": f})
    st.import_file("/etc/../sneaky/Evil.hsp", _b64(_HSP_BYTES))
    assert seen["f"] == "Evil.hsp"          # only the basename reaches the fn


def test_import_file_rejects_non_hsp(tmp_path):
    called = []
    st = _session(tmp_path, import_fn=lambda *a, **k: called.append(1))
    with pytest.raises(LibraryError) as ei:
        st.import_file("tone.hlx", _b64(_HSP_BYTES))
    assert ei.value.status == 400 and not called


def test_import_file_rejects_bad_base64(tmp_path):
    called = []
    st = _session(tmp_path, import_fn=lambda *a, **k: called.append(1))
    with pytest.raises(LibraryError) as ei:
        st.import_file("tone.hsp", "not base64!!!")
    assert ei.value.status == 400 and not called


def test_import_file_rejects_wrong_magic(tmp_path):
    called = []
    st = _session(tmp_path, import_fn=lambda *a, **k: called.append(1))
    with pytest.raises(LibraryError) as ei:
        st.import_file("tone.hsp", _b64(b"NOTMAGIC{}"))
    assert ei.value.status == 400 and not called


def test_import_file_collision_is_409(tmp_path):
    def import_fn(filename, data, *, guitar=None):
        import click
        raise click.ClickException("a tone with that name already exists")

    st = _session(tmp_path, import_fn=import_fn)
    with pytest.raises(LibraryError) as ei:
        st.import_file("dup.hsp", _b64(_HSP_BYTES))
    assert ei.value.status == 409


def test_default_import_file_lands_in_the_library(tmp_home):
    # exercise the REAL _default_import_file end to end: a valid .hsp uploaded
    # as base64 is placed in home.tones_dir() under the naming schema.
    from helixgen import home
    from helixgen.hsp import read_hsp, write_hsp

    src = tmp_home / "raw.hsp"
    write_hsp(src, {"meta": {"name": "Imported Tone"}, "preset": {"flow": []}})
    st = LibrarySession(None, tone_loader=lambda: [], guitar_loader=lambda: [],
                        ir_local_loader=lambda: [])
    out = st.import_file("raw.hsp", _b64(src.read_bytes()))
    assert out["name"] == "raw.hsp"
    landed = {read_hsp(f)["meta"]["name"]: f for f in home.tones_dir().glob("*.hsp")}
    assert "Imported Tone" in landed


def test_import_file_rejects_null_byte_name(tmp_path):
    # a NUL in the name would raise ValueError deep in write_bytes (502); it
    # must be a clean 400 at the boundary, before any fn runs.
    called = []
    st = _session(tmp_path, import_fn=lambda *a, **k: called.append(1))
    with pytest.raises(LibraryError) as ei:
        st.import_file("foo\x00.hsp", _b64(_HSP_BYTES))
    assert ei.value.status == 400 and not called


def test_default_import_file_rejects_non_object_body(tmp_home):
    # valid magic + valid JSON that isn't an object (a list) is "readable" to
    # read_hsp but is not a preset document -> clean 400, not a 502 leaking an
    # internal AttributeError from _resolve_import.
    st = LibrarySession(None, tone_loader=lambda: [], guitar_loader=lambda: [],
                        ir_local_loader=lambda: [])
    with pytest.raises(LibraryError) as ei:
        st.import_file("arr.hsp", _b64(b"rpshnosj[]"))
    assert ei.value.status == 400


def test_default_import_file_rejects_garbage_hsp(tmp_home):
    # bytes with the right magic but an unreadable body -> 400 (not imported).
    from helixgen import home

    st = LibrarySession(None, tone_loader=lambda: [], guitar_loader=lambda: [],
                        ir_local_loader=lambda: [])
    with pytest.raises(LibraryError) as ei:
        st.import_file("bad.hsp", _b64(b"rpshnosj" + b"\xff\xff not json"))
    assert ei.value.status == 400
    assert not list((home.tones_dir()).glob("*.hsp")) if home.tones_dir().is_dir() else True


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
