"""File-copy model: ``device copy`` / ``rm`` / ``move`` (2026-09-09 design).

Name-addressed upsert of an authored ``.hsp`` onto the device, plus the two
placement verbs that go with it. No manifest, no reconcile: the device is the
truth about what is loaded and in what order.

Everything here is a thin orchestration over machinery that already exists:

* :func:`helixgen.hsp.read_hsp` + :func:`transcode.hsp_to_sbepgsm` — the
  template-free ``.hsp`` -> ``_sbepgsm`` path ``device install``/``sync`` use.
* :func:`ir_upload.sync_preset_irs` — the shared per-tone IR-upload core
  (backlog #6) behind ``install --auto-irs`` and ``sync``. ``copy`` turns it
  ON by default: a copy that leaves a cab reading "No Model" is a broken copy.
* :meth:`HelixClient.install_into_pool` / ``reference_into_setlist`` /
  ``remove_reference`` / ``_raw.set_content_data`` — the pool+reference write
  surface, including sync's non-activating existing-cid content update.
* :func:`helixgen.device.reorder.reorder_setlist_item` — ``move`` is that verb
  with the target pre-resolved by name.

**Strictness (#40).** Every listing that gates a write is ``strict=True``: a
listing timeout aborts rather than reading as "slot empty". Post-write
bookkeeping listings are deliberately lenient (they can only under-report).

**Locks.** Device mutation runs inside :meth:`HelixClient.mutating` (the 2001
change-stream subscription). The machine-local advisory lease (``library``,
plus ``irs`` when uploading IRs) is the CLI's ``@_locked`` decorator's job, as
for every other engine module — this layer never takes one.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .client import Cctp, Container, HelixError


class AmbiguousName(Exception):
    """More than one preset answers to ``name`` in the scope searched.

    Carries ``.name`` and ``.candidates`` (``{cid, name, posi, setlist}``
    dicts). Never guessed past — the caller disambiguates with ``cid=``.
    """

    def __init__(self, name: str, candidates: List[Dict[str, Any]]):
        where = ", ".join(f"cid {c['cid']}" for c in candidates)
        super().__init__(
            f"ambiguous preset name {name!r} ({where}); pass --cid to pick one")
        self.name = name
        self.candidates = candidates


def _emit(progress: Optional[Callable[[dict], None]], stage: str, **kw) -> None:
    """Best-effort progress callback (plain dicts: ``{"stage": ..., ...}``)."""
    if progress is None:
        return
    try:
        progress({"stage": stage, **kw})
    except Exception:  # noqa: BLE001 - progress is decoration, never fatal
        pass


def _setlist_cid(client, setlist: str) -> int:
    cid = client.resolve_setlist_cid(setlist)
    if cid is None:
        raise ValueError(
            f"no setlist named {setlist!r} on the device (create it first "
            f"with `helixgen device setlist create {setlist}`, or check "
            "`helixgen device setlists`)")
    return int(cid)


def _scopes(client, setlist: Optional[str]):
    """``[(setlist_name_or_None, [candidate, ...]), ...]``, target setlist first.

    A candidate is the result shape :func:`resolve_target` returns. Both
    listings are strict (#40) — they gate every write in this module.
    """
    pool = client.list_presets(Container.POOL, strict=True)
    out = []
    if setlist is not None:
        by_cid = {m.get("cid_"): m for m in pool}
        refs = [m for m in client.list_container(_setlist_cid(client, setlist),
                                                 strict=True)
                if m.get("cctp") == Cctp.REFERENCE]
        out.append((setlist, [
            {"cid": m.get("cid_"), "pool_cid": m.get("rcid"),
             "name": str((by_cid.get(m.get("rcid")) or {}).get("name")
                         or m.get("name") or ""),
             "posi": m.get("posi")}
            for m in refs]))
    out.append((None, [
        {"cid": m.get("cid_"), "pool_cid": m.get("cid_"),
         "name": str(m.get("name") or ""), "posi": m.get("posi")}
        for m in pool]))
    return out


def resolve_target(client, name: str, *, setlist: Optional[str] = None,
                   cid: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Find the preset ``name`` refers to, or ``None`` if it is absent.

    Matches within ``setlist``'s references FIRST (case-insensitively, joining
    each reference's ``rcid`` to its pool preset's display name), then falls
    back to the pool. An explicit ``cid`` short-circuits name matching and
    selects by cid in the same scope order — it may be a reference cid or a
    pool cid.

    Returns ``{"cid", "pool_cid", "name", "posi"}``; raises
    :class:`AmbiguousName` when two or more entries match in a scope.
    """
    want = str(name).strip().casefold()
    for scope, cands in _scopes(client, setlist):
        if cid is not None:
            hits = [c for c in cands if c["cid"] == cid or c["pool_cid"] == cid]
        else:
            hits = [c for c in cands if c["name"].strip().casefold() == want]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise AmbiguousName(
                name, [dict(c, setlist=scope) for c in hits])
    if cid is not None:
        # An explicit --cid exists ONLY to disambiguate a duplicate name.
        # Falling through to None would read as "absent", and copy_tone would
        # CREATE a third preset — the opposite of what the flag is for. A cid
        # that matches nothing is a typo, and typos must not write.
        raise ValueError(
            f"--cid {cid} matches no preset"
            + (f" in setlist {setlist!r} or the pool" if setlist else " in the pool"))
    return None


def _upload_irs(client, body: dict, with_irs: bool, errors: List[str],
                progress) -> List[dict]:
    """Shared per-tone IR core (backlog #6). Per-IR failures never abort."""
    from . import ir_upload

    results = ir_upload.sync_preset_irs(client, body, client.ip,
                                        auto_irs=with_irs)
    for r in results:
        _emit(progress, "irs", label=r.get("name") or r.get("hash"),
              status="ok" if r.get("ok") else "error", detail=r.get("note"))
        if not r.get("ok") and r.get("outcome") != "skipped_auto_irs_off":
            errors.append(str(r.get("note") or r.get("outcome")))
    return results


def copy_tone(client, hsp_path, *, setlist: Optional[str] = None,
              pos: Optional[int] = None, with_irs: bool = True,
              cid: Optional[int] = None,
              progress: Optional[Callable[[dict], None]] = None
              ) -> Dict[str, Any]:
    """Upsert an authored ``.hsp`` onto the device under its ``meta.name``.

    Already present in the target (setlist references first, then the pool)?
    Its content is updated IN PLACE via ``/SetContentData`` on the existing
    cid — the non-activating update ``sync`` uses, so the active tone is
    untouched. Absent? The content is installed into the POOL and, when
    ``setlist`` is given, a REFERENCE is added at ``pos`` (appended — the
    lowest empty position — when ``pos`` is ``None``). A pool preset that
    exists but is not yet referenced is updated *and* referenced.

    ``setlist=None`` targets the pool only, with no setlist reference.
    ``with_irs`` (default True) uploads the tone's referenced-but-missing IRs
    first. ``cid`` picks the target explicitly, bypassing name resolution.

    Returns ``{ok, action, name, cid, pool_cid, posi, setlist, irs, errors}``;
    ``ok`` reports the PRESET write (an IR that failed to upload lands in
    ``errors`` without aborting the copy). Raises only for input the caller must
    fix before retrying: :class:`AmbiguousName` for a name that matches twice,
    ``ValueError`` for an unreadable/nameless ``.hsp``. Device and setlist
    resolution failures come back as ``ok: False`` with ``errors``.
    """
    from helixgen.hsp import read_hsp

    from . import bridge, transcode

    body = read_hsp(hsp_path)
    name = str((body.get("meta") or {}).get("name") or "").strip()
    if not name:
        raise ValueError(f"{hsp_path}: .hsp has no meta.name to copy it under")

    errors: List[str] = []
    res: Dict[str, Any] = {"ok": False, "action": None, "name": name,
                           "cid": None, "pool_cid": None, "posi": None,
                           "setlist": setlist, "irs": [], "errors": errors}
    with client.mutating():
        # AmbiguousName propagates (the caller must disambiguate); every other
        # device/authoring failure lands in errors[] so the CLI gets a result.
        try:
            target = resolve_target(client, name, setlist=setlist, cid=cid)
            res["irs"] = _upload_irs(client, body, with_irs, errors, progress)
            blob = transcode.hsp_to_sbepgsm(body, strict=True)
            # a reference is needed unless the tone is already one; plan its
            # position before any write (see _plan_ref).
            needs_ref = setlist is not None and (
                target is None or target["cid"] == target["pool_cid"])
            sl_cid, ref_pos = (_plan_ref(client, setlist, pos) if needs_ref
                               else (None, None))
            if target is not None:
                _emit(progress, "update", label=name)
                res.update(action="updated", cid=target["cid"],
                           pool_cid=target["pool_cid"], posi=target["posi"])
                if not client._raw.set_content_data(target["pool_cid"], blob):
                    raise HelixError(
                        f"device refused the content update for {name!r} "
                        f"(cid {target['pool_cid']})")
                if needs_ref:
                    # in the pool but not referenced by the target setlist:
                    # complete the upsert rather than pooling a duplicate
                    # under the same name.
                    res.update(**_reference(client, sl_cid, setlist,
                                            target["pool_cid"], ref_pos))
            else:
                _emit(progress, "install", label=name)
                res["action"] = "created"
                pool_cid = client.install_into_pool(blob, name)
                if pool_cid is None:
                    raise HelixError(f"install of {name!r} returned no cid")
                res.update(cid=pool_cid, pool_cid=pool_cid,
                           posi=_pool_posi(client, pool_cid))
                if needs_ref:
                    res.update(**_reference(client, sl_cid, setlist,
                                            pool_cid, ref_pos))
            res["ok"] = True
        except (bridge.UnresolvedModel, ValueError, HelixError, OSError) as e:
            errors.append(str(e))
            _emit(progress, res["action"] or "install", label=name,
                  status="error", detail=str(e))
    return res


def _pool_posi(client, pool_cid: int) -> Optional[int]:
    """The new preset's pool slot. Bookkeeping-only, so deliberately lenient."""
    return next((m.get("posi") for m in client.list_presets(Container.POOL)
                 if m.get("cid_") == pool_cid), None)


def _plan_ref(client, setlist: str, pos: Optional[int]):
    """Resolve ``(setlist_cid, pos)`` for a reference about to be added —
    BEFORE anything is written, so an occupied position never leaves an
    orphaned pool preset behind.

    Both listings are strict (#40): an occupied position is refused rather
    than stacking a second reference there (uncataloged device behavior —
    backlog #69), and a listing timeout must never read as "position free".
    """
    sl_cid = _setlist_cid(client, setlist)
    if pos is None:
        pos = client._lowest_empty_posi(sl_cid)
    elif client.find_by_pos(sl_cid, pos, strict=True) is not None:
        raise HelixError(
            f"setlist {setlist!r} position {pos} is not empty; remove the "
            f"incumbent first (`helixgen device rm ... --from {setlist}`)")
    return sl_cid, pos


def _reference(client, sl_cid: int, setlist: str, pool_cid: int,
               pos: int) -> Dict[str, Any]:
    """Add the planned reference. Returns ``{cid, posi}`` for the result."""
    ref_cid = client.reference_into_setlist(sl_cid, pool_cid, pos)
    if ref_cid is None:
        raise HelixError(
            f"pool cid {pool_cid} is installed but could not be referenced "
            f"into setlist {setlist!r} at {pos}")
    return {"cid": ref_cid, "posi": pos}


def _other_setlists_referencing(client, pool_cid: int, *,
                                exclude_setlist: str) -> List[str]:
    """Names of setlists (other than ``exclude_setlist``) referencing ``pool_cid``.

    Guards ``rm --also-pool`` against orphaning: deleting a pool preset another
    setlist still points at leaves that setlist referencing a dead cid. The
    listing is strict (#40) — an unreadable setlist must block the delete, not
    read as "nobody references it".
    """
    want = (exclude_setlist or "").strip().casefold()
    holders: List[str] = []
    for sl in client.list_setlists(strict=True):
        name = str(sl.get("name") or "")
        if not name or name.casefold() == want:
            continue
        for m in client.list_container(sl.get("cid_"), strict=True):
            if m.get("cctp") == Cctp.REFERENCE and m.get("rcid") == pool_cid:
                holders.append(name)
                break
    return holders


def remove_tone(client, name: str, *, setlist: str, also_pool: bool = False,
                cid: Optional[int] = None) -> Dict[str, Any]:
    """Drop ``name``'s reference from ``setlist``; the pool preset survives
    unless ``also_pool``.

    Returns ``{ok, name, removed_ref, removed_pool, errors}``. Raises
    :class:`AmbiguousName` for a name that matches twice.
    """
    errors: List[str] = []
    res: Dict[str, Any] = {"ok": False, "name": name, "removed_ref": None,
                           "removed_pool": None, "errors": errors}
    with client.mutating():
        try:
            target = resolve_target(client, name, setlist=setlist, cid=cid)
            if target is None:
                errors.append(
                    f"no preset named {name!r} in setlist {setlist!r} or the "
                    "pool")
                return res
            sl_cid = _setlist_cid(client, setlist)
            if target["cid"] != target["pool_cid"]:  # a reference; drop it
                if client.remove_reference(sl_cid, target["cid"]):
                    res["removed_ref"] = target["cid"]
                else:
                    errors.append(
                        f"device refused to remove reference cid "
                        f"{target['cid']} from setlist {setlist!r}")
            if also_pool:
                # NEVER orphan: another setlist referencing this pool preset
                # would be left pointing at a dead cid. The reference we just
                # dropped is excluded from the scan.
                holders = _other_setlists_referencing(
                    client, target["pool_cid"], exclude_setlist=setlist)
                if holders:
                    errors.append(
                        f"refusing to delete pool preset {target['name']!r} "
                        f"(cid {target['pool_cid']}): still referenced by "
                        + ", ".join(repr(h) for h in sorted(holders))
                        + " — remove it there first")
                elif client._raw.delete(int(Container.POOL),
                                        [target["pool_cid"]]):
                    res["removed_pool"] = target["pool_cid"]
                else:
                    errors.append(
                        f"device refused to delete pool cid "
                        f"{target['pool_cid']}")
            res["ok"] = not errors
        except (ValueError, HelixError, OSError) as e:
            errors.append(str(e))
    return res


def move_tone(client, name: str, *, setlist: str, to: int,
              cid: Optional[int] = None) -> Dict[str, Any]:
    """Move ``name`` to position ``to`` within ``setlist``'s reference order.

    Resolves the name here (so ambiguity is an error, never a guess) and hands
    the resolved reference cid to :func:`reorder.reorder_setlist_item` — the
    existing ``/ReorderContainerContent`` orchestrator, cid-first.

    Returns ``{ok, name, from, to, errors}``.
    """
    from . import reorder

    errors: List[str] = []
    res: Dict[str, Any] = {"ok": False, "name": name, "from": None,
                           "to": int(to), "errors": errors}
    with client.mutating():
        try:
            target = resolve_target(client, name, setlist=setlist, cid=cid)
            if target is None or target["cid"] == target["pool_cid"]:
                errors.append(
                    f"setlist {setlist!r} has no preset named {name!r} "
                    "(only presets referenced by the setlist can be moved)")
                return res
            res["from"] = target["posi"]
            reorder.reorder_setlist_item(client, setlist, str(target["cid"]),
                                         int(to))
            res["ok"] = True
        except (ValueError, HelixError, OSError) as e:
            errors.append(str(e))
    return res


__all__ = ["AmbiguousName", "resolve_target", "copy_tone", "remove_tone",
           "move_tone"]
