"""State engine for the library drawer inside `helixgen device live`.

Gathers the computer-side model (authored tones, guitars, IRs) and, when a
device is reachable, the device-side model (pool presets, setlists, device
IRs), and matches them: tones to device presets by ``meta.name``, IRs by
irhash. The whole computer side works with no device present — any device
error degrades to ``connected: False`` with the computer model intact.

Actions (copy/pull/setlist/IR) are added in later tasks and run by calling the
same functions the ``device`` CLI verbs call, under the matching advisory lock.
No shelling out. Mirror of ``liveedit/session.py`` in spirit; simpler, because
the hub fetches on demand rather than tracking a live PUB stream.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from helixgen import home
from helixgen.tone_meta import ToneMeta, split_variant_key


class LibraryError(Exception):
    """An action failed; ``status`` is the HTTP status the server returns."""

    def __init__(self, msg: str, *, status: int = 400):
        super().__init__(msg)
        self.status = status


def _default_tone_loader() -> List[ToneMeta]:
    from helixgen.tone_meta import load_all_tone_metas
    return load_all_tone_metas()


def _default_guitar_loader() -> List[Any]:
    from helixgen.guitars import load_all_profiles
    return load_all_profiles()


def _default_ir_local_loader() -> List[dict]:
    """Every locally-registered IR as ``{hash, name, wav}`` — the IrMapping is
    the authority on which IRs the computer holds; the wav basename labels it."""
    from helixgen.ir import IrMapping
    try:
        mapping = IrMapping.load()
    except Exception:  # noqa: BLE001 — no mapping yet is just "no local IRs"
        return []
    return [{"hash": h, "name": Path(str(wav)).name, "wav": str(wav)}
            for h, wav in mapping.entries.items()]


def _normalize_device_irs(h) -> List[dict]:
    """The device's IRs as ``{hash, name, mono}``. One RPC (no per-IR file
    lookup — the actions address IRs by hash, so the on-device basename isn't
    needed for the hub)."""
    return [{"hash": m.get("hash"), "name": m.get("name"), "mono": m.get("mono")}
            for m in h.list_irs(strict=True)]


def _default_push_ir(h, wav_path) -> dict:
    from helixgen.device import sftp as _sftp
    return _sftp.push_ir(h.ip, str(wav_path))


def _default_delete_ir(h, name_or_hash) -> dict:
    from helixgen.device import maintenance as mt
    return mt.delete_device_ir(h, name_or_hash, ip=h.ip)


def _default_rename_ir(h, name_or_hash, new_name) -> dict:
    from helixgen.device import maintenance as mt
    target = mt.resolve_device_ir_live(h, name_or_hash)
    ok = h.rename(target["cid_"], new_name)
    return {"ok": bool(ok), "name": target.get("name"), "new_name": new_name}


def _default_copy_tone(client, hsp_path, *, setlist, pos) -> dict:
    """The real `device copy`: upsert the .hsp by meta.name, uploading IRs.

    Lazy device import so the libraryui module stays importable without the
    ``[device]`` extra (offline-first). Raises ``AmbiguousName`` for a device
    name that matches twice — the caller converts it to a ``LibraryError``."""
    from helixgen.device import copy as _copy
    return _copy.copy_tone(client, hsp_path, setlist=setlist, pos=pos,
                           with_irs=True)


def _default_remove_tone(client, name, *, setlist, also_pool, cid) -> dict:
    from helixgen.device import copy as _copy
    return _copy.remove_tone(client, name, setlist=setlist,
                             also_pool=also_pool, cid=cid)


def _default_move_tone(client, name, *, setlist, to, cid) -> dict:
    from helixgen.device import copy as _copy
    return _copy.move_tone(client, name, setlist=setlist, to=to, cid=cid)


def _default_remove_tone_everywhere(client, cid) -> dict:
    from helixgen.device import copy as _copy
    return _copy.remove_tone_everywhere(client, cid)


def _default_ir_usage(client, name_or_hash, computer_irs) -> dict:
    """Which on-device presets reference the IR ``name_or_hash``?

    Resolves ``name_or_hash`` to a hash (an exact hash is used as-is; a name is
    matched case-insensitively against the computer + device IR listings), then
    sweeps the device USER pool: each preset's stored blob is untranscoded to a
    .hsp body and checked with ``extract_ir_hashes``. A preset whose content
    won't untranscode is ``indeterminate`` (the sweep can't prove it clean) —
    the guard treats that as a possible referencer.

    Returns ``{hash, tones: [{cid, name, setlists}], indeterminate: [cid]}``.
    Raises ``ValueError`` when a *name* can't be resolved to a hash (so the
    caller fails closed rather than sweeping for the raw name string, which
    never matches a 32-hex irhash and would silently report "unused").
    """
    import re

    from helixgen.device import bridge
    from helixgen.device.client import Cctp

    # resolve name_or_hash -> target hash
    known: Dict[str, Optional[str]] = {}
    for c in (computer_irs or []):
        h = c.get("hash")
        if h:
            known[h] = c.get("name")
    try:
        for m in _normalize_device_irs(client):
            h = m.get("hash")
            if h:
                known.setdefault(h, m.get("name"))
    except Exception:  # noqa: BLE001 — an IR listing hiccup ≠ no sweep
        pass
    want = (name_or_hash or "").strip()
    target = None
    if want in known:
        target = want
    else:
        folded = want.casefold()
        for h, nm in known.items():
            if nm and nm.strip().casefold() == folded:
                target = h
                break
    if target is None:
        if re.fullmatch(r"[0-9a-fA-F]{8,}", want):
            target = want          # a hash the listing didn't return; trust it
        else:
            # A name we couldn't resolve. Sweeping for the raw name would test
            # ``name in {32-hex hashes}`` → always False → a false "unused".
            # Fail closed: the guard must not delete on a bogus empty result.
            raise ValueError(
                f"could not resolve IR {name_or_hash!r} to a hash — delete by "
                "its 32-hex hash, or check the IR listing")

    # cid -> [setlist names] (reference rows carry rcid = pool cid)
    members: Dict[int, List[str]] = {}
    try:
        for sl in client.list_setlists():
            sl_cid, sl_name = sl.get("cid_"), sl.get("name")
            if sl_cid is None:
                continue
            for m in client.list_container(sl_cid):
                if m.get("cctp") == Cctp.REFERENCE and m.get("rcid") is not None:
                    members.setdefault(m["rcid"], []).append(sl_name)
    except Exception:  # noqa: BLE001 — setlist annotation is best-effort
        pass

    lib = None
    tones: List[dict] = []
    indeterminate: List[int] = []
    for row in client.list_presets():
        cid = row.get("cid_")
        if cid is None:
            continue
        try:
            if lib is None:
                from helixgen.library import Library, default_library_path
                lib = Library(default_library_path())
            body = _semantic_canon_blob(client.get_content(cid), lib)
            # bridge.hsp_ir_hashes (not ir.extract_ir_hashes) is the
            # authoritative "which IRs does this preset reference" — the same
            # enumeration `device copy`/`install --auto-irs` use. It walks
            # EVERY slot of EVERY block; extract_ir_hashes only reads slot[0]
            # of HX2_ImpulseResponse blocks and would miss a dual-cab / Cab+IR
            # reference, making the guard fail OPEN for those IR types.
            hashes = bridge.hsp_ir_hashes(body)
        except Exception:  # noqa: BLE001 — unreadable preset -> indeterminate
            indeterminate.append(cid)
            continue
        if target in hashes:
            tones.append({"cid": cid, "name": row.get("name"),
                          "setlists": members.get(cid, [])})
    return {"hash": target, "tones": tones, "indeterminate": indeterminate}


def _semantic_canon_blob(blob: bytes, lib):
    from helixgen.device import untranscode
    return untranscode.sbe_bytes_to_hsp(blob, name="x", library=lib, author=None)


def _semantic_canon_body(body: dict, lib):
    from helixgen.device import transcode
    return _semantic_canon_blob(transcode.hsp_to_sbepgsm(body), lib)


def _default_diff(client, cid: int, hsp_path) -> str:
    """Does device preset ``cid`` differ from the library ``hsp_path``? Returns
    ``same`` | ``changed`` | ``indeterminate``.

    Compares on the semantic-equivalence projection, not raw bytes: the device
    re-serializes on Save (cg__ ids, key order, scratch keys), so a byte
    compare false-positives on musically-identical tones. ``canon(x)`` pushes
    ``x`` through transcode→untranscode; HW-validated (2026-11-14) to reach a
    fixed point after one device pass with zero float-precision flapping across
    106 on-both tones. Both sides get the same name/author, so editable
    metadata never counts as a tone change. A tone whose structure transcode
    can't round-trip is ``indeterminate`` (the badge shows "?" not a lie)."""
    from helixgen.hsp import read_hsp
    from helixgen.library import Library, default_library_path

    lib = Library(default_library_path())
    blob = client.get_content(cid)
    try:
        dev = _semantic_canon_blob(blob, lib)
        loc = _semantic_canon_body(read_hsp(hsp_path), lib)
    except Exception:  # noqa: BLE001 — a tone transcode can't round-trip
        return "indeterminate"
    return "same" if dev == loc else "changed"


def _default_update_local(client, cid: int, hsp_path) -> dict:
    """Pull device preset ``cid`` back OVER ``hsp_path`` in place (the
    stale-after-device-save reconcile), then auto-commit. Unlike a library
    import, this overwrites the variant's own file and keeps its metadata."""
    from helixgen import cli_device, gitops, home
    from helixgen.device import untranscode
    from helixgen.hsp import write_hsp
    from helixgen.library import Library, default_library_path
    from pathlib import Path as _Path

    blob = client.get_content(cid)
    name = cli_device._preset_name_for_cid(client, cid) or _Path(hsp_path).stem
    body = untranscode.sbe_bytes_to_hsp(
        blob, name=name, library=Library(default_library_path()))
    write_hsp(hsp_path, body)
    gitops.auto_commit(home.helixgen_home(),
                       f"helixgen: update {_Path(hsp_path).name} from device")
    return {"name": name, "hsp": str(hsp_path)}


def _default_pull_tone(client, cid, *, tones_dir) -> dict:  # noqa: ARG001
    """The real `device to-hsp` + `library import`: read the device preset,
    untranscode to a .hsp, and register it in the library (placing the file IS
    the registration). ``tones_dir`` is honoured by test fakes; the real import
    writes to ``home.tones_dir()``."""
    import tempfile

    from helixgen import cli_device, cli_library, naming
    from helixgen.device import untranscode
    from helixgen.hsp import write_hsp
    from helixgen.library import Library, default_library_path

    blob = client.get_content(cid)
    name = cli_device._preset_name_for_cid(client, cid) or f"cid {cid}"
    body = untranscode.sbe_bytes_to_hsp(
        blob, name=name, library=Library(default_library_path()))
    slug = naming.logical_slug(artist=None, song=None, descriptor=name) \
        or f"cid-{cid}"
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / f"{slug}.hsp"
        write_hsp(tmp, body)
        cli_library._import_single(tmp, None, None, None, None,
                                   keep_source=False)
    return {"name": name, "cid": cid}


def _default_import_file(filename, data: bytes, *, guitar=None) -> dict:
    """Import uploaded .hsp bytes into the on-disk tone library — the real
    ``library import`` (``_import_single``): it names the tone from the file's
    own ``meta.name`` and refuses a naming collision. Disk-only, no device.
    ``read_hsp`` validates the structure (a bad file is a 400, not a stray
    import). Like ``_default_pull_tone`` the real write lands in
    ``home.tones_dir()``; test fakes replace this seam."""
    import tempfile

    from helixgen import cli_library
    from helixgen.hsp import read_hsp

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / filename
        tmp.write_bytes(data)
        try:
            doc = read_hsp(tmp)
        except Exception as e:  # noqa: BLE001 — a malformed upload is a 400
            raise LibraryError(f"that file is not a readable .hsp preset: {e}",
                               status=400) from e
        # A JSON array/scalar body is "readable" to read_hsp (it json.loads
        # fine) but is not a preset document; _import_single would then hit an
        # AttributeError deep in _resolve_import and surface as a 502 leaking an
        # internal message. Reject a non-object body here as a clean 400.
        if not isinstance(doc, dict):
            raise LibraryError("that file is not a readable .hsp preset "
                               "(not a preset document)", status=400)
        cli_library._import_single(tmp, None, None, None, guitar,
                                   keep_source=False)
    return {"name": filename}


class LibrarySession:
    """Read model for the library hub.

    ``client_factory`` returns a context-manager device client (a
    ``HelixClient``) or ``None`` for offline. Loaders/paths are injectable so
    tests need no ``~/.helixgen``.
    """

    def __init__(
        self,
        client_factory: Optional[Callable[[], Any]] = None,
        *,
        tone_loader: Callable[[], List[ToneMeta]] = _default_tone_loader,
        guitar_loader: Callable[[], List[Any]] = _default_guitar_loader,
        ir_local_loader: Callable[[], List[Any]] = _default_ir_local_loader,
        library_root: Optional[Path] = None,
        copy_fn: Callable[..., dict] = _default_copy_tone,
        pull_fn: Callable[..., dict] = _default_pull_tone,
        diff_fn: Callable[..., str] = _default_diff,
        update_local_fn: Callable[..., dict] = _default_update_local,
        remove_fn: Callable[..., dict] = _default_remove_tone,
        move_fn: Callable[..., dict] = _default_move_tone,
        push_ir_fn: Callable[..., dict] = _default_push_ir,
        delete_ir_fn: Callable[..., dict] = _default_delete_ir,
        rename_ir_fn: Callable[..., dict] = _default_rename_ir,
        usage_fn: Callable[..., dict] = _default_ir_usage,
        remove_everywhere_fn: Callable[..., dict] = _default_remove_tone_everywhere,
        import_fn: Callable[..., dict] = _default_import_file,
    ):
        self._client_factory = client_factory
        self._tone_loader = tone_loader
        self._guitar_loader = guitar_loader
        self._ir_local_loader = ir_local_loader
        self._library_root = Path(library_root) if library_root else None
        self._copy_fn = copy_fn
        self._pull_fn = pull_fn
        self._diff_fn = diff_fn
        self._update_local_fn = update_local_fn
        self._remove_fn = remove_fn
        self._move_fn = move_fn
        self._push_ir_fn = push_ir_fn
        self._delete_ir_fn = delete_ir_fn
        self._rename_ir_fn = rename_ir_fn
        self._usage_fn = usage_fn
        self._remove_everywhere_fn = remove_everywhere_fn
        self._import_fn = import_fn
        # One global lock serialises every device-mutating action; the Helix
        # client is not safe to drive from two requests at once.
        # ponytail: single mutate lock, split per-scope only if it ever bottlenecks.
        self._mutate_lock = threading.Lock()

    # -- helpers -----------------------------------------------------------

    def _root(self) -> Path:
        return self._library_root if self._library_root else home.library_dir()

    def _hsp_available(self, hsp: str) -> bool:
        if not isinstance(hsp, str) or not hsp.strip():
            return False
        p = Path(hsp)
        if not p.is_absolute():
            p = self._root() / hsp
        return p.exists()

    @staticmethod
    def _require_str(value: Any, what: str) -> str:
        """A required, non-empty string argument, or a clean 400. Guards the
        trust boundary: a non-string JSON value (int/list/bool) must not reach
        ``.strip()`` and surface as a 500."""
        if not isinstance(value, str) or not value.strip():
            raise LibraryError(f"{what} is required", status=400)
        return value.strip()

    @staticmethod
    def _opt_int(value: Any, what: str) -> Optional[int]:
        """An optional integer argument (None passes through), or a 400."""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as e:
            raise LibraryError(f"{what} must be an integer", status=400) from e

    def _resolve_hsp(self, hsp: Any) -> Path:
        if not isinstance(hsp, str) or not hsp.strip():
            raise LibraryError("no tone file given", status=400)
        p = Path(hsp)
        if not p.is_absolute():
            p = self._root() / hsp
        if not p.exists():
            raise LibraryError(f"tone file not found: {hsp}", status=404)
        # Contain to the library root: ``update_local`` WRITES through this
        # resolver, so an absolute path or ``../`` escape must not become an
        # arbitrary-file overwrite. (copy only reads, but the same containment
        # is correct there — you only copy library tones.)
        if not p.resolve().is_relative_to(self._root().resolve()):
            raise LibraryError("tone file is outside the library", status=400)
        return p

    def _tones_dir(self) -> Path:
        return (self._library_root / "tones") if self._library_root \
            else home.tones_dir()

    def _device_model(self) -> Optional[dict]:
        """``{"pool": {name: cid}, "pool_rows": [...], "setlists": [...]}`` or
        ``None`` when no device is reachable. Any device error -> ``None`` so
        the computer side is never taken down with it."""
        if self._client_factory is None:
            return None
        # Serialise device reads against writes (and each other): a GET /state
        # must not drive a device session concurrently with a mid-copy write.
        # The client is per-request, but the hardware is one device.
        try:
            with self._mutate_lock:
                client = self._client_factory()
                with client as h:
                    pool_rows = list(h.list_presets())
                    setlists = list(h.list_setlists())
                    try:
                        device_irs = _normalize_device_irs(h)
                    except Exception:  # noqa: BLE001 — an IR hiccup ≠ no device
                        device_irs = []
        except Exception:  # noqa: BLE001 — a flaky device is just "offline"
            return None
        pool: Dict[str, List[int]] = {}
        for m in pool_rows:
            name = m.get("name")
            if name is not None:
                pool.setdefault(name, []).append(m.get("cid_"))
        return {"pool": pool, "pool_rows": pool_rows, "setlists": setlists,
                "irs": device_irs}

    # -- public state ------------------------------------------------------

    def public_state(self) -> dict:
        metas = self._tone_loader()
        device = self._device_model()
        pool = device["pool"] if device else {}

        tones: List[dict] = []
        matched_names: set = set()
        facets = {"guitars": set(), "venues": set(),
                  "treatments": set(), "tags": set()}

        for meta in metas:
            variants = []
            any_on_device = False
            for key, v in meta.variants.items():
                guitar, venue = split_variant_key(key)
                venue = v.venue or venue
                detail = v.venue_detail or {}
                treatment = detail.get("treatment")
                cids = pool.get(v.preset_name) or []
                on_device = bool(cids)
                if on_device:
                    any_on_device = True
                    matched_names.add(v.preset_name)
                variants.append({
                    "key": key,
                    "guitar": guitar,
                    "venue": venue,
                    "treatment": treatment,
                    "preset_name": v.preset_name,
                    "hsp": v.hsp,
                    "available": self._hsp_available(v.hsp),
                    "presence": "both" if on_device else "computer",
                    "device_cid": cids[0] if cids else None,
                })
                if guitar:
                    facets["guitars"].add(guitar)
                if venue:
                    facets["venues"].add(venue)
                if treatment:
                    facets["treatments"].add(treatment)
            for t in meta.tags:
                facets["tags"].add(t)
            tones.append({
                "slug": meta.logical_slug,
                "name": meta.display_base,
                "artist": meta.artist,
                "song": meta.song,
                "descriptor": meta.descriptor,
                "tags": list(meta.tags),
                "presence": "both" if any_on_device else "computer",
                "variants": _ordered_variants(variants),
            })

        # device presets that match no authored variant -> device-only rows
        if device:
            for m in device["pool_rows"]:
                name = m.get("name")
                if name is None or name in matched_names:
                    continue
                tones.append({
                    "slug": None,
                    "name": name,
                    "artist": None, "song": None, "descriptor": None,
                    "tags": [],
                    "presence": "device",
                    "variants": [{
                        "key": None, "guitar": None, "venue": None,
                        "treatment": None, "preset_name": name, "hsp": None,
                        "available": False, "presence": "device",
                        "device_cid": m.get("cid_"),
                    }],
                })

        # IR slot headroom (N / cap) — only when connected; offline we don't
        # know the device count, so omit it rather than guess (spec §A.4).
        ir_headroom = None
        if device is not None:
            from helixgen.device.ir_upload import IR_LIST_CAP
            ir_headroom = {"used": len(device["irs"]), "cap": IR_LIST_CAP}

        return {
            "connected": device is not None,
            "tones": tones,
            "setlists": device["setlists"] if device else [],
            "irs": self._ir_view(device),
            "ir_headroom": ir_headroom,
            "facets": {k: sorted(v) for k, v in facets.items()},
        }

    def _ir_view(self, device: Optional[dict]) -> List[dict]:
        """One flat IR list, computer ∪ device, matched by hash. Each row:
        ``{hash, name, wav, presence, folder}`` (presence computer|device|both;
        folder is the library-first folder label, or ``None`` when unfiled)."""
        from helixgen import ir_folders
        folders = ir_folders.load(self._root())
        computer = self._ir_local_loader() or []
        device_irs = device["irs"] if device else []
        dev_by_hash = {m.get("hash"): m for m in device_irs if m.get("hash")}
        seen: set = set()
        rows: List[dict] = []
        for c in computer:
            h = c.get("hash")
            seen.add(h)
            on_dev = h in dev_by_hash
            rows.append({"hash": h, "name": c.get("name"), "wav": c.get("wav"),
                         "presence": "both" if on_dev else "computer",
                         "folder": folders.get(h)})
        for m in device_irs:
            h = m.get("hash")
            if h in seen:
                continue
            rows.append({"hash": h, "name": m.get("name"), "wav": None,
                         "presence": "device", "folder": folders.get(h)})
        rows.sort(key=lambda r: (r.get("name") or "").lower())
        return rows

    # -- reconcile actions -------------------------------------------------

    def copy_to_device(self, hsp: Any, to: Optional[str] = None,
                       pos: Optional[int] = None) -> dict:
        """Push an authored variant's ``.hsp`` onto the device (``device copy``
        — upsert by meta.name). ``to`` names a setlist (None = pool only).
        Returns the copy result; raises ``LibraryError`` for a missing file, an
        ambiguous device name, or a device failure."""
        if self._client_factory is None:
            raise LibraryError("no Stadium connected", status=409)
        if to is not None:
            to = self._require_str(to, "a setlist")
        pos = self._opt_int(pos, "position")
        abs_hsp = self._resolve_hsp(hsp)
        with self._mutate_lock:
            try:
                with self._client_factory() as h:
                    return self._copy_fn(h, abs_hsp, setlist=to, pos=pos)
            except LibraryError:
                raise
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ == "AmbiguousName":
                    raise LibraryError(str(e), status=409) from e
                raise LibraryError(f"copy failed: {e}", status=502) from e

    def pull_from_device(self, cid: Any) -> dict:
        """Pull a device preset into the library (``device to-hsp`` +
        ``library import``). Returns ``{name, cid}``; raises ``LibraryError``
        for a bad cid, a name collision, or a device failure."""
        if self._client_factory is None:
            raise LibraryError("no Stadium connected", status=409)
        try:
            cid = int(cid)
        except (TypeError, ValueError) as e:
            raise LibraryError("cid must be an integer", status=400) from e
        with self._mutate_lock:
            try:
                with self._client_factory() as h:
                    return self._pull_fn(h, cid, tones_dir=self._tones_dir())
            except LibraryError:
                raise
            except Exception as e:  # noqa: BLE001
                # a name collision (ClickException from library import, or a
                # ToneCollision) is the user's to resolve, not a server fault.
                if type(e).__name__ in ("ClickException", "ToneCollision",
                                        "UsageError"):
                    raise LibraryError(str(e) or "a tone with that name "
                                       "already exists", status=409) from e
                raise LibraryError(f"pull failed: {e}", status=502) from e

    def import_file(self, filename: Any, content_b64: Any,
                    guitar: Any = None) -> dict:
        """Import an uploaded .hsp into the on-disk tone library (``library
        import`` — names it from the file's ``meta.name``, refuses a naming
        collision). Disk-only: no device, no connection needed. The bytes
        arrive base64 in the JSON body (the server parses JSON, not multipart),
        so a very large preset can hit the server's body cap before here."""
        import base64
        import binascii

        name = self._require_str(filename, "a file name")
        if "\x00" in name:              # a NUL would blow up write_bytes -> 502
            raise LibraryError("the file name is not valid", status=400)
        base = Path(name).name          # strip any path the client sent
        if not base.lower().endswith(".hsp"):
            raise LibraryError("only .hsp preset files can be imported "
                               "(.hlx is not supported yet)", status=400)
        if not isinstance(content_b64, str) or not content_b64:
            raise LibraryError("the file content is required", status=400)
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise LibraryError("the file content is not valid base64",
                               status=400) from e
        # Cheap boundary check before touching disk: every .hsp opens with the
        # 8-byte magic. read_hsp (in the import fn) does the full structural
        # validation; this just rejects obvious non-presets with a clear 400.
        if data[:8] != b"rpshnosj":
            raise LibraryError("that is not a Helix Stadium .hsp preset",
                               status=400)
        if guitar is not None:
            guitar = self._require_str(guitar, "a guitar")
        # Serialise the library write against other mutations (disk + the
        # auto-commit inside _import_single). No client — a local-only action.
        with self._mutate_lock:
            try:
                return self._import_fn(base, data, guitar=guitar)
            except LibraryError:
                raise
            except Exception as e:  # noqa: BLE001
                # a naming collision (ClickException from library import) is the
                # user's to resolve, not a server fault.
                if type(e).__name__ in ("ClickException", "ToneCollision",
                                        "UsageError"):
                    raise LibraryError(str(e) or "a tone with that name "
                                       "already exists", status=409) from e
                raise LibraryError(f"import failed: {e}", status=502) from e

    # -- content diff (phase 2: the "changed" badge) -----------------------

    def content_diff(self, cid: Any, hsp: Any) -> dict:
        """Does the device preset ``cid`` differ from the library ``hsp``?

        Returns ``{cid, state}`` with state ``same`` | ``changed`` |
        ``indeterminate``. Read-only, but takes the mutate lock to serialise
        the device read against writes. The projection lives in ``_diff_fn``
        (``_default_diff``)."""
        cid = self._opt_int(cid, "cid")
        if cid is None:
            raise LibraryError("cid is required", status=400)
        abs_hsp = self._resolve_hsp(hsp)
        state = self._run(lambda h: self._diff_fn(h, cid, abs_hsp))
        return {"cid": cid, "state": state}

    def update_local(self, cid: Any, hsp: Any) -> dict:
        """Pull a device preset back OVER the existing library ``.hsp`` in
        place — the stale-after-device-save reconcile the "changed" badge
        points to. Unlike ``pull_from_device`` (``library import``, which
        refuses a name collision), ``_update_local_fn`` overwrites the
        variant's own file, keeps its metadata sidecar, and auto-commits."""
        cid = self._opt_int(cid, "cid")
        if cid is None:
            raise LibraryError("cid is required", status=400)
        abs_hsp = self._resolve_hsp(hsp)
        return self._run(lambda h: self._update_local_fn(h, cid, abs_hsp))

    # -- setlist management ------------------------------------------------

    def _run(self, fn: Callable[[Any], dict]) -> dict:
        """Open a client under the mutate lock, run ``fn(h)``, and normalise
        failures: a name the device can't disambiguate or a collision → 409,
        anything else from the device → 502. ``LibraryError`` passes through."""
        if self._client_factory is None:
            raise LibraryError("no Stadium connected", status=409)
        with self._mutate_lock:
            try:
                with self._client_factory() as h:
                    return fn(h)
            except LibraryError:
                raise
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                if name in ("AmbiguousName", "ClickException", "ToneCollision",
                            "UsageError"):
                    raise LibraryError(str(e), status=409) from e
                raise LibraryError(f"{e}", status=502) from e

    def setlist_create(self, name: str) -> dict:
        name = self._require_str(name, "a setlist name")

        def go(h):
            if h.resolve_setlist_cid(name) is not None:
                raise LibraryError(f"setlist {name!r} already exists",
                                   status=409)
            cid = h.create_setlist(name)
            if cid is None:
                raise LibraryError(f"device refused to create {name!r}",
                                   status=502)
            return {"name": name, "cid": cid}
        return self._run(go)

    def setlist_rename(self, name: str, new_name: str) -> dict:
        name = self._require_str(name, "a setlist")
        new_name = self._require_str(new_name, "a new name")

        def go(h):
            cid = h.resolve_setlist_cid(name)
            if cid is None:
                raise LibraryError(f"setlist {name!r} not found", status=404)
            if h.resolve_setlist_cid(new_name) is not None:
                raise LibraryError(f"a setlist named {new_name!r} already "
                                   "exists", status=409)
            if not h.rename(cid, new_name):
                raise LibraryError(f"failed to rename {name!r}", status=502)
            return {"name": new_name, "cid": cid}
        return self._run(go)

    def setlist_delete(self, name: str) -> dict:
        name = self._require_str(name, "a setlist")

        def go(h):
            cid = h.resolve_setlist_cid(name)
            if cid is None:
                raise LibraryError(f"setlist {name!r} not found", status=404)
            if not h.delete_setlist(cid):
                raise LibraryError(f"failed to delete {name!r}", status=502)
            return {"name": name, "cid": cid}
        return self._run(go)

    def setlist_duplicate(self, src: str, dst: str) -> dict:
        src = self._require_str(src, "a source setlist")
        dst = self._require_str(dst, "a destination name")

        def go(h):
            src_cid = h.resolve_setlist_cid(src)
            if src_cid is None:
                raise LibraryError(f"setlist {src!r} not found", status=404)
            dst_cid = h.resolve_setlist_cid(dst)
            if dst_cid is None:
                dst_cid = h.create_setlist(dst)
                if dst_cid is None:
                    raise LibraryError(f"device refused to create {dst!r}",
                                       status=502)
            copied = h.duplicate_setlist_refs(src_cid, dst_cid)
            return {"src": src, "dst": dst, "copied": copied}
        return self._run(go)

    def setlist_add(self, to: str, *, hsp: Any = None, cid: Any = None,
                    pos: Any = None) -> dict:
        """Add membership to a setlist. An authored tone (``hsp``) is copied in
        (IRs and all); a pool-only preset (``cid``) is referenced at ``pos``."""
        to = self._require_str(to, "a target setlist")
        if hsp:
            return self.copy_to_device(hsp, to=to, pos=pos)
        if cid is not None:
            cid = self._opt_int(cid, "cid")
            pos = self._opt_int(pos, "position")
            if pos is None:
                raise LibraryError("a setlist reference needs a position",
                                   status=400)

            def go(h):
                dest = h.resolve_setlist_cid(to)
                if dest is None:
                    raise LibraryError(f"setlist {to!r} not found", status=404)
                if h.find_by_pos(dest, pos, strict=True) is not None:
                    raise LibraryError(f"setlist {to!r} position {pos} is not "
                                       "empty", status=409)
                ref = h.reference_into_setlist(dest, cid, pos)
                if ref is None:
                    raise LibraryError("device refused the reference",
                                       status=502)
                return {"setlist": to, "pos": pos, "ref_cid": ref,
                        "pool_cid": cid}
            return self._run(go)
        raise LibraryError("nothing to add — give a tone file or a cid",
                           status=400)

    def setlist_remove(self, name: str, setlist: str, *, also_pool: bool = False,
                       cid: Any = None) -> dict:
        name = self._require_str(name, "a preset name")
        setlist = self._require_str(setlist, "a setlist")
        cid = self._opt_int(cid, "cid")
        return self._run(lambda h: self._remove_fn(
            h, name, setlist=setlist, also_pool=also_pool, cid=cid))

    def setlist_move(self, name: str, setlist: str, to: Any,
                     cid: Any = None) -> dict:
        name = self._require_str(name, "a preset name")
        setlist = self._require_str(setlist, "a setlist")
        cid = self._opt_int(cid, "cid")
        to = self._opt_int(to, "position")
        if to is None:
            raise LibraryError("a destination position is required", status=400)
        return self._run(lambda h: self._move_fn(
            h, name, setlist=setlist, to=to, cid=cid))

    # -- IR actions --------------------------------------------------------

    def _resolve_wav(self, wav: Any) -> Path:
        if not isinstance(wav, str) or not wav.strip():
            raise LibraryError("no IR file given", status=400)
        p = Path(wav)
        if not p.is_absolute():
            p = self._root() / wav
        if not p.exists():
            raise LibraryError(f"IR file not found: {wav}", status=404)
        return p

    def push_ir(self, wav: Any) -> dict:
        """Upload a local .wav onto the device (``device push-ir``). A full
        device (fw 1.4's 2048-IR cap) comes back as a LibraryError, not a
        crash."""
        p = self._resolve_wav(wav)

        def go(h):
            res = self._push_ir_fn(h, p)
            if not res.get("ok", True):
                raise LibraryError(f"upload of {p.name} failed", status=502)
            return res
        return self._run(go)

    def ir_usage(self, name_or_hash: Any) -> dict:
        """On-device presets that reference an IR. See ``_default_ir_usage``.
        Returns ``{hash, tones: [{cid, name, setlists}], indeterminate: [cid]}``."""
        name_or_hash = self._require_str(name_or_hash, "an IR name or hash")
        return self._run(lambda h: self._usage_fn(
            h, name_or_hash, self._ir_local_loader() or []))

    def remove_tone_device(self, cid: Any) -> dict:
        """Remove a tone from the device entirely (every setlist + the pool);
        the local library is untouched. See ``copy.remove_tone_everywhere``.

        A device refusal (``ok: False``) raises rather than returning 200 — so
        the page's error path fires instead of a misleading "done" toast, and a
        caller clearing an IR's referencing tones learns the IR is still in
        use. ``errors`` from the removal is surfaced in the message."""
        cid = self._opt_int(cid, "cid")
        if cid is None:
            raise LibraryError("cid is required", status=400)
        res = self._run(lambda h: self._remove_everywhere_fn(h, cid))
        if not res.get("ok", True):
            raise LibraryError(
                "device refused to fully remove the tone: "
                + "; ".join(res.get("errors") or ["unknown error"]),
                status=502)
        return res

    def delete_ir(self, name_or_hash: Any, force: bool = False) -> dict:
        name_or_hash = self._require_str(name_or_hash, "an IR name or hash")

        # Fail closed: refuse while on-device tones reference the IR (or while
        # a preset can't be read, so we can't prove it clean). `force` is the
        # only override. The sweep runs in its own device session BEFORE the
        # delete — the mutate lock is not reentrant, so this must not nest in
        # ``_run``.
        if not force:
            u = self.ir_usage(name_or_hash)
            tones, indet = u.get("tones") or [], u.get("indeterminate") or []
            if tones or indet:
                parts = []
                if tones:
                    names = ", ".join(str(t.get("name") or t.get("cid"))
                                      for t in tones)
                    parts.append(f"{len(tones)} on-device tone(s) reference it "
                                 f"({names}) — remove them first")
                if indet:
                    parts.append(f"{len(indet)} preset(s) couldn't be read to "
                                 "check — use force to delete anyway")
                raise LibraryError(
                    f"IR {name_or_hash!r} is still in use: " + "; ".join(parts),
                    status=409)

        def go(h):
            res = self._delete_ir_fn(h, name_or_hash)
            if not res.get("ok"):
                raise LibraryError(f"failed to delete IR {name_or_hash!r}",
                                   status=502)
            return res
        return self._run(go)

    def rename_ir(self, name_or_hash: Any, new_name: Any) -> dict:
        name_or_hash = self._require_str(name_or_hash, "an IR name or hash")
        new_name = self._require_str(new_name, "a new name")

        def go(h):
            res = self._rename_ir_fn(h, name_or_hash, new_name)
            if not res.get("ok"):
                raise LibraryError(f"failed to rename IR {name_or_hash!r}",
                                   status=502)
            return res
        return self._run(go)

    def set_ir_folder(self, hash_: Any, folder: Any = None) -> dict:
        """Assign an IR (by 32-hex hash) to a library folder, or unfile it with
        an empty/``None`` folder. Library-first, disk-only (the folder sidecar);
        the device never learns about folders, so this takes no device and no
        lease — it works offline. ``folder`` must be a string or ``None``."""
        from helixgen import ir_folders

        h = self._require_str(hash_, "an IR hash")
        if folder is not None and not isinstance(folder, str):
            raise LibraryError("folder must be a string", status=400)
        with self._mutate_lock:          # serialise the sidecar write
            try:
                ir_folders.set_folder(h, folder, library_dir=self._root())
            except ValueError as e:
                raise LibraryError(str(e), status=400) from e
        # ponytail: no per-assignment auto-commit — the sidecar is atomic on
        # disk and the next library-mutating op's commit captures it; a commit
        # per drag would be git noise.
        return {"hash": h, "folder": ir_folders.load(self._root()).get(h)}


def _ordered_variants(variants: List[dict]) -> List[dict]:
    """Stable, human order: by guitar then venue (None last)."""
    return sorted(
        variants,
        key=lambda v: ((v["guitar"] or "~"), (v["venue"] or "")),
    )
