"""Tone-library manifest — the tone as a first-class managed entity.

One file, ``~/.helixgen/setlists/manifest.json`` (override ``$HELIXGEN_SETLISTS``;
new default location as of manifest v3, migrated up from the legacy
``~/.helixgen/setlists.json``). It is PURE local-file logic: no device, no
network, no msgpack/zmq — importable without the ``device`` extra.

A **tone** is content + identity + management **intent** (design
``2026-07-15-library-metadata-design`` §3):

* **content** — its ``.hsp`` (``path`` + ``content_hash``); both are ``null``
  for a pathless (device-origin) tone.
* **identity** — the ``tones`` key (name), unique in the manifest, also the
  device preset key.
* **management intent** — ``source`` (provenance) and ``slot`` (desired
  on-device address; ``null`` = off device, ``"auto"`` = wants device, address
  TBD, or a concrete ``"1A".."128D"``).

Setlists are ordered membership plus a ``synced`` flag (mirrored to the device
or a local-only draft). ``"On the device"`` ⟺ ``slot != null``.

On-disk shape (version 3 — **intent only**)::

    {"version": 3,
     "tones": {"<name>": {"path": <abs .hsp | null>,
                          "content_hash": "sha256:…" | null,
                          "source": "authored"|"import-local"|"import-device"|"save"|"create",
                          "slot": "1A".."128D" | "auto" | null,
                          "auto_marked"?: true}},
     "setlists": {"<setlist>": {"tones": ["<tone name>", …], "synced": bool}}}

The **observed** cid/posi a specific device reports is NOT here — it lives in
``devices/<serial>.json`` (:mod:`helixgen.device.observations`), rebuilt
wholesale by every sync. Nor is the retired ``doc`` sidecar path (folded into
the tone-metadata JSON).

The manifest is **never hand-edited** — it is written by the authoring/sync
surface (the CLI). A version-1 document (list-valued setlists + a folded
``entries`` slot-ledger section) and a version-2 document (``doc``/``device``/
``observed`` fields) are migrated forward on load.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from helixgen import gitops, home, libinit
from helixgen.hsp import read_hsp
from helixgen.device.client import slot_label as _slot_label

MANIFEST_VERSION = 3

# provenance tags accepted on a tone record
_VALID_SOURCES = ("authored", "import-local", "import-device", "save", "create",
                  "import-hss",
                  "hsp", "push")  # last two are legacy synonyms kept for migration
# Sources with no local .hsp behind them: device edit-buffer save/create, and
# presets imported from a .hss setlist bundle (their content lives only in the
# bundle + on the device — `device slots restore` can't re-author them).
_PATHLESS_SOURCES = ("save", "create", "import-hss")

# Every user-setlist slot label, in device posi order: "1A".."128D".
# The posi->label formula lives in ONE place — ``client.slot_label`` (#51) — and
# this table is derived from it (not a second copy of the formula). A Helix
# setlist holds up to 128 banks of 4 (the Stadium XL's full user bank goes to
# 128D = 512 slots); base models simply fill fewer. Sizing to the max keeps slot
# validation + auto-assign from imposing an artificial "device full" ceiling —
# the hardware is the real capacity check. ``_posi_to_slot`` below keeps this
# table's capped / ``None``-for-out-of-range contract, distinct from
# ``client.slot_label``'s uncapped / ``""``-for-None contract.
_SLOT_BANKS = 128
_SLOT_LABELS = tuple(_slot_label(i) for i in range(_SLOT_BANKS * 4))


class ManifestError(Exception):
    """A manifest operation was rejected (e.g. a unique-name collision)."""


def default_setlists_path() -> Path:
    """Where the tone-library manifest lives: ``~/.helixgen/setlists/manifest.json``.

    Overridable wholesale with ``$HELIXGEN_SETLISTS`` (delegates to
    :func:`home.manifest_path`). This is the v3 (current) default location; a
    v2 manifest at :func:`home.legacy_manifest_path` is migrated up to here on
    load.
    """
    return home.manifest_path()


def _legacy_ledger_path() -> Path:
    """The retired standalone slot-ledger file (read once for migration only)."""
    override = os.environ.get("HELIXGEN_DEVICE_SLOTS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".helixgen" / "device-slots.json"


def _hash_file(path: Path) -> str:
    """Return the ``sha256:<hex>`` content hash of a file's raw bytes."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _posi_to_slot(posi: Any) -> Optional[str]:
    """Map a device 0-based posi to its user-setlist slot label ("1A".."128D")."""
    if not isinstance(posi, int) or not (0 <= posi < len(_SLOT_LABELS)):
        return None
    return _SLOT_LABELS[posi]


def _tone_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce any partial/legacy tone dict to the full v3 (intent-only) record
    shape — dropping the retired ``doc`` sidecar path and the observed
    ``device`` field (both migrated out to metadata / observations)."""
    out = {
        "path": rec.get("path"),
        "content_hash": rec.get("content_hash"),
        "source": rec.get("source") or "authored",
        "slot": rec.get("slot"),
    }
    if rec.get("auto_marked"):
        # provenance: this "auto" slot was stamped implicitly (synced-setlist
        # membership), not by an explicit `device add` — it dies with the
        # tone's last membership. Present only when true.
        out["auto_marked"] = True
    return out


def _setlist_record(v: Any) -> Dict[str, Any]:
    """Coerce a setlist value (v1 list OR v2/v3 {tones,synced}) to v3 shape."""
    if isinstance(v, dict):
        return {"tones": list(v.get("tones") or []), "synced": bool(v.get("synced"))}
    return {"tones": list(v or []), "synced": False}


def _copy_backup(src: Path, suffix: str) -> None:
    """Best-effort copy of ``src`` to ``src`` + ``suffix`` (e.g. ``.bak-v2``),
    written BEFORE any rewrite so a crash mid-migration never loses the old
    document."""
    try:
        if src.exists():
            shutil.copy2(src, src.with_name(src.name + suffix))
    except OSError:
        pass


def _emit_legacy_observations(v2data: Dict[str, Any]) -> None:
    """Preserve a pre-v3 document's observed cid/posi (per-tone ``device`` +
    the top-level ``observed`` section) into ``devices/legacy.json`` (serial
    ``"legacy"``). Written atomically by the observations module. A no-op when
    there's nothing observed to preserve."""
    from helixgen.device import observations as _obs

    observed = v2data.get("observed") if isinstance(v2data.get("observed"), dict) else {}
    pool = observed.get("pool") if isinstance(observed.get("pool"), dict) else {}
    obs_sl = observed.get("setlists") if isinstance(observed.get("setlists"), dict) else {}

    tones_dev: Dict[str, Any] = {}
    # observed.pool contributes placement first; a per-tone `device` (the
    # canonical mirror) wins on conflict.
    for name, entry in pool.items():
        if isinstance(entry, dict) and entry.get("cid") is not None:
            tones_dev[name] = {"cid": entry.get("cid"), "posi": entry.get("posi")}
    for name, rec in (v2data.get("tones") or {}).items():
        dev = rec.get("device") if isinstance(rec, dict) else None
        if isinstance(dev, dict):
            tones_dev[name] = {"cid": dev.get("cid"), "posi": dev.get("posi")}

    if not (tones_dev or pool or obs_sl):
        return
    obs = _obs.DeviceObservations(
        serial="legacy",
        tones=tones_dev,
        pool={k: dict(v) for k, v in pool.items() if isinstance(v, dict)},
        setlists={k: dict(v) for k, v in obs_sl.items() if isinstance(v, dict)},
    )
    _obs.save_observations(obs)


class SetlistManifest:
    """The tone library: tone registry (with placement intent) + ordered setlists."""

    def __init__(
        self,
        path: Path,
        *,
        tones: Optional[Dict[str, Any]] = None,
        setlists: Optional[Dict[str, Any]] = None,
    ):
        self.path = Path(path)
        self.tones: Dict[str, Any] = tones if tones is not None else {}
        self.setlists_map: Dict[str, Dict[str, Any]] = setlists if setlists is not None else {}

    @property
    def version(self) -> int:
        return MANIFEST_VERSION

    # -- construction ---------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SetlistManifest":
        """Load the manifest, migrating a v1/v2 document (or a legacy ledger) to
        v3.

        Path precedence (when ``path`` is not given and ``$HELIXGEN_SETLISTS``
        is unset): the new :func:`home.manifest_path` location wins if it
        exists; else a v2 manifest at :func:`home.legacy_manifest_path` is
        migrated up to the new location; else a fresh empty v3 at the new
        location. An explicit ``path`` (or a ``$HELIXGEN_SETLISTS`` override)
        is loaded — and migrated in place — directly.
        """
        if path is not None:
            p = Path(path)
            return cls._resolve_and_load(load_path=p, save_path=p, from_legacy=False)

        override = os.environ.get("HELIXGEN_SETLISTS")
        if override:
            p = Path(override).expanduser()
            return cls._resolve_and_load(load_path=p, save_path=p, from_legacy=False)

        new_path = home.manifest_path()
        if new_path.exists():
            return cls._resolve_and_load(load_path=new_path, save_path=new_path,
                                         from_legacy=False)
        legacy = home.legacy_manifest_path()
        if legacy.exists():
            return cls._resolve_and_load(load_path=legacy, save_path=new_path,
                                         from_legacy=True)
        return cls._resolve_and_load(load_path=new_path, save_path=new_path,
                                     from_legacy=False)

    @classmethod
    def _resolve_and_load(cls, *, load_path: Path, save_path: Path,
                          from_legacy: bool) -> "SetlistManifest":
        try:
            data = json.loads(load_path.read_text())
        except (OSError, ValueError):
            data = None

        if isinstance(data, dict) and data.get("version") == MANIFEST_VERSION:
            return cls._load_v3(save_path, data)
        if isinstance(data, dict) and data.get("version") == 2:
            return cls._migrate_to_v3(load_path, data, save_path=save_path,
                                      from_legacy=from_legacy, backup_suffix=".bak-v2")
        if isinstance(data, dict) and data.get("version") == 1:
            v2 = cls._v1_to_v2_dict(data)
            return cls._migrate_to_v3(load_path, v2, save_path=save_path,
                                      from_legacy=from_legacy, backup_suffix=".bak-v1")

        # No usable manifest — try a one-time migration from the standalone
        # ledger; otherwise a fresh empty v3 (written only on the next save()).
        ledger_v2 = cls._ledger_to_v2_dict()
        if ledger_v2 is not None:
            return cls._commit_v2_to_v3(ledger_v2, save_path=save_path)
        return cls(save_path)

    @classmethod
    def _load_v3(cls, save_path: Path, data: Dict[str, Any]) -> "SetlistManifest":
        setlists = {k: _setlist_record(v)
                    for k, v in (data.get("setlists") or {}).items()}
        tones = {k: _tone_record(v) for k, v in (data.get("tones") or {}).items()}
        return cls(save_path, tones=tones, setlists=setlists)

    @classmethod
    def _commit_v2_to_v3(cls, v2data: Dict[str, Any], *,
                         save_path: Path) -> "SetlistManifest":
        """Emit the observed data to ``devices/legacy.json``, build the v3
        intent-only manifest (honoring the v2 observed->synced flip), and
        persist it atomically at ``save_path``."""
        _emit_legacy_observations(v2data)
        tones = {k: _tone_record(v) for k, v in (v2data.get("tones") or {}).items()}
        setlists = {k: _setlist_record(v)
                    for k, v in (v2data.get("setlists") or {}).items()}
        # A setlist demonstrably synced (observed on device) loads as synced —
        # preserve the v2 load-time flip so `sync --all` keeps maintaining it.
        observed = v2data.get("observed") if isinstance(v2data.get("observed"), dict) else {}
        obs_sl = observed.get("setlists") if isinstance(observed.get("setlists"), dict) else {}
        for name in obs_sl:
            if name in setlists:
                setlists[name]["synced"] = True
        m = cls(save_path, tones=tones, setlists=setlists)
        m.save()
        return m

    @classmethod
    def _migrate_to_v3(cls, load_path: Path, v2data: Dict[str, Any], *,
                       save_path: Path, from_legacy: bool,
                       backup_suffix: str) -> "SetlistManifest":
        """Back up the old file, split intent (v3 manifest) from observations
        (devices/legacy.json), and — when migrating from the legacy location —
        rename the old file so a re-run doesn't re-migrate.

        Crash-safety: the ``.bak-v{n}`` copy is written FIRST, before any
        rewrite; the v3 manifest and observations are each written atomically
        (temp + ``os.replace``). A crash before the v3 file is written leaves
        the original in place (re-migrated on next run); a crash after leaves
        the new v3 file authoritative.
        """
        _copy_backup(load_path, backup_suffix)
        m = cls._commit_v2_to_v3(v2data, save_path=save_path)
        if from_legacy and load_path != save_path:
            try:
                load_path.replace(load_path.with_name(load_path.name + ".migrated-v2"))
            except OSError:
                pass
        return m

    @staticmethod
    def _v1_to_v2_dict(data: Dict[str, Any]) -> Dict[str, Any]:
        """Upgrade a version-1 document (list setlists + folded ledger) to the
        v2 dict shape (fed to the v2->v3 splitter).

        Each ledger ``entries`` row contributes a tone's ``slot`` (its
        ``slot_label``); ``observed.pool`` contributes ``device``; list-valued
        setlists become ``{tones, synced}`` (``user`` and any observed-on-device
        setlist are ``synced``).
        """
        observed = data.get("observed") if isinstance(data.get("observed"), dict) else {}
        pool = observed.get("pool") if isinstance(observed.get("pool"), dict) else {}
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        slot_by_name: Dict[str, str] = {}
        for e in entries:
            if isinstance(e, dict) and e.get("name"):
                lbl = e.get("slot_label") or _posi_to_slot(e.get("posi"))
                if lbl:
                    slot_by_name[e["name"]] = lbl

        tones: Dict[str, Any] = {}
        for name, rec in (data.get("tones") or {}).items():
            r = dict(rec) if isinstance(rec, dict) else {}
            r["slot"] = slot_by_name.get(name) or r.get("slot")
            dev = pool.get(name)
            r["device"] = dev if isinstance(dev, dict) else None
            if r["slot"] is None and isinstance(dev, dict):
                r["slot"] = _posi_to_slot(dev.get("posi"))
            tones[name] = r

        setlists: Dict[str, Any] = {}
        for name, v in (data.get("setlists") or {}).items():
            rec = _setlist_record(v)
            rec["synced"] = name == "user" or name in (
                observed.get("setlists") or {})
            setlists[name] = rec

        return {"version": 2, "tones": tones, "setlists": setlists,
                "observed": {"pool": dict(pool),
                             "setlists": dict(observed.get("setlists") or {})}}

    @classmethod
    def _ledger_to_v2_dict(cls) -> Optional[Dict[str, Any]]:
        """Fold a legacy ``device-slots.json`` into a v2 dict, or ``None`` if
        there's nothing to migrate."""
        try:
            data = json.loads(_legacy_ledger_path().read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            return None
        entries = [e for e in data["entries"] if isinstance(e, dict)]
        entries.sort(key=lambda e: (e.get("setlist") or "", e.get("posi") or 0))
        tones: Dict[str, Any] = {}
        setlists: Dict[str, Any] = {}
        observed: Dict[str, Any] = {"pool": {}, "setlists": {}}
        for e in entries:
            name = e.get("name")
            setlist = e.get("setlist")
            if not name or not setlist:
                continue
            source = e.get("source_kind") or "authored"
            source_path = e.get("source_path")
            posi = e.get("posi")
            cid = e.get("cid")
            content_hash: Optional[str] = None
            if source_path and Path(source_path).exists():
                content_hash = _hash_file(Path(source_path))
            tones[name] = {
                "path": source_path,
                "content_hash": content_hash,
                "source": source,
                "slot": e.get("slot_label") or _posi_to_slot(posi),
                "device": {"cid": cid, "posi": posi},
            }
            rec = setlists.setdefault(setlist, {"tones": [], "synced": True})
            if name not in rec["tones"]:
                rec["tones"].append(name)
            observed["pool"][name] = {"cid": cid, "posi": posi}
            sl = observed["setlists"].setdefault(setlist, {"cid": None, "refs": {}})
            sl["refs"][name] = {"ref_cid": None, "posi": posi}
        if not tones and not setlists:
            return None
        return {"version": 2, "tones": tones, "setlists": setlists,
                "observed": observed}

    # -- library: register tones ---------------------------------------------

    def register_tone(self, hsp_path: Path | str, *, source: str = "authored") -> str:
        """Register a local ``.hsp`` into the library (no setlist, off-device).

        Reads ``meta.name`` (falling back to the filename stem) as the tone name.
        Preserves an existing tone's ``slot`` if it was already known.
        Raises :class:`ManifestError` on a name collision with a different path.
        """
        p = Path(hsp_path).resolve()
        body = read_hsp(p)
        name = (body.get("meta") or {}).get("name") or p.stem
        abs_path = str(p)
        existing = self.tones.get(name)
        if existing is not None and existing.get("path") not in (None, abs_path):
            raise ManifestError(
                f"tone name {name!r} is already registered to a different path "
                f"({existing.get('path')!r}); names must be unique in the manifest"
            )
        self.tones[name] = _tone_record({
            "path": abs_path,
            "content_hash": _hash_file(p),
            "source": source,
            "slot": (existing or {}).get("slot"),
        })
        return name

    def register_pathless(self, name: str, *, source: str) -> str:
        """Register a source-less tone (device edit-buffer ``save`` / ``create``,
        or a preset imported from a ``.hss`` bundle — ``import-hss``)."""
        if source not in _PATHLESS_SOURCES:
            raise ManifestError(
                f"pathless source must be one of {_PATHLESS_SOURCES}, got {source!r}")
        existing = self.tones.get(name)
        if existing is not None and existing.get("path") is not None:
            raise ManifestError(
                f"tone name {name!r} is already registered with a path; "
                f"names must be unique in the manifest")
        self.tones[name] = _tone_record({
            "path": None, "content_hash": None, "source": source,
            "slot": (existing or {}).get("slot"),
        })
        return name

    def add_tone(self, setlist: str, hsp_path: Path | str, *,
                 pos: Optional[int] = None) -> str:
        """Register a ``.hsp`` and add it to ``setlist``'s membership (legacy API)."""
        name = self.register_tone(hsp_path, source="import-local")
        self.add_to_setlist(setlist, name, pos=pos)
        return name

    def pathless_add(self, name: str, source: str, *,
                     setlist: Optional[str] = None, pos: Optional[int] = None) -> str:
        """Register a pathless tone and optionally add it to a setlist (legacy API)."""
        self.register_pathless(name, source=source)
        if setlist is not None:
            self.add_to_setlist(setlist, name, pos=pos)
        return name

    # -- desired placement (slots + setlists) --------------------------------

    def mark_on_device(self, name: str, slot: str = "auto") -> None:
        """Mark a library tone for the device (``slot='auto'`` = address TBD).
        This is the EXPLICIT gesture (`device add`) — the mark survives setlist
        membership changes, unlike the implicit synced-setlist stamp."""
        if name not in self.tones:
            raise ManifestError(f"unknown tone {name!r}")
        if slot != "auto" and slot not in _SLOT_LABELS:
            raise ManifestError(f"invalid slot {slot!r} (expected '1A'..'128D' or 'auto')")
        self.tones[name]["slot"] = slot
        self.tones[name].pop("auto_marked", None)

    def _stamp_auto(self, name: str) -> None:
        """Implicitly mark a synced-setlist member for the device. The stamp is
        provenance-tagged so it dies with the tone's last membership."""
        tone = self.tones.get(name)
        if tone is not None and tone.get("slot") is None:
            tone["slot"] = "auto"
            tone["auto_marked"] = True

    def unsync(self, name: str) -> List[str]:
        """Take ``name`` off the device (``slot=None``) and cascade it out of every
        **synced** setlist. Keeps the tone in the library. Returns the synced
        setlists it was pulled from."""
        if name not in self.tones:
            raise ManifestError(f"unknown tone {name!r}")
        self.tones[name]["slot"] = None
        self.tones[name].pop("auto_marked", None)
        pulled: List[str] = []
        for sl, rec in self.setlists_map.items():
            if rec.get("synced") and name in rec["tones"]:
                rec["tones"].remove(name)
                pulled.append(sl)
        return pulled

    def set_setlist_synced(self, setlist: str, synced: bool) -> None:
        """Flip a setlist's ``synced`` flag. Turning it on marks every member for
        the device (``slot='auto'`` where a member has no slot yet). The flag is
        authoritative intent (persisted in the manifest), so turning it off just
        clears the flag — no observed evidence to unwind (that lives per-device
        and self-heals on the next sync)."""
        rec = self.setlists_map.setdefault(setlist, {"tones": [], "synced": False})
        rec["synced"] = bool(synced)
        if synced:
            for name in rec["tones"]:
                self._stamp_auto(name)

    def add_to_setlist(self, setlist: str, name: str, *, pos: Optional[int] = None) -> None:
        """Add ``name`` to ``setlist`` at ``pos`` (append if None; no duplicates).
        If the setlist is synced, the tone is marked on-device."""
        if name not in self.tones:
            raise ManifestError(f"unknown tone {name!r}")
        rec = self.setlists_map.setdefault(setlist, {"tones": [], "synced": False})
        if name not in rec["tones"]:
            if pos is None:
                rec["tones"].append(name)
            else:
                rec["tones"].insert(pos, name)
        if rec["synced"]:
            self._stamp_auto(name)

    def remove_from_setlist(self, setlist: str, name: str) -> bool:
        """Drop ``name`` from ``setlist``'s membership. Returns whether it was there."""
        rec = self.setlists_map.get(setlist)
        if not rec or name not in rec["tones"]:
            return False
        rec["tones"].remove(name)
        return True

    def remove_tone(self, setlist: str, name: str) -> bool:
        """Drop ``name`` from ``setlist``; GC the registry entry if now unreferenced
        AND not device-marked — a non-null ``slot`` means the tone is (or wants to
        be) on the device, so its registration must survive losing its last
        setlist (legacy API — membership removal, not a device delete).

        An IMPLICIT ``"auto"`` mark (``auto_marked`` — stamped by
        ``add_to_setlist``/``set_setlist_synced`` on synced-setlist members)
        dies with the last membership, so add-then-remove stays a no-op. An
        explicit `device add` mark (``"auto"`` without the provenance tag) and
        concrete labels protect the registration."""
        if not self.remove_from_setlist(setlist, name):
            return False
        if not self._is_referenced(name):
            tone = self.tones.get(name)
            if (tone is not None and tone.get("slot") == "auto"
                    and tone.get("auto_marked")):
                tone["slot"] = None
                tone.pop("auto_marked", None)
            if tone is not None and tone.get("slot") is None:
                self.tones.pop(name, None)
        return True

    def _is_referenced(self, name: str) -> bool:
        return any(name in rec["tones"] for rec in self.setlists_map.values())

    def create_setlist(self, name: str) -> None:
        """Create an empty setlist (idempotent — never wipes existing membership)."""
        self.setlists_map.setdefault(name, {"tones": [], "synced": False})

    def rename_setlist(self, old: str, new: str) -> bool:
        """Rename a setlist record, preserving insertion order and membership.

        Returns ``False`` when ``old`` isn't in the manifest (nothing to do —
        a device-only setlist has no local record). Raises
        :class:`ManifestError` if ``new`` already exists.
        """
        if old not in self.setlists_map:
            return False
        if new in self.setlists_map:
            raise ManifestError(f"setlist {new!r} already exists in the manifest")
        self.setlists_map = {
            (new if k == old else k): v for k, v in self.setlists_map.items()}
        return True

    # -- desired: read accessors ---------------------------------------------

    def setlists(self) -> List[str]:
        """The manifest's setlist names, in insertion order."""
        return list(self.setlists_map.keys())

    def tones_in(self, setlist: str) -> List[str]:
        """Ordered tone names in ``setlist`` (empty list if unknown)."""
        rec = self.setlists_map.get(setlist)
        return list(rec["tones"]) if rec else []

    def is_synced(self, setlist: str) -> bool:
        """Whether ``setlist`` is mirrored to the device."""
        rec = self.setlists_map.get(setlist)
        return bool(rec and rec.get("synced"))

    def tone_path(self, name: str) -> Optional[str]:
        entry = self.tones.get(name)
        return entry.get("path") if entry else None

    def content_hash(self, name: str) -> Optional[str]:
        entry = self.tones.get(name)
        return entry.get("content_hash") if entry else None

    def device_marked_tones(self) -> List[str]:
        """Names of tones with a non-null ``slot`` (on, or wanting, the device),
        in insertion order — the managed user population."""
        return [n for n, rec in self.tones.items() if rec.get("slot") is not None]

    def union_tones(self, setlists: List[str]) -> List[str]:
        """Ordered, de-duplicated union of tone names across ``setlists``."""
        seen: Dict[str, None] = {}
        for setlist in setlists:
            for name in self.tones_in(setlist):
                if name not in seen:
                    seen[name] = None
        return list(seen.keys())

    def library(self) -> List[Dict[str, Any]]:
        """A display view: every tone with slot, on/off-device, source, setlists."""
        out: List[Dict[str, Any]] = []
        for name, rec in self.tones.items():
            out.append({
                "name": name,
                "slot": rec.get("slot"),
                "on_device": rec.get("slot") is not None,
                "source": rec.get("source"),
                "setlists": [sl for sl, r in self.setlists_map.items()
                             if name in r["tones"]],
            })
        return out

    # -- persistence ----------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """The full on-disk document (intent only — no observed state)."""
        return {
            "version": MANIFEST_VERSION,
            "tones": self.tones,
            "setlists": self.setlists_map,
        }

    def save(self) -> None:
        """Atomically write the manifest as pretty JSON (temp file + ``os.replace``),
        then make sure the helixgen home is git-initialized and advisory-commit
        the change.

        The home is git-init'd unconditionally (whenever git is present);
        the commit itself is gated by the ``git_commit_tones`` preference
        (see :func:`helixgen.gitops.auto_commit`) AND skipped entirely when
        this manifest doesn't live under the home at all (e.g. an explicit
        ``$HELIXGEN_SETLISTS`` override pointing somewhere else) — committing
        a directory outside the repo makes no sense.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2))
        os.replace(tmp, self.path)

        home_dir = home.helixgen_home()
        libinit.ensure_initialized(home_dir)
        try:
            under_home = self.path.resolve().is_relative_to(home_dir.resolve())
        except OSError:
            under_home = False
        if under_home:
            gitops.auto_commit(home_dir, "helixgen: update manifest")
