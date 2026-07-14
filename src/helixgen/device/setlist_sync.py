"""Reference-based multi-setlist sync engine.

Models setlists the way the device does — a **preset pool** (container ``-2``)
plus named **setlists** under ``-5`` that hold **references** (``cctp 1003``,
``rcid`` → pool preset) — so a single authored tone can live in many setlists.

The pure reconcile logic (:func:`plan_pool` / :func:`plan_references` /
:func:`plan_gc`) is separated from device I/O so it unit-tests against plain
data with no client. :func:`sync_setlists` drives a real
:class:`~helixgen.device.client.HelixClient`:

1. reconcile the **pool** for the union of tones the target setlist(s) need
   (install missing, ``SetContentData``-update changed, skip unchanged);
2. rebuild each setlist's **references** to match manifest order via
   ``client.mirror_setlist`` (adds missing, removes extra — never orphans);
3. optionally **garbage-collect** pool presets no setlist references anymore —
   only on the whole-library (``setlists is None``) run.

Per-tone failures append to ``errors[]`` without aborting (matching the old
directory-sync's resilience contract). This retires ``sync.py``'s destructive
whole-``-2``-mirror; its IR-upload helper is copied here.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from helixgen.hsp import read_hsp

from . import bridge
from .bridge import UnresolvedModel
from .client import Container, Cctp, HelixClient, HelixError
from .manifest import SetlistManifest


# ---------------------------------------------------------------------------
# pure reconcile logic (no device)
# ---------------------------------------------------------------------------

def plan_pool(
    manifest: SetlistManifest,
    tone_names: Sequence[str],
    device_pool_names: Sequence[str],
    *,
    observed_hash_of: Callable[[str], Optional[str]],
    force: bool = False,
) -> Dict[str, List[str]]:
    """Decide, for each desired tone, whether to install / update / skip it in
    the pool.

    * name not present in ``device_pool_names`` → **install**.
    * present but its ``manifest.content_hash`` differs from the last-synced hash
      (``observed_hash_of(name)``) → **update** (re-push content in place).
    * present and hashes agree → **skip** (idempotent, fast) — unless
      ``force=True`` (the ``--repush`` mode, #25 residual), which bumps every
      already-present tone into **update** regardless of hash agreement, so a
      transcoder-output change that the ``.hsp`` hash can't see still gets
      re-pushed. A tone not yet in the pool is unaffected by ``force`` — it is
      still a plain **install**.
    """
    have = set(device_pool_names)
    install: List[str] = []
    update: List[str] = []
    skip: List[str] = []
    for name in tone_names:
        if name not in have:
            install.append(name)
        elif force or manifest.content_hash(name) != observed_hash_of(name):
            update.append(name)
        else:
            skip.append(name)
    return {"install": install, "update": update, "skip": skip}


def plan_references(desired_names_in_order: Sequence[str],
                    device_refs: Any = None) -> List[str]:
    """The desired ordered tone names for a setlist's references.

    Reference add/remove/reorder reconciliation itself is delegated to
    ``client.mirror_setlist`` (which resolves these names → pool cids and diffs
    against the device). This pure step only fixes the *desired order*;
    ``device_refs`` is accepted for call-site symmetry and is not needed here.
    """
    return list(desired_names_in_order)


def plan_gc(
    manifest_union_names: Any,
    device_pool_names: Sequence[str],
    device_referenced_names: Any,
) -> List[str]:
    """Pool presets safe to delete: those the manifest no longer wants **and**
    that no setlist currently on the device references.

    The never-orphan guarantee: a preset still referenced anywhere (in
    ``device_referenced_names``) is never returned, even if the manifest dropped
    it. Order follows ``device_pool_names``.
    """
    want = set(manifest_union_names)
    referenced = set(device_referenced_names)
    return [name for name in device_pool_names
            if name not in want and name not in referenced]


def assign_slots(manifest: SetlistManifest, occupied) -> Dict[str, str]:
    """Resolve every ``slot == "auto"`` tone to the first free user-slot label,
    avoiding both ``occupied`` (untracked / already-taken labels) and any
    concretely-slotted managed tone. Mutates the manifest records and returns
    ``{name: assigned_label}``.
    """
    from .manifest import _SLOT_LABELS

    used = set(occupied)
    for rec in manifest.tones.values():
        s = rec.get("slot")
        if s and s != "auto":
            used.add(s)
    free = (lbl for lbl in _SLOT_LABELS if lbl not in used)
    assigned: Dict[str, str] = {}
    for name, rec in manifest.tones.items():
        if rec.get("slot") == "auto":
            try:
                lbl = next(free)
            except StopIteration:
                raise ValueError("no free user slot available (device full)")
            rec["slot"] = lbl
            rec.pop("auto_marked", None)  # concrete placement: provenance moot
            assigned[name] = lbl
            used.add(lbl)
    return assigned


# ---------------------------------------------------------------------------
# IR upload helper (copied from sync.py — resolves each irhash to a local WAV)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# the device-driving entry point
# ---------------------------------------------------------------------------

def _cid(m: dict):
    return m.get("cid_", m.get("cid"))


def _looks_like_connection_drop(msg: str) -> bool:
    """True if an error string reads like a mid-sync connection drop (the
    signal _rpc emits when it exhausts its bounded reconnect budget)."""
    m = msg.lower()
    return ("reboot the helix" in m
            or "connection lost" in m
            or "connection dropped" in m)


def _build_blob(body):
    """Transcode a tone body into a stored-content blob (may raise
    UnresolvedModel / ValueError).

    Template-free: :func:`transcode.hsp_to_sbepgsm` synthesizes the device
    ``_sbepgsm`` document straight from the ``.hsp`` (models/params/IRs), so no
    device template is loaded and the write path never changes the active tone.
    """
    from . import transcode
    return transcode.hsp_to_sbepgsm(body, strict=True)


def sync_setlists(
    manifest: SetlistManifest,
    *,
    ip: str = "192.168.4.84",
    port: Optional[int] = None,
    setlists: Optional[List[str]] = None,
    gc: bool = False,
    exclude_irs: bool = False,
    repush: bool = False,
) -> Dict[str, Any]:
    """Sync one or more manifest setlists onto the device (pool-first, reference
    rebuild, optional GC).

    ``setlists`` names the setlists to sync, or ``None`` for **all** manifest
    setlists (the ``--all`` case). ``gc`` is honored **only** on the all-setlists
    run — a single/subset sync never garbage-collects the pool.

    ``repush`` (the ``--repush`` flag, #25 residual) forces every in-scope tone
    already present in the pool into the **update** bucket, even when its
    recorded ``.hsp`` content hash matches — the content refresh reuses the
    exact same ``SetContentData``-on-the-existing-cid path a normal hash-driven
    update uses (see the ``plan["update"]`` loop below), so it stays
    non-activating; only the *decision* to treat the tone as changed is
    different. Use this after a transcoder upgrade whose output differs for a
    ``.hsp`` that itself didn't change, since hash-based change detection can't
    see that. References/IR-upload/GC behavior are unaffected.

    ``setlists=None`` (the ``--all`` run) maintains only setlists opted into
    mirroring (``synced=True``); local-only drafts are never touched on the
    device. A **targeted** sync is the opt-in gesture — it flips the named
    setlist to ``synced``.

    The pool reconcile covers the union of the target setlists' members plus
    every **slot-marked** tone (the managed user population — a `device add`ed
    tone installs with no setlist membership; pathless tones absent from the
    pool are left alone), and deletes manifest tones whose ``slot`` is null
    but that helixgen previously placed (observed-placement evidence required
    — a same-named preset helixgen didn't place is never touched), never
    orphaning one a live setlist still references (reported in
    ``delete_skipped``).

    Returns ``{ok, setlists,
    pool:{installed,updated,skipped,deleted,delete_skipped},
    references:{<setlist>:{added,removed}}, gc:{deleted}, irs:[...], errors:[...]}``.
    ``ok`` is ``not errors``. Per-tone install/update/IR failures append to
    ``errors`` without aborting the run; an unresolvable setlist name is a clear
    error that skips only that setlist.
    """
    all_run = setlists is None
    do_gc = gc and all_run

    result: Dict[str, Any] = {
        "ok": True,
        "setlists": [],
        "pool": {"installed": [], "updated": [], "skipped": [], "deleted": [],
                 "delete_skipped": []},
        "references": {},
        "gc": {"deleted": []},
        "irs": [],
        "errors": [],
    }
    errors: List[str] = result["errors"]

    # --all maintains only setlists opted into mirroring (synced=True) —
    # local-only drafts are never touched on the device (design §4). A
    # targeted sync is the opt-in gesture: it flips the setlist to synced.
    # Non-empty drafts an --all run skips are named in the result so the user
    # learns why a setlist isn't syncing.
    targets = (list(setlists) if setlists is not None
               else [s for s in manifest.setlists() if manifest.is_synced(s)])
    if all_run:
        result["skipped_draft_setlists"] = [
            s for s in manifest.setlists()
            if not manifest.is_synced(s) and manifest.tones_in(s)]

    client_kwargs: Dict[str, Any] = {"ip": ip}
    if port is not None:
        client_kwargs["port"] = port

    with HelixClient(**client_kwargs) as client:
        with client.mutating():
            # 1. Resolve each target setlist by name (skip + error on absent).
            setlist_cids: Dict[str, int] = {}
            resolved: List[str] = []
            for name in targets:
                cid = client.resolve_setlist_cid(name)
                if cid is None:
                    errors.append(
                        f"setlist '{name}' not found on device; create it with "
                        f"`helixgen device setlist create '{name}'`, then re-sync")
                    continue
                setlist_cids[name] = cid
                resolved.append(name)
                manifest.set_setlist_synced(name, True)
            result["setlists"] = resolved

            # Resolve any 'auto' slots to concrete user-slot labels so the
            # manifest never persists 'auto' after a sync (managed-set mirror,
            # design §4). Runs after the synced flip above so freshly-marked
            # members resolve in the same run. NOTE: occupancy of untracked
            # device presets is not fetched yet — labels are only guaranteed
            # unique among managed tones (backlog #30).
            assign_slots(manifest, occupied=set())

            # 2. Reconcile the pool for the union of tones the targets need,
            # PLUS every slot-marked tone — the managed user population
            # (design §4): a tone with a slot wants the device even when it
            # belongs to no setlist (`device add --slot` / `device add`).
            # Pathless tones (device save/create) with no pool presence have
            # nothing local to install from and are left alone.
            pool_by_name = {m.get("name"): m
                            for m in client.list_presets(Container.POOL)}
            union = manifest.union_tones(resolved)
            in_union = set(union)
            for name in manifest.device_marked_tones():
                if name in in_union:
                    continue
                if manifest.tone_path(name) is None and name not in pool_by_name:
                    continue
                union.append(name)
                in_union.add(name)
            plan = plan_pool(
                manifest, union, list(pool_by_name.keys()),
                observed_hash_of=manifest.observed_pool_hash,
                force=repush,
            )
            result["pool"]["skipped"] = list(plan["skip"])

            def _author(name: str):
                """Read the tone, upload its IRs, return its stored-content blob."""
                path = manifest.tone_path(name)
                if not path:
                    # Bucket-agnostic wording: this serves both the install
                    # loop and the update loop (--repush can bump a pathless
                    # pool-present tone into update).
                    raise ValueError(
                        f"tone {name!r} has no .hsp source (pathless); "
                        f"nothing local to transcode its content from")
                body = read_hsp(path)
                if not exclude_irs:
                    missing = sorted(bridge.check_irs(client, body).get("missing", []))
                    if missing:
                        result["irs"].extend(_upload_missing_irs(ip, missing))
                return _build_blob(body)

            for name in plan["install"]:
                try:
                    blob = _author(name)
                    cid = client.install_into_pool(blob, name)
                    if cid is None:
                        errors.append(f"tone '{name}': install returned no cid")
                        continue
                    result["pool"]["installed"].append(name)
                except (UnresolvedModel, ValueError, HelixError) as e:
                    errors.append(f"tone '{name}': {e}")

            for name in plan["update"]:
                existing = pool_by_name.get(name)
                cid = _cid(existing) if existing else None
                if cid is None:
                    errors.append(f"tone '{name}': no pool cid to update")
                    continue
                try:
                    blob = _author(name)
                    client._raw.set_content_data(cid, blob)
                    result["pool"]["updated"].append(name)
                except (UnresolvedModel, ValueError, HelixError) as e:
                    errors.append(f"tone '{name}': {e}")

            # Refresh the pool listing (installs added new cids/posis) and record
            # observed placement + last-synced hash for everything we touched.
            pool_by_name = {m.get("name"): m
                            for m in client.list_presets(Container.POOL)}
            cid_to_name = {_cid(m): n for n, m in pool_by_name.items()}
            for name in result["pool"]["installed"] + result["pool"]["updated"]:
                m = pool_by_name.get(name)
                if m is not None:
                    manifest.record_observed_pool(
                        name, _cid(m), m.get("posi"),
                        synced_hash=manifest.content_hash(name))

            # 3. Rebuild each resolved setlist's references (manifest order).
            for name in resolved:
                setlist_cid = setlist_cids[name]
                desired = plan_references(manifest.tones_in(name))
                ordered_cids: List[int] = []
                for tone in desired:
                    m = pool_by_name.get(tone)
                    if m is not None and _cid(m) is not None:
                        ordered_cids.append(_cid(m))
                    else:
                        errors.append(
                            f"tone '{tone}' not in pool; cannot reference into "
                            f"setlist '{name}'")
                diff = client.mirror_setlist(setlist_cid, ordered_cids)
                result["references"][name] = {
                    "added": list(diff.get("added", [])),
                    "removed": list(diff.get("removed", [])),
                }
                refs: Dict[str, Any] = {}
                for item in client.list_container(setlist_cid):
                    if item.get("cctp") == Cctp.REFERENCE:
                        tone_name = cid_to_name.get(item.get("rcid"))
                        if tone_name:
                            refs[tone_name] = {"ref_cid": _cid(item),
                                               "posi": item.get("posi")}
                manifest.record_observed_setlist(name, setlist_cid, refs)

            # 4. Managed-set mirror deletes (design §4): a manifest tone with
            # slot=None that helixgen PLACED on the device in a prior sync
            # (observed evidence — never a same-named preset it didn't place)
            # was unsynced — delete it from the pool (it stays in the library)
            # unless a live setlist still references it (never-orphan, reported
            # in delete_skipped), this run is installing it anyway, or it
            # belongs to a target setlist that failed to resolve.
            unresolved_members: set = set()
            for t in targets:
                if t not in setlist_cids:
                    unresolved_members.update(manifest.tones_in(t))
            referenced = _device_referenced_names(client, pool_by_name)
            for name, rec in manifest.tones.items():
                if (rec.get("slot") is not None or name in in_union
                        or name in unresolved_members):
                    continue
                if (rec.get("device") is None
                        and manifest.observed.get("pool", {}).get(name) is None):
                    continue  # no prior-placement evidence: not ours to delete
                dev = pool_by_name.get(name)
                if dev is None:
                    continue
                if name in referenced:
                    result["pool"]["delete_skipped"].append(name)
                    continue
                try:
                    if client._raw.delete(Container.POOL, [_cid(dev)]):
                        result["pool"]["deleted"].append(name)
                        manifest.clear_observed_pool(name)
                        pool_by_name.pop(name, None)
                except HelixError as e:
                    errors.append(f"tone '{name}': {e}")

            # 5. Garbage-collect orphan pool presets (only on the --all run).
            if do_gc:
                referenced = _device_referenced_names(client, pool_by_name)
                # "wanted" = every setlist member AND every slot-marked tone —
                # a slot-only tone is not an orphan.
                union_all = set(manifest.union_tones(manifest.setlists()))
                union_all.update(manifest.device_marked_tones())
                for name in plan_gc(union_all, list(pool_by_name.keys()), referenced):
                    m = pool_by_name.get(name)
                    if m is None:
                        continue
                    # never-orphan re-verify: skip if a live reference reappeared
                    if name in _device_referenced_names(client, pool_by_name):
                        continue
                    if client._raw.delete(Container.POOL, [_cid(m)]):
                        result["gc"]["deleted"].append(name)

    manifest.save()
    result["ok"] = not errors
    # If any per-tone failure looks like a connection drop, tell the user the
    # run is safely resumable: sync is idempotent (installed tones skip on
    # re-run via content-hash), so simply re-running picks up where it left off.
    if any(_looks_like_connection_drop(e) for e in errors):
        result["hint"] = (
            "device connection dropped mid-sync; re-run `device sync` to resume "
            "(installed tones are skipped) — if it keeps dropping, reboot the "
            "Helix.")
    return result


def _device_referenced_names(client, pool_by_name: Dict[str, dict]) -> set:
    """Names of pool presets referenced by ANY setlist currently on the device
    (scans every ``cctp==1003`` reference under ``-5`` and resolves its ``rcid``
    to the pool preset name)."""
    cid_to_name = {_cid(m): n for n, m in pool_by_name.items()}
    referenced: set = set()
    for sl in client.list_setlists():
        sl_cid = _cid(sl)
        if sl_cid is None:
            continue
        for item in client.list_container(sl_cid):
            if item.get("cctp") == Cctp.REFERENCE:
                nm = cid_to_name.get(item.get("rcid"))
                if nm:
                    referenced.add(nm)
    return referenced
