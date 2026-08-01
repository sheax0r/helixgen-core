"""Playback of a recorded stimulus, owned by the CLI (he-xth / hc-ged).

`sample` mode replays a fixed recording into the instrument jack so a
normalize run is repeatable and unattended. The playback belongs HERE, in
the verb that measures, rather than in whatever agent or script drives it:
the loop has to start before the window and stop after it, including when
the window raises.

Two field-proven constraints shape this module:

- **The loop must be gapless.** ``afplay`` in a shell loop costs ~0.8-0.9 s
  of process startup per invocation, which turns a 5.00 s stimulus into a
  ~5.9 s period jittering +-0.3 s -- and an exact loop length is the whole
  reason the stimulus exists (a measurement window should contain WHOLE
  cycles, or the reading depends on window phase). sox's own ``repeat``
  loops inside one effects chain, so the default command is
  ``play -q {path} repeat 9999``.
- **The output device must be the one wired to the jack.** The Stadium is
  itself a USB audio interface and frequently steals the system default
  output, so the stimulus leaves over USB, nothing reaches the jack, and the
  measurement reports "not enough playing" with no hint why. Selecting an
  output device is not scriptable from here; the volume knob is (macOS,
  ``osascript``), which is what the calibration loop actually needs.
"""
from __future__ import annotations

import contextlib
import math
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


class StimulusError(RuntimeError):
    """Raised when a stimulus cannot be played (bad command, missing file or
    binary, a player that dies on start)."""


def argv(path: Path | str, playback_cmd: str) -> list[str]:
    """Split ``playback_cmd`` and substitute ``{path}`` as ONE argv element.

    Substituting before splitting would re-split a path containing spaces
    into two arguments, so the placeholder is replaced token-by-token after
    the split."""
    if "{path}" not in playback_cmd:
        raise StimulusError(
            f"playback command {playback_cmd!r} has no {{path}} placeholder — "
            f"it would play nothing (expected e.g. "
            f"'play -q {{path}} repeat 9999')")
    return [tok.replace("{path}", str(path))
            for tok in shlex.split(playback_cmd)]


def preflight(path: Path | str, playback_cmd: str) -> list[str]:
    """Check everything that can be checked BEFORE a played window: the file
    exists, the player binary is on PATH, and the command is not a
    shell-looped ``afplay``. Returns the argv to run."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise StimulusError(f"no stimulus file at {resolved}")

    if "afplay" in playback_cmd and any(
            tok in playback_cmd for tok in ("while", "for ", ";", "&&")):
        raise StimulusError(
            "a shell-looped `afplay` is not gapless — each invocation costs "
            "~0.8-0.9 s of process startup, so a 5.00 s stimulus plays on a "
            "~5.9 s jittering period and the measurement window no longer "
            "covers whole loop cycles. Use sox: 'play -q {path} repeat 9999'")

    command = argv(resolved, playback_cmd)
    if shutil.which(command[0]) is None:
        raise StimulusError(
            f"the playback command {command[0]!r} is not on PATH "
            f"(sox provides `play`: `brew install sox`)")
    return command


@contextlib.contextmanager
def playing(path: Path | str, playback_cmd: str):
    """Run the stimulus for the duration of the block, then stop it.

    The player is stopped on EVERY exit path -- a measurement that raises
    must not leave a loop playing into the user's amp indefinitely."""
    command = preflight(path, playback_cmd)
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        if proc.poll() is not None:
            raise StimulusError(
                f"the stimulus player exited immediately "
                f"(`{' '.join(command)}` returned {proc.poll()}) — check the "
                f"file plays by hand before measuring against it")
        yield proc
    finally:
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def set_output_volume(volume: int) -> bool:
    """Set the system output volume (macOS only). Returns False -- without
    raising -- anywhere else, so the calibration loop can fall back to
    telling the user what to change by hand."""
    if sys.platform != "darwin":
        return False
    clamped = max(0, min(100, int(round(volume))))
    subprocess.run(["osascript", "-e",
                    f"set volume output volume {clamped}"],
                   check=False, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    return True


def next_volume(volume: int, *, delta_db: float) -> int:
    """The next volume to try when the stimulus reads ``delta_db`` BELOW the
    reference (negative = too loud).

    macOS output volume is roughly proportional to amplitude, so a dB delta
    maps onto a multiplicative step. This is deliberately approximate: the
    loop re-measures, so an under-shoot costs one more window, and a
    hardware volume taper that isn't amplitude-linear only changes how many
    windows it takes to converge.

    ponytail: amplitude-proportional step, good enough because the loop
    re-measures; a per-platform taper model would only buy back an iteration.
    """
    if not delta_db:
        return volume
    stepped = volume * math.pow(10.0, delta_db / 20.0)
    nudged = int(round(stepped))
    if nudged == volume:  # never stall on a sub-integer step
        nudged = volume + (1 if delta_db > 0 else -1)
    return max(0, min(100, nudged))
