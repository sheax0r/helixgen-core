"""Live integration suite: the real CLI, the real library, the real device.

This package turns the 2026-07-15 one-off live validation of the CLI
(0.20.0, post-MCP-removal) into a permanent, programmatic suite. Every test
drives the REAL CLI surface — argument parsing included — via subprocess
(``sys.executable -c "from helixgen.cli import cli; cli()"`` with
``PYTHONPATH=<repo>/src``), never click's CliRunner: this suite exists to
catch integration breakage the in-process tests can't see.

Opt-in gating
-------------
Everything here is skipped unless ``HELIXGEN_LIVE=1`` is set, so default/CI
runs stay green and fast. Tests that touch the device additionally require a
cheap TCP reachability probe of the device's ZMQ ROUTER port (2002) to
succeed — the Stadium ignores ICMP, so ping is useless. Global-settings
write tests require a second opt-in, ``HELIXGEN_LIVE_GLOBAL=1``.

Run it::

    HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python -m pytest tests/live -q

Targeted subsets by impact area (markers registered in pyproject.toml)::

    pytest -m "live and sync" tests/live        # after touching sync code
    pytest -m "live and device_ir" tests/live   # after touching device IR code
    pytest -m "live and not device_write and not liveops" tests/live

Marker taxonomy: ``live`` on everything, plus one group marker per module —
``authoring``, ``library``, ``ir`` (local IR verbs), ``device_read``,
``device_write``, ``liveops``, ``setlists``, ``sync``, ``device_ir``,
``locks`` (the machine-local advisory device locks), and ``live_global``
(the extra-gated global-settings writes).

Device-lock integration (workspace #71)
---------------------------------------
The suite is the flagship consumer of the machine-local advisory device
locks: the ``cli`` fixture takes the REAL ``all`` lease (label
``live-test-suite``) for the whole run and releases it at teardown, so any
unrelated helixgen process on this machine blocks/fails instead of
colliding with the suite. ``live_env`` carries a per-run
``HELIXGEN_LOCK_TOKEN`` so the suite's own CLI calls pass through. The
locks root is deliberately NOT redirected to scratch (real coordination is
the point); lock files live under ``~/.helixgen/locks/<ip>/`` and the
teardown ``device unlock`` clears them.

Safety model (encoded as fixtures)
----------------------------------
* ALL helixgen state is redirected to a session scratch dir: manifest
  (``HELIXGEN_SETLISTS``), IR mapping (``HELIXGEN_IRS``), IR-hash cache
  (``HELIXGEN_IRHASH_CACHE`` — the cache is written by IR verbs regardless
  of ``HELIXGEN_IRS``, proven live 2026-07-15), preferences
  (``HELIXGEN_PREFS``) and device backups (``HELIXGEN_DEVICE_BACKUPS``).
  Only ``HELIXGEN_LIBRARY`` points at the user's real block library,
  read-only (the suite skips if it's absent). ``ingest`` tests must pass an
  explicit ``--library <scratch>`` — never let ingest write the real library.
* An upfront ``device backup`` runs (to scratch) before any device test.
* Device state (``device list/setlists/list-irs --json``) is captured before
  the first device test and re-captured at session teardown; the suite
  ITSELF FAILS if the normalized state changed. (Known blind spots: the
  preset pool (-2) can't be listed directly — the sync test compensates by
  asserting its cleanup sync's own --json result reports the HGTEST pool
  presets deleted; wedged IR FILES that never gained a registry entry are
  invisible to `list-irs` — the device_ir teardown removes them via the
  CLI's own `delete-ir --force-wedge` remedy, scoped to its just-pushed
  hash; and the ACTIVE edit buffer isn't part of the diff — the
  device_write and liveops modules `device load` an HGTEST tone, so they
  leave the edit buffer on that (deleted) tone and discard whatever UNSAVED
  edit-buffer changes existed before the run — saved presets are covered by
  the upfront backup.)
* Every artifact a test creates (presets, setlists, IRs, tones, files) is
  named with an ``HGTEST`` prefix and torn down via fixture finalizers /
  try-finally, even on failure. Nothing not HGTEST-prefixed is ever touched.
* At session teardown the suite verifies the user's REAL
  ``~/.helixgen/setlists.json``, ``~/.helixgen/irs/mapping.json`` and
  ``~/.helixgen/preferences.json`` are byte-identical to their pre-suite
  hashes — its own isolation regression check.

Deliberately excluded verbs (and why)
-------------------------------------
* ``device restore`` — recovery-only: overwrites an existing preset's
  content in place; there is no HGTEST-scoped way to exercise it that isn't
  already covered by pull/push.
* ``device sync --all`` (and ``--gc``) — unscopeable to test artifacts: it
  would reconcile the user's real device setlists.
* ``bootstrap`` — clones an external repo; not a device/CLI regression
  surface worth network flakiness here.
* ``device globaleq set`` — mutates global device config and is WRITE-ONLY
  over the network (no read-back), so even the read/set-same-value/verify
  pattern is impossible; excluded entirely.
* ``device settings set`` — global device config, but it HAS read-back, so
  a provably-safe read → set-same-value → verify round-trip exists; covered
  behind the extra ``HELIXGEN_LIVE_GLOBAL=1`` opt-in (test_global_settings).
* ``ir-cache --clear`` against the real cache — destructive of the user's
  cache; covered against the scratch cache only (the env redirect makes
  ``--clear`` safe here), plus ``--stats``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"

def _persisted_device_ip() -> str | None:
    """The #74 resolution chain's record step, replicated stdlib-only (the
    suite must know the IP before helixgen is importable): the most recently
    discovered ip across ~/.helixgen/devices/*.json ($HELIXGEN_HOME honored).

    Ordering — exactly what ``resolve_ip()`` (via ``devices_with_ips()``)
    picks: ``ip_updated_at`` desc, then ``serial`` desc as the deterministic
    tie-break. A record missing an explicit ``serial`` field falls back to its
    filename stem (``<serial>.json``), matching the CLI, so two same-recency
    records never resolve to different devices between the suite and the CLI
    it drives."""
    home = Path(os.environ.get("HELIXGEN_HOME") or (Path.home() / ".helixgen"))
    best: tuple | None = None
    try:
        files = list((home / "devices").glob("*.json"))
    except OSError:
        return None
    for p in files:
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("ip"):
            continue
        key = (float(data.get("ip_updated_at") or 0.0),
               str(data.get("serial") or p.stem))
        if best is None or key > best[0]:
            best = (key, str(data["ip"]))
    return best[1] if best else None


# #74: no built-in default IP anymore — the suite resolves exactly like the
# CLI ($HELIXGEN_HELIX_IP, else the record `helixgen device discover`
# persisted). None ⇒ device-backed tests skip with a pointer to `discover`.
DEVICE_IP = os.environ.get("HELIXGEN_HELIX_IP") or _persisted_device_ip()
# The CLI's --port has no env override, so the probe pins the same default
# the CLI uses (probing a different port than the verbs talk to would be a
# false health signal).
DEVICE_PORT = 2002

LIVE_ENABLED = os.environ.get("HELIXGEN_LIVE") == "1"
GLOBAL_ENABLED = os.environ.get("HELIXGEN_LIVE_GLOBAL") == "1"

#: Every artifact the suite creates on the device / in scratch state carries
#: this prefix; teardown helpers refuse to touch anything without it.
HGTEST = "HGTEST"

# The real dotfiles the suite must never modify (isolation regression check).
# Includes the real block library's index (the one real-state path the suite
# deliberately keeps live via HELIXGEN_LIBRARY — a future test calling
# `ingest` without an explicit --library would write there) and the
# redirected-but-guarded cache/ledger files.
_REAL_HELIXGEN = Path.home() / ".helixgen"
_GUARDED_FILES = (
    _REAL_HELIXGEN / "setlists.json",
    _REAL_HELIXGEN / "irs" / "mapping.json",
    _REAL_HELIXGEN / "preferences.json",
    _REAL_HELIXGEN / "library" / "index.json",
    _REAL_HELIXGEN / "cache" / "irhash.json",
    _REAL_HELIXGEN / "device-slots.json",
)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Force the live suite serial.

    The repo default `addopts = -ra -n auto` (pytest-xdist) is right for the
    offline suite but wrong here: this suite drives a SINGLE physical device
    and serializes on one session-scoped machine-local `all` device lock
    (see the `cli` fixture). Under xdist, session fixtures are per-worker, so
    every extra worker would contend on that lock — each with its own token —
    wait out `$HELIXGEN_LOCK_TIMEOUT`, then fail; worse, concurrent workers
    would mutate the one device in parallel. Disable workers whenever the live
    suite is enabled so the documented `HELIXGEN_LIVE=1 ... pytest tests/live`
    command self-serializes regardless of inherited addopts. tryfirst so this
    runs before xdist's own configure (conftest hooks register last / run
    first under pluggy's LIFO ordering).
    """
    if LIVE_ENABLED:
        config.option.numprocesses = 0
        config.option.dist = "no"


def pytest_collection_modifyitems(config, items):
    """Gate the whole live suite on HELIXGEN_LIVE=1 (fast collection-time skip)."""
    if not LIVE_ENABLED:
        skip = pytest.mark.skip(
            reason="live integration suite is opt-in: set HELIXGEN_LIVE=1 "
                   "(device tests also need the Helix reachable on "
                   f"{DEVICE_IP}:{DEVICE_PORT})")
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(skip)
        return
    if not GLOBAL_ENABLED:
        skip_global = pytest.mark.skip(
            reason="global-settings writes are extra-gated: set "
                   "HELIXGEN_LIVE_GLOBAL=1 (in addition to HELIXGEN_LIVE=1)")
        for item in items:
            if item.get_closest_marker("live_global"):
                item.add_marker(skip_global)


# --------------------------------------------------------------------------
# scratch state + CLI runner
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def real_library() -> Path:
    """The user's real block library — read-only. Skip the suite without it."""
    lib = Path(os.environ.get("HELIXGEN_LIBRARY",
                              str(_REAL_HELIXGEN / "library")))
    if not (lib / "index.json").exists():
        pytest.skip(f"no block library at {lib} — the live suite needs a real "
                    "ingested library (HELIXGEN_LIBRARY)")
    return lib


@pytest.fixture(scope="session")
def scratch(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session scratch root for ALL helixgen state the suite may write."""
    root = tmp_path_factory.mktemp("helixgen-live")
    (root / "irs").mkdir()
    (root / "backups").mkdir()
    (root / "work").mkdir()
    # An explicitly-set $HELIXGEN_PREFS pointing at a missing file is an
    # error, so materialize an empty prefs file.
    (root / "preferences.json").write_text("{}\n")
    return root


@pytest.fixture(scope="session")
def live_env(scratch: Path, real_library: Path) -> dict:
    """Subprocess environment: working tree on PYTHONPATH + scratch state."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.update({
        # $HELIXGEN_HOME to scratch isolates everything home-derived that
        # has no dedicated override — most importantly the per-device
        # devices/<serial>.json records (observations + the #74 discovered
        # address), which `device sync` and `device discover` write. The
        # user's real ~/.helixgen/devices/ is never touched by the suite.
        "HELIXGEN_HOME": str(scratch / "home"),
        # ...but the advisory device-lock root stays REAL (it would
        # otherwise derive from the redirected home): the whole point of
        # the suite's session lease is excluding OTHER helixgen processes
        # on this machine.
        "HELIXGEN_LOCKS": str(_REAL_HELIXGEN / "locks"),
        "HELIXGEN_SETLISTS": str(scratch / "setlists.json"),
        "HELIXGEN_DEVICE_SLOTS": str(scratch / "device-slots.json"),
        "HELIXGEN_IRS": str(scratch / "irs"),
        "HELIXGEN_IRHASH_CACHE": str(scratch / "irhash-cache.json"),
        "HELIXGEN_DEVICE_BACKUPS": str(scratch / "backups"),
        "HELIXGEN_PREFS": str(scratch / "preferences.json"),
        "HELIXGEN_LIBRARY": str(real_library),
        # The suite holds the REAL machine-local `all` device lock for the
        # whole run (workspace #71 — flagship consumer); this token lets
        # every CLI call the suite makes pass through that session lease.
        # The locks root is deliberately NOT redirected to scratch: the
        # point is excluding OTHER helixgen processes on this machine.
        "HELIXGEN_LOCK_TOKEN": f"live-test-suite-{uuid.uuid4().hex}",
    })
    # #74: no built-in default IP. When resolvable, pin it in the env so
    # every CLI call the suite makes targets the same device; when not,
    # leave it unset — device-backed tests skip, and the fail-fast tests
    # exercise the unconfigured path against the scratch home.
    if DEVICE_IP:
        env["HELIXGEN_HELIX_IP"] = DEVICE_IP
    else:
        env.pop("HELIXGEN_HELIX_IP", None)
    (scratch / "home").mkdir(exist_ok=True)
    return env


@pytest.fixture(scope="session")
def cli(live_env: dict):
    """Run the real CLI in a subprocess; returns (exit_code, stdout, stderr).

    Invokes ``sys.executable -c "from helixgen.cli import cli; cli()" <args>``
    so the working tree's CLI — argument parsing included — is exercised
    end-to-end (no CliRunner). All args are str()-coerced.
    """
    launcher = "from helixgen.cli import cli; cli()"

    def run(*args, timeout: float = 300, stdin: str | None = None):
        proc = subprocess.run(
            [sys.executable, "-c", launcher, *[str(a) for a in args]],
            capture_output=True, text=True, timeout=timeout,
            env=live_env, cwd=str(REPO_ROOT), input=stdin)
        return proc.returncode, proc.stdout, proc.stderr

    # Hold the machine-local `all` device lock for the entire run
    # (workspace #71): any helixgen process on this machine that isn't
    # carrying our HELIXGEN_LOCK_TOKEN blocks/fails instead of colliding
    # with the suite's device work. Purely local — works device-offline too.
    # #74: locks are keyed per-ip and there is no default IP — device-less
    # runs key the session lease under an explicit placeholder (harmless:
    # no device work happens; the point is still to serialize suite runs).
    lock_ip = ("--ip", DEVICE_IP) if DEVICE_IP else ("--ip", "no-device")
    code, out, err = run("device", "lock", "--scope", "all",
                         "--label", "live-test-suite", "--ttl", "7200",
                         *lock_ip)
    assert code == 0, ("could not acquire the session 'all' device lock — "
                       "is another helixgen session holding the device? "
                       f"{err or out}")
    try:
        yield run
    finally:
        run("device", "unlock", *lock_ip)


@pytest.fixture(scope="session", autouse=True)
def _real_dotfiles_guard():
    """FAIL the session if the suite modified the user's real ~/.helixgen files."""
    if not LIVE_ENABLED:
        yield
        return

    def _digests():
        return {
            str(p): (hashlib.sha256(p.read_bytes()).hexdigest()
                     if p.exists() else None)
            for p in _GUARDED_FILES
        }

    before = _digests()
    yield
    after = _digests()
    assert after == before, (
        "ISOLATION REGRESSION: the live suite modified the user's real "
        f"~/.helixgen state.\n  before: {before}\n  after:  {after}\n"
        "Every live test must operate on the scratch env only.")


# --------------------------------------------------------------------------
# device gating + safety net
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def device() -> str:
    """Cheap reachability probe (the device ignores ICMP — TCP-connect 2002)."""
    if not DEVICE_IP:
        pytest.skip("no Helix IP configured (#74: no built-in default) — "
                    "set HELIXGEN_HELIX_IP or run `helixgen device discover` "
                    "once; device-backed live tests skipped")
    try:
        with socket.create_connection((DEVICE_IP, DEVICE_PORT), timeout=3):
            pass
    except OSError as e:
        pytest.skip(f"Helix device unreachable at {DEVICE_IP}:{DEVICE_PORT} "
                    f"({e}) — device-backed live tests skipped")
    return DEVICE_IP


@pytest.fixture(scope="session")
def device_backup(device: str, cli, scratch: Path) -> Path:
    """Upfront safety backup of the user setlist, to scratch."""
    dest = scratch / "backups"
    code, out, err = cli("device", "backup", "--dir", dest, timeout=600)
    assert code == 0, f"upfront `device backup` failed: {err or out}"
    assert (dest / "manifest.json").exists()
    return dest


def _normalize_presets(raw: str):
    return sorted((m.get("cid_"), m.get("name"), m.get("posi"))
                  for m in json.loads(raw))


def _normalize_setlists(raw: str):
    return sorted((m.get("cid_"), m.get("name"), m.get("posi"))
                  for m in json.loads(raw))


def _normalize_irs(raw: str):
    return sorted((m.get("hash"), m.get("name"), m.get("posi"))
                  for m in json.loads(raw))


def _capture_device_state(cli) -> dict:
    state = {}
    for key, args, norm in (
        ("presets_user", ("device", "list", "--json"), _normalize_presets),
        ("setlists", ("device", "setlists", "--json"), _normalize_setlists),
        ("irs", ("device", "list-irs", "--json"), _normalize_irs),
    ):
        code, out, err = cli(*args)
        assert code == 0, f"state capture {' '.join(args)} failed: {err or out}"
        state[key] = norm(out)
    return state


def _sweep_stale_hgtest_artifacts(cli) -> list[str]:
    """Delete HGTEST-prefixed leftovers from a previously crashed run.

    Runs BEFORE the state capture, so a stale artifact is neither absorbed
    into the baseline (where it would silently persist) nor left to confuse
    name-based lookups (`find_user_preset` returns the first match;
    `create`'s "(1)" auto-name check would find a stale copy). Only ever
    touches HGTEST-prefixed presets/setlists/IRs.
    """
    swept = []
    code, out, _ = cli("device", "list", "--json")
    if code == 0:
        for m in json.loads(out):
            if (m.get("name") or "").startswith(HGTEST):
                cli("device", "delete", m["cid_"], "--yes")
                swept.append(f"preset {m['name']!r} (cid {m['cid_']})")
    code, out, _ = cli("device", "setlists", "--json")
    if code == 0:
        for m in json.loads(out):
            if (m.get("name") or "").startswith(HGTEST):
                cli("device", "setlist", "delete", m["name"], "--yes")
                swept.append(f"setlist {m['name']!r}")
    code, out, _ = cli("device", "list-irs", "--json")
    if code == 0:
        for m in json.loads(out):
            if (m.get("name") or "").startswith(HGTEST):
                cli("device", "delete-ir", m["hash"], "--yes")
                swept.append(f"IR {m['name']!r} ({m['hash']})")
    return swept


@pytest.fixture(scope="session")
def device_state_guard(device: str, device_backup: Path, cli) -> dict:
    """Capture device state up front; FAIL the session if it changed at the end.

    Normalized on (cid, name, posi) for presets/setlists and
    (hash, name, posi) for IRs. Every device-touching test depends on this
    (directly or via `helix`), so the capture always precedes the first
    mutation and the check runs after the last cleanup. Stale HGTEST
    leftovers from a crashed previous run are swept before the capture.
    """
    swept = _sweep_stale_hgtest_artifacts(cli)
    if swept:
        print(f"\n[tests/live] swept stale HGTEST artifacts from a previous "
              f"run: {', '.join(swept)}")
    before = _capture_device_state(cli)
    yield before
    after = _capture_device_state(cli)
    assert after == before, (
        "DEVICE STATE CHANGED across the live suite — a test leaked an "
        "artifact or mutated non-HGTEST state.\n"
        + "\n".join(
            f"[{k}]\n  before: {before[k]}\n  after:  {after[k]}"
            for k in before if before[k] != after[k]))


@pytest.fixture(scope="session")
def helix(device: str, device_state_guard: dict, cli):
    """The standard dependency for device-backed tests: probe + backup + guard."""
    return cli


@pytest.fixture()
def free_positions(helix):
    """Return N currently-empty USER-setlist positions (highest-first, so the
    suite stays far away from the user's own presets)."""
    def get(n: int = 1) -> list[int]:
        code, out, err = helix("device", "list", "--json")
        assert code == 0, err or out
        occupied = {m.get("posi") for m in json.loads(out)}
        free = [p for p in range(127, -1, -1) if p not in occupied]
        if len(free) < n:
            pytest.skip(f"need {n} empty user slots, found {len(free)}")
        return free[:n]
    return get


# --------------------------------------------------------------------------
# shared HGTEST artifacts
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def amp_blocks(cli) -> list[dict]:
    """Amps from the real library ({display_name, model_id, category})."""
    code, out, err = cli("list-blocks", "--json", "--category", "amp")
    assert code == 0, err or out
    blocks = json.loads(out)
    if not blocks:
        pytest.skip("library has no amp blocks")
    return blocks


@pytest.fixture(scope="session")
def amp_schema(cli, amp_blocks) -> dict:
    """show-block --json for the first amp (params dict included)."""
    code, out, err = cli("show-block", amp_blocks[0]["display_name"], "--json")
    assert code == 0, err or out
    return json.loads(out)


def make_recipe(scratch: Path, name: str, block: str, params: dict | None = None) -> Path:
    entry: dict = {"block": block}
    if params:
        entry["params"] = params
    recipe = {"name": name, "author": "hgtest",
              "paths": [{"blocks": [entry]}]}
    path = scratch / "work" / f"{re.sub(r'[^A-Za-z0-9]+', '-', name)}.recipe.json"
    path.write_text(json.dumps(recipe))
    return path


def generate_hsp(cli, scratch: Path, name: str, block: str) -> Path:
    """Generate an HGTEST .hsp from a one-amp recipe (auto-registers into the
    SCRATCH manifest — never the real one; live_env redirects it)."""
    assert name.startswith(HGTEST)
    recipe = make_recipe(scratch, name, block)
    out_path = scratch / "work" / f"{re.sub(r'[^A-Za-z0-9]+', '-', name)}.hsp"
    code, out, err = cli("generate", recipe, "-o", out_path)
    assert code == 0, f"generate failed: {err or out}"
    assert out_path.exists()
    return out_path


@pytest.fixture(scope="session")
def hgtest_hsp(cli, scratch: Path, amp_blocks) -> Path:
    """A session-wide HGTEST .hsp (single amp block) for device tests."""
    return generate_hsp(cli, scratch, f"{HGTEST} Base Tone",
                        amp_blocks[0]["display_name"])


CID_RE = re.compile(r"as cid (\d+)")

#: Cooldown between retries of a /CreateContent that hit the backlog-#38
#: status-1 episode — observed live 2026-07-15 to be load-correlated (many
#: rapid creates/deletes in one session) and to clear after a short idle.
CREATE_RETRY_COOLDOWN_S = 10.0


def install_preset(helix, hsp: Path, name: str, pos: int) -> int:
    """`device install` an HGTEST .hsp; returns the new cid.

    Handles the backlog-#38 /CreateContent status-1 episode: the hardened
    client already self-cleans the allocated stub and surfaces the code, so
    on that specific error this retries once after a cooldown and then
    XFAILs (callable from module fixtures — pytest.xfail in setup xfails the
    dependent tests instead of ERRORing them).
    """
    assert name.startswith(HGTEST)
    last = ""
    for attempt in range(2):
        code, out, err = helix("device", "install", hsp, name, "--pos", pos)
        if code == 0:
            m = CID_RE.search(out)
            assert m, f"no cid in install output: {out!r}"
            return int(m.group(1))
        last = (err or out).strip()
        if "/CreateContent" not in last:
            break  # a different failure mode; retrying won't help
        if attempt == 0:
            time.sleep(CREATE_RETRY_COOLDOWN_S)
    if "/CreateContent" in last:
        pytest.xfail("backlog #38 episode: /CreateContent returned a non-zero "
                     f"status on `device install` (stub self-cleaned by the "
                     f"client; retried after {CREATE_RETRY_COOLDOWN_S:.0f}s "
                     f"cooldown): {last}")
    pytest.fail(f"device install of {name!r} failed: {last}")


def delete_preset(helix, cid: int) -> None:
    """Best-effort delete of an HGTEST preset by cid (cleanup path)."""
    helix("device", "delete", cid, "--yes")


def find_user_preset(helix, name: str) -> dict | None:
    code, out, err = helix("device", "list", "--json")
    assert code == 0, err or out
    for m in json.loads(out):
        if m.get("name") == name:
            return m
    return None


def create_device_setlist(helix, name: str, retries: int = 2) -> None:
    """`device setlist create` with backlog-#38-recurrence handling.

    /CreateContent can intermittently return status 1 while STILL allocating
    the content (backlog #38). Observed live while building this suite
    (2026-07-15 evening, setlist ctype): `/status [1003, <cid>, 1]` on one
    create, then `/status [1002, <cid>, 0]` for an identical raw create
    minutes later. The CLI reports the code-1 outcome as "device refused to
    create setlist" but the setlist IS allocated, so this helper self-cleans
    the stub, retries, and — if the episode persists — XFAILs with the #38
    reference instead of leaking artifacts or failing the suite on a known
    device-state defect.
    """
    assert name.startswith(HGTEST)
    last = ""
    for attempt in range(retries + 1):
        code, out, err = helix("device", "setlist", "create", name)
        if code == 0:
            return
        last = (err or out).strip()
        # The failed create may still have allocated the setlist — self-clean.
        # The allocation can LAG the failure (ghost entries were observed
        # materializing seconds after a status-1 create), so give the listing
        # a moment first; the callers' finally-blocks and the session state
        # guard back this up for anything slower.
        time.sleep(3)
        code2, out2, _ = helix("device", "setlists", "--json")
        if code2 == 0 and any(m.get("name") == name
                              for m in json.loads(out2)):
            helix("device", "setlist", "delete", name, "--yes")
        if "refused" not in last:
            break  # a different failure mode; retrying won't help
        if attempt < retries:
            time.sleep(CREATE_RETRY_COOLDOWN_S)
    if "refused" in last:
        pytest.xfail("backlog #38 recurrence: /CreateContent returned status 1 "
                     f"on `device setlist create {name}` (stub self-cleaned): "
                     f"{last}")
    pytest.fail(f"device setlist create {name!r} failed: {last}")


def delete_hgtest_setlists(helix) -> None:
    """Cleanup: delete every DEVICE setlist whose name starts with HGTEST."""
    code, out, _ = helix("device", "setlists", "--json")
    if code != 0:
        return
    for m in json.loads(out):
        name = m.get("name") or ""
        if name.startswith(HGTEST):
            helix("device", "setlist", "delete", name, "--yes")


# --------------------------------------------------------------------------
# local IR helpers
# --------------------------------------------------------------------------

def write_test_wav(path: Path, seed: int = 7, frames: int = 2048) -> Path:
    """Deterministic 48 kHz mono 16-bit impulse-ish WAV (stdlib only)."""
    import struct
    import wave

    rng_state = seed
    def rand16():
        nonlocal rng_state
        rng_state = (1103515245 * rng_state + 12345) % (1 << 31)
        return (rng_state >> 8) % 65536 - 32768

    samples = [32000]  # impulse head
    for i in range(1, frames):
        decay = max(0.0, 1.0 - i / frames)
        samples.append(int(rand16() * 0.25 * decay))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(struct.pack(f"<{frames}h", *samples))
    return path


@pytest.fixture(scope="session")
def hgtest_wav(cli, scratch: Path) -> Path:
    """An HGTEST test IR wav; skips if libsndfile can't hash it."""
    wav = write_test_wav(scratch / "work" / f"{HGTEST}-ir.wav")
    code, out, err = cli("irhash", wav, "--json")
    if code != 0:
        pytest.skip(f"cannot hash WAVs on this machine (libsndfile?): "
                    f"{err.strip() or out.strip()}")
    return wav


@pytest.fixture(scope="session")
def hgtest_wav_hash(cli, hgtest_wav: Path) -> str:
    code, out, err = cli("irhash", hgtest_wav, "--json")
    assert code == 0, err or out
    # irhash --json emits a JSON array of {hash, path, basename}.
    rec = next(r for r in json.loads(out) if r["basename"] == hgtest_wav.name)
    h = rec["hash"]
    assert re.fullmatch(r"[0-9a-f]{32}", h), f"bad irhash payload: {out!r}"
    return h
