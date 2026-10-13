"""Git-trackable photograph of a Helix Stadium's contents (device file-copy model).

The snapshot is a **photograph, not a working document**: ``take`` writes what
the device holds right now, ``plan_restore``/``apply_restore`` replay it back.
Hand-editing the tree does nothing until a restore runs — that one-way flow of
truth is what keeps this from becoming ``device sync`` under a new name (see
``docs/superpowers/specs/2026-09-09-device-file-copy-model-design.md``).

On-disk layout, under ``<root>/<serial>/``::

    index.json               # slugged filename -> exact device name (pool + setlists)
    device.json              # model, firmware, serial, globals
    pool/<Name>.sbe          # device content bytes, verbatim, one per pool preset
    setlists/<Name>.json     # references in order: [{"name": "Dream On", "posi": 0}, ...]
                             #   (pre-0.56 backups: a bare name array, read as posi = index)
    irs/<irhash>.wav         # device-processed IR, ~32 KB, pulled over SFTP
    irs/index.json           # irhash -> {name, file, channels}

``.sbe`` files hold the device's OWN bytes — no ``.hsp`` transcoder anywhere in
the backup or restore path, so nothing the backwards decoder cannot yet read
(Command Center commands, MIDI CC bindings) is silently dropped. ``git diff``
reads them through the ``helixgen device decode`` textconv wired by
:func:`ensure_git_textconv`.

**The IR cross-check.** The device's ``-11`` IR listing cache is never
invalidated by a watched-dir import (backlog #38), so an IR another client
imported can stay unlisted for 11+ minutes. Collecting only what ``list_irs``
reports would silently under-collect and a later restore would put back presets
whose cabs read "No Model". So :func:`take` collects the **union** of the
listing and every ``irmd`` referenced by a pulled preset, then verifies each
pulled file's data-chunk MD5 equals its ``irhash``. A hash that cannot be
resolved, pulled, or verified lands in ``irs["missing"]`` with a reason — never
silently dropped.

Device reads here are all **non-activating** (``get_content``, never
``load_preset``): a backup must not disturb the player's live tone. ``take`` and
``plan_restore`` never write to the device; only :func:`apply_restore` mutates,
under :meth:`HelixClient.mutating`. The CLI wrapper owns the advisory file lock
(``@_locked("library", "irs", verb="restore")``) — no device module acquires one
itself.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import content as _content
from . import irmd as _irmd
from .reorder import SETLIST_SLOTS, _firmware, _fw_at_least
from .client import Cctp, Container, HelixError

logger = logging.getLogger(__name__)

#: Wall-clock properties change every second, so they would make every backup a
#: git diff. The photograph is of the device's *configuration*, not its clock.
VOLATILE_SETTINGS_PAGE = "date-time"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_ZERO_IRMD = b"\x00" * 16


# -- naming -----------------------------------------------------------------

def _slug(name: str) -> str:
    """Filesystem-safe slug for a device display name. Never empty."""
    s = _UNSAFE.sub("-", (name or "").strip()).strip("-._")
    return s or "untitled"


def assign_filenames(names: Sequence[str], ext: str) -> List[str]:
    """Unique, deterministic filenames for ``names`` (parallel list).

    Collision-safe in both directions, and **order-independent** so the tree
    does not churn when the device's slot order changes:

    * two DIFFERENT names that slug the same (``"Lead/Tone"`` and
      ``"Lead:Tone"``) each get a short digest of their own name appended —
      both of them, so which one "wins" never depends on listing order;
    * genuinely IDENTICAL names (the device permits duplicates) fall back to an
      occurrence counter in device order, since no content-derived suffix can
      tell them apart.

    The exact name is recovered from ``index.json``, not from the filename.
    """
    slugs = [_slug(n) for n in names]
    # Case-fold the collision keys: macOS (and Windows) filesystems are
    # case-insensitive by default, so "Lead" and "lead" are ONE file on disk.
    # Counting case-sensitively let the second silently clobber the first —
    # losing a preset from the photograph, and then restoring one preset's
    # bytes under both names.
    slug_n = Counter(s.casefold() for s in slugs)
    name_n = Counter((n or "").casefold() for n in names)
    stems = []
    for n, s in zip(names, slugs):
        if slug_n[s.casefold()] > name_n[(n or "").casefold()]:
            # a *different* name shares this slug (case-insensitively)
            s = f"{s}~{hashlib.sha256((n or '').encode('utf-8')).hexdigest()[:8]}"
        stems.append(s)
    seen: Counter = Counter()
    out = []
    for s in stems:
        seen[s.casefold()] += 1
        n_ = seen[s.casefold()]
        out.append(f"{s}{ext}" if n_ == 1 else f"{s}~{n_}{ext}")
    return out


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")


# -- IR helpers -------------------------------------------------------------

def ir_refs(blob: bytes) -> set:
    """Every ``irhash`` referenced by a device content blob.

    Walks the decoded native structure for ``mdls[*].irmd`` (the 16-byte IR
    hash an IR cab points at). Half of the IR cross-check: the listing is the
    other half, and it is the half that lies (#38).
    """
    found: set = set()

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if (k == "irmd" and isinstance(v, (bytes, bytearray))
                        and len(v) == 16 and bytes(v) != _ZERO_IRMD):
                    found.add(_irmd.irmd_to_irhash(v))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(_content.decode_any(blob))
    return found


def _wav_data_md5(data: bytes) -> Optional[str]:
    """MD5 of a WAV's ``data`` chunk — the Stadium ``irhash`` of a processed IR.

    Byte-level twin of ``helixgen.ir._md5_wav_data_chunk``, but over bytes we
    already hold, and returning ``None`` (rather than raising) for anything that
    is not a WAV — a truncated SFTP pull must read as "verification failed", not
    as an exception that aborts the backup.
    """
    di = data.find(b"data")
    if di < 0 or di + 8 > len(data):
        return None
    sz = int.from_bytes(data[di + 4:di + 8], "little")
    chunk = data[di + 8:di + 8 + sz]
    if len(chunk) != sz:
        return None
    return hashlib.md5(chunk).hexdigest()


# -- take -------------------------------------------------------------------

def _note(progress: Optional[Callable[[str], None]], msg: str) -> None:
    if progress:
        try:
            progress(msg)
        except Exception:  # noqa: BLE001 - progress is advisory, never fatal
            pass


def _serial_of(client) -> str:
    """The device's serial, or an ip-derived fallback. Never raises."""
    try:
        serial = (client.product_info() or {}).get("serial")
        if serial:
            return str(serial)
    except Exception:  # noqa: BLE001 - identity is advisory
        pass
    return f"ip-{getattr(client, 'ip', None) or 'unknown'}"


def _globals(client, errors: List[str]) -> Dict[str, Any]:
    """Every global setting the device reports, minus the wall clock."""
    from . import settings as _settings
    from .client import PropertyUnavailable
    skip = set(_settings.pages().get(VOLATILE_SETTINGS_PAGE, ()))
    out: Dict[str, Any] = {}
    for key in _settings.all_keys():
        if key in skip:
            continue
        try:
            out[key] = client.get_property(key).value
        except PropertyUnavailable:
            continue  # not defined on this firmware: absent, not unreadable
        except Exception as exc:  # noqa: BLE001 - one unreadable global is not fatal
            if key in _settings.FIRMWARE_SPECIFIC_KEYS:
                continue  # reply shape on other firmware uncharacterized
            errors.append(f"global {key}: {exc}")
    return out


def _collect_irs(client, base: Path, wanted: Sequence[str],
                 listed: Dict[str, Dict[str, Any]],
                 files: Dict[str, bytes], progress) -> Dict[str, List]:
    """Pull + verify every wanted IR. Returns the ``irs`` result section."""
    res: Dict[str, List] = {"pulled": [], "unchanged": [], "missing": []}
    index: Dict[str, Dict[str, Any]] = {}
    if not wanted:
        files["irs/index.json"] = _json_bytes(index)
        return res

    # Resolve on-device basenames first: /IrPathForHashGet is the AUTHORITATIVE
    # presence check (it reflects an import immediately, unlike the -11 cache),
    # and `pull-ir` addresses IRs by file basename, not by hash.
    todo: List[Tuple[str, str]] = []
    for h in wanted:
        row = listed.get(h, {})
        index[h] = {
            "name": row.get("name"),
            "file": None,
            "channels": (1 if row.get("mono") else 2) if row else None,
        }
        try:
            path = client.ir_path_for_hash(h, strict=True)
        except HelixError as exc:
            res["missing"].append({"irhash": h, "reason": f"path lookup failed: {exc}"})
            continue
        if not path:
            res["missing"].append(
                {"irhash": h, "reason": "referenced by a preset but not registered "
                                        "on the device"})
            continue
        fname = str(path).rsplit("/", 1)[-1]
        index[h]["file"] = fname
        rel = f"irs/{h}.wav"
        # An IR file is keyed BY its own content hash, so an existing verified
        # copy is provably identical — no need to pull it again.
        existing = base / rel
        if existing.is_file():
            data = existing.read_bytes()
            if _wav_data_md5(data) == h:
                files[rel] = data
                res["unchanged"].append(h)
                continue
        todo.append((h, fname))

    if todo:
        try:
            from .sftp import HelixSFTP
            with HelixSFTP(getattr(client, "ip", None)) as s:
                for h, fname in todo:
                    _note(progress, f"ir {h}")
                    try:
                        with tempfile.TemporaryDirectory() as td:
                            local = os.path.join(td, fname)
                            s.download_ir(fname, local)
                            data = Path(local).read_bytes()
                    except Exception as exc:  # noqa: BLE001
                        res["missing"].append(
                            {"irhash": h, "reason": f"pull failed: {exc}"})
                        continue
                    got = _wav_data_md5(data)
                    if got != h:
                        res["missing"].append(
                            {"irhash": h,
                             "reason": f"MD5 verification failed: pulled file "
                                       f"hashes to {got}"})
                        continue
                    files[f"irs/{h}.wav"] = data
                    res["pulled"].append(h)
        except Exception as exc:  # noqa: BLE001 - no SFTP => every pull is missing
            for h, _fname in todo:
                res["missing"].append({"irhash": h, "reason": f"SFTP unavailable: {exc}"})

    for m in res["missing"]:
        index.pop(m["irhash"], None)
    files["irs/index.json"] = _json_bytes(index)
    return res


def take(client, root: Path, *, dry_run: bool = False, now: Optional[str] = None,
         progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Photograph the device into ``root/<serial>/``.

    Walks the pool, the setlists and the IRs, and writes only the files whose
    bytes actually changed — an unchanged backup is a no-op commit, not a churn
    of 128 rewritten presets.

    ``dry_run=True`` computes everything (including the IR pulls it cannot skip)
    but writes NOTHING; the returned ``changed`` list is therefore the accurate
    diff-against-live, which is why the design needs no separate ``diff`` verb.

    Every device read is non-activating: preset bytes come from ``get_content``,
    never ``load_preset``. Nothing here mutates the device.

    Returns ``{serial, root, pool: {written, unchanged, removed},
    setlists: {...}, irs: {pulled, unchanged, missing}, changed, errors}``.
    """
    root = Path(root)
    errors: List[str] = []
    serial = _serial_of(client)
    base = root / _slug(serial)
    files: Dict[str, bytes] = {}

    # -- pool ---------------------------------------------------------------
    presets = client.list_presets(Container.POOL, strict=True)
    pool_names = [str(p.get("name", "") or "") for p in presets]
    pool_files = assign_filenames(pool_names, ".sbe")
    cid_to_name: Dict[Any, str] = {}
    referenced: set = set()
    name_index: Dict[str, str] = {}
    for i, (p, nm, fn) in enumerate(zip(presets, pool_names, pool_files), 1):
        cid_to_name[p.get("cid_")] = nm
        name_index[f"pool/{fn}"] = nm
        _note(progress, f"pool {i}/{len(presets)} {nm}")
        try:
            blob = client.get_content(p.get("cid_"))
        except Exception as exc:  # noqa: BLE001 - one unreadable preset is not fatal
            errors.append(f"pool preset {nm!r} (cid {p.get('cid_')}): {exc}")
            continue
        files[f"pool/{fn}"] = blob
        try:
            referenced |= ir_refs(blob)
        except Exception as exc:  # noqa: BLE001 - undecodable blob still backs up
            errors.append(f"IR scan of {nm!r} failed: {exc}")

    # -- setlists -----------------------------------------------------------
    setlists = client.list_setlists(strict=True)
    sl_names = [str(s.get("name", "") or "") for s in setlists]
    sl_files = assign_filenames(sl_names, ".json")
    for s, nm, fn in zip(setlists, sl_names, sl_files):
        name_index[f"setlists/{fn}"] = nm
        _note(progress, f"setlist {nm}")
        try:
            items = client.list_container(s.get("cid_"), strict=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"setlist {nm!r}: {exc}")
            continue
        refs = sorted((m for m in items if m.get("cctp") == Cctp.REFERENCE),
                      key=lambda m: m.get("posi", 1 << 30))
        # posi is recorded (hgc-6r5.14): fw 1.4 setlists keep real gaps, and
        # a restore must put each reference back where it was, not compacted.
        files[f"setlists/{fn}"] = _json_bytes(
            [{"name": cid_to_name.get(m.get("rcid")), "posi": m.get("posi")}
             for m in refs])

    # -- IRs: the cross-check (listing UNION preset references, #38) --------
    try:
        listed_rows = client.list_irs(strict=True)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"IR listing: {exc}")
        listed_rows = []
    listed = {r["hash"]: r for r in listed_rows if r.get("hash")}
    unlisted = sorted(referenced - set(listed))
    if unlisted:
        logger.warning(
            "%d IR(s) referenced by a preset are absent from the device's -11 "
            "listing (%s) — the listing cache is stale (backlog #38); backing "
            "them up anyway", len(unlisted), ", ".join(unlisted))
    irs = _collect_irs(client, base, sorted(set(listed) | referenced), listed,
                       files, progress)

    files["index.json"] = _json_bytes({"pool": {}, "setlists": {}} | {
        "pool": {k.split("/", 1)[1]: v for k, v in name_index.items()
                 if k.startswith("pool/")},
        "setlists": {k.split("/", 1)[1]: v for k, v in name_index.items()
                     if k.startswith("setlists/")},
    })
    dev: Dict[str, Any] = {"serial": serial}
    try:
        info = client.product_info() or {}
        dev.update({k: info.get(k) for k in
                    ("model", "helixgen_model", "device_id", "firmware",
                     "firmware_build", "firmware_date")})
    except Exception as exc:  # noqa: BLE001
        errors.append(f"product info: {exc}")
    dev["globals"] = _globals(client, errors)
    if now is not None:
        dev["taken_at"] = now
    files["device.json"] = _json_bytes(dev)

    # -- classify against what is already on disk ---------------------------
    result: Dict[str, Any] = {
        "serial": serial, "root": str(root),
        "pool": {"written": [], "unchanged": [], "removed": []},
        "setlists": {"written": [], "unchanged": [], "removed": []},
        "irs": irs, "changed": [], "errors": errors,
    }
    write: Dict[str, bytes] = {}
    for rel in sorted(files):
        cur = base / rel
        old = cur.read_bytes() if cur.is_file() else None
        # Only pool/ and setlists/ carry written/unchanged sections; irs/ is
        # classified by _collect_irs (pulled/unchanged/missing) and index.json
        # and device.json are top-level.
        top = rel.split("/", 1)[0] if "/" in rel else None
        section = result.get(top) if top in ("pool", "setlists") else None
        if old == files[rel]:
            if isinstance(section, dict):
                section["unchanged"].append(rel.split("/", 1)[1])
            continue
        write[rel] = files[rel]
        if isinstance(section, dict):
            section["written"].append(rel.split("/", 1)[1])
        result["changed"].append(f"{'+' if old is None else '~'} {rel}")

    # A partial read must never prune: anything we failed to fetch would look
    # deleted and take its previous backup down with it. IR misses count —
    # they live in irs["missing"], not errors, and a transient path-lookup or
    # SFTP failure would otherwise unlink an already-verified .wav that this
    # backup may be the only remaining copy of.
    stale: List[str] = []
    if not errors and not irs["missing"]:
        for d, pat in (("pool", "*.sbe"), ("setlists", "*.json"), ("irs", "*.wav")):
            for f in sorted((base / d).glob(pat)) if (base / d).is_dir() else []:
                rel = f"{d}/{f.name}"
                if rel not in files:
                    stale.append(rel)
                    section = result.get(d) if d in ("pool", "setlists") else None
                    if isinstance(section, dict):
                        section.setdefault("removed", []).append(f.name)
                    result["changed"].append(f"- {rel}")

    if irs["missing"] and not errors:
        logger.warning(
            "%d IR(s) could not be collected; pruning is disabled for this "
            "run so nothing already backed up is removed", len(irs["missing"]))

    if dry_run:
        return result

    # Single write phase: every device read already succeeded or was recorded,
    # so a failed pull can never leave a half-written tree behind.
    for rel, data in write.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for rel in stale:
        (base / rel).unlink()
    # Wire the textconv so `git diff` renders these .sbe blobs as JSON rather
    # than "Binary files differ". Idempotent, advisory: a repo-less root or a
    # missing git just leaves the diffs binary.
    result["textconv"] = ensure_git_textconv(root)
    return result


# -- restore ----------------------------------------------------------------

def _snapshot_dir(root: Path, serial: Optional[str]) -> Path:
    root = Path(root)
    if serial:
        return root / _slug(str(serial))
    dirs = sorted(d for d in root.glob("*") if d.is_dir())
    if len(dirs) == 1:
        return dirs[0]
    raise HelixError(
        f"{'no' if not dirs else 'more than one'} snapshot under {root} — "
        f"pass the device serial explicitly"
        + (f" (found: {', '.join(d.name for d in dirs)})" if dirs else ""))


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _by_name(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r.get("name", "") or ""), []).append(r)
    return out


def _desired_refs(doc: Any) -> Optional[List[Tuple[str, int]]]:
    """A setlist file as ``[(name, posi)]``, or ``None`` if malformed.

    Current backups record ``{"name", "posi"}`` objects; a pre-posi backup is
    a bare name array, read as compact positions over its non-null names —
    exactly what it used to restore to. Unresolvable references (``name:
    null``) are skipped, as before. A posi outside 0..127 or held twice is
    malformed (a second reference at one posi would REPLACE the first).
    """
    if not isinstance(doc, list):
        return None
    if all(not isinstance(d, dict) for d in doc):          # legacy shape
        return _legacy_refs(doc)
    out, seen = [], set()
    for d in doc:
        if not isinstance(d, dict):
            return None
        name, posi = d.get("name"), d.get("posi")
        if (not isinstance(posi, int) or isinstance(posi, bool)
                or not 0 <= posi < SETLIST_SLOTS or posi in seen):
            return None
        seen.add(posi)
        if name is not None:
            out.append((str(name), posi))
    return out


def _legacy_refs(doc: List[Any]) -> List[Tuple[str, int]]:
    names = [str(d) for d in doc if d is not None]
    return [(nm, i) for i, nm in enumerate(names)]


def _plan_setlist(sl_name: str, desired: List[Tuple[str, int]],
                  live: List[Dict[str, Any]], prune: bool,
                  ops: List[Dict[str, Any]], summary: List[str],
                  errors: List[str]) -> None:
    """Ops putting every desired reference at its recorded posi, such that NO
    op sequence — including one where any op fails and the run continues —
    ever leaves a desired preset absent from the setlist.

    ``live`` rows are ``{cid, posi, name}`` (``name`` None for a dangling
    reference). Duplicate references are allowed on the device, so a
    misplaced reference is MOVED by adding the new one first and only then
    removing the old one by its ref cid (``move_ref``); a failure at either
    step leaves the preset present (once or twice), never gone. A reorder is
    never used: on fw 1.4 it shifts every slot in between. A reference added
    at an OCCUPIED posi REPLACES the incumbent (fw 1.4, HW-observed), so
    targets must be empty: cycles (swaps) are broken by parking one reference
    at a free posi; a posi held by a reference the backup lacks there is an
    error unless ``prune`` removes it. ``_apply_op`` re-checks every target
    against a strict re-list right before writing, so a failed earlier op can
    never turn a later add into a silent replace.
    """
    todo = list(desired)
    rest = list(live)
    for nm, posi in list(todo):            # already in place
        hit = next((r for r in rest if r["name"] == nm and r["posi"] == posi), None)
        if hit is not None:
            rest.remove(hit)
            todo.remove((nm, posi))
    moves = []                             # [src_posi_or_None, name, target]
    for nm, posi in todo:
        src = next((r for r in rest if r["name"] == nm), None)
        if src is not None:
            rest.remove(src)
        moves.append([src["posi"] if src else None, nm, posi])
    occupied = {r["posi"] for r in live}
    for r in rest:                         # live refs the backup lacks
        if prune:
            ops.append({"op": "remove_ref", "setlist": sl_name,
                        "name": r["name"], "ref_cid": r["cid"], "pos": r["posi"]})
            summary.append(f"remove {r['name'] or '(dangling)'!r} from setlist "
                           f"{sl_name!r} at {r['posi']}")
            occupied.discard(r["posi"])
    fixed = {r["posi"]: r["name"] for r in rest if not prune}
    if fixed:
        summary.append(
            f"note: keeping {len(fixed)} reference(s) in setlist {sl_name!r} the backup "
            f"lacks: " + ", ".join(f"{(n or '(dangling)')!r}@{p}"
                                   for p, n in sorted(fixed.items()))
            + " (re-run with --prune to remove them)")

    def emit(src, nm, target):
        if src is None:
            ops.append({"op": "reference", "setlist": sl_name, "name": nm,
                        "pos": target})
            summary.append(f"add {nm!r} to setlist {sl_name!r} at {target}")
        else:
            ops.append({"op": "move_ref", "setlist": sl_name, "name": nm,
                        "from": src, "pos": target})
            summary.append(f"move {nm!r} in setlist {sl_name!r} {src} -> {target}")
            occupied.discard(src)
        occupied.add(target)

    while moves:
        ready = [m for m in moves if m[2] not in occupied]
        for m in ready:
            emit(*m)
            moves.remove(m)
        if ready:
            continue
        blocked = [m for m in moves if m[2] in fixed]
        if blocked:
            for m in blocked:
                moves.remove(m)
                if m[0] is not None:       # it stays put — and now blocks too
                    fixed[m[0]] = m[1]
                holder = fixed[m[2]] or "(dangling)"
                errors.append(
                    f"setlist {sl_name!r} position {m[2]} holds {holder!r}, "
                    f"which the backup does not have there — not restoring "
                    f"{m[1]!r} over it (re-run with --prune, or move "
                    f"{holder!r} first)")
            continue
        # only movers block each other: a cycle — park one at a free posi
        targets = {m[2] for m in moves}
        free = next((p for p in range(SETLIST_SLOTS)
                     if p not in occupied and p not in targets), None)
        m = next(m for m in moves if m[0] is not None)
        if free is None:
            errors.append(f"setlist {sl_name!r} has no free position to "
                          f"reorder through — restore it by hand")
            return
        emit(m[0], m[1], free)
        m[0] = free


def plan_restore(client, root: Path, *, serial: Optional[str] = None,
                 setlist: Optional[str] = None,
                 prune: bool = False) -> Dict[str, Any]:
    """Compute the ordered ops that would make the device match the snapshot.

    Read-only on both sides — nothing is executed and nothing is written. Ops
    run IRs first (so a restored cab resolves its impulse response rather than
    landing as "No Model"), then pool content, then setlist membership and
    order, then — only with ``prune=True`` — pool deletions.

    ``prune=False`` is additive-and-update: it never deletes. ``prune=True``
    also removes setlist references and pool presets the snapshot does not
    have; it is the one genuinely destructive path here.

    Identity is the **display name** (device names are not unique). Genuine
    ambiguity is an error naming the competing cids — never a guess at which
    preset the user meant.

    Returns ``{serial, ops, summary, errors}``.
    """
    base = _snapshot_dir(root, serial)
    ops: List[Dict[str, Any]] = []
    errors: List[str] = []
    summary: List[str] = []
    if not base.is_dir():
        return {"serial": serial, "ops": [], "summary": [],
                "errors": [f"no snapshot at {base}"]}

    index = _read_json(base / "index.json", None)
    if not isinstance(index, dict):
        # A missing/corrupt index is NOT an empty device. Without this a
        # `--prune` run computes "the snapshot has no presets" and emits a
        # delete for every live preset, reporting zero errors — reachable via
        # `--from <ref>` predating the tree, a partial clone, or a renamed
        # directory.
        return {"serial": base.name, "root": str(base), "prune": bool(prune),
                "ops": [], "summary": [],
                "errors": [f"{base / 'index.json'} is missing or unreadable — "
                           f"refusing to plan a restore from it"]}
    pool_index = index.get("pool") or {}
    sl_index = index.get("setlists") or {}
    if prune and not pool_index:
        return {"serial": base.name, "root": str(base), "prune": True,
                "ops": [], "summary": [],
                "errors": ["refusing to --prune against a snapshot with an "
                           "empty pool index: that would delete every preset "
                           "on the device"]}

    # The snapshot must belong to the device we are about to write to. A
    # second Helix, a warranty replacement, or an ip-<addr> fallback directory
    # would otherwise let `--prune` wipe and repopulate the WRONG device.
    snap_serial = str((_read_json(base / "device.json", {}) or {}).get("serial")
                      or "")
    try:
        live_serial = _serial_of(client)
    except Exception:  # noqa: BLE001 - an unreadable serial is not fatal here
        live_serial = ""
    if snap_serial and live_serial and snap_serial != live_serial:
        return {"serial": snap_serial, "root": str(base), "prune": bool(prune),
                "ops": [], "summary": [],
                "errors": [f"snapshot is from device {snap_serial!r} but the "
                           f"connected device is {live_serial!r} — refusing "
                           f"to restore across devices"]}

    # -- IRs first ----------------------------------------------------------
    ir_index = _read_json(base / "irs" / "index.json", {})
    for h in sorted(ir_index):
        wav = base / "irs" / f"{h}.wav"
        if not wav.is_file():
            errors.append(f"IR {h} is in the snapshot index but its .wav is missing")
            continue
        try:
            on_device = bool(client.ir_path_for_hash(h, strict=True))
        except HelixError as exc:
            errors.append(f"IR {h}: presence check failed: {exc}")
            continue
        if not on_device:
            ops.append({"op": "push_ir", "irhash": h, "file": str(wav)})
            summary.append(f"push IR {h}")

    # -- pool content -------------------------------------------------------
    live_pool = client.list_presets(Container.POOL, strict=True)
    live_by_name = _by_name(live_pool)
    snap_names: set = set()
    for fname in sorted(pool_index):
        name = pool_index[fname]
        snap_names.add(name)
        sbe = base / "pool" / fname
        if not sbe.is_file():
            errors.append(f"pool/{fname} is in index.json but missing from the tree")
            continue
        matches = live_by_name.get(name, [])
        if len(matches) > 1:
            errors.append(
                f"{name!r} is ambiguous on the device — cids "
                f"{', '.join(str(m.get('cid_')) for m in matches)}; rename one "
                f"or restore it by cid")
            continue
        if not matches:
            ops.append({"op": "create", "name": name, "file": str(sbe)})
            summary.append(f"create {name!r} in the pool")
            continue
        cid = matches[0].get("cid_")
        want = _content.to_content_data(sbe.read_bytes())
        try:
            have = _content.to_content_data(client.get_content(cid))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"could not read {name!r} (cid {cid}) to compare: {exc}")
            continue
        if want != have:
            ops.append({"op": "update", "name": name, "cid": cid, "file": str(sbe)})
            summary.append(f"update {name!r} (cid {cid})")

    # -- setlists -----------------------------------------------------------
    live_setlists = client.list_setlists(strict=True)
    fw = _firmware(client)
    slotted = _fw_at_least(fw, (1, 4))
    cid_to_name = {p.get("cid_"): str(p.get("name", "") or "") for p in live_pool}
    for fname in sorted(sl_index):
        sl_name = sl_index[fname]
        if setlist is not None and sl_name != setlist:
            continue
        desired = _desired_refs(_read_json(base / "setlists" / fname, None))
        if desired is None:
            errors.append(f"setlists/{fname} is not a JSON array of setlist "
                          f"references")
            continue
        matches = [s for s in live_setlists
                   if str(s.get("name", "") or "").strip().casefold()
                   == sl_name.strip().casefold()]
        if not matches:
            errors.append(
                f"setlist {sl_name!r} is not on the device — create it first "
                f"(`helixgen device setlist create {sl_name!r}`)")
            continue
        if len(matches) > 1:
            errors.append(
                f"setlist {sl_name!r} is ambiguous — cids "
                f"{', '.join(str(m.get('cid_')) for m in matches)}")
            continue
        sl_cid = matches[0].get("cid_")
        live = [{"cid": m.get("cid_"), "posi": m.get("posi"),
                 "name": cid_to_name.get(m.get("rcid"))}
                for m in client.list_container(sl_cid, strict=True)
                if m.get("cctp") == Cctp.REFERENCE]
        if not slotted and [p for _n, p in desired] != list(range(len(desired))):
            errors.append(
                f"setlist {sl_name!r}: the backup has gaps (positions "
                f"{sorted(p for _n, p in desired)}), which only firmware 1.4+ "
                f"setlists hold — this device runs {fw or 'unknown firmware'}; "
                f"not restoring it")
            continue
        _plan_setlist(sl_name, desired, live, prune, ops, summary, errors)

    # -- prune the pool last, once nothing references it --------------------
    if prune:
        for name in sorted(live_by_name):
            if name in snap_names:
                continue
            for m in live_by_name[name]:
                ops.append({"op": "delete_pool", "name": name, "cid": m.get("cid_")})
                summary.append(f"DELETE pool preset {name!r} (cid {m.get('cid_')})")

    return {"serial": base.name, "ops": ops, "summary": summary, "errors": errors}


def _resolve_pool_cid(client, name: str) -> int:
    matches = [p for p in client.list_presets(Container.POOL, strict=True)
               if str(p.get("name", "") or "") == name]
    if not matches:
        raise HelixError(f"no pool preset named {name!r}")
    if len(matches) > 1:
        raise HelixError(
            f"{name!r} is ambiguous — cids "
            f"{', '.join(str(m.get('cid_')) for m in matches)}")
    return matches[0].get("cid_")


def _setlist_cid(client, setlist: str) -> int:
    sl_cid = client.resolve_setlist_cid(setlist)
    if sl_cid is None:
        raise HelixError(f"no setlist named {setlist!r} on the device")
    return sl_cid


def _push_ir_verbatim(client, irhash: str, path: str) -> None:
    """Upload a snapshot IR **byte-for-byte** and confirm it registered.

    Deliberately NOT ``sftp.push_ir``: that re-runs ``write_stadium_ir`` on its
    input, and a snapshot ``.wav`` is *already* the device-processed 8192-sample
    file — re-processing would apply the exponential tail fade a second time and
    register the IR under a DIFFERENT hash, so the preset referencing it would
    never resolve. Subscribing to the 2001 change stream first is what makes the
    device's watched-dir monitor pick the file up in ~1 s instead of on its next
    ~15-20 min scan.
    """
    import time

    from .sftp import HelixSFTP
    from .subscribe import HelixSubscriber

    ip = getattr(client, "ip", None)
    with HelixSubscriber(ip, ports=(2001,)):
        time.sleep(0.6)  # let the SUB subscription reach the device
        with HelixSFTP(ip) as s:
            s.upload_ir(path, remote_name=f"{irhash}.wav")
        deadline = time.time() + 20.0
        while time.time() < deadline:
            if client.ir_path_for_hash(irhash):
                return
            time.sleep(0.5)
    raise HelixError(
        f"uploaded IR {irhash} but the device did not register it within 20 s")


def _apply_op(client, op: Dict[str, Any]) -> None:
    kind = op.get("op")
    if kind == "push_ir":
        h = op["irhash"]
        if client.ir_path_for_hash(h):
            return
        _push_ir_verbatim(client, h, op["file"])
    elif kind == "create":
        blob = Path(op["file"]).read_bytes()
        if client.install_into_pool(blob, op["name"]) is None:
            raise HelixError(f"device refused to create {op['name']!r}")
    elif kind == "update":
        # Re-resolve by name: a cid recorded at plan time can have moved.
        cid = _resolve_pool_cid(client, op["name"])
        blob = Path(op["file"]).read_bytes()
        if not client._raw.set_content_data(cid, blob):
            raise HelixError(f"device refused the content update for {op['name']!r}")
    elif kind in ("reference", "move_ref"):
        sl_cid = _setlist_cid(client, op["setlist"])
        pool_cid = _resolve_pool_cid(client, op["name"])
        rows = client.list_container(sl_cid, strict=True)
        src = None
        if kind == "move_ref":
            src = next((m for m in rows if m.get("posi") == op["from"]
                        and m.get("rcid") == pool_cid), None)
            if src is None:
                raise HelixError(
                    f"{op['name']!r} is no longer at {op['setlist']!r} "
                    f"position {op['from']}; not moving it")
        # fw 1.4 REPLACES a reference at an occupied posi: re-check right
        # before writing, so an earlier failed op can never make this a
        # silent replace of the incumbent.
        holder = next((m for m in rows if m.get("posi") == op["pos"]), None)
        if holder is not None:
            raise HelixError(
                f"{op['setlist']!r} position {op['pos']} is occupied (cid "
                f"{holder.get('cid_')}); not referencing {op['name']!r} over it")
        # ADD first — duplicates are allowed, so the preset is never absent.
        try:
            client.reference_into_setlist(sl_cid, pool_cid, op["pos"])
        except Exception:  # noqa: BLE001 - a lost reply may still have landed
            pass
        # Its return value is NOT proof: it falls back to matching rcid alone,
        # so an acked-but-unlanded add returns the OLD ref's cid. Only a NEW
        # row at the target, seen by a strict re-list, licenses the removal.
        src_cid = src.get("cid_") if src else None
        if not any(m.get("posi") == op["pos"] and m.get("rcid") == pool_cid
                   and m.get("cid_") != src_cid
                   for m in client.list_container(sl_cid, strict=True)):
            raise HelixError(
                f"device did not reference {op['name']!r} into "
                f"{op['setlist']!r} at {op['pos']}"
                + (f" ({op['name']!r} is still at {op['from']})" if src else ""))
        if src is not None and not client.remove_reference(sl_cid, src_cid):
            raise HelixError(
                f"{op['name']!r} now at {op['pos']} but the old reference at "
                f"{op['from']} could not be removed (it is listed twice)")
    elif kind == "remove_ref":
        sl_cid = _setlist_cid(client, op["setlist"])
        row = next((m for m in client.list_container(sl_cid, strict=True)
                    if m.get("cid_") == op["ref_cid"]), None)
        if row is None or row.get("posi") != op["pos"]:
            raise HelixError(
                f"reference cid {op['ref_cid']} is no longer at "
                f"{op['setlist']!r} position {op['pos']}; not removing")
        if not client.remove_reference(sl_cid, op["ref_cid"]):
            raise HelixError(
                f"device refused to remove {op['name'] or '(dangling)'!r} from "
                f"{op['setlist']!r}")
    elif kind == "delete_pool":
        cid = _resolve_pool_cid(client, op["name"])
        if not client._raw.delete(Container.POOL, [cid]):
            raise HelixError(f"device refused to delete {op['name']!r} (cid {cid})")
    else:
        raise HelixError(f"unknown restore op {kind!r}")


def apply_restore(client, plan: Dict[str, Any], *,
                  progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Execute a :func:`plan_restore` plan against the device.

    Runs the ops in plan order (IRs, then pool content, then setlist membership
    and order, then any prunes) under one :meth:`HelixClient.mutating` batch, so
    the device's change-stream monitor stays subscribed for the whole run and
    every write lands against a fresh container index rather than a lagging one.
    The CLI wrapper holds the ``library`` (+ ``irs``) advisory lock.

    A failing op is recorded and the run **continues**: one preset the device
    refuses must not strand the other 39 half-restored.

    IR uploads past the firmware 1.4 2048-IR cap refuse the WHOLE run
    before anything is written (one error naming how many fit).

    Returns ``{ok, applied, errors}``.
    """
    ops = list(plan.get("ops") or [])
    errors: List[str] = []
    applied = 0
    # firmware 1.4 caps the device IR list at 2048 (hgc-6r5.8): refuse the
    # whole restore BEFORE any write rather than half-restore past the cap.
    n_irs = sum(1 for op in ops if op.get("op") == "push_ir")
    if n_irs:
        from .ir_upload import ir_cap_note, ir_room

        room = ir_room(client)
        if room is not None and n_irs > room:
            return {"ok": False, "applied": 0,
                    "errors": [ir_cap_note(room, n_irs)]}
    with client.mutating():
        for i, op in enumerate(ops, 1):
            _note(progress, f"{i}/{len(ops)} {op.get('op')} "
                            f"{op.get('name') or op.get('irhash') or ''}")
            try:
                _apply_op(client, op)
                applied += 1
            except Exception as exc:  # noqa: BLE001 - one bad op never aborts the run
                errors.append(
                    f"{op.get('op')} {op.get('name') or op.get('irhash') or ''}: {exc}")
    return {"ok": not errors, "applied": applied, "errors": errors}


# -- git plumbing -----------------------------------------------------------

GITATTRIBUTES_LINE = "*.sbe diff=helixgen-sbe"
TEXTCONV_CONFIG = {
    "diff.helixgen-sbe.textconv": "helixgen device decode",
    "diff.helixgen-sbe.binary": "false",
}


def ensure_git_textconv(root: Path) -> Dict[str, Any]:
    """Wire ``git diff`` to read ``.sbe`` files through ``device decode``.

    Idempotent and safe to call on every backup: the ``.gitattributes`` line is
    appended only if absent (unrelated lines are never touched or reordered),
    and each git config key is written only when it does not already hold the
    wanted value. A ``root`` that is not a git repo is reported in ``errors``,
    not raised — a backup must not fail because the tree is not committed yet.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ga = root / ".gitattributes"
    errors: List[str] = []
    existing = ga.read_text().splitlines() if ga.is_file() else []
    added = GITATTRIBUTES_LINE not in [ln.strip() for ln in existing]
    if added:
        text = "\n".join(existing + [GITATTRIBUTES_LINE]) + "\n"
        ga.write_text(text)

    config: Dict[str, str] = {}
    for key, val in TEXTCONV_CONFIG.items():
        try:
            cur = subprocess.run(["git", "config", "--get", key], cwd=root,
                                 capture_output=True, text=True, check=False)
            if cur.returncode == 0 and cur.stdout.strip() == val:
                config[key] = "unchanged"
                continue
            done = subprocess.run(["git", "config", key, val], cwd=root,
                                  capture_output=True, text=True, check=False)
            if done.returncode != 0:
                config[key] = "failed"
                errors.append(
                    f"git config {key}: {done.stderr.strip() or done.returncode}")
            else:
                config[key] = "set"
        except OSError as exc:
            config[key] = "failed"
            errors.append(f"git config {key}: {exc}")
    return {"gitattributes": str(ga), "attributes_written": added,
            "config": config, "errors": errors}
