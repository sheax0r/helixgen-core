"""Shared per-tone IR-upload core (backlog #6).

Diffs a preset's referenced ``irhash``es against what the device already has
(:func:`helixgen.device.bridge.check_irs`), resolves each missing hash to a
local WAV via the helixgen IR mapping (``mapping.json``), and pushes it with
:func:`helixgen.device.sftp.push_ir` (instant registration — the device's
resulting hash is checked against the requested one).

Three call sites share this core instead of each re-implementing it:

* CLI ``device install --auto-irs`` / ``device slots restore`` —
  ``helixgen.cli._auto_upload_irs`` is a thin wrapper that echoes the same
  per-hash human messages it always has.
* ``device sync`` — ``helixgen.device.setlist_sync._upload_missing_irs`` is
  likewise a thin wrapper (kept under its original name so existing tests /
  call sites can still monkeypatch it).
* MCP ``device_install_preset`` — calls :func:`sync_preset_irs` directly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence


def upload_missing_irs(ip: str, hashes: Sequence[str]) -> List[Dict[str, Any]]:
    """Resolve + upload each of ``hashes`` (already known to be missing on the
    device).

    Returns one result dict per hash: ``{hash, ok, outcome, note, path?,
    name?, device_hash?, hash_match?}``. Never raises — every failure mode
    (unreadable mapping.json, hash not registered locally, an SFTP/push
    error, a post-upload hash mismatch, or a registration that hasn't landed
    yet) is surfaced as a per-hash entry so callers can decide how to react
    (a CLI can echo/abort, the sync engine can append to its ``errors``,  the
    MCP tool can just return the list).

    ``outcome`` is one of:

    * ``"no_mapping"`` — the local IR mapping.json couldn't be loaded; applies
      identically to every hash (nothing was attempted).
    * ``"not_found_locally"`` — the hash isn't registered in mapping.json.
    * ``"upload_error"`` — ``push_ir`` raised. Any exception is caught here
      (``push_ir`` spans SFTP/paramiko, sockets, and the ZeroMQ client, so
      the failure surface is much wider than ``HelixError``); the entry's
      ``error_type`` carries the exception class name.
    * ``"already"`` — the IR is already on the device under this hash.
    * ``"imported"`` — uploaded and the device's hash matches (``ok``).
    * ``"hash_mismatch"`` — uploaded and registered, but the device computed a
      *different* hash than the preset references — the cab won't resolve
      (the irhash-algorithm edge case for that file).
    * ``"not_yet_registered"`` — uploaded but the device hasn't confirmed
      registration within the wait window — retry shortly.
    * ``"upload_failed"`` — the upload itself failed (no exception, just
      ``ok: False`` from ``push_ir``).

    ``ok`` is True only for ``"already"``/``"imported"``.
    """
    from helixgen.ir import IrMapping
    from . import sftp

    try:
        irmap = IrMapping.load()
    except Exception as e:  # noqa: BLE001 - surfaced per-hash, never aborts here
        note = f"--auto-irs needs your local IR mapping.json: {e}"
        return [{"hash": h, "ok": False, "outcome": "no_mapping", "note": note}
                for h in hashes]

    results: List[Dict[str, Any]] = []
    for hh in hashes:
        try:
            path = irmap.resolve_by_hash(hh)
        except Exception:  # noqa: BLE001 - not registered locally
            results.append({
                "hash": hh, "ok": False, "outcome": "not_found_locally",
                "note": (f"referenced IR {hh} not found locally; register it "
                         f"(helixgen register-irs) — cab may be silent"),
            })
            continue
        try:
            res = sftp.push_ir(ip, str(path))
        except Exception as e:  # noqa: BLE001 — push_ir spans SFTP/paramiko,
            # sockets, and the ZMQ client; catch everything so one bad IR
            # never blows up the caller (consistent with the mapping-load
            # guard above). The class name is kept for diagnosis.
            results.append({"hash": hh, "ok": False, "outcome": "upload_error",
                            "path": str(path), "note": str(e),
                            "error_type": type(e).__name__})
            continue

        entry: Dict[str, Any] = {
            "hash": hh, "path": str(path), "name": res.get("name"),
            "device_hash": res.get("device_hash"),
            "hash_match": res.get("hash_match"),
        }
        if res.get("already"):
            entry.update(ok=True, outcome="already",
                        note=f"IR {hh} already on device")
        elif res.get("ok") and res.get("registered") and res.get("hash_match"):
            entry.update(ok=True, outcome="imported",
                        note=f"imported IR {res.get('name') or path.name} ({hh})")
        elif res.get("ok") and res.get("registered"):
            entry.update(ok=False, outcome="hash_mismatch",
                        note=(f"{path.name} registered as {res.get('device_hash')} "
                              f"but the preset references {hh} — cab won't resolve "
                              f"(irhash-algorithm edge case for this file)"))
        elif res.get("ok"):
            entry.update(ok=False, outcome="not_yet_registered",
                        note=(f"uploaded {path.name} ({hh}) but not yet "
                              f"registered — retry shortly"))
        else:
            entry.update(ok=False, outcome="upload_failed",
                        note=f"failed to upload {path.name} ({hh})")
        results.append(entry)
    return results


def sync_preset_irs(client, body: dict, ip: str, *, auto_irs: bool = True
                    ) -> List[Dict[str, Any]]:
    """Diff ``body``'s referenced IRs against ``client`` and, if ``auto_irs``,
    upload the missing ones (:func:`upload_missing_irs`).

    Returns ``[]`` when the preset references no IRs, or none are missing.
    When ``auto_irs`` is False and IRs ARE missing, returns one
    ``{hash, ok: False, outcome: "skipped_auto_irs_off", note}`` entry per
    missing hash instead of uploading — so a caller can still surface the
    "cab may be silent" warning without touching the device.
    """
    from . import bridge

    missing = sorted(bridge.check_irs(client, body).get("missing", []))
    if not missing:
        return []
    if not auto_irs:
        return [{"hash": h, "ok": False, "outcome": "skipped_auto_irs_off",
                 "note": (f"IR {h} is referenced but not on the device; "
                          f"enable auto_irs, or import it (helixgen "
                          f"register-irs / the editor), or the cab will be "
                          f"silent")}
                for h in missing]
    return upload_missing_irs(ip, missing)
