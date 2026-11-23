"""Matrix Mixer (device ``server.mixer.state`` property) decoder.

The **Matrix Mixer** is the Stadium's device-global, per-physical-output
**monitor mix** — one layer each for **1/4"**, **XLR** and **Phones**, mixing
the non-preset sources (the four Home paths, eight Song tracks, Nexus returns,
USB In, Aux, Bluetooth, Click, Count-In). It is **not** the preset Output block
(`P35_OutputMatrix`, per-preset gain/pan in ``.hsp``) — same word, unrelated
surface.

**Readable over the LAN** (contra the earlier RE spike, which globbed the wrong
keys): the whole mix is a single ``type:"v"`` property, **``server.mixer.state``**,
fetched with ``/PropertyValueGet`` like any Global Setting. The value blob is the
usual ``lavppgsm`` + msgpack, but ``val_`` holds a nested **4-char-code-keyed**
map (uint32-packed ASCII keys, the dialect-B convention) rather than a scalar::

    val_ = { atto, lom_, lmix:{…one layer…}, lyrs:[ …one per output… ] }

    one layer = { out_:{dst_,name,vol_,mute}, mute, solo,
                  home:{chan:[strip×4], mix_:strip},
                  song:{chan:[strip×8], mix_:strip},
                  nxus:[strip×4], usbn, auxn, bt__, clik, cntn,  # source strips
                }
    strip = { name, vol_(dB), pan_, mute, solo, enbl(-1 on / 0 off), src_ }

``lyrs`` are the three per-output layers (``out_.name`` = 1/4" / XLR / Phones,
``dst_`` 1/2/3); ``lmix`` is the "Link Mix" layer (``dst_`` 0) used when outputs
are linked.

**Read-only.** The write path (``/PropertyValueSet server.mixer.state``) is
unproven — no write code ships here. HW-decoded on a Stadium XL (fw 1.4.1,
2026-11-22); golden blob in ``tests/fixtures/mixer/``.
"""
from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional

MIXER_KEY = "server.mixer.state"
_MAGIC = b"lavppgsm"  # property value blob (same as settings.VALUE_MAGIC)

# Source strip groups, in the hardware's channel-strip order. ``home``/``song``
# carry a ``chan`` list plus a ``mix_`` summary strip; the rest are single
# strips (``nxus`` is a list of 4). A group absent from a layer is skipped.
_GROUP_ORDER = ("home", "song", "nxus", "usbn", "auxn", "bt__", "clik", "cntn")


def _require_msgpack():
    try:
        import msgpack
        return msgpack
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "the mixer codec needs msgpack; install with "
            "`pip install 'helixgen[device]'`"
        ) from exc


def _tag(key: Any) -> Any:
    """Turn a uint32-packed 4-char-code map key back into its ASCII tag."""
    if isinstance(key, int) and 0 <= key <= 0xFFFFFFFF:
        return struct.pack(">I", key).decode("ascii", "replace")
    return key


def _translate(obj: Any) -> Any:
    """Recursively rewrite uint32 map keys to their 4-char tags."""
    if isinstance(obj, dict):
        return {_tag(k): _translate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_translate(v) for v in obj]
    return obj


def _strip(s: Dict[str, Any], group: str) -> Dict[str, Any]:
    """One channel strip → a flat, JSON-friendly row for the UI."""
    return {
        "group": group,
        "name": s.get("name"),
        "vol_db": s.get("vol_"),
        "pan": s.get("pan_"),
        "mute": bool(s.get("mute")),
        "solo": bool(s.get("solo")),
        # enbl is the device's tri-bool: -1 = on, 0 = off/greyed (e.g. Aux with
        # no Aux-In routed, Bluetooth with nothing paired).
        "enabled": bool(s.get("enbl")),
    }


def _channels(layer: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in _GROUP_ORDER:
        g = layer.get(group)
        if isinstance(g, dict) and isinstance(g.get("chan"), list):
            rows += [_strip(s, group) for s in g["chan"] if isinstance(s, dict)]
            summary = g.get("mix_")
            if isinstance(summary, dict):
                rows.append(_strip(summary, group))
        elif isinstance(g, list):
            rows += [_strip(s, group) for s in g if isinstance(s, dict)]
        elif isinstance(g, dict):
            rows.append(_strip(g, group))
    return rows


def _layer(layer: Dict[str, Any]) -> Dict[str, Any]:
    out = layer.get("out_") if isinstance(layer.get("out_"), dict) else {}
    return {
        "output": out.get("name"),
        "dst": out.get("dst_"),
        "master_db": out.get("vol_"),
        "master_mute": bool(out.get("mute")),
        "mute": bool(layer.get("mute")),
        "solo": bool(layer.get("solo")),
        "channels": _channels(layer),
    }


def decode_mixer_state(blob: bytes) -> Dict[str, Any]:
    """Decode a ``server.mixer.state`` value blob into the normalized shape::

        { "layers": [ {output, dst, master_db, master_mute, mute, solo,
                       channels:[{group,name,vol_db,pan,mute,solo,enabled}]} … ],
          "link_mix": { …same shape… } | None }

    Raises :class:`ValueError` on a bad magic or a blob with no ``val_`` map.
    """
    if blob[:8] != _MAGIC:
        raise ValueError(f"mixer blob magic {blob[:8]!r} != expected {_MAGIC!r}")
    msgpack = _require_msgpack()
    try:
        body = msgpack.unpackb(blob[8:], raw=False, strict_map_key=False)
    except Exception as exc:  # msgpack errors subclass ValueError; be explicit
        raise ValueError(f"undecodable mixer blob: {exc}") from exc
    m = _translate(body)
    val = m.get("val_") if isinstance(m, dict) else None
    if not isinstance(val, dict):
        raise ValueError("mixer state blob has no val_ map")
    layers = [_layer(L) for L in val.get("lyrs", []) if isinstance(L, dict)]
    link: Optional[Dict[str, Any]] = (
        _layer(val["lmix"]) if isinstance(val.get("lmix"), dict) else None)
    return {"layers": layers, "link_mix": link}
