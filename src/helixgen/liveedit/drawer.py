"""Library drawer for ``device live``: the library hub's actions + read model,
mounted on a dedicated device client (not the live editor's)."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from helixgen.libraryui.session import LibraryError, LibrarySession


class _NonClosing:
    """Context manager handing out a persistent client; never closes it."""

    def __init__(self, client: Any):
        self._client = client

    def __enter__(self):
        return self._client

    def __exit__(self, *exc):
        return False


# action name -> callable(session, payload)
_ACTIONS: Dict[str, Callable[[LibrarySession, dict], dict]] = {
    "copy": lambda s, p: s.copy_to_device(p.get("hsp"), to=p.get("to"),
                                          pos=p.get("pos")),
    "pull": lambda s, p: s.pull_from_device(p.get("cid")),
    "content_diff": lambda s, p: s.content_diff(p.get("cid"), p.get("hsp")),
    "update_local": lambda s, p: s.update_local(p.get("cid"), p.get("hsp")),
    "setlist_create": lambda s, p: s.setlist_create(p.get("name")),
    "setlist_rename": lambda s, p: s.setlist_rename(p.get("name"),
                                                    p.get("new_name")),
    "setlist_delete": lambda s, p: s.setlist_delete(p.get("name")),
    "setlist_duplicate": lambda s, p: s.setlist_duplicate(p.get("src"),
                                                          p.get("dst")),
    "setlist_add": lambda s, p: s.setlist_add(
        p.get("to"), hsp=p.get("hsp"), cid=p.get("cid"), pos=p.get("pos")),
    "setlist_remove": lambda s, p: s.setlist_remove(
        p.get("name"), p.get("setlist"), also_pool=bool(p.get("also_pool")),
        cid=p.get("cid")),
    "setlist_move": lambda s, p: s.setlist_move(
        p.get("name"), p.get("setlist"), p.get("to"), cid=p.get("cid")),
    "push_ir": lambda s, p: s.push_ir(p.get("wav")),
    "delete_ir": lambda s, p: s.delete_ir(p.get("name_or_hash")),
    "rename_ir": lambda s, p: s.rename_ir(p.get("name_or_hash"),
                                          p.get("new_name")),
}


class LibraryDrawer:
    """``client_factory`` returns the dedicated library client (or is None for
    offline). ``irs_root`` is accepted for the caller's convenience but
    LibrarySession has no such param. ``cache`` is stored for later tasks.
    Extra kwargs (loaders, ``*_fn`` seams) pass through to LibrarySession."""

    def __init__(self, library_root, irs_root=None, *,
                 client_factory: Optional[Callable[[], Any]] = None,
                 cache: Any = None, **session_kw):
        self.irs_root = irs_root
        self.cache = cache
        self._session = LibrarySession(
            (lambda: _NonClosing(client_factory())) if client_factory else None,
            library_root=library_root, **session_kw)

    def public_state(self) -> dict:
        return self._session.public_state()

    def action(self, name: str, payload: dict) -> dict:
        fn = _ACTIONS.get(name)
        if fn is None:
            raise LibraryError(f"unknown library action {name!r}", status=404)
        return fn(self._session, payload or {})

    def apply_event(self, ev) -> Optional[dict]:
        return None  # filled in by a later task
