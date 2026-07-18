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
