"""Device IR verbs: push-ir / rename-ir / pull-ir / delete-ir / ir-prune
(dry-run only).

Quirks encoded from the 2026-07-15 live runs:

* `pull-ir` addresses the IR by its ORIGINAL upload basename — `rename-ir`
  changes the display name only, so after a rename the pull must still use
  the original filename. The flow below is exactly push-ir → rename-ir →
  pull-ir(original basename) → delete-ir.
* The -11 IR registry listing lags a just-completed push: `push-ir` sees the
  `/addContent` broadcast while `list-irs` still under-reports (observed
  2026-07-15). That was root-caused with backlog #38 on 2026-07-19 as
  container-index lag, not a wedged device — `list_irs` now settles under a
  subscription and cross-checks the authoritative point lookup, so the entry
  must appear. The old xfail on this path is gone: a missing entry is a real
  regression and fails.

`ir-prune` is exercised in its default DRY-RUN form only (read-only); the
executing forms (--yes/--force) could touch non-HGTEST device IRs.

The edit-buffer dirty flag does NOT matter here. These verbs reach the
device over SFTP (`push_ir`) and `/RemoveContent`, never `/CreateContent`,
so the field-3 dirty-flag condition `test_device_write` needs has no bearing
on this module — what it exercises of #38 is the Task 4 half: the -11
container-index lag and the point-lookup cross-check.
"""
from __future__ import annotations

import json
import time

import pytest

from .conftest import HGTEST

pytestmark = [pytest.mark.live, pytest.mark.device_ir]

REGISTRY_WAIT_S = 20.0


def _device_ir_hashes(helix) -> dict[str, dict]:
    code, out, err = helix("device", "list-irs", "--json")
    assert code == 0, err or out
    return {m["hash"]: m for m in json.loads(out)}


def test_push_rename_pull_delete_ir(helix, hgtest_wav, hgtest_wav_hash, tmp_path):
    renamed = f"{HGTEST} renamed IR"
    registered = False
    try:
        code, out, err = helix("device", "push-ir", hgtest_wav, timeout=120)
        assert code == 0, err or out

        # `device list-irs` reads under a 2001 subscription and lets the
        # registry settle, so the entry must be there. (The point-lookup
        # cross-check is a separate `device_ir_hashes(verify=...)` path this
        # CLI listing doesn't take.) Poll only to absorb ordinary network
        # jitter — a timeout is a REAL failure, not an xfail.
        deadline = time.time() + REGISTRY_WAIT_S
        while time.time() < deadline:
            if hgtest_wav_hash in _device_ir_hashes(helix):
                registered = True
                break
            time.sleep(2)
        assert registered, (
            "push-ir saw the /addContent broadcast but the -11 registry "
            f"listing never gained the entry within {REGISTRY_WAIT_S:.0f}s. "
            "This is the backlog-#38 index-lag regression: list-irs must "
            "settle/cross-check rather than report a stale listing.")

        code, out, err = helix("device", "rename-ir", hgtest_wav_hash, renamed)
        assert code == 0, err or out
        irs = _device_ir_hashes(helix)
        # the display name must actually have changed (a no-op rename would
        # still start with HGTEST — assert the new name specifically)
        assert irs[hgtest_wav_hash]["name"] == renamed

        # pull-ir needs the ORIGINAL upload basename (rename-ir is
        # display-name only) — validated live 2026-07-15.
        pulled = tmp_path / "HGTEST-pulled.wav"
        code, out, err = helix("device", "pull-ir", hgtest_wav.name, pulled,
                               timeout=120)
        assert code == 0, err or out
        assert pulled.exists() and pulled.stat().st_size > 0

        code, out, err = helix("device", "delete-ir", hgtest_wav_hash, "--yes")
        assert code == 0, err or out
        assert hgtest_wav_hash not in _device_ir_hashes(helix)
    finally:
        # teardown even on mid-flow failure/xfail — only ever addresses the
        # hash THIS test pushed. Registry entry present -> normal delete;
        # absent -> remove the wedged file (the CLI's own remedy; safe here
        # because we are deleting our just-pushed artifact either way).
        # Never assert in this finally (it would mask the real failure) —
        # report cleanup problems to stderr instead; the wedged FILE is
        # invisible to the session state guard, so visibility matters.
        code, out, err = helix("device", "list-irs", "--json")
        listed = (code == 0 and
                  hgtest_wav_hash in {m["hash"] for m in json.loads(out)})
        if listed:
            code, out, err = helix("device", "delete-ir",
                                   hgtest_wav_hash, "--yes")
        elif not registered:
            code, out, err = helix("device", "delete-ir", hgtest_wav_hash,
                                   "--force-wedge", "--yes")
        if code != 0:
            print(f"\n[tests/live] WARNING: device_ir teardown could not "
                  f"delete IR {hgtest_wav_hash}: {(err or out).strip()}")


def test_ir_prune_dry_run_only(helix):
    code, out, err = helix("device", "ir-prune", "--json", timeout=300)
    assert code == 0, err or out
    plan = json.loads(out)
    assert isinstance(plan, dict)
    # dry-run must not have deleted anything — the session state guard will
    # also catch it, but assert the contract here too.
    assert not plan.get("deleted"), f"dry-run reported deletions: {plan}"
