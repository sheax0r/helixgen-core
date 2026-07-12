"""Mirror a directory of authored ``.hsp`` tones onto a device setlist.

There is no on-disk manifest of a user's authored tones — they are loose
``.hsp`` files (typically in ``preset_output_dir``). :func:`sync_library`
**mirrors** that directory onto the target setlist (default ``user``): it
deletes every preset already in the setlist and installs the library fresh, so
the setlist ends up holding exactly the library's tones and nothing else. It
uploads each tone's referenced IRs (instant, via the 2001-subscription
``push_ir``) unless excluded and records every placement in the slot ledger.

The library on disk is the source of truth; **only the target setlist is
touched**, no backup is taken, and — as a guardrail — if the library has no
installable tone (empty dir, or every ``.hsp`` unreadable) nothing is deleted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from helixgen.hsp import read_hsp

from . import bridge
from .client import FACTORY, THROWAWAY, USER, HelixClient, HelixError, slot_label
from .ledger import SlotLedger

# A Stadium setlist holds 128 preset slots (posi 0..127; label 1A..32D).
SETLIST_CAPACITY = 128

_SETLIST_CONTAINERS = {"user": USER, "factory": FACTORY, "throwaway": THROWAWAY}


def setlist_container(name: str) -> int:
    """Map a setlist name (``user``/``factory``/``throwaway``) to its container."""
    try:
        return _SETLIST_CONTAINERS[name]
    except KeyError:
        raise HelixError(
            f"unknown setlist {name!r} (expected one of {sorted(_SETLIST_CONTAINERS)})"
        )


def _preset_name(body: dict, fallback: str) -> str:
    name = ((body.get("meta") or {}).get("name") or "").strip()
    return name or fallback


def _cid(preset: dict):
    """Device presets carry the content id as ``cid_`` (or ``cid``)."""
    return preset.get("cid", preset.get("cid_"))


def _empty_positions(occupied: set, count: int) -> List[int]:
    """Lowest ``count`` empty slot positions in a setlist (fill-empty order)."""
    out: List[int] = []
    pos = 0
    while len(out) < count and pos < SETLIST_CAPACITY:
        if pos not in occupied:
            out.append(pos)
        pos += 1
    return out


def _upload_missing_irs(ip: str, hashes: List[str]) -> List[dict]:
    """Resolve each missing irhash to a local WAV and push it (instant register).
    Returns one result dict per hash: ``{hash, ok, hash_match?, note?}``."""
    from helixgen.ir import IrMapping

    from . import sftp

    try:
        irmap = IrMapping.load()
    except Exception as e:  # noqa: BLE001 - surface as per-IR notes, don't abort
        return [{"hash": h, "ok": False,
                 "note": f"no local mapping.json ({e})"} for h in hashes]

    results: List[dict] = []
    for hh in hashes:
        try:
            path = irmap.resolve_by_hash(hh)
        except Exception:  # noqa: BLE001 - not registered locally
            results.append({"hash": hh, "ok": False,
                            "note": "not found locally (helixgen register-irs)"})
            continue
        try:
            res = sftp.push_ir(ip, str(path))
        except HelixError as e:
            results.append({"hash": hh, "ok": False, "note": str(e)})
            continue
        results.append({"hash": hh,
                        "ok": bool(res.get("registered")) or bool(res.get("already")),
                        "hash_match": res.get("hash_match"),
                        "name": res.get("name")})
    return results


def sync_library(
    directory: str | Path,
    *,
    ip: str = "192.168.4.84",
    port: Optional[int] = None,
    setlist: str = "user",
    exclude_irs: bool = False,
    template_cid: Optional[int] = None,
) -> Dict[str, Any]:
    """Mirror ``directory``'s ``.hsp`` tones onto the device ``setlist``.

    **Destructive.** The target setlist (default ``user``) is made to match the
    library exactly — the library on disk is the source of truth:

    - **Deletes every preset currently in the setlist** (managed or not), then
      installs each readable ``.hsp`` fresh into empty slots (arbitrary order).
      Overwriting a tone == delete + reinstall, so the setlist ends up holding
      exactly the library's tones and nothing else.
    - Uploads each tone's referenced IRs first (unless ``exclude_irs``); IRs
      register instantly via ``push_ir``.
    - Replaces this setlist's ledger entries with the new placements.
    - **Only the target setlist is touched**; other setlists are never modified.
    - **No backup is taken.** As a guardrail, if the library has no installable
      tone (empty dir, or every ``.hsp`` unreadable) nothing is deleted.

    Returns ``{ok, setlist, directory, deleted:[...], installed:[...],
    errors:[...]}``. Each ``deleted`` entry is ``{name, cid, slot}``;
    ``installed`` is ``{file, name, pos, slot, cid, irs}``; ``errors`` is
    ``{file, name?, error}``.
    """
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        raise HelixError(f"not a directory: {directory}")
    hsp_files = sorted(directory.glob("*.hsp"))

    result: Dict[str, Any] = {
        "ok": True, "setlist": setlist, "directory": str(directory),
        "deleted": [], "installed": [], "errors": [],
    }
    if not hsp_files:
        result["note"] = (f"no .hsp files in {directory} — nothing to mirror; "
                          f"device left untouched")
        return result

    container = setlist_container(setlist)
    ledger = SlotLedger.load()

    client_kwargs: Dict[str, Any] = {"ip": ip}
    if port is not None:
        client_kwargs["port"] = port
    with HelixClient(**client_kwargs) as client:
        # Read + validate every library tone up front. If NONE are installable,
        # bail before deleting anything — never let an empty/broken library wipe
        # the device.
        pending: List[tuple] = []  # (path, body, name)
        for f in hsp_files:
            try:
                body = read_hsp(f)
            except Exception as e:  # noqa: BLE001
                result["errors"].append({"file": f.name, "error": f"read failed: {e}"})
                continue
            pending.append((f, body, _preset_name(body, f.stem)))

        if not pending:
            result["note"] = "no readable .hsp tones — device left untouched"
            result["ok"] = False
            return result

        # 1. Delete everything currently in the setlist (mirror: library wins).
        existing = [m for m in client.list_container(container)
                    if _cid(m) is not None]
        if existing:
            cids = [_cid(m) for m in existing]
            ok = client.delete(container, cids)
            for m in existing:
                posi = m.get("posi")
                if ok:
                    result["deleted"].append({
                        "name": m.get("name"), "cid": _cid(m),
                        "slot": slot_label(posi) if posi is not None else None,
                    })
                    ledger.remove(cid=_cid(m))
                else:
                    result["errors"].append(
                        {"name": m.get("name"),
                         "error": f"delete failed for cid {_cid(m)}"})
        # Drop any remaining stale ledger entries for this setlist.
        for e in list(ledger.entries_in_order()):
            if e.get("setlist") == setlist:
                ledger.remove(setlist=setlist, posi=e.get("posi"))

        # 2. Install the whole library fresh into the now-empty slots.
        occupied = {m.get("posi") for m in client.list_container(container)
                    if m.get("posi") is not None}
        slots = _empty_positions(occupied, len(pending))
        for (f, _body, name) in pending[len(slots):]:
            result["errors"].append({"file": f.name, "name": name,
                                     "error": "no empty slot left in setlist"})
        pending = pending[:len(slots)]

        # One template skeleton for the whole run (current edit buffer, or the
        # given template preset loaded once).
        template_blob = None
        if pending:
            if template_cid is not None:
                client.load_preset(template_cid)
            template_blob = client.get_edit_buffer()

        for (f, body, name), pos in zip(pending, slots):
            try:
                irs: List[dict] = []
                if not exclude_irs:
                    missing = sorted(bridge.check_irs(client, body).get("missing", []))
                    if missing:
                        irs = _upload_missing_irs(ip, missing)
                cid = bridge.install_recipe(client, body, container, pos, name,
                                            template_blob, strict=True)
                if cid is None:
                    result["errors"].append({"file": f.name, "name": name,
                                             "error": "install returned no cid"})
                    continue
                ledger.record(setlist=setlist, posi=pos, name=name, cid=cid,
                              source_kind="hsp", source_path=str(f.resolve()))
                occupied.add(pos)
                result["installed"].append({
                    "file": f.name, "name": name, "pos": pos,
                    "slot": slot_label(pos), "cid": cid, "irs": irs,
                })
            except Exception as e:  # noqa: BLE001 - one bad tone must not abort the sync
                result["errors"].append({"file": f.name, "name": name, "error": str(e)})

    ledger.save()
    result["ok"] = not result["errors"]
    return result
