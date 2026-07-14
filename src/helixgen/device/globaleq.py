"""Global EQ (device ``dsp.globaleq.*`` property) codec + catalog.

The Stadium has **three independent Global EQs** — one per physical output
layer: **1/4"** (``qtr``), **XLR** (``xlr``), and **Phones** (``pho``). Each is a
7-band EQ (low-cut, low-shelf, low, mid, high, high-shelf, high-cut) plus an
output level. The Helix Stadium desktop app exposes them on its *Global EQ* view.

Unlike the plain ``global.*`` settings (:mod:`helixgen.device.settings`), a
Global EQ band parameter is written with a **variant** value blob whose payload
is an indexed ``{parm, valu}`` sub-map, not a bare scalar:

    /PropertyValueSet [reqid, ctx=0, blob]
    blob = "lavppgsm" + msgpack{ key_: "dsp.globaleq.<out>.<band>.<param>",
                                 type: "v",
                                 val_: { parm: <slot>, valu: <value> } }

(4-char field names are msgpack ``uint32`` of their ASCII bytes, as elsewhere.)

Protocol reverse-engineered + **hardware write-validated 2026-07-14** (all three
outputs, all seven band names accepted with ``/success`` code 0); byte-exact
against the desktop app's own writes (golden blobs in
``tests/fixtures/globaleq/``). See
``docs/superpowers/specs/2026-07-14-parity-capture-findings.md`` and
``docs/helix-protocol.md`` §11.

**Read caveat:** ``dsp.globaleq.*`` keys do **not** answer ``/PropertyValueGet``
(the device returns an empty blob — the app gets EQ state from the connect-time
sync, not a per-key read). So this module is **write + offline-catalog** only;
there is no reliable network read-back yet (tracked in ``docs/BACKLOG.md``).

This module is the pure, device-free codec/catalog (unit-tested against golden
blobs). The :class:`~helixgen.device.client.HelixClient` method that sends the
command lives in ``client.py`` and calls in here.
"""
from __future__ import annotations

import math
import struct
from typing import Any, Dict, List, NamedTuple, Tuple

VALUE_MAGIC = b"lavppgsm"  # same property-value magic as settings.py

# Output layers: key -> display name. Order = app's Global EQ layer tabs.
OUTPUTS: Dict[str, str] = {
    "qtr": '1/4"',
    "xlr": "XLR",
    "pho": "Phones",
}


class Param(NamedTuple):
    slot: int          # the `parm` index in the {parm, valu} sub-map
    type: str          # 'f' (float), 'i' (int), 'b' (bool)
    unit: str          # human unit label ("Hz", "dB", "", ...)


# The five band-parameter kinds and their fixed `parm` slot ids (from the
# device's full `globals.eq` snapshot; hardware-confirmed).
PARAMS: Dict[str, Param] = {
    "enable": Param(1, "b", ""),      # band on/off
    "freq": Param(2, "f", "Hz"),      # centre / corner frequency
    "gain": Param(3, "f", "dB"),      # shelf/peak gain
    "q": Param(4, "f", ""),           # peaking-band Q
    "slope": Param(5, "i", ""),       # cut-filter slope (int)
}

# Output level is a distinct top-level param (`dsp.globaleq.<out>.level`,
# slot 3), not a band. Kept separate because it has no band segment.
LEVEL_PARAM = Param(3, "f", "dB")

# Bands: name -> (numeric index, set of valid param names).
# Indices + per-band param sets are from the device snapshot (hardware-confirmed
# 2026-07-14). Cut filters (lowcut/highcut) have slope, no gain/Q; shelves have
# gain, no Q; peaking bands (low/mid/high) have gain + Q.
class Band(NamedTuple):
    index: int
    params: Tuple[str, ...]


BANDS: Dict[str, Band] = {
    "lowcut":    Band(0, ("enable", "freq", "slope")),
    "lowshelf":  Band(1, ("enable", "freq", "gain")),
    "low":       Band(2, ("enable", "freq", "gain", "q")),
    "mid":       Band(3, ("enable", "freq", "gain", "q")),
    "high":      Band(4, ("enable", "freq", "gain", "q")),
    "highshelf": Band(5, ("enable", "freq", "gain")),
    "highcut":   Band(6, ("enable", "freq", "slope")),
}

# Factory-default centre/corner frequency per band (Hz), from the snapshot —
# used only for display/`list`, not enforced.
DEFAULT_FREQ: Dict[str, float] = {
    "lowcut": 20.0, "lowshelf": 80.0, "low": 150.0, "mid": 2000.0,
    "high": 5000.0, "highshelf": 8000.0, "highcut": 20000.0,
}


def _require_msgpack():
    try:
        import msgpack
        return msgpack
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "the Global EQ codec needs msgpack; install with "
            "`pip install 'helixgen[device]'`"
        ) from exc


def _u32(name: str) -> int:
    return struct.unpack(">I", name.encode("ascii"))[0]


K_KEY = _u32("key_")
K_TYPE = _u32("type")
K_VAL = _u32("val_")
K_PARM = _u32("parm")
K_VALU = _u32("valu")


def outputs() -> List[str]:
    return list(OUTPUTS)


def band_names() -> List[str]:
    return list(BANDS)


def normalize(output: str, band: str, param: str) -> Tuple[str, str, str]:
    """Lower-case + validate an (output, band, param) triple. Returns the
    canonical spellings. Raises :class:`ValueError` naming the valid set."""
    o, b, p = output.strip().lower(), band.strip().lower(), param.strip().lower()
    if o not in OUTPUTS:
        raise ValueError(f"unknown output {output!r}; want one of {outputs()}")
    if p == "level":
        return o, "", "level"
    if b not in BANDS:
        raise ValueError(f"unknown band {band!r}; want one of {band_names()}")
    if p not in PARAMS:
        raise ValueError(
            f"unknown param {param!r}; want one of {list(PARAMS)} or 'level'")
    if p not in BANDS[b].params:
        raise ValueError(
            f"band {b!r} has no {p!r} param; valid: {list(BANDS[b].params)}")
    return o, b, p


def key_for(output: str, band: str, param: str) -> str:
    """The dotted property key. ``param=='level'`` yields the output-level key
    (no band segment)."""
    o, b, p = normalize(output, band, param)
    if p == "level":
        return f"dsp.globaleq.{o}.level"
    return f"dsp.globaleq.{o}.{b}.{p}"


def coerce_value(param: str, raw: Any) -> Any:
    """Coerce ``raw`` (str/number/bool) into the value the given param wants."""
    spec = LEVEL_PARAM if param == "level" else PARAMS[param]
    if spec.type == "b":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "on", "yes"):
            return True
        if s in ("0", "false", "off", "no"):
            return False
        raise ValueError(f"{param}: {raw!r} is not a boolean (true/false)")
    if spec.type == "i":
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{param}: {raw!r} is not an integer")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{param}: {raw!r} is not a number")
    if not math.isfinite(v):
        raise ValueError(f"{param}: {raw!r} is not finite")
    return v


def encode_value_blob(output: str, band: str, param: str, value: Any) -> bytes:
    """Build the byte-exact ``lavppgsm`` variant blob for a Global EQ write.

    Mirrors the desktop app's ``/PropertyValueSet`` payload byte-for-byte
    (golden-tested). ``value`` is coerced to the param's type.
    """
    o, b, p = normalize(output, band, param)
    val = coerce_value(p, value)
    slot = (LEVEL_PARAM if p == "level" else PARAMS[p]).slot
    key = key_for(o, b, p)
    msgpack = _require_msgpack()
    inner = {K_PARM: slot, K_VALU: val}
    payload = msgpack.packb(
        {K_KEY: key, K_TYPE: "v", K_VAL: inner}, use_single_float=False)
    return VALUE_MAGIC + payload


def catalog() -> List[Dict[str, Any]]:
    """Offline catalog for ``device globaleq list`` — every output×band and its
    valid params, plus each output's level."""
    rows: List[Dict[str, Any]] = []
    for o, oname in OUTPUTS.items():
        for bname, band in BANDS.items():
            rows.append({
                "output": o, "output_name": oname,
                "band": bname, "band_index": band.index,
                "params": list(band.params),
                "default_freq": DEFAULT_FREQ.get(bname),
            })
        rows.append({
            "output": o, "output_name": oname,
            "band": "", "band_index": None,
            "params": ["level"], "default_freq": None,
        })
    return rows
