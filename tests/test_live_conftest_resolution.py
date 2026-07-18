"""The live suite's stdlib-only IP resolver
(``tests/live/conftest._persisted_device_ip``) must pick exactly the device
``resolve_ip()`` would — including the deterministic serial tie-break — or a
multi-device live run would probe/target a different Stadium than the CLI it
drives (backlog #77). This offline test pins that agreement.
"""
from __future__ import annotations

import json

import pytest

from helixgen.device.discovery import resolve_ip
from tests.live.conftest import _persisted_device_ip


def _write_record(devices_dir, filename, *, ip, ip_updated_at, serial=None):
    rec = {"version": 1, "ip": ip, "ip_updated_at": ip_updated_at}
    if serial is not None:
        rec["serial"] = serial
    (devices_dir / filename).write_text(json.dumps(rec))


@pytest.fixture
def devices(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path))
    monkeypatch.delenv("HELIXGEN_HELIX_IP", raising=False)
    d = tmp_path / "devices"
    d.mkdir()
    return d


def test_recency_wins_over_serial(devices):
    _write_record(devices, "zzz.json", ip="10.0.0.1",
                  ip_updated_at=100.0, serial="zzz")
    _write_record(devices, "aaa.json", ip="10.0.0.2",
                  ip_updated_at=200.0, serial="aaa")
    assert _persisted_device_ip() == "10.0.0.2"
    assert _persisted_device_ip() == resolve_ip(warn=False)


def test_tie_break_by_serial(devices):
    # Same ip_updated_at: higher serial wins, deterministically.
    _write_record(devices, "aaa.json", ip="10.0.0.1",
                  ip_updated_at=100.0, serial="aaa")
    _write_record(devices, "zzz.json", ip="10.0.0.2",
                  ip_updated_at=100.0, serial="zzz")
    assert _persisted_device_ip() == "10.0.0.2"
    assert _persisted_device_ip() == resolve_ip(warn=False)


def test_tie_break_serial_fallback_matches_stem(devices):
    # A record missing an explicit ``serial`` field falls back to its filename
    # stem in resolve_ip(); the conftest resolver must tie-break identically.
    _write_record(devices, "zzz.json", ip="10.0.0.1",
                  ip_updated_at=100.0)  # no serial -> stem "zzz"
    _write_record(devices, "aaa.json", ip="10.0.0.2",
                  ip_updated_at=100.0, serial="aaa")
    assert resolve_ip(warn=False) == "10.0.0.1"  # "zzz" (stem) > "aaa"
    assert _persisted_device_ip() == "10.0.0.1"
    assert _persisted_device_ip() == resolve_ip(warn=False)
