"""``.hss`` setlist-bundle reader **and** byte-faithful writer (backlog #31).
**EXPERIMENTAL.**

A ``.hss`` is the Stadium app's "export setlist" file. The container and the
filled-slot framing are both pinned against **real hardware captures**:

* an *empty* setlist export (2026-07-14) — container framing, and
* a *non-empty* setlist export (2026-07-15,
  ``docs/superpowers/specs/2026-07-15-hss-and-cc-capture-findings.md``) — the
  filled-slot manifest ``type`` token and payload format.

Layout::

    <24-byte header><gzip stream>

* **Header** (bytes 0x00-0x17, 24 bytes total): ASCII tag ``GGGY`` + ``u32
  0``, ASCII tag ``LTES`` (byte-reversed spells ``SETL``) + ``u32 0``, then a
  little-endian ``u64`` version field observed as ``256``. The gzip stream
  (magic ``1f 8b``) begins immediately after, at offset 0x18.
* The decompressed body is a **POSIX ustar** tar archive: ``manifest.json``
  first, then **128** slot members named ``.1`` .. ``.128`` (1-based, matching
  the device's ``posi``). An **empty** slot is a 1-byte ``0x00`` sentinel
  member; the archive ends with the standard two zero blocks (no record-size
  padding).
* ``manifest.json`` (pretty-printed, ``indent=2``, keys sorted) =
  ``{"contents": [{"path": ".N", "type": ...}, ...128], "meta": {"device_id",
  "device_version", "name"}}``. An empty slot's ``type`` is the literal
  ``"<null>"``; a **filled** slot's is ``"application/stadium-preset"``.

**Filled-slot payload is the ``.hsp`` preset format** — magic ``rpshnosj`` +
(pretty) JSON, the same ``rpshnosj``+JSON family helixgen's ``.hsp`` writer
emits — **NOT** the device's ``_sbepgsm`` MessagePack content blob. The preset
display name lives inside the embedded ``.hsp`` at ``meta.name`` (the manifest
entry carries no name for a real export). On import each filled ``.hsp`` is
transcoded to device content (:func:`transcode.hsp_to_sbepgsm`) before install.
Synthesized fixtures / other export variants that embed an ``_sbepgsm`` /
``\\xff\\xff\\xff\\xffpgsm`` content blob are still accepted (detected by magic
bytes; the manifest ``type`` is cross-checked and a disagreement warns).

**Byte-faithful writer.** :func:`write_hss` re-emits a ``.hss`` whose 24-byte
header, gzip 10-byte header (matching ``MTIME``/``XFL``/``OS``), and *entire
decompressed tar* (member names/order/bytes + exact octal header field
formatting + two-zero-block EOF) are **byte-identical** to a real export,
*given the same slot payload bytes* (pinned by re-serializing both real
captures). The only bytes that differ are the compressed DEFLATE stream (and
its CRC/ISIZE trailer): Line 6's app uses a non-zlib DEFLATE encoder whose
output no ``zlib`` window/mem/level configuration reproduces (real: ~10 KB;
``zlib`` level 9: ~1.7 KB for the same tar). This envelope difference is
harmless — any conformant gunzip yields the identical tar. Caveat for
*content* built from helixgen tones (:func:`export_setlist_to_hss`): the app
pretty-prints the embedded ``.hsp`` JSON while helixgen writes compact, so
such an export's member bytes are not the app's bytes for the same preset —
same format family, functionally equivalent, re-importable.

Pure stdlib (``gzip`` + ``tarfile`` for reading + ``json`` + a hand-rolled
ustar header writer) for the container; embedded content decode/transcode goes
through :mod:`helixgen.device.content` / :mod:`helixgen.device.transcode`
(msgpack — already required for any ``device`` feature).
"""
from __future__ import annotations

import gzip
import io
import json
import struct
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

HSP_MAGIC = b"rpshnosj"        # .hsp preset file magic (== helixgen.hsp.HSP_MAGIC)
_SBEPGSM_MAGIC = b"_sbepgsm"   # device edit-buffer content magic
_CONTENT_DATA_MAGIC = b"\xff\xff\xff\xffpgsm"  # /SetContentData stored-content magic

FILLED_TYPE = "application/stadium-preset"  # manifest type token for a filled slot
NULL_TYPE = "<null>"                        # manifest type token for an empty slot

# Payload-format tags (HssSlot.payload_format / write_hss input validation).
FMT_HSP = "hsp"            # rpshnosj + JSON  (what a real export embeds)
FMT_SBEPGSM = "sbepgsm"    # _sbepgsm / \xff\xff\xff\xffpgsm content blob
FMT_UNKNOWN = "unknown"

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
    name: Optional[str] = None    # preset display name (from the embedded .hsp meta.name
                                  #   for a real export; manifest "name" as a fallback)
    blob: Optional[bytes] = None  # raw tar-member bytes for a filled slot; None if empty
    payload_format: Optional[str] = None  # FMT_HSP / FMT_SBEPGSM / FMT_UNKNOWN (filled only)
    raw_entry: Dict[str, Any] = field(default_factory=dict)  # the manifest contents[] entry


@dataclass
class HssBundle:
    """A parsed ``.hss`` setlist bundle."""

    name: Optional[str]
    device_id: Optional[int]
    device_version: Optional[int]
    version: int
    slots: List[HssSlot]
    mtime: Optional[int] = None   # export timestamp (gzip MTIME / tar member mtime)

    @property
    def filled_slots(self) -> List[HssSlot]:
        return [s for s in self.slots if s.filled]


def detect_payload_format(blob: Optional[bytes]) -> str:
    """Classify a filled slot's payload by its magic bytes.

    ``FMT_HSP`` for a ``.hsp`` preset (``rpshnosj``), ``FMT_SBEPGSM`` for a
    device content blob (``_sbepgsm`` or the ``/SetContentData`` framing),
    ``FMT_UNKNOWN`` otherwise. Detection is by **magic bytes**, never the
    manifest ``type`` string (which is cross-checked separately).
    """
    if not blob:
        return FMT_UNKNOWN
    if blob[:len(HSP_MAGIC)] == HSP_MAGIC:
        return FMT_HSP
    if blob[:len(_SBEPGSM_MAGIC)] == _SBEPGSM_MAGIC:
        return FMT_SBEPGSM
    if blob[:len(_CONTENT_DATA_MAGIC)] == _CONTENT_DATA_MAGIC:
        return FMT_SBEPGSM
    return FMT_UNKNOWN


def _hsp_meta_name(blob: bytes) -> Optional[str]:
    """Best-effort ``meta.name`` from an embedded ``.hsp`` payload; ``None`` if
    it isn't parseable ``rpshnosj``+JSON or has no string name."""
    if blob[:len(HSP_MAGIC)] != HSP_MAGIC:
        return None
    try:
        body = json.loads(blob[len(HSP_MAGIC):].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    meta = body.get("meta")
    name = meta.get("name") if isinstance(meta, dict) else None
    return name if isinstance(name, str) and name.strip() else None


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
        members = tf.getmembers()
    except tarfile.TarError as e:
        raise HssFormatError(f"corrupt tar archive: {e}") from e
    members_by_name = {m.name: m for m in members}
    mtime = int(members[0].mtime) if members else None

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
        fmt = detect_payload_format(member_bytes)
        # Name: a real export carries it inside the embedded .hsp (meta.name),
        # not in the manifest entry. Fall back to a manifest "name" key (older
        # synthesized fixtures) only when the payload has none.
        name = _hsp_meta_name(member_bytes) if fmt == FMT_HSP else None
        if name is None:
            raw_name = entry.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                name = raw_name
        slots.append(HssSlot(
            pos=pos, filled=True,
            name=name,
            blob=member_bytes,
            payload_format=fmt,
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
        mtime=mtime,
    )


def hss_slot_label(slot: HssSlot) -> str:
    """Human-readable label for a slot, for ``--list`` output — never crashes
    on the unconfirmed name field.

    Named ``hss_slot_label`` (not ``slot_label``) to avoid the readability trap
    of colliding with the unrelated ``client.slot_label`` (posi->bank label);
    this one labels an ``HssSlot`` bundle entry.
    """
    if not slot.filled:
        return "(empty)"
    return slot.name or f"(unnamed — slot {slot.pos})"


def looks_like_content_blob(blob: bytes) -> bool:
    """True if ``blob`` is an installable filled-slot payload — a ``.hsp``
    preset (``rpshnosj``) OR a device content blob (``_sbepgsm`` /
    ``/SetContentData`` framing).

    Used to fail an unrecognized filled slot closed with a clear per-slot error
    *before* handing it to the device (the device-write primitives raise a bare
    ``ValueError`` for content they can't decode).
    """
    return detect_payload_format(blob) in (FMT_HSP, FMT_SBEPGSM)


def _installable_content(slot: HssSlot) -> bytes:
    """Return device ``/SetContentData`` bytes for a filled ``slot``.

    A ``.hsp`` payload is transcoded (:func:`transcode.hsp_to_sbepgsm`); a
    device content blob is normalized to the stored-content framing
    (:func:`content.to_content_data`). Raises :class:`ValueError` for an
    unrecognized payload.
    """
    from . import content, transcode

    fmt = slot.payload_format or detect_payload_format(slot.blob)
    if fmt == FMT_HSP:
        body = json.loads(slot.blob[len(HSP_MAGIC):].decode("utf-8"))
        return transcode.hsp_to_sbepgsm(body)
    if fmt == FMT_SBEPGSM:
        return content.to_content_data(slot.blob)
    raise ValueError("payload is neither a .hsp preset nor a device content blob")


def import_bundle(client: Any, bundle: HssBundle, *,
                  setlist: Optional[str] = None) -> Dict[str, Any]:
    """Install every filled slot of ``bundle`` into ``client``'s pool and
    reference them into a device setlist, in bundle order.

    ``setlist`` names the destination (created if absent); ``None`` falls
    back to the bundle's own manifest name — raises :class:`ValueError` if
    neither is available. Shared by the CLI (`device setlist import-hss`) and
    any other caller so all stay behaviorally identical.

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

    A filled slot's payload is transcoded on the way in: a ``.hsp``
    (``rpshnosj``) preset via :func:`transcode.hsp_to_sbepgsm`, a device
    content blob via :func:`content.to_content_data`. The detected format is
    cross-checked against the manifest ``type`` token and a disagreement is
    reported in ``warnings`` (non-fatal — the magic bytes win).

    Returns ``{ok, setlist, cid, created, installed, warnings, errors}``
    (``ok`` is ``not errors``). Connection-level failures resolving/creating
    the setlist itself propagate as ``HelixError`` — only PER-SLOT failures
    are absorbed.
    """
    from .client import Cctp, HelixError

    filled = bundle.filled_slots
    target_setlist = setlist or bundle.name
    if not target_setlist:
        raise ValueError(
            "the bundle has no setlist name in its manifest; pass an explicit setlist name")

    if not filled:
        return {"ok": True, "setlist": target_setlist, "cid": None, "created": False,
                "installed": [], "warnings": [], "errors": []}

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
    warnings: List[str] = []
    errors: List[str] = []
    for s in filled:
        name = hss_slot_label(s)
        fmt = s.payload_format or detect_payload_format(s.blob)
        if fmt == FMT_UNKNOWN:
            errors.append(
                f"slot {s.pos} ({name!r}): payload is neither a .hsp preset "
                f"(rpshnosj) nor a device content blob; skipped, nothing "
                f"written to the device")
            continue
        # Cross-check the manifest type token against the magic-detected format
        # (magic wins; a mismatch is advisory, not fatal).
        manifest_type = s.raw_entry.get("type")
        if manifest_type == FILLED_TYPE and fmt != FMT_HSP:
            warnings.append(
                f"slot {s.pos} ({name!r}): manifest type {FILLED_TYPE!r} implies "
                f"a .hsp preset but the payload magic is {fmt!r}; trusting the "
                f"payload bytes")
        elif manifest_type == NULL_TYPE:
            warnings.append(
                f"slot {s.pos} ({name!r}): manifest type {NULL_TYPE!r} claims the "
                f"slot is EMPTY but the payload magic is {fmt!r}; trusting the "
                f"payload bytes and importing it")
        elif isinstance(manifest_type, str) and manifest_type not in (FILLED_TYPE, NULL_TYPE):
            warnings.append(
                f"slot {s.pos} ({name!r}): unexpected manifest type "
                f"{manifest_type!r}; proceeding on the {fmt!r} payload magic")
        try:
            blob = _installable_content(s)
        except Exception as e:  # noqa: BLE001 — untrusted embedded payload: a
            # structurally malformed .hsp (magic present, body not a proper
            # preset dict) raises AttributeError/TypeError deep inside the
            # transcoder, not just ValueError. ANY failure here must become a
            # per-slot error so the loop continues and already-installed slots
            # aren't orphaned by an escaping exception (the docstring's
            # per-slot fail-closed contract).
            errors.append(
                f"slot {s.pos} ({name!r}): could not decode/transcode payload "
                f"({type(e).__name__}: {e})")
            continue
        try:
            pool_cid = client.install_into_pool(blob, name)
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
            "created": created, "installed": installed, "warnings": warnings,
            "errors": errors}


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


# --- byte-faithful writer -----------------------------------------------------
#
# The tar the Stadium app embeds uses the classic libarchive/GNU octal header
# field formatting, NOT Python ``tarfile``'s ("%07o\0" vs "%06o \0"), and ends
# with just the two standard zero blocks (no ``tarfile`` RECORDSIZE padding).
# We therefore hand-write ustar headers to reproduce the decompressed tar
# byte-for-byte (verified against both real captures in tests/test_hss.py).

_MODE_DEFAULT = 0o644


def _octal8(v: int) -> bytes:
    """8-byte ustar numeric field, app style: 6 octal digits + space + NUL."""
    return ("%06o" % v).encode("ascii") + b" \x00"


def _octal12(v: int) -> bytes:
    """12-byte ustar numeric field, app style: 11 octal digits + space."""
    return ("%011o" % v).encode("ascii") + b" "


def _ustar_header(name: str, size: int, mtime: int, mode: int = _MODE_DEFAULT) -> bytes:
    """One 512-byte ustar header block, byte-identical to the app's tar."""
    name_bytes = name.encode("utf-8")
    if len(name_bytes) > 100:
        raise ValueError(f"tar member name too long for a ustar header: {name!r}")
    h = bytearray(512)
    h[0:len(name_bytes)] = name_bytes
    h[100:108] = _octal8(mode)
    h[108:116] = _octal8(0)          # uid
    h[116:124] = _octal8(0)          # gid
    h[124:136] = _octal12(size)
    h[136:148] = _octal12(mtime)
    h[148:156] = b" " * 8            # checksum placeholder (spaces) before summing
    h[156:157] = b"0"               # typeflag: regular file
    h[257:263] = b"ustar\x00"
    h[263:265] = b"00"
    h[329:337] = _octal8(0)          # devmajor
    h[337:345] = _octal8(0)          # devminor
    chksum = sum(h)
    h[148:156] = ("%06o" % chksum).encode("ascii") + b"\x00 "
    return bytes(h)


def _build_tar(members: Sequence[tuple], mtime: int) -> bytes:
    """Concatenate ``(name, data)`` members as ustar header+data blocks, then
    the standard two-zero-block EOF (no record-size padding)."""
    out = bytearray()
    for name, data in members:
        out += _ustar_header(name, len(data), mtime)
        out += data
        out += b"\x00" * ((-len(data)) % 512)   # pad data to a 512 boundary
    out += b"\x00" * 1024                         # two zero blocks = tar EOF
    return bytes(out)


def build_manifest(setlist_name: str, slot_types: Sequence[str], *,
                   device_id: int, device_version: int) -> bytes:
    """Serialize ``manifest.json`` exactly as the app does — pretty-printed
    (``indent=2``), keys sorted, ``contents`` in slot order."""
    contents = [{"path": f".{i + 1}", "type": t} for i, t in enumerate(slot_types)]
    manifest = {
        "contents": contents,
        "meta": {"device_id": int(device_id),
                 "device_version": int(device_version),
                 "name": setlist_name},
    }
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


def write_hss(setlist_name: str, presets: Sequence[Optional[bytes]], *,
              device_id: int, device_version: int,
              mtime: Optional[int] = None, version: int = _EXPECTED_VERSION,
              num_slots: int = NUM_SLOTS) -> bytes:
    """Build a byte-faithful ``.hss`` setlist bundle.

    ``presets`` is a positional sequence — index ``i`` fills slot ``i + 1``.
    Each entry is either the preset's **``.hsp`` bytes** (magic ``rpshnosj`` +
    JSON — embedded verbatim, ``type: "application/stadium-preset"``) or
    ``None`` for an empty slot (the 1-byte ``0x00`` sentinel, ``type:
    "<null>"``). A device content blob (``_sbepgsm`` / ``/SetContentData``
    framing) is also accepted for a slot and embedded verbatim, for callers
    that already hold device content. Fewer than ``num_slots`` entries pads the
    remainder empty; more is an error.

    ``mtime`` sets both the gzip ``MTIME`` and every tar member's mtime
    (defaults to the current time); pass a fixed value for reproducible output.

    Byte-faithfulness: **given the same slot payload bytes**, the 24-byte
    header, the gzip 10-byte header (``MTIME``/``XFL=2``/``OS=3``), and the
    entire decompressed tar are byte-identical to a real Stadium export
    (payloads are embedded verbatim; the container framing is reproduced
    exactly — pinned by re-serializing both real captures in
    ``tests/test_hss.py``). Only the compressed DEFLATE stream differs (the
    app uses a non-zlib encoder) — the decompressed content is identical, so
    any gunzip yields the same tar. Note the app pretty-prints the ``.hsp``
    JSON it embeds while helixgen's ``.hsp`` files are compact — both are
    valid ``rpshnosj``+JSON, but an export built from helixgen-authored tones
    will carry compact members, not the app's pretty bytes. See the module
    docstring.
    """
    if len(presets) > num_slots:
        raise ValueError(
            f"{len(presets)} presets exceeds the {num_slots}-slot capacity")
    if mtime is None:
        mtime = int(time.time())
    mtime = int(mtime)

    slot_types: List[str] = []
    members: List[tuple] = [("manifest.json", b"")]  # placeholder, filled below
    for i in range(num_slots):
        blob = presets[i] if i < len(presets) else None
        if not blob:  # None or b"" — both mean "no preset here"
            slot_types.append(NULL_TYPE)
            members.append((f".{i + 1}", _EMPTY_SENTINEL))
            continue
        fmt = detect_payload_format(blob)
        if fmt == FMT_UNKNOWN:
            raise ValueError(
                f"slot {i + 1}: payload is neither a .hsp preset (rpshnosj) nor "
                f"a device content blob; cannot embed it")
        slot_types.append(FILLED_TYPE)
        members.append((f".{i + 1}", bytes(blob)))

    manifest_bytes = build_manifest(
        setlist_name, slot_types, device_id=device_id, device_version=device_version)
    members[0] = ("manifest.json", manifest_bytes)

    tar_bytes = _build_tar(members, mtime)

    # gzip with the app's header shape: level-9 (XFL=2), MTIME=mtime, OS=3(Unix).
    gz = bytearray(gzip.compress(tar_bytes, compresslevel=9, mtime=mtime))
    gz[9] = 0x03  # OS byte: Python writes 0xff (unknown); the app writes 3 (Unix)

    header = (_TAG_A + struct.pack("<I", 0)
              + _TAG_B + struct.pack("<I", 0)
              + struct.pack("<Q", int(version)))
    return header + bytes(gz)


# --- device setlist -> .hss export --------------------------------------------

# Observed on a Stadium XL (docs/superpowers/specs/2026-07-15-...); used as a
# fallback when a live /ProductInfoGet can't supply them.
DEFAULT_DEVICE_ID = 0x260000
DEFAULT_DEVICE_VERSION = 0x1302053C


def _device_header_fields(client: Any) -> tuple:
    """(device_id, device_version) for the .hss header/manifest from
    ``/ProductInfoGet``, falling back to the observed Stadium XL constants.

    ``device_version`` packs the firmware version as
    ``(majo << 28) | (mino << 24) | (patc << 16) | buld`` — verified against
    the real capture: fw 1.3.2 build 1340 → ``0x1302053C`` (= 318899516), the
    exact ``device_version`` the app wrote into its export's manifest.
    """
    device_id = DEFAULT_DEVICE_ID
    device_version = DEFAULT_DEVICE_VERSION
    try:
        info = client.product_info()
    except Exception:  # noqa: BLE001 — header metadata only; never block the export
        return device_id, device_version
    if isinstance(info.get("device_id"), int):
        device_id = info["device_id"]
    raw = info.get("raw") if isinstance(info, dict) else None
    host = raw.get("host") if isinstance(raw, dict) else None
    vers = host.get("vers") if isinstance(host, dict) else None
    if isinstance(vers, dict):
        majo, mino, patc, buld = (vers.get(k) for k in ("majo", "mino", "patc", "buld"))
        if all(isinstance(v, int) for v in (majo, mino, patc, buld)):
            device_version = (majo << 28) | (mino << 24) | (patc << 16) | buld
    return device_id, device_version


def export_setlist_to_hss(client: Any, setlist_name: str, *,
                          manifest: Any = None,
                          mtime: Optional[int] = None) -> Dict[str, Any]:
    """Build a byte-faithful ``.hss`` from a **device** setlist. EXPERIMENTAL.

    Reads the device setlist's references (order + slot ``posi``) and embeds
    each referenced preset's **local ``.hsp``** — resolved by preset name via
    the tone-library manifest (:meth:`SetlistManifest.tone_path`) — verbatim,
    at the matching slot (``.{posi+1}``). This mirrors the app, which embeds a
    ``.hsp`` per preset; helixgen's ``.hsp`` is the source of truth.

    A referenced preset with **no local ``.hsp``** (device-born, or untracked
    by the manifest) is reported in ``skipped`` and left out — helixgen has no
    ``_sbepgsm`` → ``.hsp`` converter, so a device-only preset can't be
    re-embedded as a ``.hsp`` (backlog #31 residual). Device id/version for the
    header come from ``/ProductInfoGet``.

    Returns ``{ok, setlist, embedded, skipped, bytes}`` — ``bytes`` is the full
    ``.hss`` file content; ``ok`` is ``not skipped``. Connection failures
    propagate as :class:`HelixError`.
    """
    from .client import Cctp, Container, HelixError

    if manifest is None:
        from .manifest import SetlistManifest
        manifest = SetlistManifest.load()

    setlist_cid = client.resolve_setlist_cid(setlist_name, strict=True)
    if setlist_cid is None:
        raise HelixError(
            f"no setlist named {setlist_name!r} on the device "
            f"(create it with `device setlist create {setlist_name}` or check "
            f"`device setlists`)")

    refs = [m for m in client.list_container(setlist_cid, strict=True)
            if m.get("cctp") == Cctp.REFERENCE]
    refs.sort(key=lambda m: m.get("posi", 1 << 30))

    # pool cid -> name, to resolve each reference's target preset name
    pool_name = {m.get("cid_"): m.get("name")
                 for m in client.list_container(Container.POOL, strict=True)
                 if m.get("cctp") == Cctp.PRESET and m.get("cid_") is not None}

    presets: List[Optional[bytes]] = [None] * NUM_SLOTS
    embedded: List[str] = []
    skipped: List[str] = []
    for m in refs:
        posi = m.get("posi")
        name = pool_name.get(m.get("rcid"))
        if name is None:
            skipped.append(f"posi {posi}: reference target not found in the pool")
            continue
        path = manifest.tone_path(name)
        if not path or not Path(path).exists():
            skipped.append(
                f"{name!r}: no local .hsp in the tone library (device-born or "
                f"untracked; a device-content → .hsp export is unimplemented "
                f"— backlog #31 residual)")
            continue
        if not isinstance(posi, int) or not (0 <= posi < NUM_SLOTS):
            skipped.append(f"{name!r}: reference posi {posi!r} outside 0..{NUM_SLOTS - 1}")
            continue
        blob = Path(path).read_bytes()
        if blob[:len(HSP_MAGIC)] != HSP_MAGIC:
            skipped.append(f"{name!r}: local file {path} is not a .hsp (rpshnosj)")
            continue
        presets[posi] = blob
        embedded.append(name)

    device_id, device_version = _device_header_fields(client)
    data = write_hss(setlist_name, presets, device_id=device_id,
                     device_version=device_version, mtime=mtime)
    return {"ok": not skipped, "setlist": setlist_name,
            "embedded": embedded, "skipped": skipped, "bytes": data}
