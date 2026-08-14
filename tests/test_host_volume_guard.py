"""The offline suite must never drive the host's audio output volume.

`helixgen device normalize` sweeps the system volume as it converges, via
`osascript`. A test fixture that stubs the *measurement* but leaves
`stimulus.get_output_volume` / `set_output_volume` real reaches the developer's
speakers — `test_cli_device_normalize.py::fake_stimulus` did, at 324 live
`osascript` calls per run. `conftest._isolate_host_volume` guards it for the
whole suite; these tests pin that guard so it cannot be quietly removed.
"""
from __future__ import annotations

import subprocess

from helixgen.device import stimulus as ST


def test_osascript_never_reaches_the_host():
    """Both volume calls go through the guard, not to the machine."""
    calls: list[list[str]] = []
    real_run = ST.subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    # wrap the guard, don't replace it — we want to see what it does with these
    ST.subprocess.run, saved = spy, ST.subprocess.run
    try:
        assert ST.get_output_volume() is None      # empty stdout parses to None
        assert ST.set_output_volume(50) is True    # rc 0 reads as success
    finally:
        ST.subprocess.run = saved

    assert calls, "expected the volume helpers to attempt an osascript call"
    assert all(c[0] == "osascript" for c in calls)


def test_guard_passes_other_commands_through():
    """Only osascript is intercepted; everything else still really runs."""
    out = ST.subprocess.run(["echo", "helixgen"], capture_output=True, text=True)
    assert out.returncode == 0
    assert out.stdout.strip() == "helixgen"


def test_a_test_can_still_stub_the_volume_helpers(monkeypatch):
    """Tests that need a real-looking volume override the guard themselves."""
    monkeypatch.setattr(ST, "get_output_volume", lambda: 30)
    assert ST.get_output_volume() == 30
