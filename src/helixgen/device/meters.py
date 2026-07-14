"""Network level meters — decode the Stadium's live meter telemetry.

The same ``/dspEvent`` burst on port 2003 that carries the network-tuner pitch
scalar (see :mod:`helixgen.device.tuner`) also carries two **grid-level meter**
arrays: ``{eid_:1, mid_:796}`` and ``{eid_:1, mid_:800}``, each a **128-float**
array (observed range ~0.0-0.08). The exact semantic split between the two
mids (input/output, path 1/path 2, pre/post, …) was not characterized in the
2026-07-14 capture, so this module exposes both by their raw ``mid_`` rather
than guessing a label.

Because this rides the same always-on telemetry stream as the tuner, helixgen
can show live level meters over the network with the Stadium app closed and
without engaging anything on the device — just subscribe to 2003 and decode.

See ``docs/superpowers/specs/2026-07-14-parity-capture-findings.md`` §4.

This module is the pure decoder (unit-tested against synthetic ``/dspEvent``
maps in the same shape as the tuner's golden fixture). The ``device meters``
CLI / ``device_meters`` MCP tool subscribe and call in here.
"""
from __future__ import annotations

import struct
from typing import Any, Dict, List, NamedTuple, Optional

# The /dspEvent identity shared by both meter arrays; mid_ distinguishes them.
METER_EID = 1
METER_MIDS = (796, 800)


def _u32(name: str) -> int:
    return struct.unpack(">I", name.encode("ascii"))[0]


_K_ID = _u32("id__")
_K_EID = _u32("eid_")
_K_MID = _u32("mid_")
_K_VALS = _u32("vals")


def _get(d: Dict[Any, Any], u32key: int, strkey: str, default=None):
    """Look a field up by either its uint32-encoded 4-char key or its string
    key (msgpack decoders differ on which they surface)."""
    if u32key in d:
        return d[u32key]
    if strkey in d:
        return d[strkey]
    return default


class MeterReading(NamedTuple):
    """One decoded meter array. ``mid`` identifies which of the two streams
    (796/800) this is; ``values`` is the raw 128-float grid; ``peak`` is
    ``max(values)`` (``0.0`` for an empty array)."""
    mid: int
    values: List[float]
    peak: float


def is_meter_map(payload: Any) -> bool:
    """True if a decoded ``/dspEvent`` payload is one of the meter events."""
    if not isinstance(payload, dict):
        return False
    idd = _get(payload, _K_ID, "id__")
    if not isinstance(idd, dict):
        return False
    return (_get(idd, _K_EID, "eid_") == METER_EID
            and _get(idd, _K_MID, "mid_") in METER_MIDS)


def reading_from_map(payload: Dict[Any, Any]) -> Optional[MeterReading]:
    """Decode one meter ``/dspEvent`` payload into a :class:`MeterReading`, or
    ``None`` if it is not a meter event."""
    if not is_meter_map(payload):
        return None
    idd = _get(payload, _K_ID, "id__")
    mid = _get(idd, _K_MID, "mid_")
    vals = _get(payload, _K_VALS, "vals")
    if not isinstance(vals, (list, tuple)):
        return None
    try:
        values = [float(v) for v in vals]
    except (TypeError, ValueError):
        return None
    peak = max(values) if values else 0.0
    return MeterReading(mid=int(mid), values=values, peak=peak)


def readings_from_event_args(args: Any) -> List[MeterReading]:
    """Given a subscribed ``/dspEvent`` Event's decoded ``args`` list, return
    every :class:`MeterReading` found (a single burst may carry 0, 1, or both
    mids — plus the unrelated pitch event, which is skipped)."""
    if not isinstance(args, (list, tuple)):
        return []
    out: List[MeterReading] = []
    for a in args:
        r = reading_from_map(a) if isinstance(a, dict) else None
        if r is not None:
            out.append(r)
    return out
