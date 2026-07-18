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

Per-tone/per-setlist failures append to ``errors[]`` without aborting (matching
the old directory-sync's resilience contract) — this now also covers a strict
listing failure in the setlist-resolve step, ``mirror_setlist``'s reference
rebuild, or the never-orphan delete gate (backlog #39): each is caught for
just that setlist/tone and reported distinctly from a genuine "not found" /
"nothing to delete", never silently treated as such. This retires ``sync.py``'s
destructive whole-``-2``-mirror; its IR-upload helper is copied here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from helixgen.hsp import read_hsp

from . import bridge
from . import observations as _obs
from .bridge import UnresolvedModel
from .client import Container, Cctp, HelixClient, HelixError
from .manifest import SetlistManifest


@dataclass(frozen=True)
class ProgressEvent:
    """A single sync progress event handed to the optional ``progress`` callback
    of :func:`sync_setlists`. Purely advisory — the event stream is the ONLY new
    observable behavior when a callback is supplied; it never affects the sync's
    result, ordering, or device I/O.

    Fields:

    * ``phase`` — one of ``"plan"``, ``"install"``, ``"update"``, ``"irs"``,
      ``"references"``, ``"gc"``, ``"delete"``.
    * ``label`` — the current item (tone / IR / setlist name), or a summary
      string on the one-shot ``plan`` event.
    * ``index`` — 1-based position within the phase (``None`` where N/A, e.g.
      the ``plan`` event).
    * ``total`` — total items in the phase, when known.
    * ``status`` — ``"ok"`` / ``"error"`` / ``"skip"`` / ``None``.
    * ``detail`` — a human-readable error/skip message when relevant.
    """

    phase: str
    label: Optional[str] = None
    index: Optional[int] = None
    total: Optional[int] = None
    status: Optional[str] = None
    detail: Optional[str] = None


def _make_emitter(progress: Optional[Callable[[ProgressEvent], None]]):
    """Build a guarded event emitter. Returns a callable that no-ops when
    ``progress`` is None (zero overhead — no events, byte-for-byte-unchanged
    sync). Otherwise every callback invocation is wrapped so a raising callback
    NEVER aborts the sync: the first failure warns once to stderr, all further
    callback errors are swallowed silently."""
    warned = {"done": False}

    def emit(ev: ProgressEvent) -> None:
        if progress is None:
            return
        try:
            progress(ev)
        except Exception as e:  # noqa: BLE001 — advisory; never abort a sync
            if not warned["done"]:
                warned["done"] = True
                import sys
                print(f"warning: sync progress callback raised ({e!r}); "
                      f"continuing without progress", file=sys.stderr)

    return emit


def _device_serial(client, ip: str) -> str:
    """The connected device's serial (``/ProductInfoGet``), for keying its
    ``devices/<serial>.json`` observation file. Falls back to ``f"ip-{ip}"``
    when the query fails or reports no serial (best-effort — a wrong key just
    costs a re-observe on the next sync)."""
    try:
        serial = (client.product_info() or {}).get("serial")
        if serial:
            return str(serial)
    except Exception:  # noqa: BLE001 — advisory identity, never fatal
        pass
    return f"ip-{ip}"


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
# IR upload helper — thin wrapper around the shared core in ir_upload.py
# (backlog #6; the same core also backs `device install --auto-irs` and the
# other callers). Kept under this name/signature so existing
# call sites (and tests) can keep monkeypatching it directly.
# ---------------------------------------------------------------------------

def _upload_missing_irs(ip: str, hashes: List[str]) -> List[dict]:
    """Resolve each missing irhash to a local WAV and push it (instant
    register). Returns one result dict per hash — see
    :func:`helixgen.device.ir_upload.upload_missing_irs`."""
    from . import ir_upload

    return ir_upload.upload_missing_irs(ip, hashes)


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
    ip: Optional[str] = None,
    port: Optional[int] = None,
    setlists: Optional[List[str]] = None,
    gc: bool = False,
    exclude_irs: bool = False,
    repush: bool = False,
    progress: Optional[Callable[[ProgressEvent], None]] = None,
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

    ``progress`` is an optional advisory callback invoked with a
    :class:`ProgressEvent` at each phase/item of the sync (plan, install,
    update, irs, references, delete, gc). It is purely observational — when
    ``None`` (the default) behavior is byte-for-byte unchanged and no events
    are produced; a callback that raises never aborts the sync (it warns once
    to stderr and is thereafter ignored). It does not alter the result dict,
    ordering, or any device I/O.
    """
    all_run = setlists is None
    do_gc = gc and all_run
    emit = _make_emitter(progress)

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

    if ip is None:
        # #74 resolution chain — no hardcoded default IP; the resolved
        # address also keys the per-device serial fallback + IR uploads.
        # Re-raised as HelixError so callers (the CLI included) that catch
        # the module's documented error type get the fail-fast message,
        # never a raw traceback.
        from . import discovery

        try:
            ip = discovery.resolve_ip()
        except discovery.IPResolutionError as e:
            raise HelixError(str(e)) from e
    client_kwargs: Dict[str, Any] = {"ip": ip}
    if port is not None:
        client_kwargs["port"] = port

    with HelixClient(**client_kwargs) as client:
        # Observed cid/posi is per-device state (design §3): load this device's
        # observations, rebuild them through the sync, and save at the end.
        serial = _device_serial(client, ip)
        obs = _obs.load_observations(serial)
        with client.mutating():
            # 1. Resolve each target setlist by name (skip + error on absent).
            # ``resolve_setlist_cid`` is strict by default (#39): a listing
            # failure raises HelixError instead of silently reading as
            # "absent" — which matters here because the DEFAULT error message
            # below tells the user to `device setlist create` it. If we
            # couldn't actually tell whether it exists, telling the user to
            # create it risks minting a duplicate; caught separately below so
            # that case gets its own message and this target is simply
            # skipped (not the whole run aborted — matching the existing
            # per-target resilience contract).
            setlist_cids: Dict[str, int] = {}
            resolved: List[str] = []
            for name in targets:
                try:
                    cid = client.resolve_setlist_cid(name)
                except HelixError as e:
                    errors.append(
                        f"setlist '{name}': could not verify it exists on the "
                        f"device ({e}); skipping rather than risk creating a "
                        f"duplicate — retry the sync")
                    continue
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
            # STRICT listing (#39 audit): plan_pool's install/skip decision is
            # gated entirely on this listing — a silently-truncated read would
            # make an already-installed tone look absent and mint a
            # duplicate-named pool preset, the same failure mode #39 fixed for
            # setlists.
            pool_by_name = {m.get("name"): m
                            for m in client.list_presets(Container.POOL, strict=True)}
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
                observed_hash_of=obs.pool_hash,
                force=repush,
            )
            result["pool"]["skipped"] = list(plan["skip"])
            emit(ProgressEvent(
                "plan",
                total=len(plan["install"]) + len(plan["update"]),
                label=(f"{len(plan['install'])} install, "
                       f"{len(plan['update'])} update, "
                       f"{len(plan['skip'])} skip")))

            def _author(name: str):
                """Read the tone, upload its IRs, return its stored-content blob."""
                path = manifest.tone_path(name)
                if not path:
                    # Bucket-agnostic wording: this serves both the install
                    # loop and the update loop (--repush can bump a pathless
                    # pool-present tone into update).
                    raise ValueError(
                        "no .hsp source (pathless); "
                        "nothing local to transcode its content from")
                try:
                    body = read_hsp(path)
                except FileNotFoundError as e:
                    # Distinct from pathless: the manifest HAS a path but the
                    # file is gone (renamed/moved since registration). The raw
                    # FileNotFoundError names only the path, not the likely
                    # cause. No tone-name prefix — the per-tone catch sites
                    # add one.
                    raise ValueError(
                        f"manifest .hsp path missing on disk: {path} "
                        f"(file renamed or moved since registration?)") from e
                if not exclude_irs:
                    missing = sorted(bridge.check_irs(client, body).get("missing", []))
                    if missing:
                        ir_results = _upload_missing_irs(ip, missing)
                        result["irs"].extend(ir_results)
                        n_ir = len(ir_results)
                        for i, r in enumerate(ir_results, 1):
                            emit(ProgressEvent(
                                "irs", index=i, total=n_ir,
                                label=(r.get("name") or r.get("hash")),
                                status=("ok" if r.get("ok") else "error"),
                                detail=r.get("note")))
                return _build_blob(body)

            n_install = len(plan["install"])
            for i, name in enumerate(plan["install"], 1):
                try:
                    blob = _author(name)
                    cid = client.install_into_pool(blob, name)
                    if cid is None:
                        errors.append(f"tone '{name}': install returned no cid")
                        emit(ProgressEvent("install", label=name, index=i,
                                           total=n_install, status="error",
                                           detail="install returned no cid"))
                        continue
                    result["pool"]["installed"].append(name)
                    emit(ProgressEvent("install", label=name, index=i,
                                       total=n_install, status="ok"))
                except (UnresolvedModel, ValueError, HelixError, OSError) as e:
                    errors.append(f"tone '{name}': {e}")
                    emit(ProgressEvent("install", label=name, index=i,
                                       total=n_install, status="error",
                                       detail=str(e)))

            n_update = len(plan["update"])
            for i, name in enumerate(plan["update"], 1):
                existing = pool_by_name.get(name)
                cid = _cid(existing) if existing else None
                if cid is None:
                    errors.append(f"tone '{name}': no pool cid to update")
                    emit(ProgressEvent("update", label=name, index=i,
                                       total=n_update, status="error",
                                       detail="no pool cid to update"))
                    continue
                try:
                    blob = _author(name)
                    client._raw.set_content_data(cid, blob)
                    result["pool"]["updated"].append(name)
                    emit(ProgressEvent("update", label=name, index=i,
                                       total=n_update, status="ok"))
                except (UnresolvedModel, ValueError, HelixError, OSError) as e:
                    errors.append(f"tone '{name}': {e}")
                    emit(ProgressEvent("update", label=name, index=i,
                                       total=n_update, status="error",
                                       detail=str(e)))

            # Refresh the pool listing (installs added new cids/posis) and record
            # observed placement + last-synced hash for everything we touched.
            # STRICT (#39 audit): this refreshed listing also drives the
            # reference rebuild below (`plan_references` / `mirror_setlist`) —
            # a truncated read could make a tone that's actually in the pool
            # look absent, and `mirror_setlist` would then REMOVE its
            # still-wanted reference from the setlist as "no longer desired".
            pool_by_name = {m.get("name"): m
                            for m in client.list_presets(Container.POOL, strict=True)}
            cid_to_name = {_cid(m): n for n, m in pool_by_name.items()}
            for name in result["pool"]["installed"] + result["pool"]["updated"]:
                m = pool_by_name.get(name)
                if m is not None:
                    obs.record_pool(
                        name, _cid(m), m.get("posi"),
                        synced_hash=manifest.content_hash(name))

            # 3. Rebuild each resolved setlist's references (manifest order).
            n_ref = len(resolved)
            for i, name in enumerate(resolved, 1):
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
                # mirror_setlist's own current-references listing is strict
                # (#39 audit — a truncated read there could double-add a
                # reference); a failure here is per-setlist, matching the
                # rest of this function's resilience contract, not a reason
                # to abort every other setlist's rebuild.
                try:
                    diff = client.mirror_setlist(setlist_cid, ordered_cids)
                except HelixError as e:
                    errors.append(
                        f"setlist '{name}': could not verify its current "
                        f"references before rebuilding them ({e}); skipping "
                        f"this setlist's reference rebuild this run — retry")
                    emit(ProgressEvent("references", label=name, index=i,
                                       total=n_ref, status="error",
                                       detail=str(e)))
                    continue
                result["references"][name] = {
                    "added": list(diff.get("added", [])),
                    "removed": list(diff.get("removed", [])),
                }
                refs: Dict[str, Any] = {}
                # Deliberately non-strict (#39 audit): the actual reference
                # write already happened via mirror_setlist above; this
                # listing is bookkeeping-only (records observed ref cids/posi
                # into the manifest for next run's diffing) — a truncated
                # read here would just under-record, self-healing on the next
                # sync, not corrupt the device.
                for item in client.list_container(setlist_cid):
                    if item.get("cctp") == Cctp.REFERENCE:
                        tone_name = cid_to_name.get(item.get("rcid"))
                        if tone_name:
                            refs[tone_name] = {"ref_cid": _cid(item),
                                               "posi": item.get("posi")}
                obs.record_setlist(name, setlist_cid, refs)
                emit(ProgressEvent("references", label=name, index=i,
                                   total=n_ref, status="ok"))

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
            delete_candidates: List[str] = []
            for name, rec in manifest.tones.items():
                if (rec.get("slot") is not None or name in in_union
                        or name in unresolved_members):
                    continue
                if (obs.tones.get(name) is None
                        and obs.pool.get(name) is None):
                    continue  # no prior-placement evidence: not ours to delete
                if pool_by_name.get(name) is None:
                    continue
                delete_candidates.append(name)
            # Only pay for (and risk aborting on) the strict never-orphan
            # listing when there's actually something that could be deleted
            # (#39 audit) — a listing hiccup on an otherwise delete-free sync
            # must not cost the rest of this run's already-recorded progress.
            if delete_candidates:
                try:
                    referenced = _device_referenced_names(client, pool_by_name)
                except HelixError as e:
                    errors.append(
                        f"could not verify no-orphan safety before deleting "
                        f"{len(delete_candidates)} unsynced tone(s) ({e}); "
                        f"skipping this run's deletes — retry")
                    referenced = None
                if referenced is not None:
                    n_del = len(delete_candidates)
                    for i, name in enumerate(delete_candidates, 1):
                        if name in referenced:
                            result["pool"]["delete_skipped"].append(name)
                            emit(ProgressEvent("delete", label=name, index=i,
                                               total=n_del, status="skip"))
                            continue
                        dev = pool_by_name.get(name)
                        try:
                            if client._raw.delete(Container.POOL, [_cid(dev)]):
                                result["pool"]["deleted"].append(name)
                                obs.clear_pool(name)
                                pool_by_name.pop(name, None)
                                emit(ProgressEvent("delete", label=name,
                                                   index=i, total=n_del,
                                                   status="ok"))
                        except HelixError as e:
                            errors.append(f"tone '{name}': {e}")
                            emit(ProgressEvent("delete", label=name, index=i,
                                               total=n_del, status="error",
                                               detail=str(e)))

            # 5. Garbage-collect orphan pool presets (only on the --all run).
            if do_gc:
                try:
                    referenced = _device_referenced_names(client, pool_by_name)
                except HelixError as e:
                    errors.append(
                        f"gc: could not verify no-orphan safety ({e}); "
                        f"skipping garbage-collection this run — retry")
                else:
                    # "wanted" = every setlist member AND every slot-marked
                    # tone — a slot-only tone is not an orphan.
                    union_all = set(manifest.union_tones(manifest.setlists()))
                    union_all.update(manifest.device_marked_tones())
                    gc_candidates = plan_gc(
                        union_all, list(pool_by_name.keys()), referenced)
                    n_gc = len(gc_candidates)
                    for i, name in enumerate(gc_candidates, 1):
                        m = pool_by_name.get(name)
                        if m is None:
                            continue
                        # never-orphan re-verify: skip if a live reference
                        # reappeared; a listing failure here must likewise
                        # skip (not delete) this one candidate.
                        try:
                            if name in _device_referenced_names(client, pool_by_name):
                                continue
                        except HelixError as e:
                            errors.append(
                                f"gc: could not re-verify {name!r} is unreferenced "
                                f"before deleting it ({e}); skipping it this "
                                f"run — retry")
                            continue
                        if client._raw.delete(Container.POOL, [_cid(m)]):
                            result["gc"]["deleted"].append(name)
                            emit(ProgressEvent("gc", label=name, index=i,
                                               total=n_gc, status="ok"))

    manifest.save()
    _obs.save_observations(obs)
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
    to the pool preset name).

    STRICT listings throughout (#39 audit): this is the never-orphan gate for
    both the per-tone unsynced-delete step and the whole-library ``--gc``
    prune in ``sync_setlists`` — the exact "destructive planning off a
    listing" pattern ir-prune already hardened. A silently-truncated setlist
    or reference listing here would under-report what's referenced, making a
    still-wanted pool preset look orphaned and get **deleted**. A timeout or
    undecodable listing must abort (:class:`HelixError`) rather than risk
    that."""
    cid_to_name = {_cid(m): n for n, m in pool_by_name.items()}
    referenced: set = set()
    for sl in client.list_setlists(strict=True):
        sl_cid = _cid(sl)
        if sl_cid is None:
            continue
        for item in client.list_container(sl_cid, strict=True):
            if item.get("cctp") == Cctp.REFERENCE:
                nm = cid_to_name.get(item.get("rcid"))
                if nm:
                    referenced.add(nm)
    return referenced
