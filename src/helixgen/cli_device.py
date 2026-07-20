"""CLI entry points for `helixgen device` — network control of a Line 6
Helix Stadium over the LAN (OSC-over-ZeroMQ).

This module is a pure extraction of what was the `# --- device` section of
`helixgen.cli`; the `device` click group is imported back into `cli` there
(`cli.add_command(device)`) so `helixgen.cli:cli` stays the single entry
point and every `@device.*` registration is unchanged. Device-layer
third-party imports stay lazy inside each command (the optional `device`
extra), exactly as before.
"""
from __future__ import annotations

import functools
import json
import os
import sys
import time
from pathlib import Path

import click

from helixgen.hsp import read_hsp


# --- lazy device-layer accessors (backlog #54 / S7) -----------------------
#
# The networked-device layer is imported lazily *inside* each command body so
# the optional `device` extra's (pyzmq + msgpack) ImportError stays at
# device-verb call time — it must never surface at `helixgen`/`device --help`
# or CLI import. Importing `helixgen.device` itself is dependency-free (the
# third-party imports are lazy inside the client), so these accessors keep the
# ImportError surface exactly where it is while folding the ~65 formerly
# copy-pasted `from helixgen.device import ...` statements into two places.


def _client():
    """Lazy import of the device client pair `(HelixClient, HelixError)`."""
    from helixgen.device import HelixClient, HelixError

    return HelixClient, HelixError


def _manifest():
    """Lazy import of the manifest pair `(SetlistManifest, ManifestError)`."""
    from helixgen.device.manifest import SetlistManifest, ManifestError

    return SetlistManifest, ManifestError


def _serial_of(h, ip: str) -> str:
    """The connected device's serial (`/ProductInfoGet`), for keying its
    `devices/<serial>.json` observation file; falls back to `f"ip-{ip}"` when
    the query fails or reports no serial (preferring the client's RESOLVED
    address over the possibly-None --ip param, #74). Best-effort — never
    raises."""
    try:
        serial = (h.product_info() or {}).get("serial")
        if serial:
            return str(serial)
    except Exception:  # noqa: BLE001 — advisory identity, never fatal
        pass
    return f"ip-{getattr(h, 'ip', None) or ip}"


def _echo_library_rows(rows) -> None:
    """Emit the human-readable library listing (slot, name, on/off, setlists)."""
    for row in rows:
        sls = ", ".join(row["setlists"])
        click.echo(f"{(row['slot'] or '-'):<4} {row['name']:<28} "
                   f"{'on' if row['on_device'] else 'off':<3}  [{sls}]")


def _hss_print_listing(hss_file, bundle, filled, hss_mod) -> None:
    """`--list` output for `device setlist import-hss`: per-slot filled/empty
    state, payload format, and preset name (fully offline)."""
    click.echo(f"{hss_file.name}: setlist {bundle.name!r} "
               f"({len(filled)}/{len(bundle.slots)} slots filled)")
    for s in bundle.slots:
        state = "filled" if s.filled else "empty"
        fmt = f"[{s.payload_format}]" if s.filled else ""
        label = hss_mod.hss_slot_label(s) if s.filled else ""
        click.echo(f"  {s.pos:>3}  {state:6}  {fmt:9} {label}")


def _hss_print_dry_run(hss_file, target_setlist, filled, hss_mod) -> None:
    """`--dry-run` output for `device setlist import-hss`: the filled slots that
    would be imported, flagging any non-.hsp/non-content payload as a skip."""
    click.echo(f"DRY RUN: would import {len(filled)} preset(s) into "
               f"setlist {target_setlist!r}:")
    for s in filled:
        note = (f"  [{s.payload_format}]" if hss_mod.looks_like_content_blob(s.blob)
                else "  (would SKIP: payload isn't a .hsp or content blob)")
        click.echo(f"  slot {s.pos}: {hss_mod.hss_slot_label(s)}{note}")


def _hss_record_import_manifest(result, hss_mod) -> None:
    """Record freshly-imported presets in the tone library (pathless, source
    "import-hss") + the setlist's membership — load-bearing: without it a later
    targeted `device sync <setlist>` computes desired=[] and strips every
    reference the import just wrote. Best-effort (the device write succeeded)."""
    try:
        SetlistManifest, _ = _manifest()

        m = SetlistManifest.load()
        for w in hss_mod.record_import_in_manifest(m, result):
            click.echo(f"warning: {w}", err=True)
        m.save()
    except Exception as e:  # noqa: BLE001 — advisory; device write succeeded
        click.echo(f"warning: could not update local manifest: {e}", err=True)


# --- device: network control of a Line 6 Helix Stadium --------------------

#: Shared --ip help: honest about the resolution order (#68g, #74).
_IP_HELP = ("Helix device IP. Resolution: --ip wins, else $HELIXGEN_HELIX_IP, "
            "else the device record persisted by `helixgen device discover`. "
            "There is NO built-in default — with none of the three set, the "
            "verb fails fast (no network stall) telling you to run "
            "`helixgen device discover`. An empty or whitespace-only --ip is "
            "rejected (pass a real address, or omit the flag to fall back).")

#: Shared --setlist help for the preset verbs (#68b): the closed
#: user/factory/throwaway token set is gone — real device setlist names work.
_SETLIST_HELP = ("'user' (the preset POOL, where every user preset lives), "
                 "'factory', or a device setlist NAME (e.g. 'Throwaway' — "
                 "matched case-insensitively; setlists hold REFERENCES to "
                 "pool presets).")


def _resolve_ip_or_fail(explicit=None):
    """The #74 resolution chain (--ip > $HELIXGEN_HELIX_IP > persisted
    device record), converted to a crisp CLI failure — immediate, no
    network, naming `helixgen device discover` — when nothing resolves.
    ClickException (exit 1), matching what the client-resolving verbs
    surface, so agents see ONE exit code for the unconfigured state."""
    from helixgen.device import discovery

    try:
        return discovery.resolve_ip(explicit)
    except discovery.IPResolutionError as e:
        raise click.ClickException(str(e)) from e


def _telemetry_preflight(ip: str, port: int) -> None:
    """Fail fast when no device is reachable, BEFORE a telemetry subscribe
    (#64c). A ZMQ SUB socket connects lazily, so to `tuner`/`meters`/
    `measure` an unreachable or powered-off device is indistinguishable
    from silence — the verb would sit out its whole --seconds window and
    then report "no meter data". One cheap TCP connect to the RPC control
    port (--port) distinguishes the two up front with an instructive
    error instead."""
    from helixgen.device import discovery

    if not discovery.probe_reachable(ip, port):
        raise click.ClickException(
            f"no Helix Stadium reachable at {ip}:{port} (TCP connect "
            f"failed) — wrong IP, device off, or a stale device record? "
            f"Re-run `helixgen device discover`, or pass --ip / set "
            f"$HELIXGEN_HELIX_IP.")


def _ip_callback(ctx, param, value):
    """click callback for --ip: apply the resolution chain at parse time,
    LENIENTLY — an unresolvable IP becomes None so offline-capable modes
    (--list/--dry-run/local verbs) still parse and run. Anything that
    actually needs the device resolves-or-fails at use time (HelixClient /
    HelixSubscriber constructors, the _locked wrapper, the sftp verbs) —
    immediately and instructively, never as a connect stall.

    An *empty* or whitespace-only --ip (typically an unset shell variable
    expanded to nothing) is a mistake, not a request to fall back to the
    record — reject it loudly at parse time rather than resolving silently
    on to the next step (#77)."""
    from helixgen.device import discovery

    if value is not None and not value.strip():
        raise click.BadParameter(
            "--ip may not be empty or whitespace — omit it entirely to use "
            "$HELIXGEN_HELIX_IP or the `helixgen device discover` record, or "
            "pass a real address.")
    try:
        return discovery.resolve_ip(value)
    except discovery.IPResolutionError:
        return None


def _ip_option(f):
    """The shared --ip option (resolution chain #74) on its own, for verbs
    that don't take --port."""
    return click.option(
        "--ip",
        envvar="HELIXGEN_HELIX_IP",
        default=None,
        show_default="from `helixgen device discover` record",
        callback=_ip_callback,
        help=_IP_HELP,
    )(f)


def _port_callback(ctx, param, value):
    """click callback for --port: an explicit value wins; otherwise reuse the
    nonstandard RPC port persisted by `device discover` for the resolved
    device, falling back to the standard 2002 (#77). Resolved leniently — a
    None ip (offline modes) still yields the default."""
    from helixgen.device import discovery

    return discovery.resolve_port(ctx.params.get("ip"), explicit=value)


def _device_option(f):
    """Add shared --ip / --port options for the networked device commands."""
    f = click.option(
        "--port",
        default=None,
        show_default="from `helixgen device discover` record, else 2002",
        type=int,
        callback=_port_callback,
        help="Helix device control port. Defaults to the port persisted by "
             "`helixgen device discover` (2002 unless the device advertised "
             "a nonstandard one).",
    )(f)
    f = _ip_option(f)
    return f


# --- machine-local advisory device locks (workspace #71, 0.22.0) ----------

_NO_LOCK_HELP = ("Skip the machine-local advisory device lock for this verb "
                 "(DANGEROUS: concurrent helixgen processes may collide on "
                 "the device; see `helixgen device lock --help`).")


def _locked(*scopes: str, verb: str, when=None):
    """Auto-acquire the verb's advisory device-lock scope(s) for its duration.

    Innermost decorator (right above ``def``): wraps the raw callback, adds
    the per-verb ``--no-lock`` escape hatch, and — unless skipped — holds
    one lease per scope around the verb body (released on exit, even on
    failure). ``when(kwargs)`` may narrow the scopes dynamically (e.g.
    dry-run modes → no lock). A scope already covered by a session lease we
    own ($HELIXGEN_LOCK_TOKEN, or a `device lock` taken from this shell)
    is passed through and its TTL renewed. On contention, waits up to
    $HELIXGEN_LOCK_TIMEOUT (default 30 s; 0 = fail fast), then errors
    naming the holder.
    """
    def deco(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            no_lock = kwargs.pop("no_lock", False)
            eff = tuple(when(kwargs)) if when is not None else scopes
            if no_lock or not eff:
                return f(*args, **kwargs)
            from helixgen import locks

            # _ip_callback resolves leniently (None when unconfigured); a
            # verb about to take a device lock is going to write to the
            # device, so here the chain is enforced — fail fast with the
            # `device discover` pointer. The resolved value is written back
            # so the verb body sees the same address the lock is keyed by.
            ip = kwargs.get("ip") or _resolve_ip_or_fail()
            if "ip" in kwargs:
                kwargs["ip"] = ip
            try:
                lease = locks.acquire(ip, eff, label=f"helixgen device {verb}")
            except locks.LockHeld as e:
                raise click.ClickException(
                    f"{e} — wait and retry, raise HELIXGEN_LOCK_TIMEOUT, or "
                    f"(dangerous) pass --no-lock") from e
            with lease:
                return f(*args, **kwargs)

        return click.option("--no-lock", "no_lock", is_flag=True,
                            default=False, help=_NO_LOCK_HELP)(wrapper)
    return deco


def _resolve_setlist_dest(h, name: str):
    """Resolve a --setlist value against a live client (#68b).

    Returns ``(kind, container_cid, label)`` where ``kind`` is ``"pool"``
    (``user`` — the preset pool ``-2``), ``"factory"`` (``-1``), or
    ``"setlist"`` (a real device setlist, matched by display name
    case-insensitively; ``container_cid`` is its positive cid). The old
    closed ``user|factory|throwaway`` choice is gone: ``throwaway`` now
    resolves the device setlist actually named "Throwaway" (the old mapping
    to the setlists ROOT ``-5`` never worked: listings were empty and every
    write was rejected). Raises ``ClickException`` naming the device's real
    setlists when nothing matches.
    """
    from helixgen.device import Container, HelixError

    key = (name or "user").strip().lower()
    if key == "user":
        return "pool", int(Container.POOL), "user"
    if key == "factory":
        return "factory", int(Container.FACTORY), "factory"
    try:
        cid = h.resolve_setlist_cid(name)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    if cid is None:
        try:
            have = ", ".join(
                repr(m.get("name")) for m in h.list_setlists()) or "(none)"
        except HelixError:
            have = "(could not list)"
        raise click.ClickException(
            f"no device setlist named {name!r}; device setlists: {have}. "
            "Also valid: 'user' (the preset pool) and 'factory'.")
    # Return the setlist's CANONICAL display name, not the user's typed case
    # (the match is case-insensitive but the local manifest's setlist keys
    # are case-sensitive — a typed-case label would mint a duplicate).
    label = name
    try:
        label = next((str(m.get("name")) for m in h.list_setlists()
                      if m.get("cid_") == cid), name)
    except HelixError:
        pass
    return "setlist", int(cid), label


def _setlist_refs(h, setlist_cid: int, *, strict: bool = False):
    """A device setlist's preset REFERENCES (cctp 1003), posi-sorted, each
    with a display name (falling back to the referenced pool preset's)."""
    from helixgen.device import Cctp

    refs = [m for m in h.list_container(setlist_cid, strict=strict)
            if m.get("cctp") == Cctp.REFERENCE]
    refs.sort(key=lambda m: m.get("posi", 1 << 30))
    out = []
    for m in refs:
        m = dict(m)
        if not m.get("name") and m.get("rcid") is not None:
            ref = h.get_ref(m["rcid"]) or {}
            m["name"] = ref.get("name", "")
        out.append(m)
    return out


def _install_via_dest(h, kind: str, dest_cid: int, label: str, pos: int,
                      writer, *, force: bool = False):
    """Route a new-preset write to a --setlist destination (#68b).

    ``writer(container, cpos)`` performs the actual slot write (push /
    save / transcode-install) and returns the new pool cid. For
    kind=="pool" it writes straight at ``pos``. For kind=="setlist" the
    preset content always lives in the POOL: the setlist position ``pos``
    is checked empty (strict — always, even under ``force``; backlog #69),
    the content is written at the lowest empty pool posi, and a REFERENCE
    is added to the setlist at ``pos``. The factory container is read-only.

    ``force`` (the ``slots restore --force`` escape hatch) only ever
    applies to a POOL destination's slot check (inside ``writer``); an
    occupied NAMED-SETLIST position is refused even with ``force``:
    ``reference_into_setlist`` never removes an incumbent, so proceeding
    would stack a second reference at one position — device behavior that
    is uncataloged (backlog #69; a 2026-07-17 hardware-characterization
    attempt was blocked by a persistent backlog-#38 /CreateContent
    status-1 episode). Fail-safe: refuse, and point at removing the
    incumbent reference first. The occupancy listing is strict (#40) —
    with or without ``force``, a listing timeout aborts rather than
    reading the position as empty.

    Returns ``(new_cid, pool_posi, ref_cid_or_None)``.
    """
    from helixgen.device import Container

    if kind == "factory":
        raise click.ClickException("the factory container is read-only")
    if kind == "pool":
        return writer(dest_cid, pos), pos, None
    occupant = h.find_by_pos(dest_cid, pos, strict=True)
    if occupant is not None:
        if force:
            raise click.ClickException(
                f"setlist {label!r} position {pos} already holds a reference "
                f"(cid {occupant.get('cid_')}); --force cannot replace it: "
                "the device's behavior with two references stacked at one "
                "position is uncataloged (backlog #69). Remove the incumbent "
                f"first (`helixgen device delete {occupant.get('cid_')} "
                f"--setlist {label!r}`), then re-run.")
        raise click.ClickException(
            f"setlist {label!r} position {pos} is not empty")
    pool_pos = h._lowest_empty_posi(Container.POOL)
    new_cid = writer(int(Container.POOL), pool_pos)
    if new_cid is None:
        # the pool write failed — never send a nil-cid reference to the device
        return None, pool_pos, None
    ref_cid = h.reference_into_setlist(dest_cid, new_cid, pos)
    if ref_cid is None:
        click.echo(
            f"warning: installed cid {new_cid} into the pool (posi "
            f"{pool_pos}) but could not add the reference into setlist "
            f"{label!r} at {pos}; add it with `device create --from "
            f"{new_cid} --setlist {label!r} --pos {pos}`", err=True)
    return new_cid, pool_pos, ref_cid


def _auto_upload_irs(ip: str, hashes) -> None:
    """Upload each missing IR hash by resolving it to a local wav via the
    helixgen IR mapping, then SFTP-pushing it (device auto-registers).

    Thin echo-formatting wrapper around the shared core in
    ``helixgen.device.ir_upload`` (backlog #6 — the same core also backs
    ``device sync``). Unlike ``device sync``, which tolerates a per-IR upload
    failure and keeps going (a sync run shouldn't be all-or-nothing on IR
    trouble), ``device install --auto-irs`` **aborts the whole install** on a
    hard upload error (``push_ir`` itself failing, e.g. a dropped
    connection) — a preset whose referenced IR couldn't be pushed is never
    installed. It does so via a clean ``ClickException``, and only after
    echoing every hash's outcome (not just the first failure) so the user
    sees the full picture before the command exits non-zero."""
    from helixgen.device import ir_upload

    upload_errors = []
    for entry in ir_upload.upload_missing_irs(ip, list(hashes)):
        outcome = entry.get("outcome")
        if outcome == "no_mapping":
            # Applies identically to every hash (mapping.json itself failed
            # to load) — abort the whole command, matching the original
            # upfront-check behavior.
            raise click.ClickException(entry["note"])
        if outcome in ("already", "imported"):
            click.echo(entry["note"])
        else:
            click.echo(f"warning: {entry['note']}", err=True)
            if outcome == "upload_error":
                upload_errors.append(entry["note"])
    if upload_errors:
        raise click.ClickException(
            "IR upload failed: " + "; ".join(upload_errors))


def _tone_by_cid(cid: int):
    """Return the tone name whose observed device cid matches, or None. Reads
    the per-device observation files (design §3 — cid/posi no longer lives in
    the manifest)."""
    from helixgen.device import observations as obsmod
    return obsmod.lookup_name_by_cid(cid)


def _record_placement(*, setlist: str, posi: int, name: str, cid: int | None,
                      source_kind: str, source_path: str | None = None,
                      model: str | None = None, serial: str | None = None,
                      setlist_pos: int | None = None) -> None:
    """Record a device placement: the desired ``slot``/setlist membership go
    into the tone-library manifest (intent); the observed ``cid``/``posi`` go
    into ``devices/<serial>.json`` (observation). ``posi`` is the POOL
    position; ``setlist_pos`` (when the write targeted a named setlist) is the
    tone's position within that setlist's membership order. Best-effort: a
    failure warns but never fails the device command (the write already
    succeeded)."""
    try:
        SetlistManifest, _ = _manifest()

        m = SetlistManifest.load()
        if name not in m.tones:
            if source_path and str(source_path).endswith(".hsp"):
                name = m.register_tone(source_path, source="import-local")
            elif source_path:
                # a pushed .sbe (or other local source): store the path verbatim
                m.tones[name] = {"path": str(source_path), "content_hash": None,
                                 "source": "push", "slot": None}
            else:
                m.register_pathless(name, source="save" if source_kind == "save" else "create")
        slot = _slot_from_posi(posi)
        if slot:
            m.mark_on_device(name, slot)
        if setlist and setlist != "user":
            m.add_to_setlist(setlist, name, pos=setlist_pos)
        m.save()
        # Observed placement -> the connected device's observation file.
        if cid is not None and serial:
            from helixgen.device import observations as obsmod
            obs = obsmod.load_observations(serial)
            obs.tones[name] = {"cid": cid, "posi": posi}
            obsmod.save_observations(obs)
    except Exception as e:  # noqa: BLE001 — advisory, never fatal
        click.echo(f"warning: could not update tone library: {e}", err=True)


def _slot_from_posi(posi):
    from helixgen.device.manifest import _posi_to_slot
    return _posi_to_slot(posi)


def _ledger_rename(cid: int, new_name: str) -> None:
    """Best-effort: reflect a device rename in the tone library — the
    manifest's intent record AND the per-device observation file's ``tones``
    key (Minor 5: the observation file used to keep the stale name)."""
    try:
        SetlistManifest, _ = _manifest()

        m = SetlistManifest.load()
        old = _tone_by_cid(cid)
        if old and old != new_name:
            if old in m.tones:
                m.tones[new_name] = m.tones.pop(old)
                for rec in m.setlists_map.values():
                    rec["tones"] = [new_name if t == old else t for t in rec["tones"]]
                m.save()
            from helixgen.device import observations as obsmod
            obsmod.rename_tone(old, new_name)
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not update tone library: {e}", err=True)


def _ledger_remove(cid: int) -> None:
    """Best-effort: drop a deleted preset from the tone library (clears its
    desired on-device slot; the tone stays in the library) and drop its key
    from the per-device observation file's ``tones`` map (Minor 5). The rest
    of that observation file self-heals on the next sync."""
    try:
        SetlistManifest, _ = _manifest()

        m = SetlistManifest.load()
        name = _tone_by_cid(cid)
        if name:
            if name in m.tones:
                m.tones[name]["slot"] = None
                m.tones[name].pop("auto_marked", None)
                m.save()
            from helixgen.device import observations as obsmod
            obsmod.remove_tone(name)
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not update tone library: {e}", err=True)


def _install_hsp_open(h, body: dict, container: int, pos: int, name: str, *,
                      setlist_label: str, auto_irs: bool = False,
                      force: bool = False, ip: str | None = None) -> int:
    """Install a parsed .hsp ``body`` onto an already-open client at
    ``(container, pos)`` and return the new cid. Shared by ``device install``
    and ``device slots restore``. Raises ClickException on any failure.

    Template-free: the ``.hsp`` is transcoded straight into a device
    ``_sbepgsm`` blob (:func:`transcode.hsp_to_sbepgsm`) and written into an
    empty slot — no device template is loaded, so the active tone is untouched.

    ``force`` skips the slot-emptiness check so the push proceeds at an
    occupied posi (``device slots restore --force`` — #25; the occupant is
    NOT deleted, matching the ``.sbe`` path); without it an occupied slot is
    refused. The check is strict (backlog #40): a listing timeout raises
    instead of reading as "empty", so it never proceeds to write into a slot
    it couldn't actually confirm was free.
    """
    from helixgen.device import bridge, transcode

    if not force and h.find_by_pos(container, pos, strict=True) is not None:
        raise click.ClickException(f"{setlist_label} slot {pos} is not empty")
    missing = sorted(bridge.check_irs(h, body)["missing"])
    if missing and auto_irs:
        _auto_upload_irs(ip, missing)
    else:
        for m in missing:
            click.echo(
                f"warning: IR {m} is referenced but not on the device; "
                f"re-run with --auto-irs, or import it (helixgen register-irs / "
                f"the editor), or the cab will be silent", err=True)
    try:
        blob = transcode.hsp_to_sbepgsm(body, strict=True)
    except bridge.UnresolvedModel as e:
        raise click.ClickException(str(e)) from e
    with h.mutating():
        cid = h._raw.push_to_slot(container, pos, name, blob)
    if cid is None:
        raise click.ClickException("failed to install preset")
    return cid


@click.group(name="device")
def device() -> None:
    """Drive a networked Line 6 Helix Stadium over the LAN (Stadium-only).

    Requires the `device` extra (`pip install 'helixgen[device]'`) for the
    pyzmq/msgpack transport. Run `helixgen device discover` ONCE to find the
    Stadium on your LAN and persist its address; after that every verb
    resolves the IP automatically (--ip > $HELIXGEN_HELIX_IP > the persisted
    device record — no built-in default; with none set, verbs fail fast).

    READ vs WRITE: verbs that only read/list device state are safe (info,
    active, read, list, setlists, list-irs, blocks, params, settings
    list/get, tuner, meters, measure, watch, backup, pull, pull-ir, plus the
    offline verbs local-list, library, slots list, globaleq list and
    --list/--dry-run modes).
    Everything else MUTATES the device — and the live-ops verbs (snapshot,
    bypass, model, set-param) change the ACTIVE tone immediately. Prefer an
    empty/expendable slot when testing writes.

    The Stadium's network stack is flaky: if a verb/sync drops or stalls,
    re-run it — `sync` and the live-ops verbs are idempotent +
    auto-reconnecting; the slot-writing verbs (install/save/push/create)
    fail safe on an occupied slot instead; `setlist import-hss` is the one
    NOT-idempotent retry (see its --help). If it keeps dropping, reboot
    the Helix.

    The tone library manifest (~/.helixgen/setlists/manifest.json, override
    $HELIXGEN_SETLISTS; a legacy ~/.helixgen/setlists.json auto-migrates on
    first load) is the single management record: every generated tone
    auto-registers there; "on the device" ⟺ the tone has a slot; `device
    sync` mirrors ONLY managed tones and never touches untracked device
    presets. Presets live once in the pool (cid container -2) and setlists
    hold references to them. A specific Helix's OBSERVED placement (cid/posi)
    lives separately, in ~/.helixgen/devices/<serial>.json — not in the
    manifest.

    SEE ALSO: docs/CLI.md "Device commands" for the full per-verb reference.

    LOCKING: every device-MUTATING verb auto-acquires a machine-local
    advisory lock scope (editbuffer / library / irs / globals) for its
    duration so concurrent helixgen processes on this machine don't collide;
    hold scopes across calls with `device lock`, inspect with `device lock
    --status`, and see docs/CLI.md "Device locks" for the verb -> scope table.
    """


_LOCK_SCOPE_HELP = (
    "editbuffer = live-ops on the ACTIVE tone; library = pool/setlist/"
    "preset-content writes; irs = device IR writes; globals = Global "
    "Settings/EQ writes; all = exclusive over the whole device.")


@device.command(name="lock")
@click.option("--scope", "scopes", multiple=True,
              type=click.Choice(["editbuffer", "library", "irs", "globals",
                                 "all"]),
              default=("all",), show_default=True,
              help="Scope(s) to hold (repeatable). " + _LOCK_SCOPE_HELP)
@click.option("--label", default=None,
              help="Who/what holds the lock (shown to blocked processes; "
                   "required unless --status).")
@click.option("--ttl", type=float, default=None, show_default="900",
              help="Lease time-to-live in seconds; every covered verb you "
                   "run renews it. An expired lease is reclaimed by the "
                   "next contender. 0 = no TTL expiry (reclaim then relies "
                   "on pid-liveness or `device unlock`).")
@click.option("--status", "show_status", is_flag=True, default=False,
              help="Don't lock — report the device's current leases "
                   "(scope, holder, age, live/stale, ours) and exit 0.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="With --status: emit the lease rows as JSON.")
@_ip_option
def device_lock(scopes, label, ttl, show_status, as_json, ip) -> None:
    """Hold a machine-local advisory device lock across CLI calls (a session
    lease), so concurrent helixgen processes don't collide on the device.

    Every device-MUTATING verb already auto-acquires its scope(s) for the
    verb's duration — `device lock` is for holding a scope ACROSS calls
    (an agent session, a test run). Leases are JSON files under
    ~/.helixgen/locks/<ip>/<scope>.lock ($HELIXGEN_LOCKS overrides the
    root); no daemon, no fcntl. Purely local: the device itself is never
    contacted, and clients on OTHER machines or the Stadium desktop editor
    are NOT covered (advisory, machine-local only).

    The printed HELIXGEN_LOCK_TOKEN is how your later CLI calls prove the
    lease is theirs — export it, and covered verbs pass through (renewing
    the TTL) instead of blocking; calls from the same shell also pass
    through by parent-pid. Contenders wait $HELIXGEN_LOCK_TIMEOUT seconds
    (default 30; 0 = fail fast), reclaim stale leases (expired TTL or dead
    same-host pid) with a warning, then fail naming the holder. Release
    with `device unlock`; inspect with `device lock --status [--json]`.
    """
    from helixgen import locks

    if show_status:
        # Read-only introspection stays usable on an unconfigured machine
        # (0.22.0 behavior: exit 0), locks being keyed per-ip.
        if not ip:
            if as_json:
                click.echo(json.dumps([], indent=2))
            else:
                click.echo("no device IP configured (run `helixgen device "
                           "discover` or pass --ip) — no leases to report")
            return
        rows = locks.status(ip)
        if as_json:
            click.echo(json.dumps(rows, indent=2))
            return
        if not rows:
            click.echo(f"no device locks held for {ip}")
            return
        for r in rows:
            age = (f"{r['age_seconds']:.0f}s"
                   if isinstance(r.get("age_seconds"), (int, float)) else "?")
            ttl = (f"{r['ttl_seconds']:g}s"
                   if isinstance(r.get("ttl_seconds"), (int, float)) else "?")
            click.echo(
                f"{r['scope']:<10} {r['state']:<5} "
                f"{'ours' if r['ours'] else '    '}  {r['label']!r}  "
                f"pid {r['pid']} on {r['hostname']}  "
                f"age {age} / ttl {ttl}")
        return

    ip = ip or _resolve_ip_or_fail()  # locks are keyed per-ip (#74)
    if not label:
        raise click.ClickException(
            "--label is required (name the session holding the lock, e.g. "
            "--label 'setlist rebuild agent')")
    try:
        # Session leases record the INVOKING SHELL's pid (this CLI process
        # exits immediately); never released here — `device unlock` frees
        # them. Re-locking an owned scope renews it in place (new
        # label/ttl, SAME stored token).
        token, outcomes = locks.session_lock(
            ip, scopes, label=label,
            ttl=locks.DEFAULT_SESSION_TTL if ttl is None else ttl,
            pid=os.getppid())
    except locks.LockHeld as e:
        raise click.ClickException(str(e)) from e
    except locks.LockError as e:
        raise click.ClickException(str(e)) from e
    for s, action in outcomes:
        click.echo(f"{action} '{s}' on {ip} (label {label!r})")
    click.echo(f"HELIXGEN_LOCK_TOKEN={token}")
    click.echo("export HELIXGEN_LOCK_TOKEN so your helixgen calls pass "
               "through this lock; release with `helixgen device unlock`. "
               "Run `device lock` from your long-lived shell (not via a "
               "wrapper script): the lease records the parent pid, and a "
               "dead parent gives contenders a reclaim path after "
               f"{locks.SESSION_PID_GRACE_S:.0f}s idle.",
               err=True)


@device.command(name="unlock")
@click.option("--scope", "scopes", multiple=True,
              type=click.Choice(["editbuffer", "library", "irs", "globals",
                                 "all"]),
              default=None,
              help="Scope(s) to release (repeatable; default: every lease "
                   "you own). " + _LOCK_SCOPE_HELP)
@click.option("--force", is_flag=True, default=False,
              help="Also break leases you do NOT own (dangerous — the "
                   "holder may be mid-write on the device).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit {released, kept} as JSON.")
@_ip_option
def device_unlock(scopes, force, as_json, ip) -> None:
    """Release advisory device locks taken with `device lock`.

    Ownership is proven by $HELIXGEN_LOCK_TOKEN or by calling from the same
    shell that locked (parent-pid match). Without --scope, every lease you
    own is released and foreign leases are left (reported, not an error);
    an EXPLICIT --scope you don't own is an error unless --force (which
    breaks even a live foreign lease — dangerous). Stale leases (expired
    TTL / dead pid) can always be cleared.
    """
    ip = ip or _resolve_ip_or_fail()  # locks are keyed per-ip (#74)
    from helixgen import locks

    try:
        res = locks.release_scopes(ip, list(scopes) if scopes else None,
                                   force=force)
    except locks.LockError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(res, indent=2))
        return
    for s in res["released"]:
        click.echo(f"released '{s}' on {ip}")
    if not res["released"]:
        click.echo(f"no leases of yours to release on {ip}")
    for k in res["kept"]:
        click.echo(f"kept '{k['scope']}' — held by {k['holder']}", err=True)


@device.command(name="list")
@click.option(
    "--setlist",
    default="user",
    show_default=True,
    help="What to list: " + _SETLIST_HELP,
)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the raw device records as JSON.")
@_device_option
def device_list(setlist: str, as_json: bool, ip: str, port: int) -> None:
    """List the presets in the pool, factory, or a named setlist. Read-only.

    --setlist user (default) lists the preset POOL — where every user preset
    actually lives; each row shows the slot label, the preset's integer CID
    (the content id every other device verb addresses presets by), and its
    name. --setlist <NAME> lists a real device setlist (case-insensitive
    display name, e.g. Throwaway): its entries are REFERENCES to pool
    presets, so each row shows the position, the reference's own cid, and
    rcid= the pool preset it points at. --json emits the raw records
    (cid_, name, cctp, posi, and rcid for references).
    """
    HelixClient, HelixError = _client()
    from helixgen.device import slot_label

    try:
        with HelixClient(ip, port) as h:
            kind, container, label = _resolve_setlist_dest(h, setlist)
            if kind == "setlist":
                items = _setlist_refs(h, container)
            else:
                items = h.list_presets(container)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(items, indent=2))
        return
    if kind == "setlist":
        for m in items:
            posi = m.get("posi")
            click.echo(f"{'?' if posi is None else posi:>3}  "
                       f"cid={m.get('cid_')}  "
                       f"rcid={m.get('rcid')}  {m.get('name', '')}")
        return
    for m in items:
        click.echo(f"{slot_label(m.get('posi')):<4} cid={m.get('cid_')}  {m.get('name', '')}")


@device.command(name="setlists")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the setlist list as JSON.")
@_device_option
def device_setlists(as_json: bool, ip: str, port: int) -> None:
    """List the device's setlist containers."""
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            setlists = h.list_setlists()
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(setlists, indent=2))
        return
    for m in setlists:
        click.echo(f"cid={m.get('cid_')}  {m.get('name', '')}")


@device.command(name="discover")
@click.option("--timeout", type=float, default=3.0, show_default=True,
              help="mDNS listen window in seconds (values below 0.5 are "
                   "floored to 0.5).")
@click.option("--probe/--no-probe", default=True, show_default=True,
              help="If mDNS finds nothing, fall back to a bounded TCP "
                   "connect-probe of the LOCAL /24 subnet only, on the "
                   "Stadium's RPC port 2002 (short timeouts, bounded "
                   "concurrency; never probes beyond the local subnet, "
                   "and refuses to scan at all when this machine's own "
                   "address is not in a private RFC 1918 range).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the discovered device records as JSON.")
@click.option("--forget", "forget", metavar="SERIAL-OR-IP", default=None,
              help="Instead of discovering, PRUNE the persisted record whose "
                   "serial or IP matches SERIAL-OR-IP (a stale DHCP lease you "
                   "no longer want resolved). Exits nonzero with a clear "
                   "message if nothing matches or no records exist yet.")
def device_discover(timeout: float, probe: bool, as_json: bool,
                    forget: "str | None" = None) -> None:
    """Find Helix Stadium devices on the LAN and PERSIST their addresses.

    Discovery is mDNS/Bonjour first — the Stadium advertises
    `_stadiumserver._tcp` and answers a one-shot multicast query itself —
    with an optional local-subnet TCP probe fallback for networks that
    block multicast. Every candidate is CONFIRMED with the read-only
    /ProductInfoGet handshake (serial, model, firmware) before it is
    trusted; confirmed devices are persisted into
    ~/.helixgen/devices/<serial>.json, which every other device verb then
    resolves automatically (--ip > $HELIXGEN_HELIX_IP > this record).

    Run it once (and again whenever the device's DHCP lease changes).
    Community prior art: the Stadium desktop app's DISCOVERY layer is
    flaky while direct-to-IP sessions are stable — so helixgen discovers
    once, persists, and keeps every session direct-to-IP.

    Read-only on the device (no lock taken). With several Stadiums found,
    all are listed and persisted; the most recently discovered becomes the
    default (deterministic: ip_updated_at desc, then serial desc) — pass
    --ip on any verb to target another.

    Use --forget SERIAL-OR-IP to PRUNE a stale persisted record instead of
    discovering (matches a record's serial or IP; exits nonzero with a
    clear message when nothing matches or no records exist yet).

    \b
    Known limitations (backlog #77):
      * Both mechanisms look at the DEFAULT-ROUTE interface. With a VPN
        up, that is usually the tunnel — a LAN-attached Stadium can be
        missed. Disconnect the VPN for the one-shot discover, or bypass
        discovery with --ip / $HELIXGEN_HELIX_IP.
      * The mDNS listener hears unicast replies only (no multicast group
        join). The Stadium answers unicast (verified live, fw 1.3.2);
        firmware that replied only via multicast would fall through to
        the subnet probe.
      * The subnet probe stays inside the machine's own /24 and refuses
        public (non-RFC 1918) ranges outright.
    """
    from helixgen.device import discovery, observations

    if forget is not None:
        try:
            removed = observations.forget_device(forget)
        except FileNotFoundError as e:
            raise click.ClickException(
                f"no persisted device records yet ({e} does not exist) — "
                "nothing to forget; run `helixgen device discover` first")
        if not removed:
            raise click.ClickException(
                f"no persisted record matches {forget!r} — run "
                "`helixgen device discover --json` to see the recorded "
                "serial/IP of each device")
        for path in removed:
            click.echo(f"forgot {path.stem} ({path})", err=True)
        if as_json:
            click.echo(json.dumps([str(p) for p in removed], indent=2))
        return

    HelixClient, HelixError = _client()

    candidates = discovery.mdns_discover(timeout=timeout)
    if not candidates and probe:
        click.echo("mDNS found nothing — falling back to a TCP connect-probe "
                   "of the local /24 subnet (port 2002)…", err=True)
        candidates = [discovery.Candidate(ip=ip, via="probe")
                      for ip in discovery.probe_subnet()]
    if not candidates:
        raise click.ClickException(
            "no Helix Stadium found on the LAN (mDNS `_stadiumserver._tcp` "
            "browse" + (" + local-subnet probe" if probe else "") + "). "
            "Is the device powered on and on this network/subnet? Try a "
            "longer --timeout, or pass --ip explicitly to the other verbs.")

    confirmed = []
    for cand in sorted(candidates, key=lambda c: c.ip):
        # Confirm (and later persist) on the candidate's own RPC port — a
        # nonstandard SRV advertisement carries a nonstandard port (#77).
        cand_port = cand.rpc_port or discovery.RPC_PORT
        try:
            with HelixClient(cand.ip, cand_port) as h:
                info = h.product_info()
        except (HelixError, OSError) as e:
            click.echo(f"warning: {cand.ip} (via {cand.via}) did not pass "
                       f"the device-info handshake — skipped ({e})", err=True)
            continue
        serial = str(info.get("serial") or f"ip-{cand.ip}")
        confirmed.append({
            "ip": cand.ip,
            "serial": serial,
            "model": info.get("model"),
            "helixgen_model": info.get("helixgen_model"),
            "firmware": info.get("firmware"),
            "hostname": cand.hostname,
            "via": cand.via,
            # only the nonstandard port is recorded; None = default 2002.
            "port": cand.rpc_port,
        })
    if not confirmed:
        raise click.ClickException(
            "discovery found candidate address(es) but none passed the "
            "device-info handshake — see warnings above.")

    # Persist serial-ascending so the LAST write (newest ip_updated_at) is
    # the highest serial — matching the resolver's deterministic tie-break
    # (ip_updated_at desc, then serial desc).
    confirmed.sort(key=lambda d: d["serial"])
    for row in confirmed:
        path = observations.record_device_ip(
            row["serial"], row["ip"],
            model=row.get("model"), firmware=row.get("firmware"),
            port=row.get("port"))
        row["record"] = str(path)
    recorded = observations.devices_with_ips()
    default_serial = recorded[0]["serial"] if recorded else None
    for row in confirmed:
        row["default"] = row["serial"] == default_serial

    # $HELIXGEN_HELIX_IP outranks the record — a stale export would keep
    # verbs pointed at the old address no matter how often you re-discover.
    env_ip = os.environ.get("HELIXGEN_HELIX_IP")
    if env_ip and not any(r["ip"] == env_ip for r in confirmed):
        click.echo(f"warning: $HELIXGEN_HELIX_IP={env_ip} is set and outranks "
                   "the persisted record — verbs will keep using it, not the "
                   "address just discovered; unset it (or update it) to use "
                   "the record", err=True)

    from helixgen import home
    click.echo(f"persisted {len(confirmed)} device record(s) under "
               f"{home.devices_dir()} — device verbs now resolve the IP "
               f"automatically", err=True)
    if len(confirmed) > 1:
        click.echo("multiple devices found: the most recently discovered is "
                   "the default; pass --ip to target another", err=True)
    if as_json:
        click.echo(json.dumps(confirmed, indent=2))
        return
    for row in confirmed:
        model = row.get("helixgen_model") or row.get("model") or "?"
        star = "  <- default" if row.get("default") else ""
        port = f"  port {row['port']}" if row.get("port") else ""
        click.echo(f"{row['ip']:<15}  serial {row['serial']}  {model}  "
                   f"fw {row.get('firmware') or '?'}  (via {row['via']})"
                   f"{port}{star}")


@device.command(name="info")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the device info as JSON (includes the raw reply).")
@_device_option
def device_info(as_json: bool, ip: str, port: int) -> None:
    """Show the connected device's identity: model, firmware, serial, storage.

    Read-only (`/ProductInfoGet` — part of the editor's own connect
    handshake); never touches presets or the edit buffer.
    """
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            info = h.product_info()
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return

    def _gb(n):
        return f"{n / 1e9:.1f} GB" if isinstance(n, (int, float)) else "?"

    model = info.get("model") or "?"
    if info.get("helixgen_model"):
        model = f"{model} ({info['helixgen_model']})"
    click.echo(f"model:     {model}")
    click.echo(f"device id: {info.get('device_id')}")
    click.echo(f"serial:    {info.get('serial')}")
    fw = info.get("firmware") or "?"
    build = info.get("firmware_build")
    date = info.get("firmware_date")
    extra = " ".join(str(x) for x in (f"build {build}" if build else None,
                                      date) if x)
    click.echo(f"firmware:  {fw}{f'  ({extra})' if extra else ''}")
    click.echo(f"storage:   {_gb(info.get('sd_available_bytes'))} free of "
               f"{_gb(info.get('sd_total_bytes'))}")


@device.group(name="settings")
def device_settings() -> None:
    """Read/write the device's **Global Settings** over the network.

    The Stadium exposes its Global Settings pages (Ins/Outs, Switches/Pedals,
    Displays, Preferences, Songs, Tempo/Click, MIDI, Date/Time) plus Tuner and
    Wireless as device *properties*. `list` browses the catalog, `get` reads a
    live value, `set` writes one — no Stadium app needed. Keys are grouped into
    pages; run `helixgen device settings list` to see them.
    """


@device_settings.command(name="list")
@click.option("--page", "page", default=None,
              help="Only this page (e.g. ins-outs, midi, tuner). Omit for all.")
@click.option("--values", is_flag=True, default=False,
              help="Also fetch each key's live value + range from the device.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit as JSON.")
@_device_option
def device_settings_list(page, values, as_json, ip, port):
    """List Global-Settings keys, grouped by page (offline unless --values)."""
    from helixgen.device import settings as S
    HelixClient, HelixError = _client()

    try:
        catalog = {page: S.keys_for_page(page)} if page else S.pages()
    except KeyError:
        raise click.ClickException(
            f"unknown page {page!r}; choose from {', '.join(S.page_names())}")

    if not values:
        if as_json:
            click.echo(json.dumps(catalog, indent=2))
            return
        for pg in sorted(catalog):
            click.echo(f"\n[{pg}]")
            for k in catalog[pg]:
                click.echo(f"  {k}")
        return

    rows = []
    aborted = None
    try:
        with HelixClient(ip, port) as h:
            for pg in sorted(catalog):
                for k in catalog[pg]:
                    try:
                        d = h.get_property_def(k)
                        v = h.get_property(k)
                        rows.append({"page": pg, "key": k, "name": d.name,
                                     "value": v.value,
                                     "display": S.render_value(d, v.value),
                                     "type": d.type, "min": d.vmin, "max": d.vmax,
                                     "enum": d.enum})
                    except (HelixError, ValueError) as e:
                        rows.append({"page": pg, "key": k, "error": str(e)})
                    # a dead socket (reconnect exhausted) makes every remaining
                    # key fast-fail — stop and report a clean partial result.
                    if h.sock is None:
                        aborted = k
                        break
                if aborted:
                    break
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        out = {"settings": rows}
        if aborted:
            out["aborted_at"] = aborted
        click.echo(json.dumps(out, indent=2))
        return
    cur = None
    for r in rows:
        if r["page"] != cur:
            cur = r["page"]
            click.echo(f"\n[{cur}]")
        if "error" in r:
            click.echo(f"  {r['key']:<40} <err: {r['error']}>")
        else:
            rng = (f"  {{{', '.join(r['enum'])}}}" if r["enum"]
                   else f"  [{r['min']}..{r['max']}]")
            click.echo(f"  {r['key']:<40} = {r['display']:<16} {r['name']}{rng}")
    if aborted:
        click.echo(f"\n(connection lost — stopped at {aborted}; re-run to continue)")


@device_settings.command(name="get")
@click.argument("key")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit as JSON.")
@_device_option
def device_settings_get(key, as_json, ip, port):
    """Read one Global-Settings value (with its name, range, and enum labels)."""
    from helixgen.device import settings as S
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            d = h.get_property_def(key)
            v = h.get_property(key)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps({
            "key": key, "name": d.name, "value": v.value,
            "display": S.render_value(d, v.value), "type": d.type,
            "min": d.vmin, "max": d.vmax, "default": d.default,
            "enum": d.enum, "page": S.page_for_key(key)}, indent=2))
        return
    rng = (f"{{{', '.join(d.enum)}}}" if d.enum else f"[{d.vmin}..{d.vmax}]")
    click.echo(f"{key}")
    click.echo(f"  name    {d.name}")
    click.echo(f"  value   {S.render_value(d, v.value)}")
    click.echo(f"  range   {rng}   (default {d.default})")


@device_settings.command(name="set")
@click.argument("key")
@click.argument("value")
@_device_option
@_locked("globals", verb="settings set")
def device_settings_set(key, value, ip, port):
    """Write one Global-Settings value. VALUE may be a number or an enum label
    (e.g. `helixgen device settings set global.tuner.type Strobe`)."""
    from helixgen.device import settings as S
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            d = h.get_property_def(key)
            coerced = S.coerce_value(d, value)
            ok = h.set_property(key, d.type, coerced)
            readback = h.get_property(key)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"device did not confirm the write to {key}")
    click.echo(f"{key} = {S.render_value(d, readback.value)}  ({d.name})")


@device.group(name="globaleq")
def device_globaleq() -> None:
    """Write the device's **Global EQ** over the network (no Stadium app).

    The Stadium has three independent Global EQs — one per output layer: 1/4"
    (`qtr`), XLR (`xlr`), Phones (`pho`) — each a 7-band EQ (lowcut, lowshelf,
    low, mid, high, highshelf, highcut) plus an output level. `list` prints the
    catalog; `set` writes one band parameter. Global EQ is **write-only** over
    the network (the device serves no per-key read-back), so there is no `get`.
    """


@device_globaleq.command(name="list")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit as JSON.")
def device_globaleq_list(as_json):
    """List the Global EQ outputs, bands, and their valid params (offline)."""
    from helixgen.device import globaleq as G

    cat = G.catalog()
    if as_json:
        click.echo(json.dumps(cat, indent=2))
        return
    cur = None
    for r in cat:
        if r["output"] != cur:
            cur = r["output"]
            click.echo(f"\n[{r['output']}]  {r['output_name']}")
        if r["band"]:
            freq = f"  (default {r['default_freq']:g} Hz)" if r["default_freq"] else ""
            click.echo(f"  {r['band']:<10} #{r['band_index']}  "
                       f"params: {', '.join(r['params'])}{freq}")
        else:
            click.echo(f"  {'(output)':<10}      params: {', '.join(r['params'])}")
    click.echo("\nExample: helixgen device globaleq set qtr low gain 3.5")


@device_globaleq.command(
    name="set", context_settings={"ignore_unknown_options": True})
@click.argument("output")
@click.argument("band")
@click.argument("param")
@click.argument("value")
@_device_option
@_locked("globals", verb="globaleq set")
def device_globaleq_set(output, band, param, value, ip, port):
    """Write one Global EQ parameter.

    OUTPUT ∈ qtr/xlr/pho. BAND ∈ lowcut/lowshelf/low/mid/high/highshelf/highcut
    (or use `-` with PARAM `level` for the output level). PARAM ∈
    enable/freq/gain/q/slope/level. Examples:

      helixgen device globaleq set qtr low gain 3.5

      helixgen device globaleq set xlr lowcut enable off

      helixgen device globaleq set pho - level -2.0
    """
    from helixgen.device import globaleq as G
    HelixClient, HelixError = _client()

    band_arg = "" if band.strip() in ("-", "") else band
    try:
        key = G.key_for(output, band_arg, param)  # validates before connecting
        with HelixClient(ip, port) as h:
            ok = h.set_globaleq(output, band_arg, param, value)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"device did not confirm the Global EQ write ({key})")
    click.echo(f"{key} = {value}")


@device.command(name="read")
@click.argument("cid", type=int)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the content ref as JSON.")
@_device_option
def device_read(cid: int, as_json: bool, ip: str, port: int) -> None:
    """Read the content ref for a CID (name/slot/parent)."""
    HelixClient, HelixError = _client()
    from helixgen.device import slot_label

    try:
        with HelixClient(ip, port) as h:
            ref = h.get_ref(cid)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if ref is None:
        raise click.ClickException(f"no content ref for cid {cid}")
    if as_json:
        click.echo(json.dumps(ref, indent=2))
        return
    click.echo(f"name:   {ref.get('name', '')}")
    click.echo(f"cid:    {ref.get('cid_', cid)}")
    click.echo(f"parent: {ref.get('cpid')}")
    click.echo(f"slot:   {slot_label(ref.get('posi'))}")


@device.command(name="load")
@click.argument("cid", type=int)
@_device_option
@_locked("editbuffer", verb="load")
def device_load(cid: int, ip: str, port: int) -> None:
    """Load a preset into the edit buffer by CID."""
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            ok = h.load_preset(cid)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to load preset cid {cid}")
    click.echo(f"loaded cid {cid}")


@device.command(name="create")
@click.option("--from", "src_cid", type=int, required=True,
              help="Source preset CID to copy/reference (required).")
@click.option("--setlist", default="user", show_default=True,
              help="Destination: " + _SETLIST_HELP)
@click.option("--pos", type=int, required=True,
              help="Destination position (0-based posi; required).")
@_device_option
@_locked("library", verb="create")
def device_create(src_cid: int, setlist: str, pos: int, ip: str, port: int) -> None:
    """Copy or reference a preset into a slot; prints the new CID.

    Takes no positional arguments — both --from (the source preset's CID)
    and --pos (the destination) are required options. With --setlist user
    (default) the source is COPIED into the pool as a new independent
    preset; the device auto-names the copy after the source ("<Name> (1)"
    style) — rename it with `device rename <cid> <name>`. With a NAMED
    setlist no copy is made: a REFERENCE to the source pool preset is added
    to the setlist at --pos (references are pointers; the pool preset is
    shared), and the printed new CID is the reference's own cid.
    """
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            kind, container, label = _resolve_setlist_dest(h, setlist)
            if kind == "factory":
                raise click.ClickException("the factory container is read-only")
            if kind == "setlist":
                if h.find_by_pos(container, pos, strict=True) is not None:
                    raise click.ClickException(
                        f"setlist {label!r} position {pos} is not empty")
                new_cid = h.reference_into_setlist(container, src_cid, pos)
            else:
                new_cid = h._raw.create_from(src_cid, container, pos)
            serial = _serial_of(h, ip)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if new_cid is None:
        raise click.ClickException(
            f"failed to copy cid {src_cid} into {setlist} slot {pos}")
    if kind == "setlist":
        click.echo(f"created reference cid {new_cid} -> pool cid {src_cid} "
                   f"in setlist {label!r} at position {pos}")
        return
    click.echo(f"created cid {new_cid}")
    _record_placement(setlist=setlist, posi=pos, name=f"(copy of cid {src_cid})",
                      cid=new_cid, source_kind="copy", serial=serial)


@device.command(name="rename")
@click.argument("cid", type=int)
@click.argument("new_name")
@_device_option
@_locked("library", verb="rename")
def device_rename(cid: int, new_name: str, ip: str, port: int) -> None:
    """Rename the preset at CID."""
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            ok = h.rename(cid, new_name)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to rename cid {cid}")
    click.echo(f"renamed cid {cid} -> {new_name!r}")
    _ledger_rename(cid, new_name)


@device.command(name="delete")
@click.argument("cid", type=int)
@click.option("--setlist", default="user", show_default=True,
              help="Where the preset lives: " + _SETLIST_HELP)
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@_device_option
@_locked("library", verb="delete")
def device_delete(cid: int, setlist: str, yes: bool, ip: str, port: int) -> None:
    """Delete the preset at CID (pool), or a reference from a named setlist.

    With --setlist user (default) the pool preset itself is deleted. With a
    NAMED setlist only the setlist's REFERENCE is removed — CID may be the
    reference's own cid or the referenced pool preset's cid (rcid); the pool
    preset is never touched.
    """
    HelixClient, HelixError = _client()

    if not yes:
        click.confirm(f"Delete cid {cid} from {setlist!r}?", abort=True)
    try:
        with HelixClient(ip, port) as h:
            kind, container, label = _resolve_setlist_dest(h, setlist)
            if kind == "setlist":
                refs = _setlist_refs(h, container, strict=True)
                match = (next((m for m in refs if m.get("cid_") == cid), None)
                         or next((m for m in refs if m.get("rcid") == cid), None))
                if match is None:
                    raise click.ClickException(
                        f"setlist {label!r} has no reference with cid or "
                        f"rcid {cid} (see `device list --setlist {label!r}`)")
                ok = h.remove_reference(container, match.get("cid_"))
            else:
                ok = h._raw.delete(container, [cid])
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to delete cid {cid}")
    if kind == "setlist":
        click.echo(f"removed reference cid {match.get('cid_')} "
                   f"(-> pool cid {match.get('rcid')}) from setlist "
                   f"{label!r} — the pool preset was not touched")
        return
    click.echo(f"deleted cid {cid}")
    _ledger_remove(cid)


@device.command(name="set-param")
@click.argument("path", type=int)
@click.argument("block", type=int)
@click.argument("param_id", type=int)
@click.argument("value", type=float)
@_device_option
@_locked("editbuffer", verb="set-param")
def device_set_param(path: int, block: int, param_id: int, value: float,
                     ip: str, port: int) -> None:
    """Set one param in the live edit buffer (PATH BLOCK PARAM_ID VALUE).

    PATH/BLOCK are `device blocks` coordinates (DSP index + grid slot, sent
    to the wire unchanged). Don't guess PARAM_ID: run `device params PATH
    BLOCK` first — it lists every param's numeric pid, name, and CURRENT
    value. VALUE is in the param's RAW units — dB / Hz / enum-int exactly as
    `device params` reports them — NOT normalized 0..1. Mutates the ACTIVE
    tone immediately (volatile until the preset is saved).

    Example (proven on hardware, fw 1.3.2 — output block at path 0 grid
    slot 13; `device params 0 13` shows `gain` = pid 2, in dB):

      helixgen device set-param 0 13 2 3.0
    """
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            ok = h.set_param(path, block, param_id, value)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(
            f"failed to set param {param_id} on path {path} block {block}")
    click.echo(f"set path {path} block {block} param {param_id} = {value}")


@device.command(name="snapshot")
@click.argument("index", type=int)
@_device_option
@_locked("editbuffer", verb="snapshot")
def device_snapshot(index: int, ip: str, port: int) -> None:
    """Recall a snapshot (0-based, 0..7) on the live device.

    Changes the ACTIVE tone's current snapshot immediately (like stepping the
    snapshot footswitch). `/activateSnapshot`.
    """
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            h.activate_snapshot(index)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"recalled snapshot {index}")


@device.command(name="blocks")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit as JSON.")
@_device_option
def device_blocks(as_json: bool, ip: str, port: int) -> None:
    """List the live edit buffer's blocks with their (path, block) coordinates.

    `block` is the DSP grid slot (0-27; not necessarily contiguous — e.g.
    the output block sits at slot 13/27). These are exactly the coordinates
    `device bypass` / `device model` / `device set-param` / `device params`
    address. Reads the active edit buffer (does not change the tone).
    The on/off shown is the preset's *saved* base bypass; a volatile live
    `device bypass` toggle is not reflected here until the preset is saved.
    """
    HelixClient, HelixError = _client()
    from helixgen.ingest import humanize_model_id

    try:
        with HelixClient(ip, port) as h:
            blocks = h.edit_buffer_blocks()
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(blocks, indent=2))
        return
    if not blocks:
        click.echo("no blocks (empty edit buffer?)")
        return
    for b in blocks:
        name = humanize_model_id(b["model"]) if b.get("model") else f"?model {b['model_id']}"
        state = "on " if b["enabled"] else "OFF"
        click.echo(f"  path {b['path']} block {b['block']:>2}  [{state}]  {name}")


@device.command(name="params")
@click.argument("path", type=int)
@click.argument("block", type=int)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit as JSON.")
@_device_option
def device_params(path: int, block: int, as_json: bool, ip: str, port: int) -> None:
    """List one edit-buffer block's params: numeric pid, name, CURRENT value.

    PATH/BLOCK are `device blocks` coordinates. This is the pid-discovery
    surface for `device set-param`: each row is a param's numeric pid, its
    name from the model defs, the value currently stored in the edit buffer,
    and its type/range/default. Values are in the param's RAW units (dB, Hz,
    enum-int, bool as 0/1) — the same units `device set-param` writes; they
    are NOT normalized 0..1. Read-only (does not change the tone).
    """
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            info = h.edit_buffer_params(path, block)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return
    from helixgen.ingest import humanize_model_id

    name = (humanize_model_id(info["model"]) if info.get("model")
            else f"?model {info['model_id']}")
    click.echo(f"path {info['path']} block {info['block']}  "
               f"[{'on ' if info['enabled'] else 'OFF'}]  {name}")
    for p in info["params"]:
        cur = "-" if p["value"] is None else repr(p["value"])
        rng = (f"[{p['min']}..{p['max']}]"
               if p["min"] is not None or p["max"] is not None else "")
        click.echo(f"  pid {p['pid']:>4}  {p['name'] or '?':<22} "
                   f"= {cur:<12} {p['type'] or '?'} {rng} "
                   f"(default {p['default']})")


@device.command(name="active")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit as JSON.")
@_device_option
def device_active(as_json: bool, ip: str, port: int) -> None:
    """Show the device's ACTIVE preset: cid, name, and pool slot. Read-only.

    Reads the live device property `server.active.preset.id` (it tracks the
    player's own panel selection as well as network loads) and resolves the
    cid via the read-only /GetContentRef. Save/restore the player's
    selection around your own work: note the cid printed here, then
    `device load <cid>` to put it back.
    """
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            info = h.active_preset()
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return
    click.echo(f"cid:   {info['cid']}")
    click.echo(f"name:  {info.get('name') or '?'}")
    click.echo(f"slot:  {info.get('slot') or '?'}  (posi {info.get('posi')}, "
               f"container {info.get('ccid')})")


@device.command(name="bypass")
@click.argument("path", type=int)
@click.argument("block", type=int)
@click.argument("state", type=click.Choice(["on", "off"]))
@_device_option
@_locked("editbuffer", verb="bypass")
def device_bypass(path: int, block: int, state: str, ip: str, port: int) -> None:
    """Enable/bypass a block in the live edit buffer (PATH BLOCK on|off).

    `on` = active, `off` = bypassed. Find coordinates with `device blocks`
    (BLOCK is the grid slot, sent to the wire unchanged). Changes the ACTIVE
    tone immediately (`/BlockEnableSet`). Note: the toggle is a *volatile*
    live state — audible at once, but not written to the preset (so `device
    blocks`, which reads the saved base state, won't reflect it) until you
    save the preset.
    """
    HelixClient, HelixError = _client()

    enable = state == "on"
    try:
        with HelixClient(ip, port) as h:
            h.set_block_enable(path, block, enable)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"path {path} block {block} -> {'on' if enable else 'bypassed'}")


@device.command(name="model")
@click.argument("path", type=int)
@click.argument("block", type=int)
@click.argument("model")
@_device_option
@_locked("editbuffer", verb="model")
def device_model(path: int, block: int, model: str, ip: str, port: int) -> None:
    """Set a block's model in the live edit buffer (PATH BLOCK MODEL).

    PATH/BLOCK are `device blocks` coordinates (BLOCK = the grid slot, sent
    unchanged). MODEL is a numeric model id or a model-id string like
    `HD2_AmpBritPlexiNrm` (see `list-blocks`). The device rejects a
    cross-category swap. Changes the ACTIVE tone. `/ModelSet`.
    """
    HelixClient, HelixError = _client()
    from helixgen.device import defs as _defs

    if model.lstrip("-").isdigit():
        model_id = int(model)
    else:
        model_id = _defs.model_id_for(model)
        if model_id is None:
            raise click.ClickException(
                f"unknown model {model!r}; pass a numeric model id or an exact "
                "model-id string (see `helixgen list-blocks`)")
    try:
        with HelixClient(ip, port) as h:
            h.set_block_model(path, block, model_id)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"path {path} block {block} -> model {model} ({model_id})")


@device.command(name="reorder")
@click.argument("setlist")
@click.argument("target")
@click.option("--to", "to_index", type=int, required=True,
              help="New 0-based position within the container.")
@_device_option
@_locked("library", verb="reorder")
def device_reorder(setlist: str, target: str, to_index: int,
                   ip: str, port: int) -> None:
    """Move a preset to a new position within a setlist (`/ReorderContainerContent`).

    SETLIST is a setlist display name (e.g. `throwaway`) or a literal
    container cid; TARGET is a preset display name or a literal cid within
    that setlist. Pass `setlists` as SETLIST to instead reorder the top-level
    setlist list itself (TARGET is then a setlist name/cid) — a real setlist
    literally named "setlists" must be addressed by its container cid.

    Numeric arguments are cid-first: an all-digits TARGET/SETLIST is always
    parsed as a cid, never a display name. If an item is display-named that
    digit string, the cid reading wins with a stderr warning when the cid
    resolves in the container, and the command errors (naming the item's
    real cid) when it doesn't. --to is bounds-validated against the
    container's current length.

    This is a direct, immediate DEVICE-side write — distinct from the local
    manifest's `device slots reorder`, which only edits the tone library's
    recorded order and takes effect on the device on the next `device sync`
    (which may then reorder things right back to the manifest's order).
    """
    HelixClient, HelixError = _client()
    from helixgen.device import reorder as R

    try:
        with HelixClient(ip, port) as h:
            res = R.reorder_setlist_item(h, setlist, target, to_index)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    for w in res.get("warnings", []):
        click.echo(f"warning: {w}", err=True)
    click.echo(f"moved cid {res['moved_cid']} to position {res['new_pos']} "
               f"in {setlist!r} ({len(res['items'])} item(s) now listed)")


@device.command(name="pull")
@click.argument("cid", type=int)
@click.argument("outfile", type=click.Path(dir_okay=False, path_type=Path))
@_device_option
def device_pull(cid: int, outfile: Path, ip: str, port: int) -> None:
    """Save a preset's raw content blob (a .sbe backup) without activating it.

    Reads via the non-activating ``/GetContentData`` — the device's live tone is
    never disturbed.
    """
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            blob = h.get_content(cid)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    outfile.write_bytes(blob)
    click.echo(f"wrote {len(blob)} bytes to {outfile}")


@device.command(name="save")
@click.argument("name")
@click.option("--setlist", default="user", show_default=True,
              help="Destination: " + _SETLIST_HELP)
@click.option("--pos", type=int, required=True, help="Destination slot (posi).")
@_device_option
@_locked("library", verb="save")
def device_save(name: str, setlist: str, pos: int, ip: str, port: int) -> None:
    """Save the device's CURRENT edit buffer as a new preset; prints the new CID.

    Mirrors the editor's "Save Preset As -> Save As New". The target slot must be
    empty (checked strictly — backlog #40 — so a listing timeout raises instead
    of reading as empty). Whatever preset/edits are live on the device are
    persisted. With a NAMED --setlist the preset content is saved into the
    POOL (lowest empty slot) and a REFERENCE is added to the setlist at
    --pos.
    """
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            kind, container, label = _resolve_setlist_dest(h, setlist)

            def _writer(cont, cpos):
                if kind == "pool" and h.find_by_pos(cont, cpos, strict=True) is not None:
                    raise click.ClickException(
                        f"{label} slot {cpos} is not empty; refusing to overwrite")
                return h._raw.save_edit_buffer_to(cont, cpos, name)

            new_cid, pool_pos, ref_cid = _install_via_dest(
                h, kind, container, label, pos, _writer)
            serial = _serial_of(h, ip)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if new_cid is None:
        raise click.ClickException(f"failed to save edit buffer to {setlist} slot {pos}")
    where = (f"pool slot {pool_pos}, referenced into setlist {label!r} at {pos}"
             if kind == "setlist" else f"{label} slot {pos}")
    click.echo(f"saved edit buffer as cid {new_cid} ({name!r}) in {where}")
    _record_placement(setlist=label, posi=pool_pos, name=name, cid=new_cid,
                      source_kind="edit-buffer", serial=serial,
                      setlist_pos=pos if kind == "setlist" else None)


@device.command(name="list-irs")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit as JSON; each entry also carries `file` = the IR's "
                   "on-device .wav basename (what `device pull-ir` takes).")
@_device_option
def device_list_irs(as_json: bool, ip: str, port: int) -> None:
    """List the impulse responses on the device (name + hash).

    --json additionally resolves each IR's on-device FILE basename (`file`,
    via /IrPathForHashGet) — the file keeps its original upload basename
    even after a `device rename-ir` (which changes only the display name),
    so `file` is what `device pull-ir` needs.
    """
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            # strict: a dropped/undecodable -11 reply must not print as "no
            # IRs on the device" (#38 Task 4). The read also settles under a
            # 2001 subscription so a just-uploaded IR isn't missed.
            irs = h.list_irs(strict=True)
            if as_json:
                lookup = getattr(h, "ir_path_for_hash", None)
                for m in irs:
                    path = None
                    if callable(lookup):
                        try:
                            path = lookup(m.get("hash", ""))
                        except HelixError:
                            path = None
                    m["file"] = path.rsplit("/", 1)[-1] if path else None
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(irs, indent=2))
        return
    for m in irs:
        click.echo(f"{m.get('hash','')}  {'stereo' if not m.get('mono') else 'mono'}  {m.get('name','?')}")


@device.command(name="delete-ir")
@click.argument("name_or_hash")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.option("--force-wedge", is_flag=True, default=False,
              help="If a 32-hex hash isn't in the IR registry but its file "
                   "still resolves on the device (the delete->quick-reimport "
                   "wedge), remove the orphaned file. Do NOT use on an IR you "
                   "just imported — its listing may merely be lagging.")
@_device_option
@_locked("irs", verb="delete-ir")
def device_delete_ir(name_or_hash: str, yes: bool, force_wedge: bool,
                     ip: str, port: int) -> None:
    """Delete one user IR from the device, by name or 32-hex hash.

    Removes the IR's registry entry (container -11) AND its backing .wav on
    the device (best-effort). Presets that referenced it will show a silent
    cab until it is re-imported. See ``ir-prune`` to clean up ALL unreferenced
    IRs at once.
    """
    HelixClient, HelixError = _client()
    from helixgen.device import maintenance as mt

    if not yes:
        click.confirm(
            f"Delete IR {name_or_hash!r} from the device?", abort=True)
    try:
        with HelixClient(ip, port) as h:
            res = mt.delete_device_ir(h, name_or_hash, ip=ip,
                                      force_wedge=force_wedge)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not res["ok"]:
        raise click.ClickException(f"failed to delete IR {name_or_hash!r}")
    if res.get("cid") is None:
        click.echo(f"removed orphaned IR file for {res['name']!r} "
                   f"({res['hash']}) — it had no registry entry (wedged)")
    else:
        click.echo(f"deleted IR {res['name']!r} ({res['hash']})"
                   + ("" if res["file_removed"] else
                      "  (warning: its .wav lingers on the device filesystem)"))


@device.command(name="rename-ir")
@click.argument("name_or_hash")
@click.argument("new_name")
@_device_option
@_locked("irs", verb="rename-ir")
def device_rename_ir(name_or_hash: str, new_name: str, ip: str, port: int) -> None:
    """Rename a user IR on the device (match by name or 32-hex hash).

    Renaming changes only the display name — the IR's hash (which presets
    reference) is untouched, so nothing breaks.
    """
    HelixClient, HelixError = _client()
    from helixgen.device import maintenance as mt

    try:
        with HelixClient(ip, port) as h:
            target = mt.resolve_device_ir_live(h, name_or_hash)
            ok = h.rename(target["cid_"], new_name)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to rename IR {name_or_hash!r}")
    click.echo(f"renamed IR {target.get('name')!r} -> {new_name!r}")


@device.command(name="ir-prune")
@click.option("--yes", is_flag=True, default=False,
              help="Actually delete (default is a dry-run report).")
@click.option("--force", is_flag=True, default=False,
              help="Also delete IRs referenced only by local off-device .hsp files.")
@click.option("--ignore-warnings", "ignore_warnings", is_flag=True, default=False,
              help="Proceed even if some local tones' IR references can't be "
                   "verified (missing/unreadable .hsp).")
@click.option("--only", default=None, metavar="NAME_OR_HASH",
              help="Restrict deletion to this one IR.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the result dict as JSON.")
@_device_option
@_locked(verb="ir-prune", when=lambda kw: ("irs",) if kw.get("yes") else ())
def device_ir_prune(yes: bool, force: bool, ignore_warnings: bool,
                    only: str | None, as_json: bool,
                    ip: str, port: int) -> None:
    """Delete device IRs that no preset references any more (DRY-RUN by default).

    Diffs the device's user IRs against every IR hash referenced by the
    presets on the device (non-activating content reads across the pool),
    by the live edit buffer, and by your local tone-library sources (.hsp
    files, and the .sbe device-content blobs `device push` records). IRs
    referenced on the device are never touched; IRs referenced only by a
    local off-device tone are "protected" (need --force). Local tones whose
    recorded source can't be read are surfaced as warnings, and executing over
    warnings needs --ignore-warnings (a separate consent from --force).
    Nothing is deleted without --yes, and the plan is re-scanned and
    re-verified immediately before any delete (a disagreement aborts with
    nothing deleted).
    """
    _, HelixError = _client()
    from helixgen.device import maintenance as mt

    try:
        res = mt.ir_prune(ip=ip, port=port, execute=yes, force=force,
                          ignore_warnings=ignore_warnings, only=only)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(res, indent=2))
        return
    for w in res.get("warnings", []):
        click.echo(f"warning: {w}", err=True)
    click.echo(f"device IRs: {res['device_irs']}  "
               f"referenced: {len(res['referenced'])}  "
               f"protected: {len(res['protected'])}  "
               f"orphans: {len(res['orphans'])}")
    for m in res["protected"]:
        click.echo(f"  protected  {m.get('hash')}  {m.get('name')}  "
                   f"(local: {', '.join(m.get('local_tones', []))})")
    for m in res["orphans"]:
        click.echo(f"  orphan     {m.get('hash')}  {m.get('name')}")
    if res["dry_run"]:
        if res["orphans"] or (force and res["protected"]):
            click.echo("dry-run: nothing deleted — re-run with --yes to delete"
                       + (" (add --force for protected IRs)"
                          if res["protected"] and not force else ""))
        else:
            click.echo("dry-run: nothing to prune")
    else:
        for m in res["deleted"]:
            click.echo(f"  deleted    {m.get('hash')}  {m.get('name')}")
        click.echo(f"deleted {len(res['deleted'])} IR(s)")
    for e in res["errors"]:
        click.echo(f"error: {e}", err=True)
    if not res["ok"]:
        raise click.ClickException("ir-prune finished with errors (see above)")


@device.command(name="set-info")
@click.argument("cids", nargs=-1, type=int, required=True)
@click.option("--color", default=None,
              help="Preset color: a name (auto, white, red, dark orange, light "
                   "orange, yellow, green, turquoise, blue, violet, pink, off) "
                   "or a raw index 0-11.")
@click.option("--notes", default=None, help="Preset notes text (Preset Info panel).")
@_device_option
@_locked("library", verb="set-info")
def device_set_info(cids: tuple[int, ...], color: str | None, notes: str | None,
                    ip: str, port: int) -> None:
    """Set preset color and/or notes on one or more CIDs (batch-capable).

    Color is a content attr; notes are written via a non-activating content
    round-trip — the device's live tone is never disturbed.
    """
    HelixClient, HelixError = _client()
    from helixgen.device import maintenance as mt

    if color is None and notes is None:
        raise click.ClickException("give --color and/or --notes")
    if color is not None:
        try:
            mt.color_index(color)  # validate once, before touching any preset
        except ValueError as e:
            raise click.ClickException(str(e)) from e
    failures = []
    try:
        with HelixClient(ip, port) as h:
            for cid in cids:
                try:
                    out = mt.set_preset_info(h, cid, color=color, notes=notes)
                except HelixError as e:
                    failures.append(cid)
                    click.echo(f"cid {cid}: FAILED ({e})", err=True)
                    continue
                bits = ", ".join(f"{k}={'ok' if v else 'FAILED'}"
                                 for k, v in out.items())
                click.echo(f"cid {cid}: {bits}")
                if not all(out.values()):
                    failures.append(cid)
    except (HelixError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if failures:
        raise click.ClickException(
            f"{len(failures)} of {len(cids)} preset(s) failed: "
            + ", ".join(str(c) for c in failures))


@device.command(name="push-ir")
@click.argument("wav", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_ip_option
@_locked("irs", verb="push-ir")
def device_push_ir(wav: Path, ip: str) -> None:
    """Import an impulse-response .wav onto the device — instantly, like the editor.

    Two things make an external upload behave exactly like the editor's own
    import: (1) subscribing to the device's 2001 change stream activates its
    watched-directory monitor, so the file registers in ~0.1-1 s instead of on
    the device's slow ~15-20 min scan; (2) the uploaded IR embeds a ``HASH``
    chunk holding helixgen's ``irhash`` (as the editor's file does), so the
    device registers it under exactly that hash and the preset resolves.
    """
    # sftp path needs a real address even when --no-lock skipped the
    # _locked wrapper's resolution (#74; HelixSFTP(None) would try
    # localhost).
    ip = ip or _resolve_ip_or_fail()
    _, HelixError = _client()
    from helixgen.device import sftp as _sftp

    try:
        res = _sftp.push_ir(ip, str(wav))
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    if not res.get("ok"):
        raise click.ClickException(f"upload of {wav.name} failed")
    hh = res.get("helixgen_hash")
    dh = res.get("device_hash")
    if res.get("already"):
        click.echo(f"already on device: {res['name']} ({hh})")
    elif res.get("registered") and res.get("hash_match"):
        click.echo(f"imported + registered instantly: {res['name']} ({hh})")
    elif res.get("registered"):
        click.echo(f"registered {res['name']} but under {dh}, not the expected "
                   f"{hh} — the preset may not resolve this IR", err=True)
    else:
        click.echo(f"uploaded {res['name']} ({hh}) — {res.get('note')}", err=True)


@device.command(name="pull-ir")
@click.argument("filename")
@click.argument("outfile", type=click.Path(dir_okay=False, path_type=Path))
@_ip_option
def device_pull_ir(filename: str, outfile: Path, ip: str) -> None:
    """Download an IR .wav from the device by its on-device FILE basename.

    FILENAME is the exact `.wav` basename in the device's ir/ directory —
    discover it with `device list-irs --json` (the `file` field). The file
    keeps the basename it was originally uploaded/imported with:
    `device rename-ir` changes only the DISPLAY name (validated live), so a
    renamed IR still downloads under its original basename. EXPERIMENTAL.
    """
    ip = ip or _resolve_ip_or_fail()  # sftp path needs a real address (#74)
    _, HelixError = _client()
    from helixgen.device import sftp as _sftp

    try:
        with _sftp.HelixSFTP(ip) as s:
            s.download_ir(filename, str(outfile))
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"downloaded {filename} -> {outfile}")


@device.command(name="install")
@click.argument("hsp_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("name")
@click.option("--pos", type=int, required=True, help="Destination slot (posi); must be empty.")
@click.option("--setlist", default="user", show_default=True,
              help="Destination: " + _SETLIST_HELP)
@click.option("--auto-irs", is_flag=True, default=False,
              help="Upload any referenced IRs that aren't on the device yet "
                   "(resolved from your local IR mapping.json).")
@_device_option
@_locked(verb="install", when=lambda kw: ("library", "irs")
        if kw.get("auto_irs") else ("library",))
def device_install(hsp_file: Path, name: str, pos: int, setlist: str,
                   auto_irs: bool, ip: str, port: int) -> None:
    """Author a helixgen .hsp onto the device as a new, playable preset.

    Transcodes the .hsp straight into the device's native content format and
    installs it into an empty slot — any block chain, full fidelity, no
    template (dual-amp, parallel splits, snapshots, footswitch/EXP
    assignments all synthesized). MUTATES the device (the slot must be
    empty; the active tone is untouched). With a NAMED --setlist the preset
    lands in the POOL (lowest empty slot) and a REFERENCE is added to the
    setlist at --pos.

    If the preset references user IRs, pass --auto-irs so any that aren't on
    the device are uploaded first (resolved from the local mapping.json) —
    otherwise those cabs come up SILENT until the IRs are imported. For
    managed multi-tone workflows prefer `device sync`. EXPERIMENTAL.
    """
    from helixgen.hsp import read_hsp
    HelixClient, HelixError = _client()

    body = read_hsp(hsp_file)
    try:
        with HelixClient(ip, port) as h:
            kind, container, label = _resolve_setlist_dest(h, setlist)

            def _writer(cont, cpos):
                # _install_hsp_open does its own strict emptiness check for
                # the pool path; the setlist path just computed a fresh
                # lowest-empty pool posi, so skip re-checking it.
                return _install_hsp_open(h, body, cont, cpos, name,
                                         setlist_label=label,
                                         auto_irs=auto_irs, ip=ip,
                                         force=(kind == "setlist"))

            cid, pool_pos, _ref = _install_via_dest(
                h, kind, container, label, pos, _writer)
            serial = _serial_of(h, ip)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    where = (f"pool slot {pool_pos}, referenced into setlist {label!r} at {pos}"
             if kind == "setlist" else f"{label} slot {pos}")
    click.echo(f"installed {hsp_file.name} as cid {cid} ({name!r}) in {where}")
    _record_placement(setlist=label, posi=pool_pos, name=name, cid=cid,
                      source_kind="hsp", source_path=str(hsp_file.resolve()),
                      serial=serial,
                      setlist_pos=pos if kind == "setlist" else None)


# --- device setlist: the local manifest of desired setlist membership -------

@device.group(name="setlist")
def device_setlist() -> None:
    """Manage the local setlist manifest (~/.helixgen/setlists/manifest.json,
    override $HELIXGEN_SETLISTS; a legacy ~/.helixgen/setlists.json
    auto-migrates on first load).

    A tone is added to a setlist here (desired membership); `device sync` then
    pushes that membership onto the device as a preset pool + references. The
    manifest is never hand-edited — use these verbs.
    """


@device_setlist.command(name="list")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the whole manifest document as JSON.")
def device_setlist_list(as_json: bool) -> None:
    """List the manifest's setlists with their tone counts and members."""
    SetlistManifest, _ = _manifest()

    m = SetlistManifest.load()
    if as_json:
        click.echo(json.dumps(m.to_dict(), indent=2))
        return
    setlists = m.setlists()
    if not setlists:
        click.echo("(no setlists in manifest)")
        return
    for sl in setlists:
        tones = m.tones_in(sl)
        click.echo(f"{sl}  ({len(tones)} tone{'s' if len(tones) != 1 else ''})")
        for t in tones:
            click.echo(f"    {t}")


@device_setlist.command(name="add")
@click.argument("setlist")
@click.argument("hsp_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--pos", type=int, default=None,
              help="Insert at this 0-based position (default: append).")
def device_setlist_add_cmd(setlist: str, hsp_file: Path, pos: int | None) -> None:
    """Add an authored .hsp tone to a setlist's membership (auto-creates the setlist).

    A tone may belong to many setlists (it's referenced once in the device pool
    and shared) — adding one that's already elsewhere is expected, not a dup.
    Idempotent within a setlist; only errors if the tone's name is already
    registered to a different .hsp file (names must be unique).
    """
    SetlistManifest, ManifestError = _manifest()

    m = SetlistManifest.load()
    try:
        name = m.add_tone(setlist, hsp_file, pos=pos)
    except ManifestError as e:
        raise click.ClickException(str(e)) from e
    m.save()
    where = "appended to" if pos is None else f"inserted at {pos} in"
    click.echo(f"added {name!r} ({where} setlist {setlist!r})")


@device_setlist.command(name="remove")
@click.argument("setlist")
@click.argument("tone_name")
def device_setlist_remove_cmd(setlist: str, tone_name: str) -> None:
    """Drop a tone from a setlist's membership (TONE_NAME = display name).

    Local-only (run `device sync` to apply). The tone stays in the registry
    if another setlist still references it, or if it carries an explicit
    device mark (`device add` / a concrete slot); an implicit mark
    (auto-stamped when it joined a synced setlist) dies with its last
    membership, so add-then-remove is a no-op.
    """
    SetlistManifest, _ = _manifest()

    m = SetlistManifest.load()
    if not m.remove_tone(setlist, tone_name):
        raise click.ClickException(
            f"{tone_name!r} is not in setlist {setlist!r} "
            f"(try `helixgen device setlist list`)")
    m.save()
    click.echo(f"removed {tone_name!r} from setlist {setlist!r}")


@device_setlist.command(name="create-local")
@click.argument("setlist")
def device_setlist_create_local(setlist: str) -> None:
    """Create an empty setlist in the LOCAL manifest only (no device).

    To also create it on the device, run `helixgen device setlist create`
    (which records it locally too).
    """
    SetlistManifest, _ = _manifest()

    m = SetlistManifest.load()
    m.create_setlist(setlist)
    m.save()
    click.echo(f"created local setlist {setlist!r} (manifest only — "
               f"`device setlist create` also creates it on the device)")


@device_setlist.command(name="create")
@click.argument("setlist")
@_device_option
@_locked("library", verb="setlist create")
def device_setlist_create_cmd(setlist: str, ip: str, port: int) -> None:
    """Create a new empty setlist ON THE DEVICE (and in the local manifest).

    Uses the device's own create command (/CreateContent under the setlists
    root) — no Stadium app needed. Errors if a setlist with that name already
    exists on the device.
    """
    HelixClient, HelixError = _client()
    SetlistManifest, _ = _manifest()

    try:
        with HelixClient(ip, port) as h:
            existing = h.resolve_setlist_cid(setlist)
            if existing is not None:
                raise click.ClickException(
                    f"setlist {setlist!r} already exists on the device "
                    f"(cid {existing})")
            cid = h.create_setlist(setlist)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if cid is None:
        raise click.ClickException(f"device refused to create setlist {setlist!r}")
    try:
        m = SetlistManifest.load()
        m.create_setlist(setlist)
        m.save()
    except Exception as e:  # noqa: BLE001 — advisory; the device write succeeded
        click.echo(f"warning: could not update tone library: {e}", err=True)
    click.echo(f"created setlist {setlist!r} on the device (cid {cid})")


@device_setlist.command(name="rename")
@click.argument("setlist")
@click.argument("new_name")
@_device_option
@_locked("library", verb="setlist rename")
def device_setlist_rename_cmd(setlist: str, new_name: str, ip: str, port: int) -> None:
    """Rename a setlist ON THE DEVICE (and in the local manifest, if tracked)."""
    HelixClient, HelixError = _client()
    SetlistManifest, ManifestError = _manifest()

    try:
        with HelixClient(ip, port) as h:
            cid = h.resolve_setlist_cid(setlist)
            if cid is None:
                raise click.ClickException(
                    f"setlist {setlist!r} not found on the device")
            if h.resolve_setlist_cid(new_name) is not None:
                raise click.ClickException(
                    f"a setlist named {new_name!r} already exists on the device")
            ok = h.rename(cid, new_name)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to rename setlist {setlist!r}")
    try:
        m = SetlistManifest.load()
        if m.rename_setlist(setlist, new_name):
            m.save()
    except ManifestError as e:
        click.echo(f"warning: device renamed, but the local manifest kept "
                   f"{setlist!r}: {e}", err=True)
    except Exception as e:  # noqa: BLE001 — advisory
        click.echo(f"warning: could not update tone library: {e}", err=True)
    click.echo(f"renamed setlist {setlist!r} -> {new_name!r} (cid {cid})")


@device_setlist.command(name="delete")
@click.argument("setlist")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@_device_option
@_locked("library", verb="setlist delete")
def device_setlist_delete_cmd(setlist: str, yes: bool, ip: str, port: int) -> None:
    """Delete a setlist ON THE DEVICE. Its references die with it — the pool
    presets they pointed at are NEVER deleted (never-orphan).

    A local manifest setlist of the same name is kept as a local-only draft
    (marked unsynced).
    """
    HelixClient, HelixError = _client()
    SetlistManifest, _ = _manifest()

    try:
        with HelixClient(ip, port) as h:
            cid = h.resolve_setlist_cid(setlist)
            if cid is None:
                raise click.ClickException(
                    f"setlist {setlist!r} not found on the device")
            if not yes:
                click.confirm(
                    f"Delete setlist {setlist!r} (cid {cid}) from the device? "
                    f"(its presets stay in the pool)", abort=True)
            ok = h.delete_setlist(cid)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to delete setlist {setlist!r}")
    try:
        m = SetlistManifest.load()
        if setlist in m.setlists_map:
            m.set_setlist_synced(setlist, False)
            m.save()
    except Exception as e:  # noqa: BLE001 — advisory
        click.echo(f"warning: could not update tone library: {e}", err=True)
    click.echo(f"deleted setlist {setlist!r} from the device — its pool "
               f"presets were not touched")


@device_setlist.command(name="duplicate")
@click.argument("src")
@click.argument("dst")
@_device_option
@_locked("library", verb="setlist duplicate")
def device_setlist_duplicate_cmd(src: str, dst: str, ip: str, port: int) -> None:
    """Duplicate a setlist ON THE DEVICE: copy SRC's references into DST.

    DST is created on the device if absent; if it exists it must be empty.
    References are pointers — the pool presets are shared, not copied.
    """
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            src_cid = h.resolve_setlist_cid(src)
            if src_cid is None:
                raise click.ClickException(f"setlist {src!r} not found on the device")
            dst_cid = h.resolve_setlist_cid(dst)
            created = False
            if dst_cid is None:
                dst_cid = h.create_setlist(dst)
                created = True
                if dst_cid is None:
                    raise click.ClickException(
                        f"device refused to create setlist {dst!r}")
            copied = h.duplicate_setlist_refs(src_cid, dst_cid)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if created:
        try:
            SetlistManifest, _ = _manifest()

            m = SetlistManifest.load()
            m.create_setlist(dst)
            m.save()
        except Exception as e:  # noqa: BLE001 — advisory; device write succeeded
            click.echo(f"warning: could not update tone library: {e}", err=True)
    click.echo(f"duplicated setlist {src!r} -> {dst!r} "
               f"({'created, ' if created else ''}{copied} reference(s) copied)")


@device_setlist.command(name="import-hss")
@click.argument("hss_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--list", "list_only", is_flag=True, default=False,
              help="List the bundle's contents only — offline, no device write.")
@click.option("--setlist", "setlist_name", default=None,
              help="Destination setlist name (default: the bundle's own name).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would be installed/created without writing to the device.")
@_device_option
@_locked(verb="setlist import-hss", when=lambda kw: () if (kw.get("list_only") or kw.get("dry_run")) else ("library",))
def device_setlist_import_hss(hss_file: Path, list_only: bool, setlist_name: str | None,
                              dry_run: bool, ip: str, port: int) -> None:
    """EXPERIMENTAL: import a `.hss` setlist-bundle export (backlog #31, READ side).

    A `.hss` is the Stadium app's "export setlist" file: a 24-byte header +
    gzip + tar of `manifest.json` + 128 fixed slot files. `--list` decodes it
    fully offline (no device needed) and prints each slot's filled/empty state,
    payload format, and preset name. Without `--list`, each filled slot is
    installed into the device POOL (non-activating) and referenced into a
    device setlist (created if absent) in the bundle's slot order — reusing the
    same install + setlist-create + reference primitives as `device install` /
    `device sync`.

    Both the container framing (header/gzip/tar/manifest/128-slot/empty-sentinel)
    and the FILLED-slot framing are pinned against real captured exports. A
    filled slot embeds the preset's `.hsp` (magic `rpshnosj` + JSON); it is
    transcoded to device content on the way in. Device content blobs
    (`_sbepgsm` / `/SetContentData`) are also accepted (detected by magic).

    Imported presets are recorded in the local tone library as PATHLESS tones
    (source `import-hss`) with membership in the destination setlist, so a
    later `device sync <setlist>` preserves their references instead of
    stripping them. They have no local `.hsp`, so `device slots restore`
    can't re-author them.

    NOT idempotent on retry: re-running after a partial failure installs and
    references the already-succeeded slots AGAIN (duplicate pool presets +
    references). After a partial failure, delete the setlist + the orphaned
    pool presets (or import into a fresh setlist) before retrying.
    """
    from helixgen.device import hss as hss_mod

    try:
        bundle = hss_mod.read_hss(hss_file)
    except hss_mod.HssFormatError as e:
        raise click.ClickException(str(e)) from e

    filled = bundle.filled_slots

    if list_only:
        _hss_print_listing(hss_file, bundle, filled, hss_mod)
        return

    target_setlist = setlist_name or bundle.name
    if not target_setlist:
        raise click.ClickException(
            "the bundle has no setlist name in its manifest; pass --setlist explicitly")

    if not filled:
        click.echo(f"no filled slots in {hss_file.name}; nothing to import")
        return

    if dry_run:
        _hss_print_dry_run(hss_file, target_setlist, filled, hss_mod)
        return

    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            result = hss_mod.import_bundle(h, bundle, setlist=setlist_name)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    installed = result["installed"]
    errors = result["errors"]
    click.echo(f"imported {len(installed)}/{len(filled)} preset(s) from {hss_file.name} "
               f"into setlist {result['setlist']!r} "
               f"({'created, ' if result['created'] else ''}cid {result['cid']})")
    _hss_record_import_manifest(result, hss_mod)
    for w in result.get("warnings", []):
        click.echo(f"  warning: {w}", err=True)
    if errors:
        for e in errors:
            click.echo(f"  warning: {e}", err=True)
        raise click.ClickException(
            f"{len(errors)}/{len(filled)} preset(s) failed to import; see warnings above")


@device_setlist.command(name="export-hss")
@click.argument("setlist")
@click.argument("out_file", type=click.Path(dir_okay=False, path_type=Path))
@_device_option
def device_setlist_export_hss(setlist: str, out_file: Path, ip: str, port: int) -> None:
    """EXPERIMENTAL: export a DEVICE setlist to a `.hss` bundle (backlog #31).

    Reads the named device setlist's references (order + slot) and assembles a
    byte-faithful `.hss` — 24-byte header + gzip + tar of `manifest.json` + 128
    slot files — embedding each referenced preset's local `.hsp` (resolved by
    preset name via the tone library) verbatim, exactly as the Stadium app
    embeds a `.hsp` per preset. The output's header + decompressed tar are
    byte-identical to a real app export (only the compressed gzip stream
    differs — the app uses a non-zlib DEFLATE encoder).

    A referenced preset with NO local `.hsp` (device-born, or untracked by the
    tone library) is SKIPPED with a warning — helixgen has no device-content →
    `.hsp` converter, so a device-only preset can't be re-embedded (backlog #31
    residual). The `.hss` is still written with the presets that did resolve.
    """
    from helixgen.device import hss as hss_mod
    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            result = hss_mod.export_setlist_to_hss(h, setlist)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e

    out_file.write_bytes(result["bytes"])
    click.echo(f"wrote {out_file} ({len(result['bytes'])} bytes) — "
               f"{len(result['embedded'])} preset(s) from setlist {setlist!r}")
    for name in result["embedded"]:
        click.echo(f"  embedded: {name}")
    for s in result["skipped"]:
        click.echo(f"  warning: skipped {s}", err=True)


@device_setlist.command(name="sync-on")
@click.argument("setlist")
def device_setlist_sync_on(setlist: str) -> None:
    """Mark a setlist as device-synced (marks all its tones for the device)."""
    SetlistManifest, _ = _manifest()

    m = SetlistManifest.load()
    m.set_setlist_synced(setlist, True)
    m.save()
    click.echo(f"setlist {setlist!r} is now synced; run `helixgen device sync {setlist}`")


@device_setlist.command(name="sync-off")
@click.argument("setlist")
def device_setlist_sync_off(setlist: str) -> None:
    """Mark a setlist as a local-only draft (not mirrored to the device)."""
    SetlistManifest, _ = _manifest()

    m = SetlistManifest.load()
    m.set_setlist_synced(setlist, False)
    m.save()
    click.echo(f"setlist {setlist!r} is now a local-only draft")


@device.command(name="add")
@click.argument("tone")
@click.option("--slot", default="auto",
              help="Only 'auto' (the default) is accepted: sync picks the address. "
                   "An explicit label ('1A'..'128D') is REJECTED — targeted "
                   "placement is unimplemented (backlog #30), and the manifest "
                   "used to record the label while sync silently ignored it.")
def device_add_cmd(tone: str, slot: str) -> None:
    """Mark a library tone for the device (placed on the next `device sync`).

    `--slot` accepts only 'auto'. An explicit slot label is refused rather
    than silently ignored: sync never converted the recorded label into a
    device address (it installs at the lowest empty slot), so the flag
    reported success and changed nothing. See backlog #30.
    """
    SetlistManifest, ManifestError = _manifest()

    if slot != "auto":
        raise click.ClickException(
            f"--slot {slot!r} is not supported: targeted placement is "
            f"unimplemented (backlog #30). `device sync` installs at the "
            f"lowest empty slot regardless of the label, so recording {slot!r} "
            f"would report a placement that never happens. Use --slot auto "
            f"(the default), then move the preset with `device reorder`.")

    m = SetlistManifest.load()
    try:
        m.mark_on_device(tone, slot)
    except ManifestError as e:
        raise click.ClickException(str(e)) from e
    m.save()
    click.echo(f"{tone!r} marked for device (slot {slot})")


@device.command(name="unsync")
@click.argument("tone")
def device_unsync_cmd(tone: str) -> None:
    """Take a tone off the device on next sync (keeps it in the library).

    Also removes the tone from every SYNCED setlist's membership (a synced
    membership would put it right back on the next sync); the output names
    the setlists it was pulled from. Local-only draft setlists keep it.
    """
    SetlistManifest, ManifestError = _manifest()

    m = SetlistManifest.load()
    try:
        pulled = m.unsync(tone)
    except ManifestError as e:
        raise click.ClickException(str(e)) from e
    m.save()
    msg = f"{tone!r} unsynced (deleted from device on next sync)"
    if pulled:
        msg += f"; removed from synced setlists: {', '.join(pulled)}"
    click.echo(msg)


@device.command(name="library")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit raw JSON.")
def device_library_cmd(as_json: bool) -> None:
    """List every library tone: slot, on/off device, setlist memberships."""
    SetlistManifest, _ = _manifest()

    rows = SetlistManifest.load().library()
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    _echo_library_rows(rows)


class _SyncProgressRenderer:
    """Renders a `sync_setlists` `ProgressEvent` stream to STDERR ONLY, never
    touching stdout (the summary lines + `--json` output stay byte-for-byte
    unchanged regardless of whether this renderer is active).

    Two modes, chosen once at construction:

    * **rich** — stderr is a live TTY and `--no-progress` wasn't given: each
      phase with a stable per-phase total (`install`/`update`/`references`/
      `delete`/`gc`) gets its own `click.progressbar` (manually driven —
      events are push-based, so `render_progress()`/`update()`/
      `render_finish()` are called by hand as events arrive and the phase
      changes).
    * **plain** — stderr isn't a TTY, or `--no-progress` was given: no bar,
      just one short line per phase start and one per item, with no
      carriage-return redraw — safe for redirected/non-TTY stderr (CI logs,
      `--json` piped to a file, etc).

    The `irs` phase is special-cased in both modes: its `index`/`total` are
    scoped PER authored tone (they reset for every tone with missing IRs), so
    it is always rendered as a running line rather than a bar that would
    visibly reset mid-sync. Critically, `irs` events are NOT a phase
    transition — the engine emits them INSIDE authoring, interleaved between
    install/update events for different tones (IR upload happens before that
    tone's install/update event). Rendering an `irs` line never closes or
    reopens the enclosing install/update bar/banner, which stays open and
    current across it.

    Error/skip statuses always get a visible note line, in both modes.
    """

    def __init__(self, no_progress: bool):
        self._stream = sys.stderr
        self.rich = (not no_progress) and self._stream_is_tty()
        self._phase = None
        self._bar = None

    def _stream_is_tty(self) -> bool:
        """Whether stderr is an interactive TTY, degrading to plain (False) for
        any stream that lacks ``isatty`` or whose ``isatty()`` raises (a closed
        / broken stderr must never crash the sync -- progress is advisory)."""
        try:
            isatty = getattr(self._stream, "isatty", None)
            return bool(isatty and isatty())
        except Exception:  # noqa: BLE001 -- broken/closed stderr -> plain mode
            return False

    def _echo(self, line: str) -> None:
        click.echo(line, err=True)

    def _close_bar(self) -> None:
        if self._bar is not None:
            try:
                self._bar.render_finish()
            except Exception:  # noqa: BLE001 — progress is advisory only
                pass
            self._bar = None

    def _note(self, phase: str, ev) -> None:
        if ev.status == "error":
            self._echo(f"  ! {phase} error: {ev.label}: {ev.detail or 'failed'}")
        elif ev.status == "skip":
            detail = f": {ev.detail}" if ev.detail else ""
            self._echo(f"  - {phase} skip: {ev.label}{detail}")

    def __call__(self, ev) -> None:
        phase = ev.phase

        if phase == "plan":
            self._close_bar()
            self._phase = None
            self._echo(f"sync: {ev.label}")
            return

        if phase == "irs":
            # A lightweight side-channel line, NOT a phase transition: IR
            # uploads happen INSIDE authoring, interleaved between install/
            # update events for different tones (see setlist_sync._author).
            # Do NOT touch self._phase or self._bar here -- closing/reopening
            # the enclosing install/update bar on every irs event would
            # finish it early and open a second, duplicate bar for the next
            # item in that same phase. Its index/total are scoped PER
            # authored tone (they reset for every tone with missing IRs), so
            # it is always rendered as a running line rather than a bar.
            status = f" {ev.status}" if ev.status and ev.status != "ok" else ""
            self._echo(f"  uploading IR {ev.index}/{ev.total}: {ev.label}{status}")
            self._note(phase, ev)
            return

        if phase != self._phase:
            self._close_bar()
            self._phase = phase
            if self.rich and ev.total:
                bar = click.progressbar(length=ev.total, label=phase,
                                         file=self._stream)
                bar.render_progress()
                self._bar = bar
            else:
                self._echo(f"sync: {phase} ({ev.total or 0})")

        if self.rich and self._bar is not None:
            self._bar.current_item = ev.label
            self._bar.update(1)
        else:
            status = f" {ev.status}" if ev.status and ev.status != "ok" else ""
            self._echo(f"  {phase} {ev.index}/{ev.total}: {ev.label}{status}")

        self._note(phase, ev)

    def close(self) -> None:
        """Finish any still-open bar. No terminal event marks the end of the
        stream, so the CLI calls this once after `sync_setlists` returns."""
        self._close_bar()


def _make_sync_progress_renderer(no_progress: bool) -> _SyncProgressRenderer:
    """Build the `progress=` callback for `sync_setlists` used by `device
    sync`. See `_SyncProgressRenderer` for the rich/plain rendering rules;
    the returned object is callable (`renderer(event)`) and also exposes
    `.close()` to finish any still-open progress bar once the sync ends."""
    return _SyncProgressRenderer(no_progress)


@device.command(name="sync")
@click.argument("setlist_name", metavar="SETLIST", required=False)
@click.option("--all", "all_setlists", is_flag=True, default=False,
              help="Sync every setlist in the manifest (the whole-library reconcile).")
@click.option("--gc", is_flag=True, default=False,
              help="Garbage-collect pool presets no setlist references (only with --all).")
@click.option("--exclude-irs", is_flag=True, default=False,
              help="Install tones only; do not upload their referenced IRs.")
@click.option("--repush", is_flag=True, default=False,
              help="Force re-transcode + re-push every in-scope tone's content, "
                   "even when its .hsp bytes are unchanged since the last sync. "
                   "Plain sync already re-pushes genuinely edited .hsp files "
                   "(it recomputes the file hash at sync time), so this is only "
                   "for the unchanged-bytes case — refreshing already-synced "
                   "tones after a transcoder upgrade.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the raw engine result dict as JSON.")
@click.option("--no-progress", is_flag=True, default=False,
              help="Disable the live progress display (plain one-line-per-phase "
                   "output instead).")
@_device_option
@_locked(verb="sync", when=lambda kw: ("library",) if kw.get("exclude_irs") else ("library", "irs"))
def device_sync(setlist_name: str | None, all_setlists: bool, gc: bool,
                exclude_irs: bool, repush: bool, as_json: bool,
                no_progress: bool, ip: str, port: int) -> None:
    """Sync the manifest's setlists onto the device (pool + references).

    Give a single SETLIST name, or --all for every manifest setlist. The engine
    reconciles the preset pool (install/update/skip), then rebuilds each
    setlist's references to manifest order — never orphaning a still-referenced
    pool preset. --gc (only with --all) prunes pool presets no setlist wants any
    more. Plain sync recomputes each pool tone's .hsp file hash at sync time, so
    an in-place edit to an already-synced tone is detected and re-pushed on the
    next plain sync. --repush treats every in-scope tone already in the pool as
    changed — re-pushing its content via the same non-activating SetContentData-
    on-the-existing-cid path an ordinary hash-triggered update uses — even when
    its .hsp bytes are unchanged since the last sync. Use it only for that
    unchanged-bytes case: a transcoder upgrade can change what an unchanged .hsp
    produces, which a byte-hash comparison can't see.
    A setlist the device doesn't have is reported as a clear error (create
    it first with `helixgen device setlist create <name>`). Sync is a
    managed-set mirror: it never touches untracked device presets and never
    orphans a pool preset another setlist still references. Idempotent — if
    the flaky network drops a run, just re-run it. Shows a live per-phase
    progress display on stderr (a progress bar when stderr is a TTY, plain
    one-line-per-phase text otherwise); pass --no-progress to force the
    plain text form. stdout (this summary, and --json) is never affected by
    the progress display. EXPERIMENTAL.
    """
    SetlistManifest, _ = _manifest()
    from helixgen.device.setlist_sync import sync_setlists
    _, HelixError = _client()

    if bool(setlist_name) == bool(all_setlists):
        raise click.ClickException(
            "give exactly one of a SETLIST name or --all (not both, not neither)")
    if gc and not all_setlists:
        click.echo("warning: --gc is ignored without --all "
                   "(a single-setlist sync never garbage-collects)", err=True)
        gc = False

    setlists = None if all_setlists else [setlist_name]
    renderer = _make_sync_progress_renderer(no_progress)
    try:
        res = sync_setlists(SetlistManifest.load(), ip=ip, port=port,
                            setlists=setlists, gc=gc, exclude_irs=exclude_irs,
                            repush=repush, progress=renderer)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    finally:
        renderer.close()

    if as_json:
        click.echo(json.dumps(res, indent=2))
        return

    pool = res.get("pool", {})
    click.echo(f"pool: {len(pool.get('installed', []))} installed, "
               f"{len(pool.get('updated', []))} updated, "
               f"{len(pool.get('skipped', []))} skipped")
    pool_deleted = pool.get("deleted", [])
    if pool_deleted:
        click.echo(f"pool: deleted {len(pool_deleted)} unsynced preset(s): "
                   f"{', '.join(pool_deleted)}")
    for name in pool.get("delete_skipped", []):
        click.echo(f"pool: kept {name!r} (unsynced, but another device setlist "
                   f"still references it — sync that setlist or use --all)")
    for sl, diff in res.get("references", {}).items():
        click.echo(f"setlist {sl!r}: +{len(diff.get('added', []))} references, "
                   f"-{len(diff.get('removed', []))} references")
    deleted = res.get("gc", {}).get("deleted", [])
    if deleted:
        click.echo(f"gc: deleted {len(deleted)} orphan pool preset(s): "
                   f"{', '.join(deleted)}")
    for er in res.get("errors", []):
        click.echo(f"error: {er}", err=True)
    synced = res.get("setlists", [])
    click.echo(f"synced {len(synced)} setlist(s): {', '.join(synced) or '(none)'}")
    drafts = res.get("skipped_draft_setlists", [])
    if drafts:
        click.echo(f"note: skipped {len(drafts)} local-only draft setlist(s): "
                   f"{', '.join(drafts)} — run `device sync <setlist>` or "
                   f"`device setlist sync-on <setlist>` to mirror one")


@device.command(name="push")
@click.argument("infile", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("name")
@click.option("--setlist", default="user", show_default=True,
              help="Destination: " + _SETLIST_HELP)
@click.option("--pos", type=int, required=True, help="Destination slot (posi); must be empty.")
@_device_option
@_locked("library", verb="push")
def device_push(infile: Path, name: str, setlist: str, pos: int, ip: str, port: int) -> None:
    """Install a local content file (.sbe backup) into a new preset slot.

    Restores a backup / clones a preset / installs authored content. The target
    slot must be empty (checked strictly — backlog #40 — so a listing timeout
    raises instead of reading as empty). With a NAMED --setlist the content
    lands in the POOL (lowest empty slot) and a REFERENCE is added to the
    setlist at --pos. The .sbe is recorded as the tone's local source in the
    tone library (ir-prune decodes it for IR references).
    """
    HelixClient, HelixError = _client()

    blob = infile.read_bytes()
    try:
        with HelixClient(ip, port) as h:
            kind, container, label = _resolve_setlist_dest(h, setlist)

            def _writer(cont, cpos):
                if kind == "pool" and h.find_by_pos(cont, cpos, strict=True) is not None:
                    raise click.ClickException(f"{label} slot {cpos} is not empty")
                return h._raw.push_to_slot(cont, cpos, name, blob)

            new_cid, pool_pos, _ref = _install_via_dest(
                h, kind, container, label, pos, _writer)
            serial = _serial_of(h, ip)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if new_cid is None:
        raise click.ClickException(f"failed to push {infile} into {setlist} slot {pos}")
    where = (f"pool slot {pool_pos}, referenced into setlist {label!r} at {pos}"
             if kind == "setlist" else f"{label} slot {pos}")
    click.echo(f"pushed {infile.name} as cid {new_cid} ({name!r}) in {where}")
    _record_placement(setlist=label, posi=pool_pos, name=name, cid=new_cid,
                      source_kind="sbe", source_path=str(infile.resolve()),
                      serial=serial,
                      setlist_pos=pos if kind == "setlist" else None)


@device.command(name="restore")
@click.argument("infile", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("cid", type=int)
@_device_option
@_locked("library", verb="restore")
def device_restore(infile: Path, cid: int, ip: str, port: int) -> None:
    """Overwrite an EXISTING preset's content from a local file (.sbe).

    Warning: replaces the content at CID in place.
    """
    HelixClient, HelixError = _client()

    blob = infile.read_bytes()
    try:
        with HelixClient(ip, port) as h:
            ok = h._raw.set_content_data(cid, blob)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if not ok:
        raise click.ClickException(f"failed to restore content to cid {cid}")
    click.echo(f"restored content of cid {cid} from {infile.name}")


@device.group(name="slots", invoke_without_command=True)
@click.pass_context
def device_slots(ctx: click.Context) -> None:
    """The local record of which tone helixgen put in which device slot.

    Placement commands (install / save / push / create) record here; rename and
    delete keep it in sync. Bare `device slots` lists the record offline.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(device_slots_list)


@device_slots.command(name="list")
@click.option("--verify", is_flag=True, default=False,
              help="Cross-check the live device and flag drift (needs the Helix).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit raw JSON (the library view, or verify records with --verify).")
@_device_option
def device_slots_list(verify: bool, as_json: bool, ip: str, port: int) -> None:
    """List every library tone: slot, on/off device, setlists. Offline unless --verify."""
    SetlistManifest, _ = _manifest()

    m = SetlistManifest.load()
    rows = m.library()

    if verify:
        HelixClient, HelixError = _client()

        from helixgen.device import Container

        on_device = {}
        try:
            with HelixClient(ip, port) as h:
                for p in h.list_presets(int(Container.POOL)):
                    on_device[p.get("name")] = p
        except (HelixError, OSError) as e:
            raise click.ClickException(str(e)) from e
        records = []
        for row in rows:
            if not row["on_device"]:
                status = "offline"
            elif row["name"] in on_device:
                status = "ok"
            else:
                status = "missing"
            records.append({**row, "status": status})
        for name in on_device:
            if name not in m.tones:
                records.append({"name": name, "slot": None, "status": "untracked"})
        if as_json:
            click.echo(json.dumps(records, indent=2))
        else:
            for r in records:
                click.echo(f"{(r.get('slot') or '-'):<4} {r.get('status', ''):<9} {r.get('name', '')}")
        return

    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    _echo_library_rows(rows)


@device_slots.command(name="restore")
@click.argument("target")
@click.option("--pos", type=int, default=None,
              help="Override the destination slot (default: the recorded slot).")
@click.option("--setlist", default=None,
              help="Override the destination: " + _SETLIST_HELP)
@click.option("--force", is_flag=True, default=False,
              help="Push even if the destination POOL slot is occupied "
                   "(pool destinations only; an occupied named-setlist "
                   "position is always refused — backlog #69).")
@_device_option
@_locked("library", verb="slots restore")
def device_slots_restore(target: str, pos: int | None, setlist: str | None,
                         force: bool, ip: str, port: int) -> None:
    """Put a recorded tone back in its slot. TARGET is the tone name or slot label.

    Re-installs the recorded source: an .hsp (from `install`) is re-authored; an
    .sbe (from `push`) is re-pushed. Tones saved from the live edit buffer or
    copied on-device have no local source and can't be restored this way.
    With a NAMED --setlist the content is restored into the POOL and a
    REFERENCE is added to the setlist at the destination position; if that
    position already holds a reference the restore is refused even with
    --force (a second reference would stack at one position — uncataloged
    device behavior, backlog #69) — remove the incumbent reference first
    (`device delete <cid> --setlist <name>`).
    """
    SetlistManifest, _ = _manifest()

    m = SetlistManifest.load()
    name = target if target in m.tones else None
    if name is None:  # try to match a slot label
        name = next((n for n, r in m.tones.items() if r.get("slot") == target), None)
    if name is None:
        raise click.ClickException(f"no library tone matching {target!r} "
                                   f"(try `helixgen device slots`)")

    rec = m.tones[name]
    src_path = rec.get("path")
    dest_setlist = setlist or "user"
    # Slot resolution (#25): an explicit --pos wins; else the recorded slot
    # label; else the last observed device posi (a synced tone's concrete
    # position is recorded in devices/<serial>.json even when ``slot`` is
    # unresolved).
    dest_pos = pos
    if dest_pos is None:
        dest_pos = _posi_from_slot(rec.get("slot"))
    if dest_pos is None:
        from helixgen.device import observations as obsmod
        dev = obsmod.lookup_tone(name)
        if isinstance(dev, dict) and isinstance(dev.get("posi"), int):
            dest_pos = dev["posi"]
    if dest_pos is None:
        raise click.ClickException(f"{name!r} has no recorded slot; pass --pos")

    if not src_path:
        raise click.ClickException(
            f"no local source recorded for {name!r} "
            f"(pathless save/create); back it up first (helixgen device pull / backup)")
    src = Path(src_path)
    if not src.is_file():
        raise click.ClickException(f"recorded source no longer exists: {src}")

    HelixClient, HelixError = _client()

    try:
        with HelixClient(ip, port) as h:
            kind, container, label = _resolve_setlist_dest(h, dest_setlist)
            if kind == "setlist" and pos is None:
                raise click.ClickException(
                    f"restoring into a named setlist needs an explicit --pos: "
                    f"the recorded slot/posi for {name!r} is a POOL position, "
                    f"not a position within setlist {label!r}")

            if src.suffix == ".sbe":
                def _writer(cont, cpos):
                    # strict (backlog #40): a listing timeout must raise, not
                    # read as "empty" and push into an occupied slot.
                    if (kind == "pool" and not force
                            and h.find_by_pos(cont, cpos, strict=True) is not None):
                        raise click.ClickException(
                            f"{label} slot {cpos} is not empty (use --force)")
                    return h._raw.push_to_slot(cont, cpos, name, src.read_bytes())
            else:  # hsp
                from helixgen.hsp import read_hsp

                body = read_hsp(src)

                def _writer(cont, cpos):
                    return _install_hsp_open(
                        h, body, cont, cpos, name, setlist_label=label,
                        force=force or kind == "setlist", ip=ip)

            cid, pool_pos, _ref = _install_via_dest(
                h, kind, container, label, dest_pos, _writer, force=force)
            serial = _serial_of(h, ip)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    if cid is None:
        raise click.ClickException(f"failed to restore {name!r}")
    where = (f"pool slot {pool_pos}, referenced into setlist {label!r} at "
             f"{dest_pos}" if kind == "setlist" else f"{label} slot {dest_pos}")
    click.echo(f"restored {name!r} to {where} (cid {cid}) from {src.name}")
    _record_placement(setlist=label, posi=pool_pos, name=name,
                      cid=cid, source_kind=src.suffix.lstrip("."), source_path=str(src),
                      serial=serial,
                      setlist_pos=dest_pos if kind == "setlist" else None)


@device_slots.command(name="reorder")
@click.argument("target")
@click.option("--to", "to_index", type=int, required=True,
              help="New 0-based position within the setlist order.")
@click.option("--setlist", "setlist_name", default="user",
              help="Which setlist's order to change (default: user).")
def device_slots_reorder(target: str, to_index: int, setlist_name: str) -> None:
    """Move a tone to a new position within a setlist's order.

    Local only — reorders the manifest; run `device sync <setlist>` to apply it to
    the device. TARGET is the tone name.
    """
    SetlistManifest, _ = _manifest()

    m = SetlistManifest.load()
    members = m.tones_in(setlist_name)
    if target not in members:
        raise click.ClickException(
            f"{target!r} is not in setlist {setlist_name!r} "
            f"(try `helixgen device slots`)")
    members.remove(target)
    members.insert(max(0, to_index), target)
    m.setlists_map[setlist_name]["tones"] = members
    m.save()
    click.echo(f"reordered {setlist_name}; order is now: {', '.join(members)}")


def _posi_from_slot(slot):
    from helixgen.device.manifest import _SLOT_LABELS
    if slot in (None, "auto"):
        return None
    try:
        return _SLOT_LABELS.index(slot)
    except ValueError:
        return None


@device.command(name="backup")
@click.option("--setlist", default="user", show_default=True,
              help="What to back up: " + _SETLIST_HELP)
@click.option("--dir", "out_dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Output dir (default ~/.helixgen/device-backups/ "
                                 "or $HELIXGEN_DEVICE_BACKUPS).")
@_device_option
def device_backup(setlist: str, out_dir, ip: str, port: int) -> None:
    """Back up presets to local .sbe files + a manifest.

    --setlist user (default) backs up the whole preset POOL; --setlist
    <NAME> backs up the pool presets a named device setlist references (in
    setlist order). Reads each preset via the non-activating
    `/GetContentData`, so the device's live tone is never disturbed. Works
    offline afterwards via `device local-list`.
    """
    HelixClient, HelixError = _client()
    from helixgen.device import backup as _backup
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with HelixClient(ip, port) as h:
            kind, container, label = _resolve_setlist_dest(h, setlist)
            if kind == "setlist":
                presets = [{"cid_": m.get("rcid"), "name": m.get("name", ""),
                            "posi": m.get("posi")}
                           for m in _setlist_refs(h, container, strict=True)
                           if m.get("rcid") is not None]
                entries = _backup.backup_setlist(
                    h, out_dir=out_dir, now=now, presets=presets,
                    setlist_name=label)
            else:
                entries = _backup.backup_setlist(h, container, out_dir, now=now)
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    dest = out_dir or _backup.default_backup_dir()
    click.echo(f"backed up {len(entries)} preset(s) to {dest}")


@device.command(name="local-list")
@click.option("--dir", "out_dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Backup dir to read (offline; no device needed).")
@click.option("--json", "as_json", is_flag=True, default=False)
def device_local_list(out_dir, as_json: bool) -> None:
    """List locally backed-up presets (works with the Helix disconnected)."""
    from helixgen.device import backup as _backup

    entries = _backup.local_list(out_dir)
    if as_json:
        click.echo(json.dumps(entries, indent=2))
        return
    for e in entries:
        click.echo(f"{e.get('slot_label',''):<4} {e.get('name','?'):<28} "
                   f"[{e.get('fmt','?')}] {e.get('file','')}")


@device.command(name="watch")
@click.option("--seconds", type=float, default=5.0, show_default=True,
              help="How long to watch the device's live event streams.")
@click.option("--filter", "filter_addr", multiple=True,
              help="Only show these OSC addresses (repeatable).")
@_device_option
def device_watch(seconds: float, filter_addr, ip: str, port: int) -> None:
    """Watch the device's live property/telemetry streams (ports 2001/2003)."""
    from helixgen.device.subscribe import HelixSubscriber
    _, HelixError = _client()

    flt = set(filter_addr) or None
    try:
        with HelixSubscriber(ip) as sub:
            for ev in sub.stream(duration=seconds, filter_addrs=flt):
                click.echo(f"{ev.port}  {ev.addr:<20} {ev.args}")
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e


@device.command(name="tuner")
@click.option("--seconds", type=float, default=15.0, show_default=True,
              help="How long to run the tuner (streams live pitch).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit one JSON reading per line instead of a live display.")
@_device_option
def device_tuner(seconds: float, as_json: bool, ip: str, port: int) -> None:
    """Live network tuner — reads the device's always-on pitch detector.

    Subscribes to the 2003 telemetry stream and decodes the pitch readout (no
    Stadium app, and no need to engage the hardware tuner — the detector is
    always live). Play a note and watch the note/cents update. Ctrl-C to stop.

    Reachability is preflighted (one cheap TCP probe of the --port control
    port) so an unreachable device fails fast with a clear error instead of
    streaming silence for the whole window (the telemetry SUB socket
    connects lazily and cannot tell a dead host from a quiet one).
    """
    from helixgen.device.subscribe import HelixSubscriber
    _, HelixError = _client()
    from helixgen.device import tuner as T

    ip = _resolve_ip_or_fail(ip)
    _telemetry_preflight(ip, port)

    def _bar(cents: int) -> str:
        # 21-cell meter, centre = in tune; ◀/▶ show flat/sharp direction
        pos = max(-10, min(10, round(cents / 5)))
        cells = ["·"] * 21
        cells[10] = "|"
        cells[10 + pos] = "◀" if pos < 0 else ("▶" if pos > 0 else "●")
        return "".join(cells)

    last = None
    try:
        with HelixSubscriber(ip) as sub:
            for ev in sub.stream(duration=seconds, filter_addrs={"/dspEvent"},
                                 include_noise=True):
                r = T.reading_from_event_args(ev.args)
                if r is None:
                    continue
                if as_json:
                    click.echo(json.dumps({
                        "signal": r.signal, "note": r.name, "cents": r.cents,
                        "hz": round(r.hz, 2), "midi": round(r.midi, 3)}))
                    continue
                if not r.signal:
                    line = "  —   (no signal)".ljust(48)
                else:
                    line = (f"  {r.name:<4} {r.cents:+3d}c  "
                            f"{r.hz:7.2f} Hz  {_bar(r.cents)}").ljust(48)
                if line != last:
                    click.echo("\r" + line, nl=False)
                    last = line
        if not as_json:
            click.echo("")  # finish the live line
    except KeyboardInterrupt:
        if not as_json:
            click.echo("")
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e


@device.command(name="meters")
@click.option("--seconds", type=float, default=15.0, show_default=True,
              help="How long to run the meters (streams live level telemetry).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit one JSON reading per line instead of a live display.")
@_device_option
def device_meters(seconds: float, as_json: bool, ip: str, port: int) -> None:
    """Live network level meters — reads the device's grid-level telemetry.

    Subscribes to the 2003 telemetry stream and decodes the two meter arrays
    (`/dspEvent` eid_=1, mid_=796/800 — 128-float grid level data) that ride
    the same burst as the network tuner (no Stadium app needed). Read-only.
    Ctrl-C to stop.

    Reachability is preflighted (one cheap TCP probe of the --port control
    port) so an unreachable device fails fast with a clear error instead of
    streaming silence for the whole window (the telemetry SUB socket
    connects lazily and cannot tell a dead host from a quiet one).
    """
    from helixgen.device.subscribe import HelixSubscriber
    _, HelixError = _client()
    from helixgen.device import meters as M

    ip = _resolve_ip_or_fail(ip)
    _telemetry_preflight(ip, port)

    def _bar(peak: float, scale: float = 0.08, cells: int = 24) -> str:
        n = max(0, min(cells, round((peak / scale) * cells)))
        return "#" * n + "-" * (cells - n)

    last: dict = {}
    try:
        with HelixSubscriber(ip) as sub:
            for ev in sub.stream(duration=seconds, filter_addrs={"/dspEvent"},
                                 include_noise=True):
                for r in M.readings_from_event_args(ev.args):
                    if as_json:
                        click.echo(json.dumps({
                            "mid": r.mid, "peak": round(r.peak, 4),
                            "values": [round(v, 4) for v in r.values]}))
                        continue
                    last[r.mid] = r
                    line = "  ".join(
                        f"{mid}: {_bar(last[mid].peak)} {last[mid].peak:.3f}"
                        for mid in sorted(last))
                    click.echo("\r" + line.ljust(70), nl=False)
        if not as_json:
            click.echo("")  # finish the live line
    except KeyboardInterrupt:
        if not as_json:
            click.echo("")
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e


#: Shared --source help for measure/normalize (workspace #82, core half).
_SOURCE_HELP = ("Signal source feeding the chain. 'input' (default): a "
                "player on the instrument jack — samples are gated on a "
                "real pitch reading plus input level (hum/silence ignored). "
                "'loop': a front-of-chain LOOPER replays a recorded signal "
                "— the input jack is structurally silent (no pitch, no "
                "input level), so samples are gated on CHAIN-OUT level "
                "instead, and the number to compare across targets is the "
                "raw output_db (gain_db is null: no input reference; the "
                "looped source is identical across targets by "
                "construction).")


@device.command(name="measure")
@click.option("--seconds", type=float, default=20.0, show_default=True,
              help="How long to sample the telemetry window.")
@click.option("--min-playing", type=int, default=40, show_default=True,
              help="Minimum playing-gated samples for a trustworthy result "
                   "(~10 samples/sec of actual playing).")
@click.option("--source", type=click.Choice(["input", "loop"]),
              default="input", show_default=True, help=_SOURCE_HELP)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the result as one JSON object.")
@_device_option
def device_measure(seconds: float, min_playing: int, source: str,
                   as_json: bool, ip: str, port: int) -> None:
    """Measure how loud the ACTIVE tone is while you play — read-only.

    Samples the 2003 telemetry for --seconds and reduces the playing-gated
    readings (real pitch + non-silent input; hum and silence are ignored) to
    robust dB statistics: instrument input level, chain-out level, and the
    input-invariant chain gain (output/input) — gain_db is the number to
    compare across snapshots/presets when level-matching. PLAY STEADILY
    during the window — the result reports how much actual playing it saw
    and fails (exit code 1, JSON ok:false + reason) when there wasn't
    enough to trust; just re-run it.

    With --source loop (a front-of-chain looper replaying a recorded
    signal), the input-jack gate above would read pure silence — samples
    are gated on chain-out level instead, gain_db is null (no input
    reference), and output_db is the number to compare across targets.

    Reachability is preflighted (one cheap TCP probe of the --port control
    port) so an unreachable device fails fast with a clear error instead of
    sitting out the whole window and reporting "no meter data" (the
    telemetry SUB socket connects lazily and cannot tell a dead host from a
    quiet one).
    """
    from helixgen.device.subscribe import HelixSubscriber
    from helixgen.device import HelixError
    from helixgen.device import measure as ME

    ip = _resolve_ip_or_fail(ip)
    _telemetry_preflight(ip, port)

    collected = []
    interrupted = False
    t0 = time.monotonic()
    try:
        with HelixSubscriber(ip) as sub:
            events = sub.stream(duration=seconds,
                                filter_addrs={"/dspEvent"},
                                include_noise=True)
            for sample in ME.samples_from_events(events):
                collected.append(sample)
    except KeyboardInterrupt:
        interrupted = True  # summarize the partial window, don't discard it
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e
    # #64d: report the window actually sampled — a Ctrl-C'd partial window
    # must not claim the full --seconds, and playing_seconds derives from
    # the observed sample rate inside summarize().
    elapsed = time.monotonic() - t0
    result = ME.summarize(collected, seconds=elapsed,
                          min_playing=min_playing, source=source)

    if as_json:
        click.echo(json.dumps({k: (round(v, 2) if isinstance(v, float) else v)
                               for k, v in result._asdict().items()}))
    else:
        window = f"{result.seconds:.1f}s"
        if interrupted:
            window += f" (Ctrl-C at {result.seconds:.1f}s of {seconds:.0f}s)"
        click.echo(f"window   : {window} "
                   f"({result.n_samples} samples, "
                   f"{result.playing_seconds:.1f}s playing)")
        click.echo(f"input    : {result.input_db:7.2f} dB")
        click.echo(f"output   : {result.output_db:7.2f} dB "
                   f"(p75 {result.output_db_p75:.2f} dB)")
        if result.gain_db is None:
            click.echo("gain     :     n/a (loop source — compare output_db "
                       "across targets)")
        else:
            click.echo(f"gain     : {result.gain_db:7.2f} dB (chain out/in)")
        if not result.ok:
            click.echo(f"NOT OK   : {result.reason}")
    if not result.ok:
        raise SystemExit(1)


# --- device normalize (loudness phase 2, backlog #62) -----------------------

def _measure_window(ip: str, seconds: float, min_playing: int,
                    source: str = "input"):
    """One playing-gated telemetry window -> ``measure.MeasureResult``
    (the same reduction `device measure` performs). ``source`` selects the
    gate: ``"input"`` (jack pitch+level) or ``"loop"`` (chain-out level,
    workspace #82)."""
    from helixgen.device.subscribe import HelixSubscriber
    from helixgen.device import measure as ME

    collected = []
    t0 = time.monotonic()
    with HelixSubscriber(ip) as sub:
        events = sub.stream(duration=seconds, filter_addrs={"/dspEvent"},
                            include_noise=True)
        for sample in ME.samples_from_events(events):
            collected.append(sample)
    # #64d: summarize over the window actually sampled (observed rate).
    return ME.summarize(collected, seconds=time.monotonic() - t0,
                        min_playing=min_playing, source=source)


def _normalize_resolve_target(results, target_db):
    """The TOTAL-loudness target (dB) for a normalize run: ``--target-db``
    when given, else the FIRST successfully measured target (the anchor) —
    its measured chain gain PLUS the output level already in force. Returns
    ``(target_total_db, anchor_result_or_None)``; raises ``ClickException``
    when no anchor exists."""
    if target_db is not None:
        return float(target_db), None
    for r in results:
        if r.get("ok"):
            return r["total_db"], r
    raise click.ClickException(
        "no target could be measured (every window had too little playing) "
        "— nothing to anchor the trims to; re-run and play steadily, or "
        "give --target-db")


def _normalize_plan(results, target_total, tolerance_db):
    """Stamp each ok result with its ``trim_db`` (0.0 = in band). Trims are
    sized from each target's TOTAL loudness (chain gain + output level in
    force), which is what makes a re-run of the loop a no-op — see
    ``normalize.total_loudness``."""
    from helixgen.device import normalize as NZ

    for r in results:
        r["trim_db"] = (
            NZ.compute_trim(r["total_db"], target_total, tolerance_db)
            if r.get("ok") else None)
        r["applied"] = False


def _normalize_record_library(entries, *, scope, target_total_db,
                              tolerance_db, seconds, source):
    """Upsert a ``normalized`` record onto every fully-normalized ``.hsp``
    that is a registered tone-library variant (resolved via the library's
    tone metadata -- see ``tone_meta.find_variant_by_hsp``). ``entries`` is
    ``(hsp_path, targets)`` pairs -- each ``targets`` a list of that file's
    per-target result dicts, stored VERBATIM (full measurement telemetry:
    the chain-out dBFS ``output_db`` flags in-chain clipping, which agents
    consume for gain-staging fixes). Non-library paths are silently ignored
    (no warning spam), and ANY per-entry failure -- variant detection,
    deserialization, or the metadata save -- warns to stderr without
    failing the normalize run or its --json report (the trims in the .hsp
    are the real outcome; the record is advisory and re-creatable). Records
    overwrite -- latest run wins. Returns the recorded rows for the --json
    payload."""
    import copy
    from datetime import datetime

    from helixgen import __version__, tone_meta

    recorded = []
    for hsp_path, targets in entries:
        # the warning label is computed BEFORE anything that can raise:
        # identity-derived attributes (meta.logical_slug) themselves raise
        # ValueError on a hand-corrupted meta, so they must never be
        # evaluated inside the failure path
        label = Path(hsp_path).name
        try:
            found = tone_meta.find_variant_by_hsp(hsp_path)
            if found is None:
                continue
            meta, key = found
            variant = meta.variants[key]
            label = variant.preset_name or label
            variant.normalized = {
                "at": datetime.now().astimezone().isoformat(
                    timespec="seconds"),
                "scope": scope,
                "source": source,
                "target_total_db": round(float(target_total_db), 2),
                "tolerance_db": float(tolerance_db),
                "seconds": float(seconds),
                "helixgen_version": __version__,
                "targets": copy.deepcopy(list(targets)),
            }
            tone_meta.save_tone_meta(meta)
            recorded.append({"tone": meta.logical_slug, "variant": key,
                             "preset_name": variant.preset_name,
                             "path": str(hsp_path)})
        except Exception as e:
            click.echo(f"warning: could not record normalization on library "
                       f"tone for {label!r}: {e}", err=True)
            continue
    return recorded


@device.command(name="normalize")
@click.argument("preset", required=False,
                type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--setlist", default=None, metavar="NAME",
              help="Level-match every tone of this LOCAL manifest setlist "
                   "instead of one preset's snapshots (mutually exclusive "
                   "with the PRESET argument).")
@click.option("--target-db", type=float, default=None,
              help="Absolute target for each target's TOTAL loudness in dB "
                   "— the median chain gain (output/input, as `device "
                   "measure` reports) PLUS the output-block level already "
                   "in force. Default: the first successfully measured "
                   "target is the anchor and everything else is trimmed to "
                   "match its total.")
@click.option("--seconds", type=float, default=20.0, show_default=True,
              help="Measurement window per target.")
@click.option("--min-playing", type=int, default=40, show_default=True,
              help="Minimum playing-gated samples for a trustworthy "
                   "measurement (~10 samples/sec of actual playing).")
@click.option("--tolerance-db", type=float, default=1.0, show_default=True,
              help="Deltas at or below this magnitude are in band and NOT "
                   "trimmed (don't chase meter noise).")
@click.option("--source", type=click.Choice(["input", "loop"]),
              default="input", show_default=True, help=_SOURCE_HELP)
@click.option("--yes", is_flag=True, default=False,
              help="Actually write the trims into the local .hsp file(s) "
                   "(default is a measure-and-report dry-run).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the run (per-target measurements, trims, written "
                   "files) as one JSON object; progress goes to stderr.")
@_device_option
@_locked("editbuffer", verb="normalize")
def device_normalize(preset: Path | None, setlist: str | None,
                     target_db: float | None, seconds: float,
                     min_playing: int, tolerance_db: float, source: str,
                     yes: bool, as_json: bool, ip: str, port: int) -> None:
    """Level-match snapshots or a setlist by MEASURING while you play (DRY-RUN
    by default).

    The closed loop over `device measure` (loudness spec phase 2): recall
    each target on the device, prompt you to PLAY the same riff steadily for
    the window, then compute each target's dB trim so its TOTAL loudness —
    the measured median chain gain PLUS the output-block level already in
    force (the meter taps sit upstream of the output gain, so the measured
    gain alone never includes an existing trim) — matches the target total:
    the anchor's (the first target that measured ok) unless --target-db
    gives an absolute total. Sizing trims from totals makes the loop
    IDEMPOTENT: re-running it (or running it over a hand-balanced preset
    whose output levels already equalize) computes in-band zero trims
    instead of compounding. Deltas within --tolerance-db are in band and
    left alone. Targets whose window had too little actual playing are
    SKIPPED with a warning (and the run exits 1 to flag the partial result).

    Two scopes: `device normalize <preset.hsp>` level-matches the preset's
    NAMED snapshots — it recalls each snapshot on the device, so the ACTIVE
    device tone must BE this preset (device sync/install it first). The
    active preset's name is verified against the .hsp before anything is
    measured; a mismatch aborts the run (an unverifiable name only warns).
    `device normalize --setlist <name>` level-matches every tone of a local
    manifest setlist that has a local .hsp and an observed device placement
    (loads each by CID and verifies the loaded preset's name matches the
    tone — a mismatch means a stale observation and that tone is SKIPPED;
    tones without a local .hsp or a placement are SKIPPED too). The
    previously ACTIVE preset is restored (best-effort) after a setlist run;
    snapshot scope restores the preset's on-load snapshot.

    DRY-RUN by default: measuring happens, trims are only reported. Re-run
    with --yes to write them into the LOCAL .hsp file(s) — the source of
    truth — as output-block `level` moves (per-snapshot overrides in
    snapshot scope; a whole-preset shift, base plus any per-snapshot array,
    in setlist scope — a uniform shift that preserves the preset's own
    scene-to-scene and path-to-path balance). The device copy is NOT
    written by this verb: run `device sync <setlist>` (or `device install`)
    afterwards to rebuild it from the .hsp. If a mid-run write fails, the
    error lists the files already written. Recalling snapshots / loading
    presets does change the device's ACTIVE tone selection while measuring.

    The output block's `level` is dB-native, so a trim is EXACT by
    construction — and (phase-0 hardware finding) every meter tap sits
    UPSTREAM of the output block's gain, so the trim is INVISIBLE to
    `device measure`: the loop trusts the dB math and deliberately does NOT
    re-measure to confirm (a re-measure would falsely report "no change").

    With --source loop (a front-of-chain LOOPER replays a recorded signal;
    workspace #82), the input-jack gate reads pure silence — measuring
    gates on chain-out level instead, and each target's total loudness is
    its raw measured chain-out output_db PLUS the output level in force
    (the looped source is identical across targets by construction, so
    output_db differences ARE the chain differences; gain_db is null).
    Keep the SAME loop replaying across every target of a run.

    When a --yes run's .hsp is a registered tone-library variant (its path
    resolves to a variant in the library's tone metadata), the run is also
    RECORDED on that variant as a `normalized` record — timestamp, scope,
    target total, tolerance, window seconds, and the FULL per-target
    measurements exactly as --json reports them (chain gain, chain-out
    dBFS — a value over 0 flags in-chain clipping, the gain-staging tell —
    playing seconds, output level in force, total loudness, trim, applied;
    in-band zero trims included: a zero-trim run still confirms the tone
    measures level-matched). Records overwrite: latest run wins. A
    snapshot-scope run with any SKIPPED target records nothing (the tone
    was not fully level-matched); setlist scope records each measured-ok
    tone. Non-library .hsp files are untouched, and dry-run never writes
    metadata. Summaries via `describe <tone>` / `library show <name>`
    (full telemetry under `library show --json`); this verb's --json
    reports the records under "library_recorded".
    """
    HelixClient, HelixError = _client()
    from helixgen.device import normalize as NZ
    from helixgen.hsp import write_hsp

    if (preset is None) == (setlist is None):
        raise click.ClickException(
            "give exactly one scope: a PRESET .hsp (snapshot scope) or "
            "--setlist NAME (setlist scope)")

    say = (lambda msg: click.echo(msg, err=True)) if as_json else click.echo
    results: list[dict] = []
    written: list[str] = []
    warnings: list[str] = []
    # #82: the per-target prompt and comparison metric depend on the source
    # — a human on the jack plays; a looper replays; input mode compares the
    # input-invariant chain gain, loop mode the raw chain-out output_db.
    play_prompt = ("PLAY the same riff steadily"
                   if source == "input"
                   else "keep the LOOPER replaying the same recorded riff")
    measured_key = "gain_db" if source == "input" else "output_db"
    measured_label = "chain gain" if source == "input" else "chain out"

    def _measured_fields(res) -> dict:
        return {"ok": res.ok, "reason": res.reason,
                "gain_db": (None if res.gain_db is None
                            else round(res.gain_db, 2)),
                "output_db": round(res.output_db, 2),
                "playing_seconds": round(res.playing_seconds, 1)}

    try:
        if preset is not None:
            scope = "snapshots"
            body = read_hsp(preset)
            targets = NZ.snapshot_targets(body)
            if not targets:
                raise click.ClickException(
                    f"{preset} has no named snapshots to level-match "
                    f"(name them in the recipe/on the device first)")
            if len(targets) < 2 and target_db is None:
                raise click.ClickException(
                    "only one named snapshot — nothing to match it against "
                    "(give --target-db for an absolute target)")
            say(f"normalize (snapshot scope): {len(targets)} named "
                f"snapshot(s) in {preset}")
            expected_name = (body.get("meta") or {}).get("name")
            with HelixClient(ip, port) as h:
                # identity guard: the loop recalls snapshots on whatever tone
                # is ACTIVE — verify it IS this .hsp before measuring anything
                try:
                    active_info = h.active_preset()
                except HelixError:
                    active_info = None
                active_name = (active_info or {}).get("name")
                if not expected_name or not active_name:
                    say("warning: could not verify the device's ACTIVE "
                        "preset name — proceeding; make sure the active "
                        "tone IS this preset (device sync/install it first)")
                elif active_name != expected_name:
                    raise click.ClickException(
                        f"the device's ACTIVE preset is {active_name!r} "
                        f"(cid {active_info.get('cid')}), not this .hsp's "
                        f"{expected_name!r} — sync/install and select it "
                        f"first, then re-run")
                for idx, name in targets:
                    h.activate_snapshot(idx)
                    say(f"snapshot {idx} {name!r}: {play_prompt} for "
                        f"~{seconds:.0f}s ...")
                    res = _measure_window(ip, seconds, min_playing, source)
                    entry = {"snapshot": idx, "name": name,
                             **_measured_fields(res)}
                    if res.ok:
                        say(f"  measured {measured_label} "
                            f"{entry[measured_key]:+.2f} dB "
                            f"({res.playing_seconds:.1f}s playing)")
                        level = NZ.reference_output_level(body, idx)
                        entry["output_level_db"] = round(level, 1)
                        entry["total_db"] = round(
                            entry[measured_key] + level, 2)
                    else:
                        say(f"  warning: SKIPPED — {res.reason}")
                    results.append(entry)
                # leave the device on the preset's on-load snapshot
                active = ((body.get("preset") or {}).get("params")
                          or {}).get("activesnapshot", 0)
                try:
                    h.activate_snapshot(active if isinstance(active, int)
                                        and not isinstance(active, bool) else 0)
                except HelixError:
                    pass

            target, anchor = _normalize_resolve_target(results, target_db)
            _normalize_plan(results, target, tolerance_db)
            if yes:
                for r in results:
                    if r.get("ok") and r["trim_db"]:
                        warnings.extend(NZ.apply_snapshot_trim(
                            body, r["snapshot"], r["trim_db"]))
                        r["applied"] = True
                if any(r["applied"] for r in results):
                    write_hsp(preset, body)
                    written.append(str(preset))
            anchor_json = ({"snapshot": anchor["snapshot"],
                            "name": anchor["name"]} if anchor else None)
            payload = {"scope": scope, "preset": str(preset)}
        else:
            scope = "setlist"
            SetlistManifest, _ManifestError = _manifest()
            from helixgen.device import observations as OBS

            m = SetlistManifest.load()
            rec = m.setlists_map.get(setlist)
            if rec is None:
                raise click.ClickException(
                    f"setlist {setlist!r} is not in the local manifest "
                    f"(see `device setlist list`)")
            tone_names = list(rec.get("tones") or [])
            if not tone_names:
                raise click.ClickException(f"setlist {setlist!r} has no tones")
            say(f"normalize (setlist scope): {len(tone_names)} tone(s) in "
                f"{setlist!r}")
            with HelixClient(ip, port) as h:
                obs = OBS.load_observations(_serial_of(h, ip))
                # save the player's current selection; restored after the run
                try:
                    prev_cid = (h.active_preset() or {}).get("cid")
                except HelixError:
                    prev_cid = None
                for name in tone_names:
                    trec = m.tones.get(name) or {}
                    hsp_path = trec.get("path")
                    placement = obs.tone_placement(name)
                    if not hsp_path or not Path(hsp_path).exists():
                        say(f"warning: {name!r}: SKIPPED — no local .hsp to "
                            f"write trims into")
                        results.append({"tone": name, "path": hsp_path,
                                        "ok": False,
                                        "reason": "no local .hsp"})
                        continue
                    if not placement or placement.get("cid") is None:
                        say(f"warning: {name!r}: SKIPPED — no observed device "
                            f"placement (run `device sync {setlist}` first)")
                        results.append({"tone": name, "path": hsp_path,
                                        "ok": False,
                                        "reason": "not observed on device"})
                        continue
                    h.load_preset(int(placement["cid"]))
                    # identity guard: a stale observed CID silently measures
                    # some OTHER preset — verify the loaded preset's name
                    try:
                        loaded_name = (h.active_preset() or {}).get("name")
                    except HelixError:
                        loaded_name = None
                    if loaded_name and loaded_name != name:
                        say(f"warning: {name!r}: SKIPPED — cid "
                            f"{placement['cid']} on the device is named "
                            f"{loaded_name!r} (stale observation? run "
                            f"`device sync {setlist}` first)")
                        results.append({
                            "tone": name, "path": hsp_path, "ok": False,
                            "reason": f"device name mismatch: cid "
                                      f"{placement['cid']} is "
                                      f"{loaded_name!r}"})
                        continue
                    if not loaded_name:
                        say(f"warning: {name!r}: could not verify the "
                            f"loaded preset's name — proceeding")
                    say(f"tone {name!r}: {play_prompt} for "
                        f"~{seconds:.0f}s ...")
                    res = _measure_window(ip, seconds, min_playing, source)
                    entry = {"tone": name, "path": hsp_path,
                             **_measured_fields(res)}
                    if res.ok:
                        say(f"  measured {measured_label} "
                            f"{entry[measured_key]:+.2f} dB "
                            f"({res.playing_seconds:.1f}s playing)")
                        level = NZ.reference_output_level(
                            read_hsp(Path(hsp_path)))
                        entry["output_level_db"] = round(level, 1)
                        entry["total_db"] = round(
                            entry[measured_key] + level, 2)
                    else:
                        say(f"  warning: SKIPPED — {res.reason}")
                    results.append(entry)
                # best-effort: put the player's selection back
                if prev_cid is not None:
                    try:
                        h.load_preset(int(prev_cid))
                    except HelixError:
                        pass

            target, anchor = _normalize_resolve_target(results, target_db)
            _normalize_plan(results, target, tolerance_db)
            if yes:
                for r in results:
                    if r.get("ok") and r["trim_db"]:
                        tone_body = read_hsp(Path(r["path"]))
                        warnings.extend(NZ.apply_base_trim(
                            tone_body, r["trim_db"]))
                        try:
                            write_hsp(Path(r["path"]), tone_body)
                        except OSError as e:
                            already = ("; trims ALREADY written to: "
                                       + ", ".join(written)
                                       if written else
                                       "; no files had been written yet")
                            raise click.ClickException(
                                f"failed writing {r['path']}: {e}{already}. "
                                f"Re-running normalize is safe — trims are "
                                f"sized from total loudness, so already-"
                                f"written files re-measure in band.") from e
                        written.append(r["path"])
                        r["applied"] = True
            anchor_json = {"tone": anchor["tone"]} if anchor else None
            payload = {"scope": scope, "setlist": setlist}
    except HelixError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(str(e)) from e

    for w in warnings:
        say(f"warning: {w}")

    # library recording: a --yes run whose .hsp is a registered library
    # variant gets a `normalized` record on that variant's tone metadata --
    # the run parameters plus the FULL per-target telemetry, verbatim (in-
    # band zero trims included: they confirm level-match). Snapshot scope
    # records only a COMPLETE run (any skipped snapshot means the tone was
    # not fully level-matched); setlist scope records each measured-ok tone
    # with its own single target entry. Dry-run never writes metadata.
    library_recorded: list[dict] = []
    if yes:
        if scope == "snapshots":
            if results and all(r.get("ok") for r in results):
                library_recorded = _normalize_record_library(
                    [(str(preset), results)], scope=scope,
                    target_total_db=target, tolerance_db=tolerance_db,
                    seconds=seconds, source=source)
        else:
            entries = [(r["path"], [r]) for r in results if r.get("ok")]
            library_recorded = _normalize_record_library(
                entries, scope=scope, target_total_db=target,
                tolerance_db=tolerance_db, seconds=seconds, source=source)

    skipped = [r for r in results if not r.get("ok")]
    if as_json:
        payload.update({
            "source": source,
            "target_total_db": round(target, 2),
            "anchor": anchor_json,
            "tolerance_db": tolerance_db,
            "dry_run": not yes,
            "targets": results,
            "written": written,
            "library_recorded": library_recorded,
        })
        click.echo(json.dumps(payload, indent=2))
    else:
        anchor_desc = ("--target-db" if anchor_json is None else
                       f"anchor {anchor_json.get('name', anchor_json.get('tone'))!r}")
        click.echo(f"plan (target = {anchor_desc}, {target:+.2f} dB total "
                   f"loudness = {measured_label} + output level):")
        for r in results:
            label = (f"snapshot {r['snapshot']} {r['name']!r}"
                     if scope == "snapshots" else f"tone {r['tone']!r}")
            if not r.get("ok"):
                click.echo(f"  {label}: SKIPPED ({r['reason']})")
            elif r["trim_db"]:
                state = "applied" if r["applied"] else "would trim"
                measured_word = "gain" if source == "input" else "out"
                click.echo(f"  {label}: {r['total_db']:+.2f} dB total "
                           f"({r[measured_key]:+.2f} {measured_word} "
                           f"{r['output_level_db']:+.1f} level) — {state} "
                           f"{r['trim_db']:+.1f} dB")
            else:
                mark = " (anchor)" if r is anchor else ""
                click.echo(f"  {label}: {r['total_db']:+.2f} dB total — "
                           f"in band (±{tolerance_db:g} dB){mark}")
        if yes:
            if written:
                for p in written:
                    click.echo(f"wrote {p}")
                click.echo(
                    "run `helixgen device sync <setlist>` (or `device "
                    "install`) to rebuild the device copy — output-level "
                    "trims are exact dB moves the meter grids cannot see "
                    "(deliberately NOT re-measured).")
            else:
                click.echo("nothing to write (every target in band or skipped)")
            for rec in library_recorded:
                click.echo(f"recorded in library: {rec['tone']} "
                           f"(variant {rec['variant']})")
        else:
            click.echo("dry-run: no trims written — re-run with --yes to "
                       "write them into the .hsp")
    if skipped:
        raise SystemExit(1)
