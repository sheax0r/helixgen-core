"""Setlist/preset reorder — name resolution for ``/ReorderContainerContent``.

``/ReorderContainerContent [cmd, containerCID, msgpack[movedCIDs], newPos]``
(decoded 2026-07-14) moves item(s) already inside a container to a new
position. It works on the shapes helixgen cares about:

- **A setlist's preset references** — ``container`` is the setlist's cid;
  its items are ``cctp==1003`` *references* whose ``rcid`` points at the
  shared pool preset. The item that must appear in ``movedCIDs`` is the
  **reference's own cid**, not the pool preset's cid — a setlist can
  reference the same pool preset only once, so this is unambiguous, but it
  means resolving a preset **display name** within a setlist needs a
  reference → (via ``rcid``) → pool-preset-name join.
- **The pool (``-2``) itself** — its items are ``cctp==1000`` presets, which
  carry their own ``name`` directly; name resolution matches those too
  (address the pool by passing ``-2`` as the container cid).
- **The setlists root itself (``-5``)** — reordering the list of setlists.
  Its items are ``cctp==1001`` setlists, which carry their own ``name``
  directly (no indirection).

See ``docs/superpowers/specs/2026-07-14-parity-capture-findings.md`` §1/§9.

This module is the pure name-resolution layer (:func:`resolve_target_cid` is
unit-testable against plain listings — the ``maintenance.py``/
``setlist_sync.py`` pattern of separating planning from device I/O).
:meth:`HelixClient.reorder_container` is the wire primitive;
:func:`reorder_setlist_item` below is the thin device-driving orchestrator
the CLI (``device reorder``) and MCP (``device_reorder``) call.

**Relationship to the manifest-based ``device slots reorder``:** that verb
edits the LOCAL manifest's desired order for a tone-library setlist — it
takes effect on the device only via a later ``device sync``, and a
subsequent sync can reorder things right back to the manifest's recorded
order. This module's ``device reorder`` is the direct, immediate DEVICE-side
operation; it does not read or write the manifest at all.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from .client import Cctp, Container

#: The <setlist> keyword that redirects `device reorder` to reorder the
#: setlists themselves (container = the setlists root, -5) instead of the
#: preset references within one named setlist.
#:
#: CAVEAT: the keyword is checked before name resolution, so a real setlist
#: literally named "setlists" (any case) can never be addressed by name here
#: — pass its container cid as the <setlist> argument instead (a literal
#: integer is accepted and used as the container cid directly).
SETLISTS_ROOT_KEYWORD = "setlists"

_INT_RE = re.compile(r"-?\d+")


def _as_int_literal(s: str) -> Optional[int]:
    """``int(s)`` if ``s`` is a well-formed (optionally negative) integer
    literal, else ``None``. Stricter than ``lstrip('-').isdigit()`` — a
    malformed token like ``"--5"`` resolves by *name* (yielding the module's
    own "no ... named" error) instead of leaking a raw ``int()`` ValueError."""
    return int(s) if _INT_RE.fullmatch(s) else None


def _candidates_and_names(items: Sequence[dict], *, is_setlists_root: bool,
                          pool_names: Optional[Dict[Any, str]]):
    """The container's addressable items paired with their display names.

    - Setlists root: ``cctp==SETLIST`` items, named by their own ``name``.
    - Any other container: ``cctp==REFERENCE`` items (a setlist's entries),
      named via the reference → ``rcid`` → pool-preset-name join, **plus**
      ``cctp==PRESET`` items named by their own ``name`` — the latter is what
      makes reorder-by-name work against the pool (``-2``) itself, whose
      entries are presets, not references.
    """
    names = pool_names or {}
    out = []
    for m in items:
        cctp = m.get("cctp")
        if is_setlists_root:
            if cctp == Cctp.SETLIST:
                out.append((m, str(m.get("name", ""))))
        elif cctp == Cctp.REFERENCE:
            out.append((m, str(names.get(m.get("rcid"), ""))))
        elif cctp == Cctp.PRESET:
            out.append((m, str(m.get("name", ""))))
    return out


def resolve_target_cid(items: Sequence[dict], target: str, *,
                       is_setlists_root: bool,
                       pool_names: Optional[Dict[Any, str]] = None,
                       warnings: Optional[List[str]] = None) -> int:
    """Resolve ``target`` (a display name or a literal cid) to the item cid
    ``/ReorderContainerContent`` expects for this container.

    ``items`` is the container's own listing (``HelixClient.list_container``
    output — plain dicts, no device I/O here). A purely-digit (optionally
    signed) ``target`` is taken as a literal cid — **cid-first**: that's how
    a caller disambiguates or bypasses name lookup. When an item's display
    name is exactly that digit string with a *different* cid:

    - if an item with the literal cid IS present in the container (both
      readings verifiable), the cid reading wins deliberately and a warning
      naming the shadowed item is appended to ``warnings`` (when given);
    - if NO item carries the literal cid, the cid reading can only be a
      mistake, so a :class:`ValueError` points at the named item's real cid.

    - ``is_setlists_root=True``: ``items`` are matched by ``cctp==SETLIST``
      and their own ``name`` field.
    - ``is_setlists_root=False``: ``items`` are matched by ``cctp==REFERENCE``
      (a setlist's entries, display-named via ``pool_names`` — a ``{pool_cid:
      display_name}`` map, i.e. ``HelixClient.list_presets(Container.POOL)``
      keyed by ``cid_`` — joined on each reference's ``rcid``) and by
      ``cctp==PRESET`` (the pool ``-2``'s own entries, named directly).

    Raises :class:`ValueError` on no match, an ambiguous name match, or a
    numeric target that matches only a display name (never a present cid).
    """
    t = str(target).strip()
    pairs = _candidates_and_names(items, is_setlists_root=is_setlists_root,
                                  pool_names=pool_names)
    kind = "setlist" if is_setlists_root else "preset"
    lit = _as_int_literal(t)
    if lit is not None:
        # Cid-first with a numeric-display-name collision check: an item
        # literally NAMED this digit string (with a different cid) must never
        # be reordered silently by mistake — but when the literal cid itself
        # is present, the cid reading is well-defined and wins (raising here
        # would make the cid-carrying item unaddressable).
        clashes = [m for m, name in pairs
                   if name.casefold() == t.casefold() and m.get("cid_") != lit]
        cid_present = any(m.get("cid_") == lit for m in items)
        if clashes and cid_present:
            cids = ", ".join(str(m.get("cid_")) for m in clashes)
            if warnings is not None:
                warnings.append(
                    f"target {t!r} also matches the display name of {kind} "
                    f"cid {cids}; interpreting it as cid {lit} (cid-first) — "
                    f"pass {cids} to address the named item instead")
            return lit
        if clashes:
            cids = ", ".join(str(m.get("cid_")) for m in clashes)
            raise ValueError(
                f"no item in this container has cid {lit}, but a {kind} is "
                f"literally NAMED {t!r} (cid {cids}). A purely-numeric "
                f"target is always parsed as a cid, so pass that {kind}'s "
                f"real cid ({cids}) to address it.")
        return lit
    matches = [m for m, name in pairs if name.casefold() == t.casefold()]
    available = sorted({name or "?" for _m, name in pairs})
    if len(matches) == 1:
        return matches[0]["cid_"]
    if len(matches) > 1:
        cids = ", ".join(str(m.get("cid_")) for m in matches)
        raise ValueError(
            f"ambiguous {kind} name {target!r} (cids {cids}); pass the cid "
            "directly to disambiguate")
    raise ValueError(
        f"no {kind} named {target!r} found"
        + (f"; available: {', '.join(available)}" if available else ""))


def reorder_setlist_item(client, setlist: str, target: str,
                         to_index: int) -> Dict[str, Any]:
    """Resolve ``setlist``/``target`` against a live ``client`` and move
    ``target`` to ``to_index`` via ``/ReorderContainerContent``.

    ``setlist`` is a setlist display name (resolved via
    ``client.resolve_setlist_cid`` — the same real-setlist model
    ``device setlist create/rename/delete/duplicate`` use), the literal
    keyword ``"setlists"`` (case-insensitive) to instead reorder the
    top-level setlist list within the setlists root, or a literal integer to
    address a container by cid directly — cid-first, with the same
    collision policy as numeric *targets*: a setlist display-named that
    digit string is shadowed with a warning when the cid itself resolves,
    and raises with the named setlist's real cid when it doesn't (the
    escape hatch for a setlist whose display name the keyword shadows —
    see :data:`SETLISTS_ROOT_KEYWORD`).

    Returns ``{ok, container, moved_cid, new_pos, items, warnings}``.
    """
    warnings: List[str] = []
    s = str(setlist).strip()
    lit = _as_int_literal(s)
    if s.casefold() == SETLISTS_ROOT_KEYWORD:
        is_root = True
        container_cid = int(Container.SETLISTS_ROOT)
    elif lit is not None:
        container_cid = lit
        is_root = container_cid == int(Container.SETLISTS_ROOT)
        if not is_root:
            # Same (i)/(ii) collision policy as numeric targets, against the
            # real setlist listing: a setlist display-named this digit string
            # must never be silently shadowed by a cid that doesn't resolve.
            # STRICT (#39 audit): this listing gates which container the
            # subsequent write targets — a truncated read could hide a real
            # name clash and let the reorder silently land on the wrong
            # container.
            setlists = client.list_setlists(strict=True)
            # Same name-match as ``resolve_setlist_cid`` (shared #52 helper),
            # filtered over the single strict listing we already hold — the
            # cid_present membership scan below reuses that same listing.
            clashes = [m for m in client.list_setlists_by_name(s, setlists=setlists)
                       if m.get("cid_") != lit]
            cid_present = (
                lit == int(Container.POOL)
                or any(m.get("cid_") == lit for m in setlists))
            if clashes and cid_present:
                cids = ", ".join(str(m.get("cid_")) for m in clashes)
                warnings.append(
                    f"setlist argument {s!r} also matches the display name "
                    f"of setlist cid {cids}; interpreting it as container "
                    f"cid {lit} (cid-first) — pass {cids} to address the "
                    f"named setlist instead")
            elif clashes:
                cids = ", ".join(str(m.get("cid_")) for m in clashes)
                raise ValueError(
                    f"no setlist container has cid {lit}, but a setlist is "
                    f"literally NAMED {s!r} (cid {cids}). A purely-numeric "
                    f"setlist argument is always parsed as a container cid, "
                    f"so pass that setlist's real cid ({cids}) to address "
                    f"it.")
    else:
        is_root = False
        container_cid = client.resolve_setlist_cid(setlist)
        if container_cid is None:
            raise ValueError(
                f"no setlist named {setlist!r} on the device (create it "
                f"first with `helixgen device setlist create {setlist}`, or "
                "check `helixgen device setlists`)")
    # STRICT (#39 audit): this listing directly gates the reorder RPC below
    # (bounds check + which cid gets moved) — a truncated/partial read could
    # misresolve the target or accept an out-of-range position silently.
    items = client.list_container(container_cid, strict=True)
    # Bounds-check the destination against the fresh listing — how the device
    # handles an out-of-range newPos is uncharacterized, so refuse it here.
    n = len(items)
    if not 0 <= int(to_index) < max(n, 1):
        raise ValueError(
            f"--to {to_index} is out of range for container {container_cid} "
            f"({n} item(s); valid positions 0..{max(n - 1, 0)})")
    pool_names = None
    if not is_root:
        pool_names = {m.get("cid_"): m.get("name")
                      for m in client.list_presets(Container.POOL, strict=True)}
    moved_cid = resolve_target_cid(items, target, is_setlists_root=is_root,
                                   pool_names=pool_names, warnings=warnings)
    # A literal-cid target bypasses name lookup, but must still live in this
    # container — the device silently no-ops a reorder of an absent cid
    # (/status success, order unchanged; live-observed 2026-07-14), which
    # would otherwise read as a false success.
    if not any(m.get("cid_") == moved_cid for m in items):
        have = ", ".join(str(m.get("cid_")) for m in items) or "(empty)"
        raise ValueError(
            f"cid {moved_cid} is not in container {container_cid} "
            f"(its item cids: {have})")
    new_items = client.reorder_container(container_cid, [moved_cid], to_index)
    return {"ok": True, "container": container_cid, "moved_cid": moved_cid,
            "new_pos": int(to_index), "items": new_items,
            "warnings": warnings}
