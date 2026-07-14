"""Global-settings (device *property*) codec + catalog.

The Stadium exposes its global settings — and various live values — as
**properties** in a dotted namespace (``global.*``, ``dsp.globaleq.*``,
``preset.*``, ``volatile.*``). Each property has a **definition** (name, type,
range, enum labels, default) and a **current value**. Both travel over the 2002
RPC channel as ``msgpack`` blobs with a distinctive dialect: the 4-character
field names are encoded as msgpack ``uint32`` (their ASCII big-endian value),
not as strings.

Protocol (hardware-reverse-engineered 2026-07-13, see
``docs/superpowers/specs/2026-07-13-global-settings-re-findings.md``):

- read current value   ``/PropertyValueGet [reqid, key:s]`` → value blob
- read definition      ``/PropertyDefWithKeyGet [reqid, key:s]`` → def blob
- write value          ``/PropertyValueSet [reqid, ctx:i=0, valueblob:b]`` → ``/success``

This module is the **codec** (pure, device-free, unit-tested against golden
blobs). The :class:`~helixgen.device.client.HelixClient` methods that send these
commands live in ``client.py`` and call in here.
"""
from __future__ import annotations

import json
import math
import os
import struct
from typing import Any, Dict, List, NamedTuple, Optional

_INT64_MIN, _INT64_MAX = -(2 ** 63), 2 ** 63 - 1

# Keys whose write would sever this very (network) control channel or lock the
# device out of remote control — refused by :func:`guard_key` so neither a
# catalog browse nor a direct `settings set` can strand the device. (Change
# these on the device's own touchscreen.)
DANGEROUS_KEYS = frozenset({
    "global.wifi.enable",       # disabling WiFi kills the LAN transport
    "global.remote.access",     # disabling remote access locks out all clients
})

_PAGES_FILE = os.path.join(os.path.dirname(__file__), "_settings_pages.json")
_PAGES_CACHE: Optional[Dict[str, List[str]]] = None


def pages() -> Dict[str, List[str]]:
    """Curated ``page-name -> [property key]`` catalog of the device's Global
    Settings, grouped like the app's Global Settings screens. Sourced from the
    app binary's ``global.*`` namespace; the device supplies each key's name,
    type, range, and enum labels via :meth:`HelixClient.get_property_def`."""
    global _PAGES_CACHE
    if _PAGES_CACHE is None:
        with open(_PAGES_FILE) as f:
            _PAGES_CACHE = json.load(f)
    # return a fresh copy so a mutating caller can't poison the process cache
    return {k: list(v) for k, v in _PAGES_CACHE.items()}


def page_names() -> List[str]:
    return sorted(pages())


def keys_for_page(page: str) -> List[str]:
    """Keys on ``page``; raises :class:`KeyError` for an unknown page."""
    p = pages()
    if page not in p:
        raise KeyError(page)
    return list(p[page])


def guard_key(key: str) -> None:
    """Raise :class:`ValueError` for a write that would strand the device."""
    if key in DANGEROUS_KEYS:
        raise ValueError(
            f"refusing to set {key!r}: changing it over the network can sever "
            "this control channel and lock out remote access. Change it on the "
            "device's own screen."
        )


def all_keys() -> List[str]:
    out: List[str] = []
    for ks in pages().values():
        out.extend(ks)
    return sorted(out)


def page_for_key(key: str) -> Optional[str]:
    for page, ks in pages().items():
        if key in ks:
            return page
    return None

# 8-byte magics
VALUE_MAGIC = b"lavppgsm"   # property *value* blob
DEF_MAGIC = b"fedppgsm"     # property *definition* blob


def _require_msgpack():
    try:
        import msgpack
        return msgpack
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "the device settings codec needs msgpack; install with "
            "`pip install 'helixgen[device]'`"
        ) from exc


def _u32(name: str) -> int:
    """The device's uint32 encoding of a 4-char msgpack field name."""
    return struct.unpack(">I", name.encode("ascii"))[0]


# value-blob field keys
K_KEY = _u32("key_")
K_TYPE = _u32("type")
K_VAL = _u32("val_")
# def-blob field keys
K_DVAL = _u32("dval")
K_NAME = _u32("name")
K_SHRT = _u32("shrt")
K_UNTS = _u32("unts")
K_VMAX = _u32("vmax")
K_VMIN = _u32("vmin")
K_VNME = _u32("vnme")
K_ID = _u32("id__")


class PropertyValue(NamedTuple):
    """A property's current value."""
    key: str
    type: str           # 'f' (float) or 'i' (int)
    value: Any          # float or int


class PropertyDef(NamedTuple):
    """A property's definition (the self-describing catalog entry)."""
    key: str
    name: str           # display name (newlines collapsed to spaces)
    short: str
    type: str           # 'f' or 'i'
    vmin: Any
    vmax: Any
    default: Any
    enum: List[str]     # value labels for enum props (empty if continuous)
    units: int
    id: Optional[int]


def encode_value_blob(key: str, typ: str, value: Any) -> bytes:
    """Build a property *value* blob byte-for-byte as the app does.

    ``typ`` is ``'f'`` (packs ``value`` as msgpack float64) or ``'i'`` (packs as
    int). The map is ``{key_, type, val_}`` with uint32 field keys.
    """
    if typ not in ("f", "i"):
        raise ValueError(f"unsupported property type {typ!r} (want 'f' or 'i')")
    msgpack = _require_msgpack()
    if typ == "f":
        val: Any = float(value)
        if not math.isfinite(val):
            raise ValueError(f"{key}: non-finite value {value!r} not allowed")
    else:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{key}: {value!r} is not an integer")
        val = int(value)
        if not _INT64_MIN <= val <= _INT64_MAX:
            raise ValueError(f"{key}: {val} outside 64-bit integer range")
    payload = msgpack.packb(
        {K_KEY: key, K_TYPE: typ, K_VAL: val}, use_single_float=False)
    return VALUE_MAGIC + payload


def _unpack(blob: bytes, magic: bytes) -> Dict[int, Any]:
    if blob[:8] != magic:
        raise ValueError(
            f"blob magic {blob[:8]!r} != expected {magic!r}")
    msgpack = _require_msgpack()
    try:
        m = msgpack.unpackb(blob[8:], raw=False, strict_map_key=False)
    except Exception as exc:  # msgpack errors subclass ValueError, but be explicit
        raise ValueError(f"undecodable property blob: {exc}") from exc
    if not isinstance(m, dict):
        raise ValueError(f"property blob body is {type(m).__name__}, not a map")
    return m


def decode_value_blob(blob: bytes) -> PropertyValue:
    """Decode a ``lavppgsm`` value blob into a :class:`PropertyValue`."""
    m = _unpack(blob, VALUE_MAGIC)
    typ = m.get(K_TYPE, "f")
    return PropertyValue(key=m.get(K_KEY, ""), type=typ, value=m.get(K_VAL))


def decode_property_def(blob: bytes) -> PropertyDef:
    """Decode a ``fedppgsm`` definition blob into a :class:`PropertyDef`."""
    m = _unpack(blob, DEF_MAGIC)
    dval = m.get(K_DVAL) if isinstance(m.get(K_DVAL), dict) else {}
    default = dval.get(K_VAL)
    typ = dval.get(K_TYPE, "f")
    if typ not in ("f", "i"):
        typ = "f"
    raw_name = m.get(K_NAME)
    name = (raw_name if isinstance(raw_name, str) else "").replace("\n", " ")
    vnme = m.get(K_VNME)
    enum = [str(x) for x in vnme] if isinstance(vnme, list) else []
    return PropertyDef(
        key=m.get(K_KEY) or dval.get(K_KEY) or "",
        name=name,
        short=m.get(K_SHRT) or "",
        type=typ,
        vmin=m.get(K_VMIN),
        vmax=m.get(K_VMAX),
        default=default,
        enum=enum,
        units=m.get(K_UNTS, 0),
        id=m.get(K_ID),
    )


def coerce_value(pdef: PropertyDef, raw: str) -> Any:
    """Coerce a user-supplied string ``raw`` into the value this property wants,
    validating against the definition. Accepts an enum label (case-insensitive)
    or its index for enum props; a number within ``[vmin, vmax]`` otherwise.
    Raises :class:`ValueError` with a helpful message on any mismatch.
    """
    if pdef.enum:
        # match a label (case-insensitive) first, then a bare index
        for i, label in enumerate(pdef.enum):
            if raw.strip().lower() == label.lower():
                return i
        try:
            idx = int(raw)
        except ValueError:
            raise ValueError(
                f"{pdef.key}: {raw!r} is not one of {pdef.enum}")
        if not 0 <= idx < len(pdef.enum):
            raise ValueError(
                f"{pdef.key}: index {idx} out of range for {pdef.enum}")
        return idx
    if pdef.type == "i":
        try:
            val: Any = int(raw)
        except ValueError:
            raise ValueError(f"{pdef.key}: {raw!r} is not an integer")
        if not _INT64_MIN <= val <= _INT64_MAX:
            raise ValueError(f"{pdef.key}: {val} outside 64-bit integer range")
    else:
        try:
            val = float(raw)
        except ValueError:
            raise ValueError(f"{pdef.key}: {raw!r} is not a number")
        if not math.isfinite(val):
            raise ValueError(f"{pdef.key}: {raw!r} is not a finite number")
    if pdef.vmin is not None and val < pdef.vmin:
        raise ValueError(f"{pdef.key}: {val} below min {pdef.vmin}")
    if pdef.vmax is not None and val > pdef.vmax:
        raise ValueError(f"{pdef.key}: {val} above max {pdef.vmax}")
    return val


def render_value(pdef: PropertyDef, value: Any) -> str:
    """Human string for a current value: an enum label when applicable."""
    if pdef.enum and isinstance(value, int) and 0 <= value < len(pdef.enum):
        return f"{pdef.enum[value]} ({value})"
    return str(value)
