"""Tests for the ``.hss`` setlist-bundle READER (backlog #31, EXPERIMENTAL).

Two fixture tiers:

* ``tests/fixtures/hss/throwaway_empty.hss`` — a REAL capture (an empty
  setlist export off a Stadium XL, 2026-07-14; gitignored like
  ``tests/fixtures/device_content/``). Pins header/gzip/tar/manifest/128-slot
  parsing against ground truth. Tests using it skip cleanly when absent.
* Synthesized bundles built by ``_build_hss`` below, which replicate the
  documented container framing exactly but can only prove the reader is
  internally consistent with *our own* filled-slot assumption — not that it
  matches a real Line 6 export (no non-empty sample exists yet; see
  ``src/helixgen/device/hss.py`` module docstring and backlog #31).
"""
from __future__ import annotations

import gzip
import io
import json
import struct
import tarfile
from pathlib import Path

import pytest

pytest.importorskip("msgpack")

from helixgen.device import hss  # noqa: E402
from helixgen.device import content  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures"
REAL_EMPTY_HSS = FIXTURE_DIR / "hss" / "throwaway_empty.hss"
DEVICE_CONTENT_DIR = FIXTURE_DIR / "device_content"


# --- synthesized-bundle builder (mirrors src/helixgen/device/hss.py's documented
#     framing so the reader's own logic is exercised end-to-end) ----------------

def _build_hss(*, setlist_name: str = "MySetlist", device_id: int = 0x260000,
               device_version: int = 0x1302053C, version: int = 256,
               filled: dict | None = None) -> bytes:
    """Build raw ``.hss`` bytes: header + gzip(tar(manifest.json + 128 slots)).

    ``filled`` maps 1-based slot position -> ``(name, blob_bytes)`` for the
    slots that should be non-empty; every other slot is the 1-byte sentinel.
    """
    filled = filled or {}
    contents = []
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        # manifest.json written after we know its own content, so build the
        # contents[] list first, add slot members, then append manifest.
        for pos in range(1, hss.NUM_SLOTS + 1):
            path = f".{pos}"
            if pos in filled:
                name, blob = filled[pos]
                contents.append({"path": path, "type": "content", "name": name})
                data = blob
            else:
                contents.append({"path": path, "type": "<null>"})
                data = b"\x00"
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))

        manifest = {
            "meta": {"name": setlist_name, "device_id": device_id,
                     "device_version": device_version},
            "contents": contents,
        }
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        info = tarfile.TarInfo(name=hss.MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(manifest_bytes))

    gz = gzip.compress(tar_buf.getvalue())
    header = (hss._TAG_A + struct.pack("<I", 0)
              + hss._TAG_B + struct.pack("<I", 0)
              + struct.pack("<Q", version))
    return header + gz


def _real_sbepgsm_blob(name: str = "preset_151") -> bytes:
    path = DEVICE_CONTENT_DIR / f"{name}.sbepgsm"
    if not path.exists():
        pytest.skip(f"device-content fixture absent: {path}")
    return path.read_bytes()


# --- header/container parsing -------------------------------------------------

def test_read_hss_rejects_short_file():
    with pytest.raises(hss.HssFormatError):
        hss.read_hss(b"too short")


def test_read_hss_rejects_bad_tag_a():
    data = bytearray(_build_hss())
    data[0:4] = b"XXXX"
    with pytest.raises(hss.HssFormatError, match="GGGY"):
        hss.read_hss(bytes(data))


def test_read_hss_rejects_bad_tag_b():
    data = bytearray(_build_hss())
    data[8:12] = b"XXXX"
    with pytest.raises(hss.HssFormatError, match="LTES"):
        hss.read_hss(bytes(data))


def test_read_hss_rejects_non_gzip_payload():
    header = (hss._TAG_A + struct.pack("<I", 0)
              + hss._TAG_B + struct.pack("<I", 0)
              + struct.pack("<Q", 256))
    with pytest.raises(hss.HssFormatError, match="gzip"):
        hss.read_hss(header + b"not gzip data at all")


def test_read_hss_rejects_non_tar_payload():
    header = (hss._TAG_A + struct.pack("<I", 0)
              + hss._TAG_B + struct.pack("<I", 0)
              + struct.pack("<Q", 256))
    gz = gzip.compress(b"not a tar archive")
    with pytest.raises(hss.HssFormatError, match="tar"):
        hss.read_hss(header + gz)


def test_read_hss_rejects_missing_manifest():
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        info = tarfile.TarInfo(name=".1")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"\x00"))
    header = (hss._TAG_A + struct.pack("<I", 0)
              + hss._TAG_B + struct.pack("<I", 0)
              + struct.pack("<Q", 256))
    gz = gzip.compress(tar_buf.getvalue())
    with pytest.raises(hss.HssFormatError, match="manifest.json"):
        hss.read_hss(header + gz)


def test_read_hss_accepts_bytes_or_path(tmp_path):
    data = _build_hss()
    bundle_from_bytes = hss.read_hss(data)
    p = tmp_path / "x.hss"
    p.write_bytes(data)
    bundle_from_path = hss.read_hss(p)
    assert bundle_from_bytes.name == bundle_from_path.name == "MySetlist"


# --- empty-bundle shape (synthesized) ------------------------------------------

def test_read_hss_empty_bundle_synthesized():
    bundle = hss.read_hss(_build_hss())
    assert bundle.name == "MySetlist"
    assert bundle.device_id == 0x260000
    assert bundle.device_version == 0x1302053C
    assert bundle.version == 256
    assert len(bundle.slots) == hss.NUM_SLOTS
    assert bundle.filled_slots == []
    for i, slot in enumerate(bundle.slots, start=1):
        assert slot.pos == i
        assert slot.filled is False
        assert slot.blob is None
        assert slot.name is None


# --- REAL captured empty export (ground truth) ---------------------------------

@pytest.mark.skipif(not REAL_EMPTY_HSS.exists(),
                    reason=f"real capture absent: {REAL_EMPTY_HSS}")
def test_read_hss_real_empty_capture():
    bundle = hss.read_hss(REAL_EMPTY_HSS)
    assert bundle.name == "Throwaway"
    assert bundle.device_id == 0x260000
    assert bundle.device_version == 0x1302053C
    assert bundle.version == 256
    assert len(bundle.slots) == 128
    assert bundle.filled_slots == []
    assert all(not s.filled and s.blob is None for s in bundle.slots)
    # slots are strictly positional/ordered .1..128
    assert [s.pos for s in bundle.slots] == list(range(1, 129))


@pytest.mark.skipif(not REAL_EMPTY_HSS.exists(),
                    reason=f"real capture absent: {REAL_EMPTY_HSS}")
def test_read_hss_real_capture_accepts_raw_bytes():
    data = REAL_EMPTY_HSS.read_bytes()
    bundle = hss.read_hss(data)
    assert bundle.name == "Throwaway"


# --- filled-slot path (synthesized, framing-assumption flagged) ----------------

def test_read_hss_filled_slot_synthesized_with_real_content_blob():
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(filled={3: ("Lead Tone", blob)})
    bundle = hss.read_hss(data)
    assert len(bundle.filled_slots) == 1
    slot = bundle.filled_slots[0]
    assert slot.pos == 3
    assert slot.filled is True
    assert slot.name == "Lead Tone"
    assert slot.blob == blob
    # the embedded blob itself decodes as real preset content (msgpack)
    doc = content.decode_any(slot.blob)
    assert set(doc.keys()) >= {"sfg_", "pm__"}


def test_read_hss_filled_slot_missing_name_falls_back_gracefully():
    blob = _real_sbepgsm_blob("preset_152")
    data = _build_hss(filled={1: (None, blob)})
    # simulate a manifest entry that omits "name" entirely (the exact
    # unconfirmed case flagged in the module docstring)
    bundle = hss.read_hss(data)
    slot = bundle.slots[0]
    assert slot.filled is True
    assert slot.name is None
    assert hss.slot_label(slot) == "(unnamed — slot 1)"


def test_read_hss_multiple_filled_slots_preserve_order():
    blob151 = _real_sbepgsm_blob("preset_151")
    blob152 = _real_sbepgsm_blob("preset_152")
    data = _build_hss(filled={1: ("First", blob151), 5: ("Fifth", blob152)})
    bundle = hss.read_hss(data)
    assert [s.pos for s in bundle.filled_slots] == [1, 5]
    assert [s.name for s in bundle.filled_slots] == ["First", "Fifth"]
    assert bundle.slots[1].filled is False  # slot 2 stays empty
    # non-adjacent filled slots don't disturb the fixed 128-slot ordering
    assert [s.pos for s in bundle.slots] == list(range(1, 129))


def test_slot_label_empty():
    empty_slot = hss.HssSlot(pos=7, filled=False)
    assert hss.slot_label(empty_slot) == "(empty)"


def test_hss_format_error_is_value_error():
    assert issubclass(hss.HssFormatError, ValueError)


# --- fails-closed on a malformed manifest shape (adversarial review findings) --
# read_hss's own contract is "fail with HssFormatError, never a raw traceback" —
# these pin that against the specific manifest shapes an unconfirmed real
# filled-slot export could plausibly have.

def _wrap(gz_body: bytes, version: int = 256) -> bytes:
    header = (hss._TAG_A + struct.pack("<I", 0)
              + hss._TAG_B + struct.pack("<I", 0)
              + struct.pack("<Q", version))
    return header + gzip.compress(gz_body)


def _tar_with_manifest_json(manifest_bytes: bytes, *, extra_members: dict | None = None) -> bytes:
    """Build a raw tar body whose manifest.json is exactly ``manifest_bytes``
    (bypassing json.dumps so malformed-but-parseable JSON can be tested)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=hss.MANIFEST_NAME)
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))
        for name, data in (extra_members or {}).items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_read_hss_rejects_manifest_not_an_object():
    data = _wrap(_tar_with_manifest_json(b'["not", "an", "object"]'))
    with pytest.raises(hss.HssFormatError, match="top level"):
        hss.read_hss(data)


def test_read_hss_rejects_meta_not_an_object():
    data = _wrap(_tar_with_manifest_json(b'{"meta": "oops", "contents": []}'))
    with pytest.raises(hss.HssFormatError, match='"meta"'):
        hss.read_hss(data)


def test_read_hss_rejects_contents_not_an_array():
    data = _wrap(_tar_with_manifest_json(b'{"meta": {}, "contents": 5}'))
    with pytest.raises(hss.HssFormatError, match='"contents"'):
        hss.read_hss(data)


def test_read_hss_rejects_non_regular_file_manifest_member():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=hss.MANIFEST_NAME)
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
    data = _wrap(buf.getvalue())
    with pytest.raises(hss.HssFormatError, match="regular file"):
        hss.read_hss(data)


def test_read_hss_rejects_non_regular_file_slot_member():
    manifest = json.dumps({"meta": {"name": "X"}, "contents": []}).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=hss.MANIFEST_NAME)
        info.size = len(manifest)
        tf.addfile(info, io.BytesIO(manifest))
        dir_info = tarfile.TarInfo(name=".1")
        dir_info.type = tarfile.DIRTYPE
        tf.addfile(dir_info)
    data = _wrap(buf.getvalue())
    with pytest.raises(hss.HssFormatError, match="regular file"):
        hss.read_hss(data)


def test_read_hss_tolerates_non_dict_contents_entries():
    """A contents[] list containing non-dict junk (e.g. a stray string) is
    skipped, not a crash — only dict entries are indexed by path."""
    manifest = json.dumps({"meta": {"name": "X"},
                           "contents": ["not-a-dict", {"path": ".1", "type": "<null>"}]}).encode()
    data = _wrap(_tar_with_manifest_json(manifest))
    bundle = hss.read_hss(data)
    assert bundle.name == "X"
    assert len(bundle.slots) == hss.NUM_SLOTS


def test_read_hss_zero_byte_slot_member_is_empty():
    """A 0-byte slot member classifies as EMPTY — 'no content at all' cannot
    be a filled preset, and calling it filled would surface a bogus
    per-slot error downstream."""
    data = _build_hss(filled={3: ("Ghost", b"")})
    bundle = hss.read_hss(data)
    slot = bundle.slots[2]
    assert slot.pos == 3
    assert slot.filled is False
    assert slot.blob is None
    assert bundle.filled_slots == []


def test_read_hss_one_byte_nonzero_payload_is_filled_but_unrecognized():
    """A 1-byte NON-zero member is the boundary case against the 0x00 empty
    sentinel: it counts as FILLED (blob preserved verbatim), and the import
    path classifies it as an unrecognized payload (per-slot error), never as
    empty."""
    data = _build_hss(filled={1: ("Tiny", b"\x01")})
    bundle = hss.read_hss(data)
    slot = bundle.slots[0]
    assert slot.filled is True
    assert slot.blob == b"\x01"
    assert not hss.looks_like_content_blob(slot.blob)

    class StubClient:
        installs = []

        def resolve_setlist_cid(self, name):
            return 4242

        def list_container(self, cid, **kw):
            return []

        def install_into_pool(self, blob, name, **kw):  # pragma: no cover - must not run
            type(self).installs.append(name)
            return 1

    result = hss.import_bundle(StubClient(), bundle)
    assert result["ok"] is False
    assert result["installed"] == []
    assert len(result["errors"]) == 1 and "slot 1" in result["errors"][0]
    assert StubClient.installs == []  # never sent to the device


def test_read_hss_non_string_slot_name_falls_back_to_none():
    """A manifest name field of the wrong type (e.g. an int) never leaks into
    HssSlot.name — the field is documented as Optional[str]."""
    blob = _real_sbepgsm_blob("preset_151")
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        for pos in range(1, hss.NUM_SLOTS + 1):
            data = blob if pos == 1 else b"\x00"
            info = tarfile.TarInfo(name=f".{pos}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        contents = [{"path": f".{p}",
                    "type": "content" if p == 1 else "<null>",
                    **({"name": 12345} if p == 1 else {})}
                   for p in range(1, hss.NUM_SLOTS + 1)]
        manifest = json.dumps({"meta": {"name": "X"}, "contents": contents}).encode()
        info = tarfile.TarInfo(name=hss.MANIFEST_NAME)
        info.size = len(manifest)
        tf.addfile(info, io.BytesIO(manifest))
    data = _wrap(tar_buf.getvalue())
    bundle = hss.read_hss(data)
    slot = bundle.slots[0]
    assert slot.filled is True
    assert slot.name is None
    assert isinstance(slot.raw_entry.get("name"), int)  # preserved verbatim in raw_entry
