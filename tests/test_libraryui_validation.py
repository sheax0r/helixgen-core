"""Review finding #1/#4: a wrong-typed JSON value must give a clean 400,
never a 500 from an uncaught AttributeError, and never a silent mis-placement
from a non-int position."""
from __future__ import annotations

import pytest

from helixgen.libraryui.session import LibraryError, LibrarySession


def _sess(offline=True):
    factory = None if offline else (lambda: object())
    return LibrarySession(factory, tone_loader=lambda: [],
                          guitar_loader=lambda: [], ir_local_loader=lambda: [])


@pytest.mark.parametrize("call", [
    lambda s: s.delete_ir(123),
    lambda s: s.delete_ir([1]),
    lambda s: s.rename_ir(123, "x"),
    lambda s: s.rename_ir("abc", 5),
    lambda s: s.setlist_create([1]),
    lambda s: s.setlist_rename(1, "x"),
    lambda s: s.setlist_delete(True),
    lambda s: s.setlist_duplicate("a", 2),
    lambda s: s.setlist_add(5),
    lambda s: s.setlist_remove("n", True),
    lambda s: s.setlist_move("n", "sl", "not-int"),
])
def test_wrongtyped_arg_is_400_not_500(call):
    with pytest.raises(LibraryError) as ei:
        call(_sess())
    assert ei.value.status == 400


def test_non_int_pos_on_copy_is_400():
    s = _sess(offline=False)  # factory set so we pass the offline gate
    with pytest.raises(LibraryError) as ei:
        s.copy_to_device("tones/whatever.hsp", pos="three")
    assert ei.value.status == 400


def test_non_int_pos_on_setlist_add_reference_is_400():
    s = _sess(offline=False)
    with pytest.raises(LibraryError) as ei:
        s.setlist_add("Band", cid=10, pos="three")
    assert ei.value.status == 400
