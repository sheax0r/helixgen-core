"""Device IR verbs: push-ir / rename-ir / pull-ir / delete-ir / ir-prune
(dry-run only).

Quirks encoded from the 2026-07-15 live runs:

* `pull-ir` addresses the IR by its ORIGINAL upload basename — `rename-ir`
  changes the display name only, so after a rename the pull must still use
  the original filename. The flow below is exactly push-ir → rename-ir →
  pull-ir(original basename) → delete-ir.
* The device can enter a flaky episode (same family as backlog #38) where
  `push-ir` sees the `/addContent` broadcast ("imported + registered
  instantly", hash_match) yet the -11 IR registry listing NEVER gains the
  entry (observed 2026-07-15 evening: still unlisted 40+ min later, while
  the path index resolved the hash — the "wedged" shape). rename-ir /
  delete-ir resolve via the registry, so the flow can't continue; the test
  then cleans its own file via `delete-ir <its-own-hash> --force-wedge` (the
  CLI's designed wedge remedy, scoped strictly to the hash this test just
  pushed) and XFAILs.

`ir-prune` is exercised in its default DRY-RUN form only (read-only); the
executing forms (--yes/--force) could touch non-HGTEST device IRs.
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

        # the registry listing can lag the /addContent broadcast — poll
        deadline = time.time() + REGISTRY_WAIT_S
        while time.time() < deadline:
            if hgtest_wav_hash in _device_ir_hashes(helix):
                registered = True
                break
            time.sleep(2)
        if not registered:
            pytest.xfail(
                "device did not durably register the pushed IR: push-ir saw "
                "the /addContent broadcast but the -11 registry listing never "
                f"gained the entry within {REGISTRY_WAIT_S:.0f}s (flaky-device "
                "episode, backlog #38 family — observed live 2026-07-15; "
                "power-cycle the Helix and re-run)")

        code, out, err = helix("device", "rename-ir", hgtest_wav_hash, renamed)
        assert code == 0, err or out
        irs = _device_ir_hashes(helix)
        assert irs[hgtest_wav_hash]["name"].startswith(HGTEST)

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
        if hgtest_wav_hash in _device_ir_hashes(helix):
            helix("device", "delete-ir", hgtest_wav_hash, "--yes")
        elif not registered:
            helix("device", "delete-ir", hgtest_wav_hash,
                  "--force-wedge", "--yes")


def test_ir_prune_dry_run_only(helix):
    code, out, err = helix("device", "ir-prune", "--json", timeout=300)
    assert code == 0, err or out
    plan = json.loads(out)
    assert isinstance(plan, dict)
    # dry-run must not have deleted anything — the session state guard will
    # also catch it, but assert the contract here too.
    assert not plan.get("deleted"), f"dry-run reported deletions: {plan}"
