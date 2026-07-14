"""Network tuner — decode the Stadium's live pitch telemetry.

The Stadium runs a **continuous background pitch detector** and streams its
readout on port **2003** as a ``/dspEvent`` whose msgpack payload is
``{id__:{eid_:10, mid_:796}, vals:[<float>]}``. The float is a **fractional MIDI
note number**: the integer part is the MIDI note, and the fractional part × 100
is the **cents** offset. ``-1.0`` is the no-signal sentinel.

Because the detector is always live (it is not gated on the hardware tuner
screen), helixgen can show a tuner over the network with the Stadium app closed
and without engaging anything on the device — just subscribe to 2003 and decode.

Reverse-engineered + hardware-verified 2026-07-14 (a slightly-flat low-E read
39.90 → E2 −10 cents = 81.93 Hz). See
``docs/superpowers/specs/2026-07-14-parity-capture-findings.md`` §4.

This module is the pure decoder (unit-tested against a golden dspEvent). The
``device tuner`` CLI / ``device_tuner`` MCP tool subscribe and call in here.
"""
from __future__ import annotations

import struct
from typing import Any, Dict, NamedTuple, Optional

# The /dspEvent identity that carries the pitch scalar.
PITCH_EID = 10
PITCH_MID = 796
NO_SIGNAL = -1.0

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# reference for A4 = MIDI 69 = 440 Hz (the actual device ref pitch is a global
# setting; this is the standard default used for the Hz readout)
_A4_MIDI = 69
_A4_HZ = 440.0


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


class TunerReading(NamedTuple):
    """A decoded pitch reading. ``signal`` is False when the detector reports no
    pitch (silence); the note/cents/hz fields are then meaningless."""
    signal: bool
    note: str           # "E", "A#", …  ("" when no signal)
    octave: int         # scientific-pitch octave (E2 → 2)
    cents: int          # −50..+50, signed offset from the nearest note
    midi: float         # raw fractional MIDI note
    hz: float           # frequency in Hz

    @property
    def name(self) -> str:
        """Human note name with octave, e.g. ``"E2"`` (``"—"`` when no signal)."""
        return "—" if not self.signal else f"{self.note}{self.octave}"


def reading_from_midi(midi: float) -> TunerReading:
    """Convert a fractional MIDI note into a :class:`TunerReading`."""
    if midi is None or midi <= 0:
        return TunerReading(False, "", 0, 0, float(midi or -1.0), 0.0)
    nearest = round(midi)
    cents = int(round((midi - nearest) * 100))
    note = NOTE_NAMES[nearest % 12]
    octave = nearest // 12 - 1
    hz = _A4_HZ * 2.0 ** ((midi - _A4_MIDI) / 12.0)
    return TunerReading(True, note, octave, cents, float(midi), hz)


def is_pitch_map(payload: Any) -> bool:
    """True if a decoded ``/dspEvent`` payload is the pitch event."""
    if not isinstance(payload, dict):
        return False
    idd = _get(payload, _K_ID, "id__")
    if not isinstance(idd, dict):
        return False
    return (_get(idd, _K_EID, "eid_") == PITCH_EID
            and _get(idd, _K_MID, "mid_") == PITCH_MID)


def pitch_from_map(payload: Dict[Any, Any]) -> Optional[float]:
    """Extract the raw fractional-MIDI float from a pitch ``/dspEvent`` payload,
    or ``None`` if it is not a pitch event or has no value."""
    if not is_pitch_map(payload):
        return None
    vals = _get(payload, _K_VALS, "vals")
    if not isinstance(vals, (list, tuple)) or not vals:
        return None
    try:
        return float(vals[0])
    except (TypeError, ValueError):
        return None


def reading_from_event_args(args: Any) -> Optional[TunerReading]:
    """Given a subscribed ``/dspEvent`` Event's decoded ``args`` list, return a
    :class:`TunerReading` if it is the pitch event, else ``None``. A no-signal
    (``-1.0``) reading returns a ``signal=False`` reading (not ``None``)."""
    if not isinstance(args, (list, tuple)):
        return None
    for a in args:
        if is_pitch_map(a):
            midi = pitch_from_map(a)
            if midi is None or midi == NO_SIGNAL:
                return TunerReading(False, "", 0, 0, NO_SIGNAL, 0.0)
            return reading_from_midi(midi)
    return None
