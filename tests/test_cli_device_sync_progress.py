"""CLI tests for `device sync`'s live progress rendering (TASK 2).

The engine seam (`helixgen.device.setlist_sync.sync_setlists(progress=...)`)
is exercised elsewhere (`tests/test_setlist_sync_progress.py`); this module
is purely about the CLI's *rendering* of that event stream to stderr, and the
hard invariant that stdout (the plain-text summary and `--json`) never
changes because of it.

Never touches a real device or `~/.helixgen`: `sync_setlists` is monkeypatched
where `cli_device.device_sync` imports it (a local, per-call
``from helixgen.device.setlist_sync import sync_setlists``), replaced with a
fake that (a) records the ``progress`` callback it was given and (b) drives a
scripted sequence of `ProgressEvent`s through it before returning a canned
result dict shaped like the real engine's.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device.setlist_sync import ProgressEvent


@pytest.fixture(autouse=True)
def _configured_device_ip(monkeypatch):
    """#74: device verbs have no built-in default IP; simulate a configured
    user so `device sync` reaches the (faked) engine call."""
    monkeypatch.setenv("HELIXGEN_HELIX_IP", "10.0.0.99")


CANNED_RESULT = {
    "ok": True,
    "setlists": ["Main"],
    "pool": {
        "installed": ["Tone A"],
        "updated": ["Tone B"],
        "skipped": ["Tone C", "Tone D"],
        "deleted": [],
        "delete_skipped": [],
    },
    "references": {"Main": {"added": ["Tone A"], "removed": []}},
    "gc": {},
    "irs": {},
    "errors": [],
    "skipped_draft_setlists": [],
}


def _scripted_events():
    """One representative event per phase, including an error and a skip."""
    return [
        ProgressEvent("plan", total=2, label="1 install, 1 update, 2 skip"),
        ProgressEvent("install", label="Tone A", index=1, total=1, status="ok"),
        ProgressEvent("update", label="Tone B", index=1, total=1, status="ok"),
        # IR phase: scoped PER tone (index/total reset for this one tone).
        ProgressEvent("irs", label="deadbeef" * 4, index=1, total=1, status="ok"),
        ProgressEvent("references", label="Main", index=1, total=1, status="ok"),
        ProgressEvent("delete", label="Tone Z", index=1, total=2, status="skip",
                      detail="another setlist still references it"),
        ProgressEvent("delete", label="Tone Y", index=2, total=2, status="error",
                      detail="device rejected delete"),
    ]


def _interleaved_scripted_events():
    """Mirrors the REAL engine order for a sync authoring 2 tones where a
    non-final tone uploads an IR: `irs` events happen INSIDE authoring,
    BEFORE that tone's install/update event (see setlist_sync._author and
    the install/update loops) -- NOT as a separate phase between install
    events. Regression coverage for the bug where the renderer treated
    `irs` as a phase transition, prematurely closing the still-incomplete
    install bar and opening a second, duplicate one for the next install."""
    return [
        ProgressEvent("plan", total=2, label="2 install, 0 update, 0 skip"),
        ProgressEvent("irs", label="tone1-ir" * 4, index=1, total=1, status="ok"),
        ProgressEvent("install", label="Tone 1", index=1, total=2, status="ok"),
        ProgressEvent("irs", label="tone2-ir" * 4, index=1, total=1, status="ok"),
        ProgressEvent("install", label="Tone 2", index=2, total=2, status="ok"),
        ProgressEvent("references", label="Main", index=1, total=1, status="ok"),
    ]


def _make_fake_sync_setlists(recorder: dict):
    """A drop-in replacement for `sync_setlists` that records the `progress`
    kwarg it received, drives the scripted events through it, then returns
    the canned result — never touching a manifest, client, or network."""

    def _fake(manifest, *, ip=None, port=None, setlists=None, gc=False,
               exclude_irs=False, repush=False, progress=None):
        recorder["progress"] = progress
        recorder["called"] = True
        if progress is not None:
            for ev in _scripted_events():
                progress(ev)
        return CANNED_RESULT

    return _fake


def _patch_sync(monkeypatch, recorder):
    import helixgen.device.setlist_sync as setlist_sync_mod

    monkeypatch.setattr(setlist_sync_mod, "sync_setlists",
                        _make_fake_sync_setlists(recorder))


def _invoke(args):
    return CliRunner().invoke(cli, ["device", "sync"] + args)


def test_progress_callback_is_wired_and_non_none(monkeypatch):
    """The verb passes a real, callable renderer as `progress=` — not None,
    and not something that blows up when driven with events."""
    recorder = {}
    _patch_sync(monkeypatch, recorder)

    result = _invoke(["Main"])

    assert result.exit_code == 0, result.output + result.stderr
    assert recorder.get("called") is True
    cb = recorder.get("progress")
    assert cb is not None
    assert callable(cb)


def test_stdout_identical_with_and_without_no_progress(monkeypatch):
    """stdout (the summary) must be byte-for-byte the same whether the live
    progress renderer is active or suppressed with --no-progress — only
    stderr may differ."""
    rec1, rec2 = {}, {}

    _patch_sync(monkeypatch, rec1)
    res_progress = _invoke(["Main"])

    _patch_sync(monkeypatch, rec2)
    res_no_progress = _invoke(["Main", "--no-progress"])

    assert res_progress.exit_code == 0, res_progress.stderr
    assert res_no_progress.exit_code == 0, res_no_progress.stderr
    assert res_progress.stdout == res_no_progress.stdout
    assert "pool: 1 installed, 1 updated, 2 skipped" in res_progress.stdout
    assert "synced 1 setlist(s): Main" in res_progress.stdout


def test_no_progress_plain_lines_on_stderr(monkeypatch):
    """--no-progress renders plain one-line-per-phase text on stderr: no
    carriage-return bar redraws, and the plan summary line is present."""
    recorder = {}
    _patch_sync(monkeypatch, recorder)

    result = _invoke(["Main", "--no-progress"])

    assert result.exit_code == 0, result.stderr
    assert "\r" not in result.stderr
    assert "sync: 1 install, 1 update, 2 skip" in result.stderr
    # stdout stays the plain summary, untouched by progress rendering.
    assert "pool: 1 installed, 1 updated, 2 skipped" in result.stdout


def test_non_tty_falls_back_to_plain_even_without_no_progress(monkeypatch):
    """CliRunner's captured stderr is never a live TTY, so plain-mode
    rendering kicks in even when --no-progress isn't passed."""
    recorder = {}
    _patch_sync(monkeypatch, recorder)

    result = _invoke(["Main"])

    assert result.exit_code == 0, result.stderr
    assert "\r" not in result.stderr
    assert "sync: 1 install, 1 update, 2 skip" in result.stderr
    assert "pool: 1 installed, 1 updated, 2 skipped" in result.stdout


def test_json_stdout_is_clean_json_progress_on_stderr(monkeypatch):
    """--json stdout must parse as clean JSON of the result dict; the live
    progress stream goes to stderr only, never contaminating stdout."""
    recorder = {}
    _patch_sync(monkeypatch, recorder)

    result = _invoke(["Main", "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == CANNED_RESULT
    # Progress still rendered to stderr even under --json.
    assert "sync: 1 install, 1 update, 2 skip" in result.stderr


def test_error_and_skip_events_surface_on_stderr_not_stdout(monkeypatch):
    """The scripted `delete` events include a status="skip" and a
    status="error" — both must produce a visible stderr note, and neither
    may appear in stdout (which only reflects the canned result dict)."""
    recorder = {}
    _patch_sync(monkeypatch, recorder)

    result = _invoke(["Main", "--no-progress"])

    assert result.exit_code == 0, result.stderr
    assert "Tone Z" in result.stderr
    assert "skip" in result.stderr
    assert "another setlist still references it" in result.stderr
    assert "Tone Y" in result.stderr
    assert "error" in result.stderr
    assert "device rejected delete" in result.stderr
    # These per-item progress details are not part of the stdout contract.
    assert "Tone Z" not in result.stdout
    assert "Tone Y" not in result.stdout


def test_rich_mode_uses_progressbar_when_stderr_is_a_tty(monkeypatch):
    """Directly exercise the renderer's TTY branch (CliRunner can't fake a
    real TTY): force `isatty()` True on the constructed renderer's stream
    and confirm it drives a `click.progressbar` instead of plain lines."""
    from helixgen.cli_device import _make_sync_progress_renderer

    renderer = _make_sync_progress_renderer(no_progress=False)
    renderer._stream.isatty = lambda: True
    renderer.rich = True

    bars_created = []
    import click as click_mod

    class _FakeBar:
        def __init__(self, length, label, file):
            self.length = length
            self.label = label
            self.updates = 0
            self.finished = False
            bars_created.append(self)

        def render_progress(self):
            pass

        def update(self, n):
            self.updates += n

        def render_finish(self):
            self.finished = True

    monkeypatch.setattr(click_mod, "progressbar",
                        lambda length, label, file: _FakeBar(length, label, file))

    for ev in _scripted_events():
        renderer(ev)
    renderer.close()

    assert bars_created, "expected at least one progressbar for a fixed-total phase"
    # install/update/references/delete each get their own bar; all finished.
    assert all(b.finished for b in bars_created)
    assert any(b.updates >= 1 for b in bars_created)


def test_rich_mode_keeps_install_bar_open_across_interleaved_irs_events(monkeypatch):
    """Regression for the CRITICAL bug: the engine emits `irs` events INSIDE
    authoring, interleaved between install events for different tones
    (`irs(tone1)`, `install(tone1)`, `irs(tone2)`, `install(tone2)` -- see
    setlist_sync._author and the install loop). The renderer's `irs` branch
    must NOT treat `irs` as a phase transition: it must not close the
    still-open install bar or reset `self._phase`, so a single install bar
    is created and reaches 100% (2/2), not two duplicate half-finished bars.
    """
    from helixgen.cli_device import _make_sync_progress_renderer

    renderer = _make_sync_progress_renderer(no_progress=False)
    renderer._stream.isatty = lambda: True
    renderer.rich = True

    bars_created = []
    irs_lines = []
    import click as click_mod

    class _FakeBar:
        def __init__(self, length, label, file):
            self.length = length
            self.label = label
            self.updates = 0
            self.finished = False
            bars_created.append(self)

        def render_progress(self):
            pass

        def update(self, n):
            self.updates += n

        def render_finish(self):
            self.finished = True

    monkeypatch.setattr(click_mod, "progressbar",
                        lambda length, label, file: _FakeBar(length, label, file))

    orig_echo = renderer._echo

    def _capture_echo(line):
        if "uploading IR" in line:
            irs_lines.append(line)
        orig_echo(line)

    renderer._echo = _capture_echo

    for ev in _interleaved_scripted_events():
        renderer(ev)
    renderer.close()

    install_bars = [b for b in bars_created if b.label == "install"]
    assert len(install_bars) == 1, (
        f"expected exactly ONE install bar, got {len(install_bars)} "
        f"(irs events must not open a second bar mid-phase)")
    install_bar = install_bars[0]
    assert install_bar.updates == install_bar.length == 2
    assert install_bar.finished

    # The IR side-channel lines still appear on stderr.
    assert len(irs_lines) == 2
    assert any("tone1-ir" in line for line in irs_lines)
    assert any("tone2-ir" in line for line in irs_lines)


def test_plain_mode_install_banner_not_duplicated_by_interleaved_irs(monkeypatch):
    """Plain-mode counterpart of the rich-mode regression above: an `irs`
    event arriving between two `install` events must not reprint the
    `sync: install (...)` phase banner a second time, and the IR line must
    still show up as its own one-liner."""
    recorder = {}

    def _fake(manifest, *, ip=None, port=None, setlists=None, gc=False,
              exclude_irs=False, repush=False, progress=None):
        recorder["called"] = True
        if progress is not None:
            for ev in _interleaved_scripted_events():
                progress(ev)
        return CANNED_RESULT

    import helixgen.device.setlist_sync as setlist_sync_mod
    monkeypatch.setattr(setlist_sync_mod, "sync_setlists", _fake)

    result = _invoke(["Main", "--no-progress"])

    assert result.exit_code == 0, result.stderr
    assert result.stderr.count("sync: install") == 1
    assert "uploading IR 1/1: tone1-ir" in result.stderr
    assert "uploading IR 1/1: tone2-ir" in result.stderr
    # Both install items rendered as one-liners under the single banner.
    assert "  install 1/2: Tone 1" in result.stderr
    assert "  install 2/2: Tone 2" in result.stderr



def test_renderer_degrades_to_plain_when_stderr_isatty_raises(monkeypatch):
    """A closed/broken stderr whose `isatty()` raises must NOT crash renderer
    construction (progress is advisory) — it degrades to plain mode. Regression
    for the whole-branch review finding."""
    import sys as _sys
    from helixgen.cli_device import _make_sync_progress_renderer

    class _BrokenStderr:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

        def write(self, *a, **k):  # pragma: no cover - not exercised here
            pass

        def flush(self):  # pragma: no cover
            pass

    monkeypatch.setattr(_sys, "stderr", _BrokenStderr())
    # Construction must succeed and pick plain mode, not raise.
    renderer = _make_sync_progress_renderer(no_progress=False)
    assert renderer.rich is False
