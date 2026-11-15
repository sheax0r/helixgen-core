# Headphones venue: HighCut ceiling on both mics — design

**Date:** 2026-11-15
**Status:** approved (user preapproved implementation 2026-11-15)
**Repos:** engine → `helixgen-core`; skill + synced docs + pin → `helixgen`
**Builds on:** `2026-11-11-venue-variants-design.md` (venue feature, core 0.59.0)

## Problem

The `headphones` venue transform (`venue._headphones`) adds a parallel room-mic
IR beside the close-mic IR with `sibling_params={"Level": level}` *only*. The
room IR block therefore keeps its wide-open `HighCut` (20100 Hz) / `LowCut`
(19.9 Hz) no matter how the close IR is voiced, so the room lane passes
full-range top-end hash — harsh through headphones. Observed on *My Chemical
Romance — Thank You for the Venom* (Ibanez Prestige): an 8 kHz close-IR cut
helped, but the room lane stayed bright until the same cut was added by hand.

## Decision (from brainstorm)

Venues stay **imperative** (`apply_venue`); `venue_detail` is already the
reverse-patch record and stays so. The overlay idea is applied only where it
pays: the close-mic cut is recorded in `venue_detail` (`close_cut_before`) so
`direct` restores it. No declarative overlay engine.

**Ceiling on both mics, preference-driven.** One number — the headphones
close-mic HighCut ceiling — defaults to **8000 Hz** (one-tone evidence; where
Blackstar Beam / Kemper room sims roll off), is configurable, and `null`
disables it.

- Shipped default lives in **preferences** (`headphones_highcut_hz`), resolved
  by the CLI and passed to the engine as a kwarg. `venue.py` stays
  preference-free. Precedence: `--room-highcut` flag → pref file / env → built-in
  default 8000.
- When the close IR's base `HighCut` is **wide open** (== block default 20100)
  and a ceiling is set, apply the ceiling to the **close** IR and record
  `close_cut_before`. When the close IR already carries a cut (hand-set), leave
  it; the room mirrors whatever the close IR ends up with.
- The **room IR mirrors the close IR's** `HighCut`/`LowCut` — base, plus
  per-snapshot values when the close cut varies by snapshot. Only values that
  differ from the room block's own default are written (wide-open close → room
  unchanged from today, so the no-ceiling case is byte-identical).
- **Re-run the energy match** for the room Level using band-limited energy that
  reflects the applied cut (one-pole LP/HP approximation of the Helix filter;
  the Level is a starting point tuned by ear via the ROOM MIC footswitch). The
  ROOM MIC footswitch `max` keeps pointing at the resulting Level.
- Ceiling + mirror apply only on the **room-IR** treatment path. The Tile
  fallback leaves the close IR alone (the room-mic rationale doesn't apply).

## Reversal

`headphones → direct` (`_strip_room_stage`) removes the room stage as today
(the room IR block, and its cuts, vanish with `unwrap_split`). Added step: when
`venue_detail.close_cut_before` is present, restore the close IR's `HighCut` to
the recorded value after unwrapping, so round-trip headphones → direct →
headphones is clean.

## venue_detail additions

`room_highcut`, `room_lowcut` (what the room IR received, or null), and
`close_cut_before` (`{"HighCut": 20100.0}` when the close IR's cut was changed,
else null). Reversal depends only on `close_cut_before`; the room keys are
informational and let the retrofit tell "we set it" from "hand-set".

## Retrofit (existing variants)

Existing headphones variants were built the old way (room wide open). Add
`library venue-retrofit`, **dry-run by default**, `--apply` to write. It sweeps
every tone's headphones variants with `treatment == "room-ir"`, and for each:
re-derives the cut on the **existing** split (set-param on the present close/room
IRs, re-match Level, update ROOM MIC `max`, update `venue_detail`). It **skips**
a variant whose room IR already carries a non-default `HighCut` with no
`room_highcut` recorded (someone cut it by hand). Writes atomically + commits
like `library venue`.

## Non-goals

- No declarative/overlay venue framework.
- No default `LowCut` ceiling (LowCut is mirrored from the close IR only).
- No change to `direct` / `amp-in` beyond the close-IR restore above.
- No auto-retrofit; the sweep is explicit and dry-run by default.

## Tests

close IR with explicit cuts (room mirrors them); close IR wide open (ceiling on
both, room mirrors); per-snapshot close cut (room mirrors the array); round trip
headphones → direct → headphones; retrofit skips a hand-edited room IR; pref/flag
resolution incl. `null` disables; band-limited rematch moves the Level.
