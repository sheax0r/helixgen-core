"""Machine-local advisory device locks, exercised through the REAL CLI
(workspace #71). The session `all` lease (label "live-test-suite") is taken
by the ``cli`` fixture for the whole run; these tests verify that lease is
visible, that a genuinely foreign process (no token, different parentage)
BLOCKS on mutating verbs and cannot lock/unlock over us, and that verbs
carrying the suite's token pass through transparently.

Most tests here need NO device: the lock layer is purely local and blocks
BEFORE any network connection. Only the liveops spot-check drives the
hardware (transparent auto-acquire around a real `device bypass`).
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from .conftest import HGTEST, REPO_ROOT, delete_preset, find_user_preset, \
    install_preset

pytestmark = [pytest.mark.live, pytest.mark.locks]

#: Runs the CLI through an INTERMEDIATE python process, so the CLI's parent
#: pid is NOT the pytest process that holds the session lease — i.e. a
#: faithful stand-in for an unrelated agent's helixgen call (the direct
#: ``cli`` fixture would pass through by parent-pid even without the token).
_DETACH = """\
import json, subprocess, sys
args = json.loads(sys.argv[1])
p = subprocess.run(
    [sys.executable, "-c", "from helixgen.cli import cli; cli()"] + args,
    capture_output=True, text=True)
sys.stdout.write(p.stdout)
sys.stderr.write(p.stderr)
sys.exit(p.returncode)
"""


def run_foreign(env: dict, *args, drop_token: bool = True,
                fail_fast: bool = True):
    """Run a CLI verb as a simulated FOREIGN process (detached parentage,
    no session token, fail-fast lock timeout)."""
    env = dict(env)
    if drop_token:
        env.pop("HELIXGEN_LOCK_TOKEN", None)
    if fail_fast:
        env["HELIXGEN_LOCK_TIMEOUT"] = "0"
    proc = subprocess.run(
        [sys.executable, "-c", _DETACH, json.dumps([str(a) for a in args])],
        capture_output=True, text=True, timeout=300, env=env,
        cwd=str(REPO_ROOT))
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------
# the session lease itself (no device needed)
# --------------------------------------------------------------------------

def test_status_reports_the_session_all_lease(cli):
    code, out, err = cli("device", "lock", "--status", "--json")
    assert code == 0, err or out
    rows = {r["scope"]: r for r in json.loads(out)}
    assert "all" in rows, f"session lease missing: {out}"
    lease = rows["all"]
    assert lease["label"] == "live-test-suite"
    assert lease["state"] == "live"
    assert lease["ours"] is True
    assert lease["kind"] == "session"


def test_foreign_mutating_verb_blocks_naming_the_holder(cli, live_env):
    """An untokened, unrelated process fail-fasts on a mutating verb — the
    lock check fires BEFORE any device connection, so this needs no device
    and mutates nothing."""
    code, out, err = run_foreign(live_env, "device", "load", "999999")
    assert code != 0
    assert "locked" in err
    assert "live-test-suite" in err
    assert "--no-lock" in err or "HELIXGEN_LOCK_TIMEOUT" in err


def test_foreign_read_verb_takes_no_lock(cli, live_env):
    """Read-only verbs acquire nothing — a foreign `device lock --status`
    succeeds while we hold `all`."""
    code, out, err = run_foreign(live_env, "device", "lock", "--status",
                                 "--json")
    assert code == 0, err or out
    rows = {r["scope"]: r for r in json.loads(out)}
    assert rows["all"]["ours"] is False  # foreign process doesn't own it


@pytest.mark.parametrize("scope", ["editbuffer", "library", "irs", "globals",
                                   "all"])
def test_foreign_session_lock_conflicts_with_all(cli, live_env, scope):
    code, out, err = run_foreign(
        live_env, "device", "lock", "--scope", scope, "--label", "intruder")
    assert code != 0
    assert "live-test-suite" in err


def test_foreign_unlock_cannot_break_the_live_lease(cli, live_env):
    code, out, err = run_foreign(live_env, "device", "unlock",
                                 "--scope", "all")
    assert code != 0
    assert "live-test-suite" in err
    # ... and the lease is still there
    code, out, err = cli("device", "lock", "--status", "--json")
    assert code == 0, err or out
    assert any(r["scope"] == "all" and r["state"] == "live"
               for r in json.loads(out))


def test_tokened_relock_renews_instead_of_deadlocking(cli):
    """`device lock` again from the suite's own session (same token) is a
    renewal, not a self-deadlock."""
    code, out, err = cli("device", "lock", "--scope", "all",
                         "--label", "live-test-suite", "--ttl", "7200")
    assert code == 0, err or out


# --------------------------------------------------------------------------
# transparent auto-acquire on a real device verb (the liveops spot-check)
# --------------------------------------------------------------------------

def test_liveops_bypass_transparently_acquires_and_releases(
        helix, hgtest_hsp, live_env):
    """A normal mutating verb (volatile `device bypass` toggle on an HGTEST
    tone) passes through the session lease via the token, touches the device,
    and leaves no per-verb lease behind."""
    code, out, err = helix("device", "list", "--json")
    assert code == 0, err or out
    occupied = {m.get("posi") for m in json.loads(out)}
    free = [p for p in range(127, -1, -1) if p not in occupied]
    if not free:
        pytest.skip("no empty user slot for the locks spot-check tone")
    name = f"{HGTEST} LockSpot"
    cid = install_preset(helix, hgtest_hsp, name, free[0])
    try:
        code, out, err = helix("device", "load", cid)
        assert code == 0, err or out
        code, out, err = helix("device", "blocks", "--json")
        assert code == 0, err or out
        amp = next((b for b in json.loads(out)
                    if "Amp" in (b.get("model") or "")), None)
        assert amp is not None
        for state in ("off", "on"):
            code, out, err = helix("device", "bypass",
                                   amp["path"], amp["block"], state)
            assert code == 0, err or out
        # no leftover editbuffer lease; the session `all` lease survives
        code, out, err = helix("device", "lock", "--status", "--json")
        assert code == 0, err or out
        rows = {r["scope"]: r for r in json.loads(out)}
        assert "editbuffer" not in rows
        assert rows["all"]["label"] == "live-test-suite"
    finally:
        delete_preset(helix, cid)
        assert find_user_preset(helix, name) is None, \
            f"cleanup failed: {name!r} still on device"


def test_no_lock_escape_hatch_bypasses_the_lock_layer(helix, live_env):
    """--no-lock skips the lock check entirely: a foreign (untokened)
    process reaches the device instead of failing on the lease. Uses a
    read-modify-nothing failure path (`device load` of a nonexistent cid) so
    nothing mutates."""
    code, out, err = run_foreign(live_env, "device", "load", "999999",
                                 "--no-lock")
    # it got PAST the lock (whatever the device said about cid 999999)
    assert "locked" not in err or "live-test-suite" not in err
