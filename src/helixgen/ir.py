"""User-IR registration: maps Helix `irhash` slot values to local .wav paths."""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import math
import os
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from helixgen import home


class IrMappingError(ValueError):
    """Raised when an IR mapping operation is rejected (conflict, ambiguity, etc.)."""


def default_irs_path() -> Path:
    """Return the default IRs directory: ``home.library_irs_dir()``.

    Honors ``$HELIXGEN_IRS`` (``library_irs_dir()`` checks it first), falling
    back to ``library_dir()/"irs"``. mapping.json + the per-IR sidecars live
    here as of the library-metadata flip; :meth:`IrMapping.load` bridges a
    pre-flip ``mapping.json`` still sitting at ``legacy_irs_dir()`` up to this
    location on first use.
    """
    return home.library_irs_dir()


@dataclass
class IrMapping:
    """Hash→wav-path mapping for user IRs registered with helixgen."""

    irs_dir: Path
    entries: dict[str, str] = field(default_factory=dict)
    # Set only when this mapping was bridged from a pre-flip legacy
    # mapping.json (``legacy_irs_dir()``): the path to rename to
    # ``mapping.json.migrated-legacy`` after the first successful save() at the
    # (library) location. ``None`` in every other case.
    _legacy_source: Path | None = field(default=None, repr=False, compare=False)

    @classmethod
    def _load_from(cls, irs_dir: Path) -> "IrMapping":
        """Load mapping.json straight out of ``irs_dir`` (no legacy bridge)."""
        irs_dir = Path(irs_dir)
        mapping_file = irs_dir / "mapping.json"
        if not mapping_file.exists():
            return cls(irs_dir=irs_dir, entries={})
        data = json.loads(mapping_file.read_text())
        return cls(irs_dir=irs_dir, entries=dict(data))

    @classmethod
    def load(cls, irs_dir: Path | None = None) -> "IrMapping":
        """Load the IR mapping, bridging a pre-flip legacy mapping.json up to
        the library location on first use.

        Path precedence:

        - An explicit ``irs_dir`` (or ``$HELIXGEN_IRS``) is a self-consistent,
          caller-chosen location — loaded directly, NEVER bridged.
        - Otherwise the default (library) location wins if its mapping.json
          exists. If it does NOT but a pre-flip ``mapping.json`` still sits at
          ``legacy_irs_dir()`` (``~/.helixgen/irs``), its entries are ADOPTED
          while ``irs_dir`` is set to the library location, so the next
          ``save()`` writes to the new place (and renames the legacy file to
          ``mapping.json.migrated-legacy``). Relative legacy values are
          absolutized against the legacy dir first so they keep resolving.
          Mirrors
          ``SetlistManifest``'s v2->v3 legacy bridge.
        - A second run finds the library mapping.json and never re-bridges.
        """
        if irs_dir is not None:
            return cls._load_from(Path(irs_dir))
        if os.environ.get("HELIXGEN_IRS"):
            # An explicit env override is a self-consistent location: no bridge.
            return cls._load_from(home.library_irs_dir())

        lib = home.library_irs_dir()
        if (lib / "mapping.json").exists():
            return cls._load_from(lib)

        legacy_file = home.legacy_irs_dir() / "mapping.json"
        if legacy_file.exists():
            legacy_dir = home.legacy_irs_dir()
            data = json.loads(legacy_file.read_text())
            bridged: dict[str, str] = {}
            for h, v in dict(data).items():
                p = Path(v)
                if not p.is_absolute():
                    p = (legacy_dir / p).resolve()
                bridged[h] = str(p)
            m = cls(irs_dir=lib, entries=bridged)
            m._legacy_source = legacy_file
            return m

        return cls(irs_dir=lib, entries={})

    def save(self) -> None:
        """Write mapping.json atomically. Creates irs_dir if needed.

        When this mapping was bridged from a pre-flip legacy ``mapping.json``
        (``load()`` set ``_legacy_source``), the legacy file is renamed to
        ``mapping.json.migrated-legacy`` AFTER the new file is safely
        written — never before, and only once (so a re-run never re-bridges).
        A rename failure
        is swallowed (advisory, matches ``SetlistManifest``)."""
        self.irs_dir.mkdir(parents=True, exist_ok=True)
        target = self.irs_dir / "mapping.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.entries, indent=2, sort_keys=True))
        os.replace(tmp, target)
        legacy = self._legacy_source
        if legacy is not None and Path(legacy).resolve() != target.resolve():
            try:
                Path(legacy).replace(
                    Path(legacy).with_name(
                        Path(legacy).name + ".migrated-legacy"))
            except OSError:
                pass
            self._legacy_source = None

    def register(self, hash_: str, wav_path: Path, *, force: bool = False) -> None:
        """Bind hash → wav_path. Idempotent for same (hash, file). Raises IrMappingError on conflict unless force=True."""
        wav_path = Path(wav_path)
        if not wav_path.is_file():
            raise FileNotFoundError(f"wav file not found: {wav_path}")
        canonical = self._canonical(wav_path)
        existing = self.entries.get(hash_)
        if existing is not None:
            if existing == canonical:
                return  # idempotent
            if not force:
                raise IrMappingError(
                    f"hash {hash_} is already mapped to {existing}; "
                    f"refusing to overwrite with {canonical} (use force=True)"
                )
        self.entries[hash_] = canonical

    def resolve_by_hash(self, hash_: str) -> Path:
        """Return absolute Path for hash. Raises IrMappingError on miss."""
        if hash_ not in self.entries:
            raise IrMappingError(f"unknown IR hash {hash_}")
        return self._absolute(self.entries[hash_])

    def resolve_by_basename(self, basename: str) -> tuple[str, Path]:
        """Return (hash, absolute_path) for unique basename match.

        Case-sensitive. Raises IrMappingError on ambiguous or missing.
        """
        matches = [
            (h, p) for h, p in self.entries.items() if os.path.basename(p) == basename
        ]
        if not matches:
            raise IrMappingError(f"no registered IR matches basename {basename!r}")
        if len(matches) > 1:
            paths = ", ".join(p for _, p in matches)
            raise IrMappingError(
                f"ambiguous IR basename {basename!r}; matches: {paths}"
            )
        h, p = matches[0]
        return h, self._absolute(p)

    def _absolute(self, stored: str) -> Path:
        p = Path(stored)
        if p.is_absolute():
            return p
        return (self.irs_dir / p).resolve()

    def _canonical(self, wav_path: Path) -> str:
        """Return path relative to irs_dir if under it, else absolute."""
        wav_abs = wav_path.resolve()
        irs_abs = self.irs_dir.resolve()
        try:
            return str(wav_abs.relative_to(irs_abs))
        except ValueError:
            return str(wav_abs)


IR_MODEL_PREFIX = "HX2_ImpulseResponse"


# -- compute_stadium_irhash -------------------------------------------------
#
# Reproduces the IR hash Helix Stadium assigns to a WAV during import.
# Algorithm reverse-engineered from the Mac app binary (see
# memory/project_irhash_algorithm_cracked.md):
#
#   1. libsndfile float-read of source → PCM_24 write to tmp1     (first quant)
#   2. re-read tmp1 as float
#   3. for stereo source, take left channel only
#   4. truncate or zero-pad to next-power-of-2, capped at 8192
#   5. if truncating, apply expf(i * -1/25.6) to the last 128 samples
#   6. write to tmp2 as PCM_24 / 48 kHz / mono                    (second quant)
#   7. MD5 of tmp2's data chunk content → the irhash
#
# The double float→PCM_24 round-trip is load-bearing: libsndfile-1.2.2's
# float-to-PCM_24 quantization introduces a tiny rounding error on certain
# samples, and Stadium's pipeline incurs it twice. Calling libsndfile via
# ctypes hits that path; Python's soundfile wrapper takes a different,
# lossless path that would NOT reproduce Stadium's bytes.


_SFM_READ = 0x10
_SFM_WRITE = 0x20
_SF_FORMAT_WAV = 0x010000
_SF_FORMAT_PCM_S8 = 0x0001
_SF_FORMAT_PCM_16 = 0x0002
_SF_FORMAT_PCM_24 = 0x0003
_SF_FORMAT_PCM_32 = 0x0004
_SFC_SET_ADD_PEAK_CHUNK = 0x1050

_FADE_K = -1.0 / 25.6            # DAT_1011ee7f0 in the binary
_TRUNC_LEN = 0x2000              # 8192
_TRUNC_THRESH = 0x1FFF           # 8191
_FADE_LEN = 128

# Front-door guard before handing a file to libsndfile (which has a CVE
# history parsing malformed audio). IRs are tiny — a 48 kHz mono PCM_24 IR is
# a few hundred KB — but be generous with the cap to accommodate long/stereo
# sources without letting an arbitrarily huge file reach the parser.
_MAX_WAV_BYTES = 64 * 1024 * 1024  # 64 MB


def _validate_wav_front_door(wav_path: Path) -> None:
    """Cheap sanity checks before opening a WAV via libsndfile.

    Rejects oversized files and anything lacking the RIFF/WAVE magic, so an
    arbitrary/malicious blob never reaches libsndfile. Raises ValueError.
    """
    size = wav_path.stat().st_size
    if size > _MAX_WAV_BYTES:
        raise ValueError(
            f"{wav_path} is {size} bytes; refusing files larger than "
            f"{_MAX_WAV_BYTES} bytes"
        )
    with open(wav_path, "rb") as f:
        header = f.read(12)
    if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError(
            f"{wav_path} is not a RIFF/WAVE file (bad magic)"
        )


class _SF_INFO(ctypes.Structure):
    _fields_ = [
        ("frames", ctypes.c_int64),
        ("samplerate", ctypes.c_int),
        ("channels", ctypes.c_int),
        ("format", ctypes.c_int),
        ("sections", ctypes.c_int),
        ("seekable", ctypes.c_int),
    ]


_libsndfile = None
_libsndfile_attempted = False


def _load_libsndfile():
    """Locate and bind libsndfile via ctypes. Returns the CDLL or raises."""
    global _libsndfile, _libsndfile_attempted
    if _libsndfile_attempted:
        if _libsndfile is None:
            raise RuntimeError(
                "libsndfile shared library not found; install it "
                "(macOS: `brew install libsndfile`; "
                "Debian/Ubuntu: `apt install libsndfile1`)"
            )
        return _libsndfile
    _libsndfile_attempted = True
    # search the usual places; ctypes.util.find_library returns None on miss
    candidates = [
        ctypes.util.find_library("sndfile"),
        "/opt/homebrew/opt/libsndfile/lib/libsndfile.dylib",
        "/usr/local/opt/libsndfile/lib/libsndfile.dylib",
        "libsndfile.so.1",
        "libsndfile.dylib",
    ]
    for path in candidates:
        if not path:
            continue
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            continue
        lib.sf_open.restype = ctypes.c_void_p
        lib.sf_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(_SF_INFO)]
        lib.sf_close.argtypes = [ctypes.c_void_p]
        lib.sf_close.restype = ctypes.c_int
        lib.sf_readf_float.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int64]
        lib.sf_readf_float.restype = ctypes.c_int64
        lib.sf_writef_float.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int64]
        lib.sf_writef_float.restype = ctypes.c_int64
        lib.sf_command.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        lib.sf_command.restype = ctypes.c_int
        _libsndfile = lib
        return lib
    raise RuntimeError(
        "libsndfile shared library not found; install it "
        "(macOS: `brew install libsndfile`; "
        "Debian/Ubuntu: `apt install libsndfile1`)"
    )


def _format_for_subtype(subtype: int) -> int:
    """Match Stadium's source-subtype → output-format mapping."""
    if subtype == _SF_FORMAT_PCM_16:
        return _SF_FORMAT_WAV | _SF_FORMAT_PCM_16
    if subtype == _SF_FORMAT_PCM_S8:
        return _SF_FORMAT_WAV | _SF_FORMAT_PCM_S8
    if subtype == _SF_FORMAT_PCM_24:
        return _SF_FORMAT_WAV | _SF_FORMAT_PCM_24
    return _SF_FORMAT_WAV | _SF_FORMAT_PCM_32


def _next_pow2(n: int) -> int:
    if n <= 1:
        return 1
    p = 1
    while p < n:
        p *= 2
    return p


def _render_processed_ir(sf, wav_path: Path, out_path: str) -> None:
    """Run Stadium's import-time IR preprocessing and write the canonical IR to
    ``out_path`` — the exact file the Helix editor uploads on import (source left
    channel, truncated to 8192 samples / padded to next pow-2, exp fade, after
    two libsndfile float round-trips). The device's ``irhash`` is MD5 of this
    file's data chunk, and the device stores an IR *as* this processed file — so
    uploading THIS (not the raw source) is what makes a pushed IR resolve.
    Caller must have validated ``wav_path``.
    """
    tmp1 = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp1.close()
    open_handles: list = []

    def _close_all():
        for h in open_handles:
            try:
                sf.sf_close(h)
            except Exception:
                pass
        open_handles.clear()

    try:
        # --- Phase 1: stream-copy source floats to tmp1 as same bit depth ---
        src_info = _SF_INFO()
        src = sf.sf_open(str(wav_path).encode(), _SFM_READ, ctypes.byref(src_info))
        if not src:
            raise RuntimeError(
                f"libsndfile could not read {wav_path} (not a valid WAV?)"
            )
        open_handles.append(src)
        if src_info.samplerate != 48000:
            raise NotImplementedError(
                f"only 48 kHz sources are supported (got {src_info.samplerate} Hz); "
                "resample to 48 kHz (e.g. `sox in.wav -r 48000 out.wav`) and retry"
            )
        src_subtype = src_info.format & 0xFFFF
        src_channels = src_info.channels
        out_format = _format_for_subtype(src_subtype)

        t1_info = _SF_INFO(0, 48000, src_channels, out_format, 0, 0)
        t1 = sf.sf_open(tmp1.name.encode(), _SFM_WRITE, ctypes.byref(t1_info))
        if not t1:
            raise RuntimeError("libsndfile failed to open tmp1 for write")
        open_handles.append(t1)
        sf.sf_command(t1, _SFC_SET_ADD_PEAK_CHUNK, None, 0)
        chunk = (ctypes.c_float * (1024 * src_channels))()
        while True:
            n = sf.sf_readf_float(src, chunk, 1024)
            if n <= 0:
                break
            sf.sf_writef_float(t1, chunk, n)
        _close_all()

        # --- Phase 2: re-read tmp1, pick left channel, truncate/pad, fade ---
        rd_info = _SF_INFO()
        rd = sf.sf_open(tmp1.name.encode(), _SFM_READ, ctypes.byref(rd_info))
        if not rd:
            raise RuntimeError("libsndfile failed to reopen tmp1 for read")
        open_handles.append(rd)
        n_frames = rd_info.frames
        n_ch = rd_info.channels
        all_buf = (ctypes.c_float * (n_frames * n_ch))()
        sf.sf_readf_float(rd, all_buf, n_frames)
        _close_all()

        # left channel only when stereo (Stadium deinterleaves and discards R)
        mono = (ctypes.c_float * n_frames)()
        if n_ch == 1:
            ctypes.memmove(mono, all_buf, n_frames * ctypes.sizeof(ctypes.c_float))
        else:
            for i in range(n_frames):
                mono[i] = all_buf[i * n_ch]

        # output length: truncate >8191 to 8192, else next pow-2 (with pad)
        if n_frames > _TRUNC_THRESH:
            out_len = _TRUNC_LEN
        elif (n_frames & (n_frames - 1)) == 0 and n_frames > 0:
            out_len = n_frames
        else:
            out_len = _next_pow2(n_frames)

        out = (ctypes.c_float * out_len)()
        copy_n = min(n_frames, out_len)
        ctypes.memmove(out, mono, copy_n * ctypes.sizeof(ctypes.c_float))

        # exp fade-out applies only when source filled the buffer (truncation
        # / exact pow-2 cases); when zero-padding, the fade is skipped.
        if n_frames >= out_len:
            for i in range(_FADE_LEN):
                out[out_len - _FADE_LEN + i] *= math.exp(i * _FADE_K)

        # --- Phase 3: write the processed IR to out_path (48k / mono) ---
        t2_info = _SF_INFO(0, 48000, 1, out_format, 0, 0)
        t2 = sf.sf_open(str(out_path).encode(), _SFM_WRITE, ctypes.byref(t2_info))
        if not t2:
            raise RuntimeError("libsndfile failed to open output for write")
        open_handles.append(t2)
        sf.sf_command(t2, _SFC_SET_ADD_PEAK_CHUNK, None, 0)
        sf.sf_writef_float(t2, out, out_len)
        _close_all()
    finally:
        _close_all()
        try:
            os.unlink(tmp1.name)
        except OSError:
            pass


def _md5_wav_data_chunk(wav_path: str) -> str:
    """MD5 (hex) of a WAV's ``data`` chunk content — the Stadium irhash of a
    processed IR file."""
    with open(wav_path, "rb") as f:
        raw = f.read()
    di = raw.find(b"data")
    if di < 0:
        raise RuntimeError("no data chunk in WAV output")
    sz = struct.unpack("<I", raw[di + 4:di + 8])[0]
    return hashlib.md5(raw[di + 8:di + 8 + sz]).hexdigest()


def write_stadium_ir(wav_path: Path | str, out_path: Path | str) -> str:
    """Write the device-canonical processed IR for ``wav_path`` to ``out_path``
    and return its ``irhash``.

    This is the file the Helix editor uploads on import — the source truncated to
    the Stadium's 8192-sample IR (with fade); its data-chunk MD5 IS the irhash,
    and the device stores the IR as exactly this file. Upload THIS to the device
    (not the raw WAV) so the device registers the IR under the hash helixgen and
    presets reference.
    """
    sf = _load_libsndfile()
    wav_path = Path(wav_path)
    if not wav_path.is_file():
        raise FileNotFoundError(f"wav file not found: {wav_path}")
    _validate_wav_front_door(wav_path)
    _render_processed_ir(sf, wav_path, str(out_path))
    irhash = _md5_wav_data_chunk(str(out_path))
    _insert_hash_chunk(str(out_path), irhash)
    return irhash


def _insert_hash_chunk(wav_path: str, irhash_hex: str) -> None:
    """Insert a ``HASH`` chunk (the 32-char ASCII hex of the irhash) after the
    ``fmt `` chunk, matching the file the Helix editor uploads on IR import.

    The device reads this chunk and registers the IR under exactly this hash
    instead of recomputing its own — so an externally-uploaded IR resolves the
    preset that references helixgen's ``irhash``. Without it, the device assigns
    its own (different) hash to the write.
    """
    with open(wav_path, "rb") as f:
        raw = f.read()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return  # not a canonical WAV; leave it alone
    insert_at = len(raw)
    p = 12
    while p + 8 <= len(raw):
        cid = raw[p:p + 4]
        sz = struct.unpack("<I", raw[p + 4:p + 8])[0]
        end = p + 8 + sz + (sz & 1)  # chunks are word-aligned
        if cid == b"fmt ":
            insert_at = end
            break
        p = end
    chunk = b"HASH" + struct.pack("<I", 32) + irhash_hex.encode("ascii")
    new = raw[:insert_at] + chunk + raw[insert_at:]
    new = new[:4] + struct.pack("<I", len(new) - 8) + new[8:]  # fix RIFF size
    with open(wav_path, "wb") as f:
        f.write(new)


def compute_stadium_irhash(wav_path: Path | str) -> str:
    """Return the 32-char hex IR hash Helix Stadium would assign to this WAV.

    Reproduces Stadium's import-time preprocessing pipeline (see module
    comments for the full algorithm) and computes MD5 of the resulting
    data chunk content. Output is the same hash that appears in `.hsp`
    presets in the slot's `irhash` field.

    Currently supports 48 kHz sources (the fast path Stadium takes for
    most IR libraries). Non-48 kHz sources go through libsamplerate in
    Stadium and are not yet supported here. Stereo input is reduced to
    the left channel, matching Stadium's import behavior.
    """
    sf = _load_libsndfile()
    wav_path = Path(wav_path)
    if not wav_path.is_file():
        raise FileNotFoundError(f"wav file not found: {wav_path}")
    _validate_wav_front_door(wav_path)

    tmp2 = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp2.close()
    try:
        _render_processed_ir(sf, wav_path, tmp2.name)
        return _md5_wav_data_chunk(tmp2.name)
    finally:
        try:
            os.unlink(tmp2.name)
        except OSError:
            pass


def extract_ir_hashes(preset_body: dict) -> list[str]:
    """Return slot-level irhash values from a .hsp body dict, in (path, position) order.

    Blocks whose `slot[0].model` does not start with HX2_ImpulseResponse are ignored.
    """
    hashes: list[tuple[int, int, str]] = []
    for path_obj in preset_body.get("preset", {}).get("flow", []):
        if not isinstance(path_obj, dict):
            continue
        for v in path_obj.values():
            if not isinstance(v, dict) or "slot" not in v:
                continue
            slot_list = v["slot"]
            if not isinstance(slot_list, list) or not slot_list or not isinstance(slot_list[0], dict):
                continue
            slot = slot_list[0]
            if not str(slot.get("model", "")).startswith(IR_MODEL_PREFIX):
                continue
            if "irhash" not in slot:
                continue
            hashes.append((int(v.get("path", 0)), int(v.get("position", 0)), slot["irhash"]))
    hashes.sort()
    return [h for _, _, h in hashes]
