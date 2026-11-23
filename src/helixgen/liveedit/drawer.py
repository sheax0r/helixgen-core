"""Library drawer for ``device live``: the library hub's actions + read model,
mounted on a dedicated device client (not the live editor's)."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
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
    "delete_ir": lambda s, p: s.delete_ir(p.get("name_or_hash"),
                                          force=bool(p.get("force"))),
    "rename_ir": lambda s, p: s.rename_ir(p.get("name_or_hash"),
                                          p.get("new_name")),
    "ir_usage": lambda s, p: s.ir_usage(p.get("name_or_hash")),
    "remove_tone_device": lambda s, p: s.remove_tone_device(p.get("cid")),
    "import_file": lambda s, p: s.import_file(p.get("filename"),
                                              p.get("content_b64"),
                                              guitar=p.get("guitar")),
}

# The advisory device-lock scopes each action needs, matching the CLI verbs'
# @_locked decorators (helixgen.locks scopes). The page takes these TRANSIENTLY
# per write — it does NOT sit on library+irs for its whole life, which would
# lock out an agent pushing tones while the page is open. Actions absent here
# touch no device library/IR state (reads, or LOCAL-only writes like `pull` /
# `update_local`) and so take no lease.
ACTION_SCOPES: Dict[str, tuple] = {
    "copy": ("library", "irs"),
    "setlist_create": ("library",),
    "setlist_rename": ("library",),
    "setlist_delete": ("library",),
    "setlist_duplicate": ("library",),
    "setlist_add": ("library", "irs"),   # hsp path copies a tone in, IRs and all
    "setlist_remove": ("library",),
    "setlist_move": ("library",),
    "push_ir": ("irs",),
    "delete_ir": ("irs",),
    "rename_ir": ("irs",),
    "remove_tone_device": ("library",),   # drops pool preset + setlist refs
    # ir_usage is a read-only sweep -> no lease (like content_diff).
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
        self._library_root = library_root
        self._known: set = set()  # device cids we already show (echo guard)
        self._confirmed: set = set()  # cids re-diffed THIS connect; only these
        # show a non-provisional badge — a cached verdict is display state, not
        # confirmed truth (the device may have drifted while the app was closed,
        # which no library-side hash can detect). begin_reconcile() clears this.
        self._session = LibrarySession(
            (lambda: _NonClosing(client_factory())) if client_factory else None,
            library_root=library_root, **session_kw)

    def connect(self, client_factory: Callable[[], Any],
                cache: Any = None) -> None:
        """Wire the dedicated library client + diff cache once a device is
        found (offline cold-start builds the drawer with neither). The factory
        is wrapped the same way the constructor wraps it, so actions never
        close the shared client."""
        self.cache = cache
        self._session._client_factory = lambda: _NonClosing(client_factory())

    def _hsp_hash(self, hsp) -> Optional[str]:
        """sha256 of the library .hsp file, the cache's library-side key."""
        if not hsp:
            return None
        p = Path(hsp)
        if not p.is_absolute() and self._library_root:
            p = Path(self._library_root) / hsp
        try:
            return hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            return None

    def public_state(self) -> dict:
        """The hub read model, with each on-both variant stamped a ``diff``
        badge. The badge is the cache's verdict ONLY for a cid re-confirmed
        this connect; otherwise ``verifying`` — a stale cross-session verdict is
        never shown as confirmed truth (spec anti-goal: never lie about the
        device). The background reconcile confirms them on each connect."""
        st = self._session.public_state()
        for tone in st.get("tones", []):
            for v in tone.get("variants", []):
                cid = v.get("device_cid")
                if cid is not None:
                    self._known.add(cid)
                if v.get("presence") != "both":
                    continue
                h = self._hsp_hash(v.get("hsp"))
                if not h:
                    v["diff"] = "indeterminate"
                elif cid in self._confirmed and self.cache is not None:
                    v["diff"] = self.cache.verdict(str(cid), h) or "verifying"
                else:
                    v["diff"] = "verifying"
        return st

    def begin_reconcile(self) -> None:
        """A (re)connect re-verifies every on-both preset this session: forget
        prior confirmations so each badge reverts to ``verifying`` until the
        reconcile reconfirms it against the live device."""
        self._confirmed.clear()

    def _hsp_for_cid(self, cid):
        for tone in self._session.public_state().get("tones", []):
            for v in tone.get("variants", []):
                if v.get("device_cid") == cid:
                    return v.get("hsp")
        return None

    def resolve_diff(self, cid, hsp=None) -> str:
        """Compute one preset's verdict (``same``/``changed``/``indeterminate``)
        via the inner session's ``content_diff`` and put it in the cache keyed
        by the library .hsp hash. ``hsp`` is an optional hint so the reconcile
        need not re-read the device to map ``cid`` → ``.hsp``. Persisting the
        cache to disk is the caller's job (the reconcile saves once)."""
        if hsp is None:
            hsp = self._hsp_for_cid(cid)
        if not hsp:
            return "indeterminate"
        state = self._session.content_diff(cid, hsp).get("state", "indeterminate")
        h = self._hsp_hash(hsp)
        if self.cache is not None and h:
            self.cache.put(str(cid),
                           {"hsp_hash": h, "verdict": state, "ts": time.time()})
        self._confirmed.add(cid)  # confirmed against the live device this connect
        return state

    def action(self, name: str, payload: dict) -> dict:
        fn = _ACTIONS.get(name)
        if fn is None:
            raise LibraryError(f"unknown library action {name!r}", status=404)
        return fn(self._session, payload or {})

    def apply_event(self, ce) -> Optional[dict]:
        """React to a decoded content-store :class:`ContentEvent`. Returns a
        drawer-state delta for the page (or ``None`` for an echo / irrelevant
        event). A ``save`` invalidates that cid's cached verdict so the badge
        re-verifies; the caller schedules a ``resolve_diff`` on the library
        client to settle it."""
        if ce is None:
            return None
        if ce.kind == "save":
            if self.cache is not None and ce.cid is not None:
                self.cache.invalidate(str(ce.cid))
            self._confirmed.discard(ce.cid)  # back to provisional until re-diffed
            return {"kind": "badge", "cid": ce.cid, "diff": "verifying"}
        if ce.kind in ("add", "update"):
            if ce.kind == "add" and ce.cid in self._known:
                return None  # echo of a row we already show
            if ce.cid is not None:
                self._known.add(ce.cid)
            return {"kind": ce.kind, "row": ce.row}
        if ce.kind == "remove":
            for c in (ce.cids or []):
                self._known.discard(c)
            return {"kind": "remove", "cids": list(ce.cids or [])}
        if ce.kind == "setlist_members":
            return {"kind": "setlist_members", "setlist_cid": ce.container_cid,
                    "members": ce.members}
        if ce.kind == "load":
            return {"kind": "active", "cid": ce.cid}
        return None
