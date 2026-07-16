"""Device library maintenance — IR delete/rename/prune + preset color/notes.

Implements the capture-free subset of backlog #20 (design:
``docs/superpowers/specs/2026-07-14-ir-library-polish-design.md``):

* **IR maintenance (#11):** resolve a device IR by name-or-hash, delete or
  rename it, and ``ir_prune`` — diff the device's user IRs (container ``-11``)
  against every IR hash referenced by the presets **on** the device (scanning
  the pool with non-activating ``get_content`` reads) *and* by local
  tone-library sources (``.hsp`` files and ``.sbe`` device-content blobs),
  then delete the orphans. **Dry-run by default**; locally-referenced
  ("protected") IRs need ``force``.
* **Preset color / notes:** the color is the ``colr`` content attr (an int
  enum, ``/SetContentAttrs``); the notes text is the ``preset.meta.info``
  property inside the content blob's ``pm__`` list, edited via a
  non-activating ``get_content`` → ``/SetContentData`` round-trip.

The pure planning helpers (``plan_ir_prune`` / ``resolve_device_ir`` /
``content_ir_hashes`` / ``color_index``) are separated from device I/O so they
unit-test against plain data (the ``setlist_sync`` pattern).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from helixgen.hsp import read_hsp

from . import content as _content
from . import irmd as _irmd
from .client import Cctp, Container, HelixClient, HelixError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# preset colors (the `colr` attr)
# ---------------------------------------------------------------------------

#: The device's preset-color palette, in enum order. ``colr`` is an **int**
#: index into this list (a string sent to the device is silently coerced to 0
#: — live-verified 2026-07-14). Index→name order is inferred from the Stadium
#: app's color menu; pass a raw index if a name renders unexpectedly.
PRESET_COLORS = ("auto", "white", "red", "oranged", "orangel", "yellow",
                 "green", "bluel", "blue", "violet", "pink", "off")

#: Display labels for the non-obvious tokens (the app's own menu labels).
COLOR_LABELS = {"oranged": "Dark Orange", "orangel": "Light Orange",
                "bluel": "Turquoise"}

#: The pm__ property key that holds a preset's notes text.
NOTES_KEY = "preset.meta.info"


def color_index(color: Any) -> int:
    """Normalize a color (name, display label, or index) to the ``colr`` int.

    Accepts a palette token (``"red"``), a display label (``"Dark Orange"``,
    ``"Turquoise"``), or an int / int-string index ``0..11``. Raises
    :class:`ValueError` naming the valid choices otherwise.
    """
    if isinstance(color, bool):  # bool is an int subclass; reject explicitly
        raise ValueError(f"invalid color {color!r}")
    if isinstance(color, int) or (isinstance(color, str) and color.strip().isdigit()):
        idx = int(color)
        if 0 <= idx < len(PRESET_COLORS):
            return idx
        raise ValueError(
            f"color index {idx} out of range 0..{len(PRESET_COLORS) - 1}")
    want = str(color).strip().casefold()
    for i, token in enumerate(PRESET_COLORS):
        if want == token or want == COLOR_LABELS.get(token, token).casefold():
            return i
    names = ", ".join(
        COLOR_LABELS.get(t, t).lower() if t in COLOR_LABELS else t
        for t in PRESET_COLORS)
    raise ValueError(f"unknown color {color!r}; valid: {names} (or an index 0-11)")


# ---------------------------------------------------------------------------
# IR reference collection (pure)
# ---------------------------------------------------------------------------

def content_ir_hashes(doc: Any) -> set:
    """Every IR hash referenced by a decoded device content doc.

    Device content references an IR as ``mdls[*].irmd`` = the raw 16-byte
    Stadium hash (mental-model #3). Walks the whole document so dual-cab /
    split layouts are all covered; returns 32-hex strings (== helixgen
    ``irhash``).
    """
    found: set = set()

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "irmd" and isinstance(v, (bytes, bytearray)) and len(v) == 16:
                    found.add(_irmd.irmd_to_irhash(v))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(doc)
    return found


def device_referenced_ir_hashes(client, pool: Optional[List[dict]] = None
                                ) -> Dict[str, List[str]]:
    """IR hashes referenced by everything currently LIVE on the device.

    Scans the **pool** (container ``-2``) — setlists hold references into the
    pool and factory presets cannot reference user IRs — plus the **edit
    buffer** (an unsaved tone may reference an IR no stored preset does).
    Reads each preset with the non-activating ``get_content``
    (mental-model #4). ``pool`` may carry an already-fetched **strict** pool
    listing to avoid re-listing.

    Raises :class:`HelixError` if the pool listing, any preset's content, or
    the edit buffer cannot be read: an incomplete reference set must never
    feed a prune (fail closed).
    """
    if pool is None:
        pool = client.list_presets(Container.POOL, strict=True)
    out: Dict[str, List[str]] = {}
    for m in pool:
        cid = m.get("cid_")
        name = m.get("name", f"cid {cid}")
        try:
            doc = _content.decode_any(client.get_content(cid))
        except (HelixError, ValueError) as e:
            raise HelixError(
                f"could not read content of pool preset {name!r} (cid {cid}): "
                f"{e}; aborting — an incomplete reference scan must not feed "
                f"an IR prune") from e
        for h in content_ir_hashes(doc):
            out.setdefault(h, []).append(name)
    try:
        buf = _content.decode_any(client.get_edit_buffer())
    except (HelixError, ValueError) as e:
        raise HelixError(
            f"could not read the edit buffer: {e}; aborting — an incomplete "
            f"reference scan must not feed an IR prune") from e
    for h in content_ir_hashes(buf):
        out.setdefault(h, []).append("(edit buffer)")
    return out


def _reference_is_dangling(client, rcid) -> bool:
    """Whether a setlist reference points at a cid the device no longer has.

    Probes the cid directly (``get_content_ref``); a missing content ref means
    the pool preset was **deleted** but the setlist still references it (a
    *dangling* reference) rather than the pool listing being merely incomplete.
    A read error is ambiguous, so it is treated as NOT dangling — the caller
    then falls back to the (retryable) incomplete-listing path instead of
    telling the user to remove a reference we could not verify.
    """
    if rcid is None:
        return False
    probe = getattr(client, "get_ref", None)
    if not callable(probe):
        return False
    try:
        return probe(rcid) is None
    except HelixError:
        return False


def _verify_pool_covers_references(client, pool_cids) -> None:
    """Sanity cross-check for a pool listing feeding a prune (finding 1b).

    Every setlist reference's ``rcid`` must point at a cid present in the
    pool listing. One that doesn't is either:

    * a **dangling reference** — the pool preset was deleted but the setlist
      still points at it. Probing the cid confirms it is gone; we raise an
      **actionable** error naming the stale reference so the user can remove
      it (backlog #32b — the old code always blamed an "incomplete listing"
      and told the user to reboot, which never helped); or
    * a genuinely **incomplete** pool listing (e.g. a partially-decoded
      chunked reply) — any orphan computed from it would be bogus, so abort
      and retry.

    Raises :class:`HelixError` in either case.
    """
    pool_cids = set(pool_cids)
    for sl in client.list_setlists(strict=True):
        sl_cid = sl.get("cid_")
        if sl_cid is None:
            continue
        for item in client.list_container(sl_cid, strict=True):
            if item.get("cctp") != Cctp.REFERENCE:
                continue
            rcid = item.get("rcid")
            if rcid in pool_cids:
                continue
            if _reference_is_dangling(client, rcid):
                raise HelixError(
                    f"setlist {sl.get('name')!r} has a stale (dangling) "
                    f"reference to cid {rcid}, whose pool preset no longer "
                    f"exists — a leftover from a deleted preset. Remove it "
                    f"before pruning: re-sync the setlist (helixgen device "
                    f"sync {sl.get('name')!r}) or delete the setlist entry, "
                    f"then retry.")
            raise HelixError(
                f"pool listing looks incomplete: setlist "
                f"{sl.get('name')!r} references cid {rcid} "
                f"which the pool listing doesn't contain; aborting — "
                f"retry (reboot the Helix if it persists)")


def local_referenced_ir_hashes(manifest=None):
    """IR hashes referenced by the tone library's local sources — ``.hsp``
    files AND ``.sbe`` device-content blobs (the source ``device push``
    records) — plus warnings for tones whose protection could NOT be
    verified.

    Local references protect an IR from pruning even when no on-device preset
    references it (the tone may be off-device today and synced back
    tomorrow). A ``.sbe`` source is decoded as device content and its
    ``irmd`` hashes collected directly (backlog/live-validation #68i — it
    used to be force-parsed as a ``.hsp`` and warn about a missing
    ``rpshnosj`` magic on a perfectly normal ``device push`` flow). A tone
    with a **recorded but missing/unreadable** source can't prove which IRs
    it would protect — skipping it silently would make the prune MORE
    aggressive, so each such tone is surfaced as a warning and ``ir_prune``
    refuses to execute over warnings without ``force`` (fail closed).
    Returns ``(hashes, warnings)``.
    """
    from . import bridge

    if manifest is None:
        from .manifest import SetlistManifest
        manifest = SetlistManifest.load()
    out: Dict[str, List[str]] = {}
    warnings: List[str] = []
    for name, rec in getattr(manifest, "tones", {}).items():
        path = rec.get("path") if isinstance(rec, dict) else None
        if not path:
            continue  # pathless tones (device-origin) record no local IRs
        is_sbe = str(path).endswith(".sbe")
        try:
            if is_sbe:
                hashes = content_ir_hashes(
                    _content.decode_any(Path(path).read_bytes()))
            else:
                hashes = bridge.hsp_ir_hashes(read_hsp(path))
        except Exception as e:  # noqa: BLE001 — any unreadable source is a
            # verification warning (fail closed), never a planning crash
            warnings.append(
                f"tone {name!r}: cannot read its "
                f"{'.sbe device-content source' if is_sbe else '.hsp'} "
                f"({path}): {e} — its IR references cannot protect anything")
            continue
        for h in hashes:
            out.setdefault(h, []).append(name)
    return out, warnings


# ---------------------------------------------------------------------------
# IR resolution + prune planning (pure)
# ---------------------------------------------------------------------------

def resolve_device_ir(irs: Sequence[dict], name_or_hash: str) -> dict:
    """Match one device IR by 32-hex hash or (case-insensitive) name.

    Device IR names carry no extension, so a trailing ``.wav`` on the query is
    tolerated. Raises :class:`ValueError` when nothing (or more than one
    name) matches.
    """
    q = str(name_or_hash).strip()
    qh = q.casefold()
    if len(qh) == 32 and all(c in "0123456789abcdef" for c in qh):
        for m in irs:
            if str(m.get("hash", "")).casefold() == qh:
                return m
    base = q[:-4] if qh.endswith(".wav") else q
    matches = [m for m in irs
               if str(m.get("name", "")).casefold() == base.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        cids = ", ".join(str(m.get("cid_")) for m in matches)
        raise ValueError(
            f"ambiguous IR name {base!r} (cids {cids}); use the 32-hex hash "
            f"(helixgen device list-irs)")
    names = ", ".join(sorted(str(m.get("name", "?")) for m in irs)) or "(none)"
    raise ValueError(
        f"no device IR matches {name_or_hash!r}; device IRs: {names}")


def resolve_device_ir_live(client, name_or_hash: str, *, tries: int = 5,
                           delay: float = 0.4) -> dict:
    """:func:`resolve_device_ir` against a live client, retrying the listing.

    The ``-11`` container listing lags for a few seconds after an IR write
    (the same lag ``ir_path_for_hash`` documents), so a just-pushed IR can be
    invisible to a single ``list_irs``. Retries under ``client.mutating()``
    (the 2001 subscription that activates prompt index propagation) before
    giving up. Ambiguity fails fast — only the no-match case retries.

    The listing is **strict** (#32c): a timeout / truncated ``-11`` reply
    raises :class:`HelixError` instead of reading as an empty list, so the
    ``--force-wedge`` fallback in :func:`delete_device_ir` never mistakes a
    dropped listing for "no such IR" (it catches only ``ValueError``).
    """
    import time

    with client.mutating():
        last_err: Optional[ValueError] = None
        for i in range(tries):
            try:
                return resolve_device_ir(client.list_irs(strict=True), name_or_hash)
            except ValueError as e:
                if "ambiguous" in str(e):
                    raise
                last_err = e
                if i < tries - 1:
                    time.sleep(delay)
        raise last_err  # type: ignore[misc]


def plan_ir_prune(
    device_irs: Sequence[dict],
    device_ref: Dict[str, List[str]],
    local_ref: Dict[str, List[str]],
) -> Dict[str, List[dict]]:
    """Bucket every device IR: ``referenced`` (by an on-device preset — never
    deletable), ``protected`` (unreferenced on the device but referenced by a
    local off-device ``.hsp`` — deleted only with force), ``orphans`` (safe to
    delete)."""
    referenced: List[dict] = []
    protected: List[dict] = []
    orphans: List[dict] = []
    for m in device_irs:
        h = m.get("hash")
        if h in device_ref:
            referenced.append(dict(m, presets=list(device_ref[h])))
        elif h in local_ref:
            protected.append(dict(m, local_tones=list(local_ref[h])))
        else:
            orphans.append(dict(m))
    return {"referenced": referenced, "protected": protected, "orphans": orphans}


# ---------------------------------------------------------------------------
# device-driving entry points
# ---------------------------------------------------------------------------

def _remove_ir_backing_file(ip: str, client, ir: dict) -> bool:
    """Best-effort removal of a deleted IR's backing ``.wav`` on the device.

    After ``/RemoveContent`` on ``-11`` the file lingers in the device's
    ``ir/`` dir for several minutes until the device lazily garbage-collects
    it (live-observed 2026-07-14); during that window ``/IrPathForHashGet``
    still resolves and a re-``push_ir`` false-positives "already on device".
    Removing the file immediately closes that window. Returns whether the
    file is gone (failures — e.g. no SSH key — never fail the delete
    itself)."""
    from . import sftp as _sftp

    name = None
    path = None
    try:
        lookup = getattr(client, "ir_path_for_hash", None)
        path = lookup(ir.get("hash", "")) if lookup else None
    except HelixError:
        path = None
    if path:
        name = path.rsplit("/", 1)[-1]
    elif ir.get("name"):
        name = f"{ir['name']}.wav"
    if not name:
        return False
    try:
        with _sftp.HelixSFTP(ip) as s:
            s.remove_ir_file(name)
        return True
    except HelixError as e:
        logger.warning("could not remove IR file %r from the device (%s); "
                       "the IR is unregistered but its .wav lingers", name, e)
        return False


def delete_device_ir(client, name_or_hash: str, *, ip: str,
                     remove_file: bool = True,
                     force_wedge: bool = False) -> Dict[str, Any]:
    """Delete one device IR completely: registry entry (``/RemoveContent`` on
    ``-11``) plus its backing ``.wav`` (best-effort, over SFTP).

    Returns ``{ok, cid, name, hash, file_removed}``. ``ok`` reflects the
    registry delete; ``file_removed`` is advisory (a lingering file only
    affects a later re-import's "already on device" shortcut).

    Wedged-state fallback (live-observed 2026-07-14): a delete → quick
    re-import of the SAME IR can leave the device with the backing file and
    ``/IrPathForHashGet`` resolving but NO ``-11`` registry entry (its
    content index reconciles lazily). Because the ``-11`` listing lag after a
    normal import can outlive the resolver's retry budget, a
    lagging-but-HEALTHY IR looks identical from here — so the file-only
    cleanup **requires an explicit ``force_wedge=True``** and a **32-hex
    hash** query. Without it, an unresolvable query raises (the safe
    default). (Name queries can't address the wedge; use the hash.)
    """
    try:
        target = resolve_device_ir_live(client, name_or_hash)
    except ValueError as resolve_err:
        # No registry entry — possibly the wedged file-only state, which is
        # addressable by hash via the path index (no registry cid to delete).
        q = str(name_or_hash).strip().casefold()
        is_hash = len(q) == 32 and all(c in "0123456789abcdef" for c in q)
        path = None
        if remove_file and is_hash:
            try:
                path = client.ir_path_for_hash(q)
            except HelixError:
                path = None
        if not path:
            raise
        if not force_wedge:
            raise ValueError(
                f"{resolve_err} — but the device's path index still resolves "
                f"this hash. Either the IR was just imported and the listing "
                f"is lagging (wait and retry), or it is wedged (file present, "
                f"never re-listed). If you are sure it is wedged, re-run with "
                f"force_wedge/--force-wedge to remove the orphaned file."
            ) from resolve_err
        name = path.rsplit("/", 1)[-1]
        from . import sftp as _sftp
        with _sftp.HelixSFTP(ip) as s:
            s.remove_ir_file(name)
        stem = name[:-4] if name.casefold().endswith(".wav") else name
        return {"ok": True, "cid": None, "name": stem, "hash": q,
                "file_removed": True}
    file_removed = False
    ok = bool(client.delete_irs([target["cid_"]]))
    if ok and remove_file:
        file_removed = _remove_ir_backing_file(ip, client, target)
    return {"ok": ok, "cid": target.get("cid_"), "name": target.get("name"),
            "hash": target.get("hash"), "file_removed": file_removed}


def ir_prune(
    *,
    ip: str = "192.168.4.84",
    port: Optional[int] = None,
    execute: bool = False,
    force: bool = False,
    ignore_warnings: bool = False,
    only: Optional[str] = None,
    manifest=None,
) -> Dict[str, Any]:
    """Delete device IRs no preset references any more (backlog #11).

    **Dry-run by default** — nothing is deleted unless ``execute``. An IR
    referenced by any preset on the device is never a candidate. An IR
    referenced only by a local tone-library ``.hsp`` is *protected*: reported,
    deleted only when ``force`` is also set. ``only`` narrows the deletion to
    a single IR (name-or-hash) — naming a referenced IR raises.

    Two INDEPENDENT consents (#32a — previously conflated under ``force``):

    * ``force`` — also delete *protected* IRs (referenced only by a local
      off-device ``.hsp``); and
    * ``ignore_warnings`` — proceed even though some local tones' IR
      references could not be verified (a missing/unreadable recorded
      ``.hsp``). Without it, execute mode fails closed on any warning.

    Safety rails (adversarial-review hardening, PR #37):

    * every listing the plan trusts is **strict** (a silent-empty/partial
      ``/GetContainerContents`` raises instead of reading as "no presets");
    * the pool listing is **cross-checked** against every setlist's
      references (an ``rcid`` missing from the pool ⇒ incomplete listing OR a
      dangling reference ⇒ abort with an actionable error);
    * the **edit buffer** counts as a reference source;
    * unverifiable local tones (recorded ``.hsp`` missing/unreadable) surface
      in ``warnings``; executing over warnings requires ``ignore_warnings``;
    * execute mode **re-scans and re-plans immediately before deleting** and
      aborts if the two plans disagree.

    Returns ``{ok, dry_run, device_irs, referenced, protected, orphans,
    deleted, warnings, errors}`` (IR entries carry ``name``/``hash``/``cid_``;
    ``referenced`` entries list their ``presets``; ``protected`` their
    ``local_tones``).
    """
    kwargs: Dict[str, Any] = {"ip": ip}
    if port is not None:
        kwargs["port"] = port
    result: Dict[str, Any] = {
        "ok": True, "dry_run": not execute, "device_irs": 0,
        "referenced": [], "protected": [], "orphans": [], "deleted": [],
        "warnings": [], "errors": [],
    }
    local_ref, local_warnings = local_referenced_ir_hashes(manifest)
    result["warnings"] = list(local_warnings)

    def _scan(client):
        """One full strict scan → (irs, plan)."""
        pool = client.list_presets(Container.POOL, strict=True)
        _verify_pool_covers_references(
            client, {m.get("cid_") for m in pool})
        irs = client.list_irs(strict=True)
        device_ref = device_referenced_ir_hashes(client, pool=pool)
        return irs, plan_ir_prune(irs, device_ref, local_ref)

    def _candidates(irs, plan):
        cands = list(plan["orphans"])
        if force:
            cands += plan["protected"]
        if only is not None:
            target = resolve_device_ir(irs, only)
            if any(m.get("cid_") == target.get("cid_")
                   for m in plan["referenced"]):
                raise ValueError(
                    f"IR {target.get('name')!r} is referenced by a preset on "
                    f"the device; refusing to prune it")
            cands = [m for m in cands
                     if m.get("cid_") == target.get("cid_")]
            if not cands:
                raise ValueError(
                    f"IR {target.get('name')!r} is protected (referenced by a "
                    f"local .hsp); re-run with force to delete it")
        return cands

    def _plan_key(plan):
        return ({m.get("hash") for m in plan["orphans"]},
                {m.get("hash") for m in plan["protected"]})

    with HelixClient(**kwargs) as client:
        irs, plan = _scan(client)
        result["device_irs"] = len(irs)
        result.update(plan)
        candidates = _candidates(irs, plan)

        if execute:
            if local_warnings and not ignore_warnings:
                raise ValueError(
                    "refusing to execute: some local tones' IR references "
                    "could not be verified (their protection is unknown) — "
                    "fix the manifest paths or re-run with ignore_warnings "
                    "(--ignore-warnings). "
                    + "; ".join(local_warnings))
            # Re-scan and re-plan immediately before deleting; a disagreement
            # means the device listings are unstable (or something changed
            # mid-run) and no delete can be trusted.
            irs2, plan2 = _scan(client)
            if _plan_key(plan2) != _plan_key(plan):
                raise HelixError(
                    "device listings changed between the plan scan and the "
                    "confirm scan; aborting prune with nothing deleted — "
                    "re-run")
            candidates = _candidates(irs2, plan2)
            for m in candidates:
                try:
                    if client.delete_irs([m["cid_"]]):
                        entry = {k: m.get(k) for k in ("cid_", "name", "hash")}
                        entry["file_removed"] = _remove_ir_backing_file(
                            ip, client, m)
                        result["deleted"].append(entry)
                    else:
                        result["errors"].append(
                            f"device refused to delete IR {m.get('name')!r}")
                except HelixError as e:
                    result["errors"].append(
                        f"IR {m.get('name')!r}: {e}")
    result["ok"] = not result["errors"]
    return result


# ---------------------------------------------------------------------------
# preset notes (pm__ `preset.meta.info`) + color (`colr` attr)
# ---------------------------------------------------------------------------

def get_preset_notes(client, cid: int) -> Optional[str]:
    """Read a preset's notes text (non-activating), or ``None`` if unset."""
    doc = _content.decode_any(client.get_content(cid))
    for e in doc.get("pm__", []) or []:
        if isinstance(e, dict) and e.get("key_") == NOTES_KEY:
            v = e.get("val_")
            return str(v) if v is not None else None
    return None


def set_preset_notes(client, cid: int, text: str) -> bool:
    """Write a preset's notes text without activating it.

    Notes are NOT a content attr — they live as the ``preset.meta.info``
    property entry in the content blob's ``pm__`` list (live-verified
    2026-07-14). Reads the stored content (``get_content``), updates or
    inserts the entry (keeping ``pm__`` sorted by key, the device's observed
    ordering), and writes it back with ``/SetContentData``.
    """
    doc = _content.decode_any(client.get_content(cid))
    pm = doc.get("pm__")
    if not isinstance(pm, list):
        pm = []
        doc["pm__"] = pm
    for e in pm:
        if isinstance(e, dict) and e.get("key_") == NOTES_KEY:
            e["val_"] = str(text)
            e["type"] = "s"
            break
    else:
        pm.append({"key_": NOTES_KEY, "type": "s", "val_": str(text)})
        pm.sort(key=lambda e: str(e.get("key_", "")) if isinstance(e, dict) else "")
    with client.mutating():
        return bool(client._raw.set_content_data(
            cid, _content.encode_content_data(doc)))


def set_preset_info(client, cid: int, *, color: Any = None,
                    notes: Optional[str] = None) -> Dict[str, bool]:
    """Set a preset's color and/or notes. At least one must be given.

    Color goes over ``/SetContentAttrs {colr: <int>}`` (see
    :func:`color_index`); notes via :func:`set_preset_notes`. Returns
    ``{"color": ok?, "notes": ok?}`` for whichever were requested.
    """
    if color is None and notes is None:
        raise ValueError("nothing to set: give a color and/or notes")
    out: Dict[str, bool] = {}
    if color is not None:
        idx = color_index(color)
        with client.mutating():
            out["color"] = bool(client.set_attrs(cid, {"colr": idx}))
    if notes is not None:
        out["notes"] = set_preset_notes(client, cid, notes)
    return out
