"""State engine for `helixgen library ui` — the library hub.

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
        remove_fn: Callable[..., dict] = _default_remove_tone,
        move_fn: Callable[..., dict] = _default_move_tone,
        push_ir_fn: Callable[..., dict] = _default_push_ir,
        delete_ir_fn: Callable[..., dict] = _default_delete_ir,
        rename_ir_fn: Callable[..., dict] = _default_rename_ir,
    ):
        self._client_factory = client_factory
        self._tone_loader = tone_loader
        self._guitar_loader = guitar_loader
        self._ir_local_loader = ir_local_loader
        self._library_root = Path(library_root) if library_root else None
        self._copy_fn = copy_fn
        self._pull_fn = pull_fn
        self._remove_fn = remove_fn
        self._move_fn = move_fn
        self._push_ir_fn = push_ir_fn
        self._delete_ir_fn = delete_ir_fn
        self._rename_ir_fn = rename_ir_fn
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

    def _resolve_hsp(self, hsp: Any) -> Path:
        if not isinstance(hsp, str) or not hsp.strip():
            raise LibraryError("no tone file given", status=400)
        p = Path(hsp)
        if not p.is_absolute():
            p = self._root() / hsp
        if not p.exists():
            raise LibraryError(f"tone file not found: {hsp}", status=404)
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
        try:
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

        return {
            "connected": device is not None,
            "tones": tones,
            "setlists": device["setlists"] if device else [],
            "irs": self._ir_view(device),
            "facets": {k: sorted(v) for k, v in facets.items()},
        }

    def _ir_view(self, device: Optional[dict]) -> List[dict]:
        """One flat IR list, computer ∪ device, matched by hash. Each row:
        ``{hash, name, wav, presence}`` (presence computer|device|both)."""
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
                         "presence": "both" if on_dev else "computer"})
        for m in device_irs:
            h = m.get("hash")
            if h in seen:
                continue
            rows.append({"hash": h, "name": m.get("name"), "wav": None,
                         "presence": "device"})
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
        def go(h):
            if not (name or "").strip():
                raise LibraryError("a setlist name is required", status=400)
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
        def go(h):
            if not (new_name or "").strip():
                raise LibraryError("a new name is required", status=400)
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
        def go(h):
            cid = h.resolve_setlist_cid(name)
            if cid is None:
                raise LibraryError(f"setlist {name!r} not found", status=404)
            if not h.delete_setlist(cid):
                raise LibraryError(f"failed to delete {name!r}", status=502)
            return {"name": name, "cid": cid}
        return self._run(go)

    def setlist_duplicate(self, src: str, dst: str) -> dict:
        def go(h):
            src_cid = h.resolve_setlist_cid(src)
            if src_cid is None:
                raise LibraryError(f"setlist {src!r} not found", status=404)
            if not (dst or "").strip():
                raise LibraryError("a destination name is required",
                                   status=400)
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
        if not (to or "").strip():
            raise LibraryError("a target setlist is required", status=400)
        if hsp:
            return self.copy_to_device(hsp, to=to, pos=pos)
        if cid is not None:
            try:
                cid = int(cid)
            except (TypeError, ValueError) as e:
                raise LibraryError("cid must be an integer", status=400) from e
            if pos is None:
                raise LibraryError("a setlist reference needs a position",
                                   status=400)
            pos = int(pos)

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
        if not (setlist or "").strip():
            raise LibraryError("a setlist is required", status=400)
        return self._run(lambda h: self._remove_fn(
            h, name, setlist=setlist, also_pool=also_pool, cid=cid))

    def setlist_move(self, name: str, setlist: str, to: Any,
                     cid: Any = None) -> dict:
        try:
            to = int(to)
        except (TypeError, ValueError) as e:
            raise LibraryError("position must be an integer", status=400) from e
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

    def delete_ir(self, name_or_hash: Any) -> dict:
        if not (name_or_hash or "").strip():
            raise LibraryError("an IR name or hash is required", status=400)

        def go(h):
            res = self._delete_ir_fn(h, name_or_hash)
            if not res.get("ok"):
                raise LibraryError(f"failed to delete IR {name_or_hash!r}",
                                   status=502)
            return res
        return self._run(go)

    def rename_ir(self, name_or_hash: Any, new_name: Any) -> dict:
        if not (name_or_hash or "").strip():
            raise LibraryError("an IR name or hash is required", status=400)
        if not (new_name or "").strip():
            raise LibraryError("a new name is required", status=400)

        def go(h):
            res = self._rename_ir_fn(h, name_or_hash, new_name)
            if not res.get("ok"):
                raise LibraryError(f"failed to rename IR {name_or_hash!r}",
                                   status=502)
            return res
        return self._run(go)


def _ordered_variants(variants: List[dict]) -> List[dict]:
    """Stable, human order: by guitar then venue (None last)."""
    return sorted(
        variants,
        key=lambda v: ((v["guitar"] or "~"), (v["venue"] or "")),
    )
