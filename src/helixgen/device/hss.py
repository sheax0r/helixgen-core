"""Read-side ``.hss`` setlist-bundle support (backlog #31). **EXPERIMENTAL.**

.. warning::
   **FILLED-SLOT FRAMING BELOW IS NOW KNOWN-WRONG (corrected 2026-07-15).** A
   real non-empty export was captured — see
   ``docs/superpowers/specs/2026-07-15-hss-and-cc-capture-findings.md``. A
   *filled* slot's manifest entry is ``{"path": ".N", "type":
   "application/stadium-preset"}`` and its ``.N`` payload is the **`.hsp`
   preset format** (magic ``rpshnosj`` + JSON), **NOT** the ``_sbepgsm`` /
   ``\\xff\\xff\\xff\\xffpgsm`` content blob the assumption below (and the
   ``import-hss`` install path) presume — feeding ``slot.blob`` through
   :func:`helixgen.device.content.decode_any` **raises** on a real export. The
   container parse (header/gzip/tar/manifest/128-slots/empty-sentinel) is
   correct; the filled-slot *decode/install* path and a byte-faithful writer
   are the remaining work (parse ``.N`` as ``rpshnosj``+JSON → recipe/ingest;
   writer emits the same + ``type: "application/stadium-preset"``).

A ``.hss`` is the Stadium app's "export setlist" file. Format decoded via a
hardware capture 2026-07-14 — see
``docs/superpowers/specs/2026-07-14-parity-capture-findings.md`` §8:

    <24-byte header><gzip stream>

* **Header** (bytes 0x00-0x17, 24 bytes total): ASCII tag ``GGGY`` + ``u32
  0``, ASCII tag ``LTES`` (byte-reversed spells ``SETL``) + ``u32 0``, then a
  little-endian ``u64`` version field observed as ``256``. The gzip stream
  (magic ``1f 8b``) begins immediately after, at offset 0x18.
* The decompressed body is a **POSIX ustar** tar archive containing
  ``manifest.json`` plus **128** slot members named ``.1`` .. ``.128``
  (1-based, matching the device's ``posi``). An **empty** slot is a 1-byte
  ``0x00`` sentinel member.
* ``manifest.json`` = ``{"meta": {"name", "device_id", "device_version"},
  "contents": [{"path": ".N", "type": "<null>"}, ...]}`` — 128 entries, in
  slot order. ``type: "<null>"`` marks an empty slot.

This structure is pinned against a **real captured export** (an empty
setlist — see ``tests/test_hss.py``), so header/gzip/tar/manifest/128-slots/
empty-sentinel parsing is solid.

⚠️ **FILLED-SLOT FRAMING IS UNCONFIRMED.** The only real sample captured so
far is an *empty* setlist, so nothing is known first-hand about what a filled
``.N`` member or its manifest ``contents[]`` entry look like. Based on the
findings-doc note that "filled slots embed the preset's ``_sbepgsm`` content
as the ``.N`` payload", this module assumes:

1. A filled slot's tar member is simply the preset's stored content blob
   (either the ``_sbepgsm`` edit-buffer magic or the ``\\xff\\xff\\xff\\xffpgsm``
   ``/SetContentData`` magic — both already understood by
   :mod:`helixgen.device.content`).
2. Its manifest ``contents[]`` entry carries the preset's display name under a
   ``"name"`` key (content blobs themselves have no name field — the device
   reports preset names as a container attribute, never inside content — so
   *something* in the manifest must carry it for a filled slot to be
   nameable). If that key is absent, callers get ``None`` rather than a
   guessed value.

Both assumptions are pinned only against **synthesized** fixtures built from
this same assumption (see ``tests/test_hss.py``) — they prove the reader is
internally consistent, not that they match Line 6's real filled-slot bytes.
**A real non-empty ``.hss`` export is needed to confirm or correct them**
before a byte-faithful *writer* is attempted (writer is out of scope here;
see backlog #31).

Pure stdlib (``gzip`` + ``tarfile`` + ``json``) for the container format; the
embedded content blob decode goes through :mod:`helixgen.device.content`
(msgpack — already required for any ``device`` feature).
"""
from __future__ import annotations

import gzip
import io
import json
import struct
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

HEADER_LEN = 24
_TAG_A = b"GGGY"
_TAG_B = b"LTES"
# The only real capture observed this as 256. Deliberately NOT enforced as a
# hard requirement (unlike the GGGY/LTES tags) — a different firmware/bundle
# generation could plausibly use a different version, and read_hss's job is
# to parse what it can, not to gatekeep on a single sample's exact value.
# HssBundle.version carries whatever was read so callers can inspect it.
_EXPECTED_VERSION = 256
NUM_SLOTS = 128
_EMPTY_SENTINEL = b"\x00"
MANIFEST_NAME = "manifest.json"


class HssFormatError(ValueError):
    """The file is not a well-formed ``.hss`` setlist bundle."""


@dataclass
class HssSlot:
    """One of the bundle's 128 fixed slot positions."""

    pos: int                      # 1-based, matches the ".N" tar member / device posi
    filled: bool
    name: Optional[str] = None    # ASSUMED source: manifest contents[] entry; see module doc
    blob: Optional[bytes] = None  # raw tar-member bytes for a filled slot; None if empty
    raw_entry: Dict[str, Any] = field(default_factory=dict)  # the manifest contents[] entry


@dataclass
class HssBundle:
    """A parsed ``.hss`` setlist bundle."""

    name: Optional[str]
    device_id: Optional[int]
    device_version: Optional[int]
    version: int
    slots: List[HssSlot]

    @property
    def filled_slots(self) -> List[HssSlot]:
        return [s for s in self.slots if s.filled]


def _read_bytes(source: Union[str, Path, bytes]) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    return Path(source).read_bytes()


def _parse_header(data: bytes) -> int:
    """Validate the 24-byte header and return the version field."""
    if len(data) < HEADER_LEN:
        raise HssFormatError(
            f"file too short ({len(data)} bytes) to hold a .hss header "
            f"({HEADER_LEN} bytes)")
    if data[0:4] != _TAG_A:
        raise HssFormatError(
            f"bad .hss header: expected {_TAG_A!r} at offset 0, got {data[0:4]!r}")
    if data[8:12] != _TAG_B:
        raise HssFormatError(
            f"bad .hss header: expected {_TAG_B!r} at offset 8, got {data[8:12]!r}")
    (version,) = struct.unpack_from("<Q", data, 16)
    return version


def read_hss(source: Union[str, Path, bytes]) -> HssBundle:
    """Parse a ``.hss`` setlist bundle (file path or raw bytes).

    Raises :class:`HssFormatError` on anything that doesn't match the decoded
    container shape: bad header, non-gzip payload, non-tar payload, missing
    ``manifest.json``, ``manifest.json`` that isn't valid JSON or whose
    ``meta``/``contents`` aren't shaped as documented (top-level not an
    object, ``meta``/``contents`` of the wrong type), or a tar member that
    isn't a regular file. This fails CLOSED on purpose — since the filled-slot
    shape is unconfirmed against real hardware (see the module docstring), an
    unexpected real export should raise a clear, catchable error here rather
    than crash with a raw traceback partway through. Individual filled-slot
    payloads are returned verbatim (not decoded) — callers that need the
    preset content should feed ``slot.blob`` through
    :func:`helixgen.device.content.decode_any` /
    :meth:`~helixgen.device.client.HelixClient.install_into_pool`.
    """
    data = _read_bytes(source)
    version = _parse_header(data)

    gz_stream = data[HEADER_LEN:]
    try:
        raw = gzip.decompress(gz_stream)
    except OSError as e:
        raise HssFormatError(
            f"header looked right but the rest isn't a valid gzip stream: {e}"
        ) from e

    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r")
    except tarfile.TarError as e:
        raise HssFormatError(f"gzip decompressed but isn't a valid tar archive: {e}") from e

    try:
        with tf:
            return _read_tar(tf, version)
    except tarfile.TarError as e:
        raise HssFormatError(f"corrupt tar archive: {e}") from e


def _extract_bytes(tf: tarfile.TarFile, member: tarfile.TarInfo, what: str) -> bytes:
    """``tf.extractfile(member).read()``, failing closed with
    :class:`HssFormatError` (not a raw ``AttributeError``) if ``member`` isn't
    a regular file (a directory/symlink entry, which ``extractfile`` returns
    ``None`` for)."""
    fobj = tf.extractfile(member)
    if fobj is None:
        raise HssFormatError(f"{what} ({member.name!r}) is not a regular file in the tar archive")
    return fobj.read()


def _read_tar(tf: tarfile.TarFile, version: int) -> HssBundle:
    try:
        members_by_name = {m.name: m for m in tf.getmembers()}
    except tarfile.TarError as e:
        raise HssFormatError(f"corrupt tar archive: {e}") from e

    if MANIFEST_NAME not in members_by_name:
        raise HssFormatError(f"{MANIFEST_NAME} missing from the bundle's tar archive")

    manifest_bytes = _extract_bytes(tf, members_by_name[MANIFEST_NAME], "manifest.json")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as e:
        raise HssFormatError(f"manifest.json is not valid JSON: {e}") from e
    if not isinstance(manifest, dict):
        raise HssFormatError(
            f"manifest.json's top level is a {type(manifest).__name__}, expected an object")

    meta = manifest.get("meta") or {}
    if not isinstance(meta, dict):
        raise HssFormatError(
            f"manifest.json's \"meta\" is a {type(meta).__name__}, expected an object")

    contents = manifest.get("contents") or []
    if not isinstance(contents, list):
        raise HssFormatError(
            f"manifest.json's \"contents\" is a {type(contents).__name__}, expected an array")
    contents_by_path = {c.get("path"): c for c in contents if isinstance(c, dict)}

    slots: List[HssSlot] = []
    for pos in range(1, NUM_SLOTS + 1):
        path = f".{pos}"
        entry = contents_by_path.get(path, {})
        if not isinstance(entry, dict):
            entry = {}
        member = members_by_name.get(path)
        if member is None:
            # No tar member at all for this slot: treat as empty (defensive —
            # the real/synthesized samples we've seen always emit all 128).
            slots.append(HssSlot(pos=pos, filled=False, raw_entry=entry))
            continue
        member_bytes = _extract_bytes(tf, member, f"slot {path}")
        # Empty = the documented 1-byte 0x00 sentinel, or (defensively) a
        # 0-byte member — "no content at all" cannot be a filled preset, and
        # classifying it as filled would surface a bogus per-slot error.
        filled = len(member_bytes) > 0 and member_bytes != _EMPTY_SENTINEL
        if not filled:
            slots.append(HssSlot(pos=pos, filled=False, raw_entry=entry))
            continue
        raw_name = entry.get("name")
        slots.append(HssSlot(
            pos=pos, filled=True,
            name=raw_name if isinstance(raw_name, str) and raw_name.strip() else None,
            blob=member_bytes,
            raw_entry=entry,
        ))

    def _str_or_none(v: Any) -> Optional[str]:
        return v if isinstance(v, str) else None

    return HssBundle(
        name=_str_or_none(meta.get("name")),
        device_id=meta.get("device_id") if isinstance(meta.get("device_id"), int) else None,
        device_version=(meta.get("device_version")
                        if isinstance(meta.get("device_version"), int) else None),
        version=version,
        slots=slots,
    )


def slot_label(slot: HssSlot) -> str:
    """Human-readable label for a slot, for ``--list`` output — never crashes
    on the unconfirmed name field."""
    if not slot.filled:
        return "(empty)"
    return slot.name or f"(unnamed — slot {slot.pos})"


def looks_like_content_blob(blob: bytes) -> bool:
    """True if ``blob`` starts with a magic :mod:`helixgen.device.content`
    recognizes (``_sbepgsm`` or the ``/SetContentData`` framing).

    Used to fail a filled slot closed with a clear per-slot error *before*
    handing it to the device — since the filled-slot byte framing is an
    unconfirmed assumption (see the module docstring), a real export could
    plausibly carry something this reader's assumption doesn't anticipate,
    and the device-write primitives raise a bare ``ValueError`` (not
    ``HelixError``) for content they can't decode.
    """
    from . import content
    return content.is_content_blob(blob) or content.is_content_data(blob)


def import_bundle(client: Any, bundle: HssBundle, *,
                  setlist: Optional[str] = None) -> Dict[str, Any]:
    """Install every filled slot of ``bundle`` into ``client``'s pool and
    reference them into a device setlist, in bundle order.

    ``setlist`` names the destination (created if absent); ``None`` falls
    back to the bundle's own manifest name — raises :class:`ValueError` if
    neither is available. Shared by the CLI (`device setlist import-hss`) and
    the MCP tool (`device_import_hss`) so both stay behaviorally identical.

    Safe against a destination that already has references (a reused, populated
    setlist): new references are **appended** after whatever's already there —
    positions are chosen from the setlist's own current occupancy, never a raw
    ``enumerate()`` index — so nothing existing is disturbed or overwritten,
    matching the never-orphan/never-clobber precedent the rest of the device
    client establishes (e.g. ``duplicate_setlist_refs`` refusing a non-empty
    destination). A slot whose blob doesn't look like content
    (:func:`looks_like_content_blob`) is skipped with a clear per-slot error
    instead of being sent to the device. Per-slot ``HelixError``/``ValueError``
    failures are likewise caught and reported in ``errors`` without aborting
    the rest of the import.

    Returns ``{ok, setlist, cid, created, installed, errors}`` (``ok`` is
    ``not errors``). Connection-level failures resolving/creating the setlist
    itself propagate as ``HelixError`` — only PER-SLOT failures are absorbed.
    """
    from .client import Cctp, HelixError

    filled = bundle.filled_slots
    target_setlist = setlist or bundle.name
    if not target_setlist:
        raise ValueError(
            "the bundle has no setlist name in its manifest; pass an explicit setlist name")

    if not filled:
        return {"ok": True, "setlist": target_setlist, "cid": None, "created": False,
                "installed": [], "errors": []}

    setlist_cid = client.resolve_setlist_cid(target_setlist)
    created = False
    if setlist_cid is None:
        setlist_cid = client.create_setlist(target_setlist)
        created = True
        if setlist_cid is None:
            raise ValueError(f"device refused to create setlist {target_setlist!r}")

    # Append after whatever's already in the setlist (position 0 for a
    # freshly created one) — never collide with existing references.
    # STRICT listing: a flaky-network timeout must abort (HelixError) rather
    # than silently read as "empty setlist" and land new references at
    # positions real ones already occupy — same fail-closed rule as
    # duplicate_setlist_refs / ir-prune.
    existing_positions = {
        m.get("posi") for m in client.list_container(setlist_cid, strict=True)
        if m.get("cctp") == Cctp.REFERENCE
    }
    next_pos = 0

    installed: List[str] = []
    errors: List[str] = []
    for s in filled:
        name = slot_label(s)
        if not looks_like_content_blob(s.blob):
            errors.append(
                f"slot {s.pos} ({name!r}): payload doesn't look like a recognized "
                f"preset content blob (unconfirmed filled-slot framing — see "
                f"backlog #31); skipped, nothing written to the device")
            continue
        try:
            pool_cid = client.install_into_pool(s.blob, name)
        except (HelixError, ValueError) as e:
            errors.append(f"slot {s.pos} ({name!r}): {e}")
            continue
        if pool_cid is None:
            errors.append(f"slot {s.pos} ({name!r}): install returned no cid")
            continue
        while next_pos in existing_positions:
            next_pos += 1
        pos = next_pos
        try:
            ref_cid = client.reference_into_setlist(setlist_cid, pool_cid, pos)
        except (HelixError, ValueError) as e:
            errors.append(f"slot {s.pos} ({name!r}): {e}")
            continue
        if ref_cid is None:
            errors.append(
                f"slot {s.pos} ({name!r}): installed but could not reference "
                f"into setlist {target_setlist!r}")
            continue
        # Only claim `pos` once the reference actually landed — a failed
        # attempt leaves it free for the next successful slot to reuse
        # rather than leaving a permanent gap.
        existing_positions.add(pos)
        next_pos = pos + 1
        installed.append(name)

    return {"ok": not errors, "setlist": target_setlist, "cid": setlist_cid,
            "created": created, "installed": installed, "errors": errors}


def record_import_in_manifest(manifest: Any, result: Dict[str, Any]) -> List[str]:
    """Record a successful :func:`import_bundle` ``result`` in the tone-library
    manifest (a :class:`~helixgen.device.manifest.SetlistManifest` instance —
    caller loads and saves it). Returns a list of warning strings (empty when
    everything recorded cleanly).

    **This is load-bearing, not bookkeeping.** ``device sync <setlist>`` (the
    explicit, targeted form — not gated by the ``synced`` flag) mirrors the
    device setlist's references to the manifest's membership. If the imported
    presets aren't in the manifest's membership, the next targeted sync of
    that setlist computes ``desired=[]`` and **strips every reference the
    import just wrote**. So each successfully installed+referenced preset is
    registered as a PATHLESS tone (``source="import-hss"`` — its content came
    from the bundle, no local ``.hsp`` exists, so `device slots restore`
    can't re-author it) and appended to the setlist's membership, mirroring
    ``_record_placement``'s pattern for device-born presets.

    A name that's already registered to a **path-backed** tone is reported as
    a warning and left out of the membership (recording it would make the next
    sync overwrite the imported device preset with the local ``.hsp``'s
    content; leaving it out means that sync strips its reference instead —
    the user must rename one of the two, and the warning says so).
    """
    from .manifest import ManifestError

    warnings: List[str] = []
    manifest.create_setlist(result["setlist"])  # idempotent, keeps membership
    for name in result["installed"]:
        try:
            manifest.register_pathless(name, source="import-hss")
        except ManifestError as e:
            warnings.append(
                f"{name!r}: not recorded in the tone library ({e}) — the next "
                f"`device sync {result['setlist']}` will DROP its reference; "
                f"rename the conflicting local tone (or the imported preset) "
                f"and re-import to keep both")
            continue
        manifest.add_to_setlist(result["setlist"], name)
    return warnings
