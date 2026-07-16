"""Live `device discover` + the #74 IP-resolution chain (0.24.0).

Discovery itself is read-only on the device (an mDNS query the Stadium
answers itself, plus the read-only /ProductInfoGet confirmation), so no
device lock scope applies. All persistence lands in the suite's scratch
$HELIXGEN_HOME — the user's real ~/.helixgen/devices/ records are never
touched (see conftest live_env).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import DEVICE_IP, REPO_ROOT

pytestmark = [pytest.mark.live, pytest.mark.discover]


def _run(env, *args, timeout=120):
    proc = subprocess.run(
        [sys.executable, "-c", "from helixgen.cli import cli; cli()",
         *[str(a) for a in args]],
        capture_output=True, text=True, timeout=timeout,
        env=env, cwd=str(REPO_ROOT))
    return proc.returncode, proc.stdout, proc.stderr


def test_discover_finds_and_persists_the_device(device, cli, live_env):
    """mDNS discovery finds the real Stadium, confirms it via the
    /ProductInfoGet handshake, and persists the record into the scratch
    home's devices/<serial>.json."""
    code, out, err = cli("device", "discover", "--json", timeout=120)
    assert code == 0, err or out
    rows = json.loads(out)
    assert any(r["ip"] == DEVICE_IP for r in rows), (rows, DEVICE_IP)
    hit = next(r for r in rows if r["ip"] == DEVICE_IP)
    assert hit["serial"], hit
    assert hit["firmware"], hit
    record = Path(hit["record"])
    # persisted in the SCRATCH home, never the real one
    assert str(record).startswith(live_env["HELIXGEN_HOME"]), record
    data = json.loads(record.read_text())
    assert data["ip"] == DEVICE_IP
    assert data["serial"] == hit["serial"]


def test_verb_resolves_ip_from_persisted_record_alone(device, cli, live_env):
    """After a discover, a normal read verb works with NO --ip and NO
    $HELIXGEN_HELIX_IP — resolved purely from the persisted record."""
    code, out, err = cli("device", "discover", "--json", timeout=120)
    assert code == 0, err or out
    env = dict(live_env)
    env.pop("HELIXGEN_HELIX_IP", None)
    code, out, err = _run(env, "device", "info", "--json")
    assert code == 0, err or out
    info = json.loads(out)
    assert info.get("serial"), info


def test_fail_fast_without_any_ip_source(tmp_path, live_env):
    """No --ip, no env, an empty scratch home: the verb must fail
    IMMEDIATELY (not a connect stall) and point at `device discover`.
    Device-independent (works with the Helix off)."""
    env = dict(live_env)
    env.pop("HELIXGEN_HELIX_IP", None)
    env["HELIXGEN_HOME"] = str(tmp_path / "empty-home")
    start = time.monotonic()
    code, out, err = _run(env, "device", "info")
    elapsed = time.monotonic() - start
    assert code != 0
    assert "helixgen device discover" in (err + out)
    assert elapsed < 8, f"fail-fast took {elapsed:.1f}s — smells like a stall"
