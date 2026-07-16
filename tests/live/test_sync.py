"""Sync + tone-library verbs: device sync <setlist> / device add / unsync /
slots reorder (local) / device reorder (device-side).

Scope safety: ONLY targeted `device sync <HGTEST setlist>` is ever run —
`sync --all` (and `--gc`) is unscopeable to test artifacts and would
reconcile the user's real device setlists, so it is excluded from this suite.

The full lifecycle test closes the pool blind spot explicitly: the cleanup
sync's own `--json` result must report the HGTEST pool presets DELETED
(the session-level device-state diff cannot list the pool container -2
directly).
"""
from __future__ import annotations

import json

import pytest

from .conftest import (HGTEST, create_device_setlist, delete_hgtest_setlists,
                       generate_hsp)

pytestmark = [pytest.mark.live, pytest.mark.sync]

SETLIST = f"{HGTEST}-SYNC"
TONE_A = f"{HGTEST} Sync A"
TONE_B = f"{HGTEST} Sync B"


def test_device_add_and_unsync_are_local(cli, scratch, amp_blocks):
    """`device add` / `device unsync` mutate only the (scratch) manifest."""
    tone = f"{HGTEST} AddUnsync"
    generate_hsp(cli, scratch, tone, amp_blocks[0]["display_name"])
    code, out, err = cli("device", "add", tone, "--slot", "auto")
    assert code == 0, err or out
    code, out, err = cli("device", "library", "--json")
    assert code == 0, err or out
    assert tone in json.dumps(json.loads(out))
    code, out, err = cli("device", "unsync", tone)
    assert code == 0, err or out


def _sync_json(helix, setlist: str) -> dict:
    code, out, err = helix("device", "sync", setlist, "--json", timeout=600)
    assert code == 0, f"device sync {setlist} failed: {err or out}"
    return json.loads(out)


def test_sync_lifecycle(helix, cli, scratch, amp_blocks):
    """create setlist → add 2 tones → sync (installs) → re-sync (idempotent)
    → device-side reorder → local slots reorder + sync → unsync both + sync
    (pool presets deleted) → delete setlist."""
    hsp_a = generate_hsp(cli, scratch, TONE_A, amp_blocks[0]["display_name"])
    hsp_b = generate_hsp(cli, scratch, TONE_B, amp_blocks[0]["display_name"])
    try:
        create_device_setlist(helix, SETLIST)

        for hsp in (hsp_a, hsp_b):
            code, out, err = helix("device", "setlist", "add", SETLIST, hsp)
            assert code == 0, err or out

        res = _sync_json(helix, SETLIST)
        assert not res.get("errors"), res["errors"]
        installed = set(res["pool"].get("installed", []))
        assert {TONE_A, TONE_B} <= installed, res["pool"]

        # idempotent: second sync installs nothing, skips both
        res = _sync_json(helix, SETLIST)
        assert not res.get("errors"), res["errors"]
        assert not res["pool"].get("installed")
        assert {TONE_A, TONE_B} <= set(res["pool"].get("skipped", []))

        # direct DEVICE-side reorder within the HGTEST setlist
        code, out, err = helix("device", "reorder", SETLIST, TONE_B,
                               "--to", "0")
        assert code == 0, err or out

        # local manifest reorder + sync applies it back
        code, out, err = helix("device", "slots", "reorder", TONE_B,
                               "--to", "0", "--setlist", SETLIST)
        assert code == 0, err or out
        res = _sync_json(helix, SETLIST)
        assert not res.get("errors"), res["errors"]

        # unsync both → the next targeted sync deletes them from the device
        for tone in (TONE_A, TONE_B):
            code, out, err = helix("device", "unsync", tone)
            assert code == 0, err or out
        res = _sync_json(helix, SETLIST)
        assert not res.get("errors"), res["errors"]
        deleted = set(res["pool"].get("deleted", []))
        assert {TONE_A, TONE_B} <= deleted, (
            f"HGTEST pool presets not deleted by the cleanup sync: {res}")

        code, out, err = helix("device", "setlist", "delete", SETLIST, "--yes")
        assert code == 0, err or out
    finally:
        # belt-and-braces: unsync + sync once more, then drop HGTEST setlists
        for tone in (TONE_A, TONE_B):
            helix("device", "unsync", tone)
        helix("device", "sync", SETLIST, timeout=600)
        delete_hgtest_setlists(helix)
