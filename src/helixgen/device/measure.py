"""Playing-gated loudness measurement over the 2003 telemetry stream.

Phase 1 of the loudness-feedback spec
(``docs/superpowers/specs/2026-07-14-loudness-feedback-normalization.md``):
turn the raw grid meters (:mod:`helixgen.device.meters`) plus the always-on
pitch stream (:mod:`helixgen.device.tuner`) into a robust "how loud is this
tone" statistic.

Design facts (HW-characterized 2026-07-14, Stadium XL):

- The meter grids stream at ~10 Hz per mid and are linear amplitude
  envelopes. Cells 0-1 of mid 796 = instrument input; mid 800's populated
  cells = chain-out (the output sends). All taps sit upstream of the output
  block's ``gain``.
- Input level alone cannot gate out single-coil hum (hum 0.01-0.07 overlaps
  playing levels), but the pitch detector reads ``-1.0`` (no pitch) for hum —
  so a sample counts as *playing* only when the latest pitch reading is a
  real pitch AND the input envelope is above a small floor.
- ``gain_db`` (chain out ÷ instrument in, per sample, medianed) is the
  input-invariant loudness metric — robust to how hard the player picks.

This module is pure (unit-tested on synthetic events); the ``device measure``
CLI verb owns the subscribe loop.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Iterable, Iterator, List, NamedTuple, Optional

from . import meters, tuner

# Observed per-mid event rate of the 2003 meter stream (HW 2026-07-14). Used
# only to express gated sample counts as approximate seconds of playing.
STREAM_HZ = 10.0

# Input envelope below this is treated as digital silence even if the pitch
# detector claims a note (paranoia guard; hum sits ~1e-2, playing >= ~5e-4).
INPUT_FLOOR = 1e-4

# Default minimum gated samples for a trustworthy measurement (~4 s of
# actual playing at the observed stream rate).
MIN_PLAYING = 40

# A pitch reading older than this many output readings (~2 s at the stream
# rate) is treated as unknown — guards the hum gate against a pitch stream
# that stops re-emitting (observed to re-emit continuously, but cheap to not
# depend on it).
PITCH_STALE_AFTER = 20


class MeasureSample(NamedTuple):
    """One paired telemetry observation (emitted per mid-800 reading)."""
    input_level: float
    output_level: float
    pitch: Optional[float]   # latest fractional-MIDI pitch; -1.0 = no pitch;
                             # None = no pitch event seen yet


class MeasureResult(NamedTuple):
    seconds: float           # wall-clock window that was sampled
    n_samples: int           # all samples seen
    n_playing: int           # samples that passed the playing gate
    playing_seconds: float   # n_playing / STREAM_HZ (approximate)
    input_db: float          # median instrument-input level while playing
    output_db: float         # median chain-out level while playing
    output_db_p75: float     # 75th-percentile chain-out level while playing
    gain_db: float           # median per-sample (output - input) in dB
    ok: bool
    reason: str              # "" when ok


def samples_from_events(events: Iterable[Any]) -> Iterator[MeasureSample]:
    """Pair the interleaved pitch / input-grid / output-grid events into
    :class:`MeasureSample`s — one per mid-800 (chain-out) reading, carrying
    the latest-seen pitch and input levels."""
    last_pitch: Optional[float] = None
    last_input = 0.0
    pitch_age = 0
    for ev in events:
        args = getattr(ev, "args", None)
        if not isinstance(args, (list, tuple)):
            continue
        for payload in args:
            if not isinstance(payload, dict):
                continue
            p = tuner.pitch_from_map(payload)
            if p is not None:
                last_pitch = p
                pitch_age = 0
                continue
            r = meters.reading_from_map(payload)
            if r is None:
                continue
            if r.mid == meters.INPUT_MID:
                last_input = meters.input_level(r)
            elif r.mid == meters.OUTPUT_MID:
                pitch = last_pitch if pitch_age <= PITCH_STALE_AFTER else None
                yield MeasureSample(input_level=last_input,
                                    output_level=meters.output_level(r),
                                    pitch=pitch)
                pitch_age += 1


def is_playing(s: MeasureSample, input_floor: float = INPUT_FLOOR) -> bool:
    """True when the player is actually sounding notes: a real pitch reading
    (hum and silence report ``-1.0``; ``None`` = unknown) and a non-silent
    input envelope."""
    return (s.pitch is not None and s.pitch >= 0.0
            and s.input_level > input_floor)


def summarize(samples: Iterable[MeasureSample], seconds: float,
              min_playing: int = MIN_PLAYING) -> MeasureResult:
    """Reduce a sample stream to a :class:`MeasureResult` of robust dB stats
    over the playing-gated subset."""
    all_samples: List[MeasureSample] = list(samples)
    playing = [s for s in all_samples if is_playing(s)]

    def _result(ok: bool, reason: str) -> MeasureResult:
        if playing:
            in_db = meters.to_db(statistics.median(s.input_level for s in playing))
            outs = sorted(s.output_level for s in playing)
            out_db = meters.to_db(statistics.median(outs))
            p75_db = meters.to_db(
                outs[max(0, math.ceil(0.75 * len(outs)) - 1)])
            gain_db = statistics.median(
                meters.to_db(s.output_level) - meters.to_db(s.input_level)
                for s in playing)
        else:
            in_db = out_db = p75_db = gain_db = meters.DB_FLOOR
        return MeasureResult(
            seconds=float(seconds), n_samples=len(all_samples),
            n_playing=len(playing),
            playing_seconds=len(playing) / STREAM_HZ,
            input_db=in_db, output_db=out_db, output_db_p75=p75_db,
            gain_db=gain_db, ok=ok, reason=reason)

    if not all_samples:
        return _result(False, "no meter data")
    if len(playing) < min_playing:
        return _result(
            False,
            f"not enough playing ({len(playing)} gated samples < "
            f"{min_playing}; play steadily while measuring)")
    return _result(True, "")
