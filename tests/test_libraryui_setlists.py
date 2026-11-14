"""Task 4: setlist management — CRUD + membership (with a fake client)."""
from __future__ import annotations

import pytest

from helixgen.libraryui.session import LibraryError, LibrarySession


class _FakeClient:
    """Minimal setlist-aware stand-in. ``calls`` records what was invoked."""

    def __init__(self, setlists=None, occupied=None):
        # name(casefold) -> cid
        self._sl = {k.casefold(): v for k, v in (setlists or {}).items()}
        self._occupied = occupied or set()  # {(container_cid, pos)}
        self._next = 9000
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def resolve_setlist_cid(self, name):
        return self._sl.get((name or "").casefold())

    def create_setlist(self, name):
        self._next += 1
        self._sl[name.casefold()] = self._next
        self.calls.append(("create", name, self._next))
        return self._next

    def rename(self, cid, new_name):
        self.calls.append(("rename", cid, new_name))
        return True

    def delete_setlist(self, cid):
        self.calls.append(("delete", cid))
        return True

    def duplicate_setlist_refs(self, src_cid, dst_cid):
        self.calls.append(("duplicate", src_cid, dst_cid))
        return 3

    def find_by_pos(self, container, pos, strict=False):
        return object() if (container, pos) in self._occupied else None

    def reference_into_setlist(self, container, src_cid, pos):
        self._next += 1
        self.calls.append(("reference", container, src_cid, pos))
        return self._next


def _session(client=None, *, offline=False, remove_fn=None, move_fn=None,
             copy_fn=None):
    kw = dict(tone_loader=lambda: [], guitar_loader=lambda: [],
              ir_local_loader=lambda: [])
    for k, v in (("remove_fn", remove_fn), ("move_fn", move_fn),
                 ("copy_fn", copy_fn)):
        if v is not None:
            kw[k] = v
    factory = None if offline else (lambda: client)
    return LibrarySession(factory, **kw)


def test_create_calls_through():
    c = _FakeClient()
    res = _session(c).setlist_create("Encore")
    assert res["name"] == "Encore"
    assert ("create", "Encore", res["cid"]) in c.calls


def test_create_duplicate_name_refused():
    c = _FakeClient(setlists={"Mike": 10})
    with pytest.raises(LibraryError) as ei:
        _session(c).setlist_create("mike")  # case-insensitive clash
    assert ei.value.status == 409


def test_rename_missing_is_404():
    c = _FakeClient()
    with pytest.raises(LibraryError) as ei:
        _session(c).setlist_rename("Nope", "Later")
    assert ei.value.status == 404


def test_rename_onto_existing_refused():
    c = _FakeClient(setlists={"A": 1, "B": 2})
    with pytest.raises(LibraryError) as ei:
        _session(c).setlist_rename("A", "B")
    assert ei.value.status == 409


def test_delete_calls_through():
    c = _FakeClient(setlists={"Gone": 7})
    _session(c).setlist_delete("Gone")
    assert ("delete", 7) in c.calls


def test_duplicate_creates_dst_when_absent():
    c = _FakeClient(setlists={"Src": 5})
    res = _session(c).setlist_duplicate("Src", "Dst")
    assert res["copied"] == 3
    assert any(x[0] == "create" and x[1] == "Dst" for x in c.calls)
    assert any(x[0] == "duplicate" and x[1] == 5 for x in c.calls)


def test_add_reference_pool_only_preset():
    c = _FakeClient(setlists={"Band": 3})
    res = _session(c).setlist_add("Band", cid=1868, pos=4)
    assert res["pool_cid"] == 1868 and res["pos"] == 4
    assert ("reference", 3, 1868, 4) in c.calls


def test_add_reference_occupied_pos_refused():
    c = _FakeClient(setlists={"Band": 3}, occupied={(3, 4)})
    with pytest.raises(LibraryError) as ei:
        _session(c).setlist_add("Band", cid=1868, pos=4)
    assert ei.value.status == 409
    assert not any(x[0] == "reference" for x in c.calls)  # nothing stacked


def test_add_reference_needs_pos():
    c = _FakeClient(setlists={"Band": 3})
    with pytest.raises(LibraryError) as ei:
        _session(c).setlist_add("Band", cid=1868)
    assert ei.value.status == 400


def test_add_authored_tone_delegates_to_copy():
    seen = {}

    def copy_fn(h, hsp_path, *, setlist, pos):
        seen.update(setlist=setlist, pos=pos)
        return {"ok": True, "name": "X"}

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        hsp = Path(td) / "x.hsp"
        hsp.write_text("{}")
        st = LibrarySession(lambda: _FakeClient(setlists={"Band": 3}),
                            tone_loader=lambda: [], guitar_loader=lambda: [],
                            ir_local_loader=lambda: [], copy_fn=copy_fn,
                            library_root=Path(td))
        st.setlist_add("Band", hsp="x.hsp", pos=2)
    assert seen == {"setlist": "Band", "pos": 2}


def test_remove_delegates_to_remove_fn():
    seen = {}

    def remove_fn(h, name, *, setlist, also_pool, cid):
        seen.update(name=name, setlist=setlist, also_pool=also_pool)
        return {"ok": True, "name": name}

    _session(_FakeClient(), remove_fn=remove_fn).setlist_remove(
        "Dream On", "Mike", also_pool=True)
    assert seen == {"name": "Dream On", "setlist": "Mike", "also_pool": True}


def test_move_delegates_to_move_fn():
    seen = {}

    def move_fn(h, name, *, setlist, to, cid):
        seen.update(name=name, setlist=setlist, to=to)
        return {"ok": True}

    _session(_FakeClient(), move_fn=move_fn).setlist_move("Dream On", "Mike", 5)
    assert seen == {"name": "Dream On", "setlist": "Mike", "to": 5}


def test_offline_refused():
    with pytest.raises(LibraryError) as ei:
        _session(offline=True).setlist_create("X")
    assert ei.value.status == 409
