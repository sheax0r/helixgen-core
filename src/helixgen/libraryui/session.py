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


def _default_copy_tone(client, hsp_path, *, setlist, pos) -> dict:
    """The real `device copy`: upsert the .hsp by meta.name, uploading IRs.

    Lazy device import so the libraryui module stays importable without the
    ``[device]`` extra (offline-first). Raises ``AmbiguousName`` for a device
    name that matches twice — the caller converts it to a ``LibraryError``."""
    from helixgen.device import copy as _copy
    return _copy.copy_tone(client, hsp_path, setlist=setlist, pos=pos,
                           with_irs=True)


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
        ir_local_loader: Callable[[], List[Any]] = lambda: [],
        library_root: Optional[Path] = None,
        copy_fn: Callable[..., dict] = _default_copy_tone,
        pull_fn: Callable[..., dict] = _default_pull_tone,
    ):
        self._client_factory = client_factory
        self._tone_loader = tone_loader
        self._guitar_loader = guitar_loader
        self._ir_local_loader = ir_local_loader
        self._library_root = Path(library_root) if library_root else None
        self._copy_fn = copy_fn
        self._pull_fn = pull_fn
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
        except Exception:  # noqa: BLE001 — a flaky device is just "offline"
            return None
        pool: Dict[str, List[int]] = {}
        for m in pool_rows:
            name = m.get("name")
            if name is not None:
                pool.setdefault(name, []).append(m.get("cid_"))
        return {"pool": pool, "pool_rows": pool_rows, "setlists": setlists}

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
            "irs": {"computer": [], "device": []},  # Task 5
            "facets": {k: sorted(v) for k, v in facets.items()},
        }

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


def _ordered_variants(variants: List[dict]) -> List[dict]:
    """Stable, human order: by guitar then venue (None last)."""
    return sorted(
        variants,
        key=lambda v: ((v["guitar"] or "~"), (v["venue"] or "")),
    )
