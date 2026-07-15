"""CLI + MCP tests for `device measure` / `device_measure` (loudness phase 1).

Never touch a real device: HelixSubscriber is monkeypatched to replay a
synthetic telemetry stream (pitch + input grid + output grid bursts).
"""
import json

import pytest
from click.testing import CliRunner

from helixgen.cli import cli

msgpack = pytest.importorskip("msgpack")


class FakeSubscriber:
    """Context manager whose stream() yields pre-canned events."""

    events = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, duration=None, filter_addrs=None, include_noise=False):
        yield from type(self).events


class _Ev:
    def __init__(self, args):
        self.args = args


def _grid(cells):
    vals = [0.0] * 128
    for i, v in cells.items():
        vals[i] = v
    return vals


def _burst(pitch, inp, out):
    return [
        _Ev([{"id__": {"eid_": 10, "mid_": 796}, "vals": [pitch]}]),
        _Ev([{"id__": {"eid_": 1, "mid_": 796},
              "vals": _grid({0: inp, 1: inp})}]),
        _Ev([{"id__": {"eid_": 1, "mid_": 800},
              "vals": _grid({108: out, 109: out})}]),
    ]


def _patch(monkeypatch, events):
    from helixgen.device import subscribe as sub_mod
    FakeSubscriber.events = events
    monkeypatch.setattr(sub_mod, "HelixSubscriber", FakeSubscriber)


def test_cli_measure_json_ok(monkeypatch):
    _patch(monkeypatch, [e for _ in range(60) for e in _burst(40.0, 0.02, 0.5)])
    result = CliRunner().invoke(
        cli, ["device", "measure", "--seconds", "6", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["n_playing"] == 60
    assert payload["gain_db"] == pytest.approx(27.96, abs=0.01)


def test_cli_measure_fails_without_playing(monkeypatch):
    # hum only: pitch stays -1.0 -> gated out -> ok=False, exit code 1
    _patch(monkeypatch, [e for _ in range(60) for e in _burst(-1.0, 0.03, 0.5)])
    result = CliRunner().invoke(
        cli, ["device", "measure", "--seconds", "6", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "playing" in payload["reason"]


def test_cli_measure_human_output(monkeypatch):
    _patch(monkeypatch, [e for _ in range(60) for e in _burst(40.0, 0.02, 0.5)])
    result = CliRunner().invoke(cli, ["device", "measure", "--seconds", "6"])
    assert result.exit_code == 0, result.output
    assert "output" in result.output and "dB" in result.output


def test_mcp_device_measure_handler(monkeypatch):
    from mcp_server import tools
    _patch(monkeypatch, [e for _ in range(60) for e in _burst(40.0, 0.02, 0.5)])
    out = tools.device_measure_handler(seconds=6.0)
    assert out["ok"] is True
    assert out["n_samples"] == 60
    assert out["output_db"] == pytest.approx(-6.02, abs=0.01)


def test_mcp_device_measure_handler_reports_gate_failure(monkeypatch):
    from mcp_server import tools
    _patch(monkeypatch, [e for _ in range(10) for e in _burst(-1.0, 0.02, 0.5)])
    out = tools.device_measure_handler(seconds=6.0)
    assert out["ok"] is False and "playing" in out["reason"]
