"""Live device WRITE verbs: install/load/create/rename/delete/set-info/
pull/push/save. Every artifact is HGTEST-prefixed, placed in a discovered
EMPTY slot, and deleted in a finalizer even on failure.

`device restore` is deliberately NOT exercised (recovery-only: it overwrites
an existing preset in place; pull→push covers the same content plumbing on
fresh HGTEST slots).

Residual: `device load` (here and in liveops) changes the ACTIVE tone —
whatever UNSAVED edit-buffer changes existed before the run are discarded,
and the edit buffer is left on the (deleted) HGTEST tone. Saved presets are
covered by the upfront session backup.

Setup note — the #38 guard needs a DIRTY edit buffer
----------------------------------------------------
Backlog #38 was root-caused 2026-07-19: field 3 of the /CreateContent
/status reply is the device's edit-buffer dirty flag (`hist` in
/EditBufferStateGet), not an error code. The old client read a dirty buffer
as failure and DELETED the content it had just correctly written.

So the interesting condition for this module is a dirty edit buffer, and a
clean one exercises nothing. No manual setup is needed — and none would
hold: `test_load_installed_preset` runs earlier here and `device load` clears
the flag. The `dirty_edit_buffer` fixture therefore dirties the buffer itself
immediately before the save, and SKIPS if it can't. The old code failed every
/CreateContent in that state; the current code must pass.
`test_save_edit_buffer` is the primary guard — if it SKIPS, the guard did not
run, which is not the same as passing.
"""
from __future__ import annotations

import json

import pytest

from .conftest import (CID_RE, HGTEST, delete_preset, find_user_preset,
                       install_preset)

pytestmark = [pytest.mark.live, pytest.mark.device_write]


@pytest.fixture(scope="module")
def installed(helix, hgtest_hsp, request):
    """One HGTEST preset installed for the module; deleted afterwards."""
    code, out, err = helix("device", "list", "--json")
    assert code == 0, err or out
    occupied = {m.get("posi") for m in json.loads(out)}
    free = [p for p in range(127, -1, -1) if p not in occupied]
    if not free:
        pytest.skip("no empty user slot for the write tests")
    pos = free[0]
    name = f"{HGTEST} Write Base"
    cid = install_preset(helix, hgtest_hsp, name, pos)
    yield {"cid": cid, "name": name, "pos": pos}
    delete_preset(helix, cid)
    leftover = find_user_preset(helix, name)
    assert leftover is None, f"cleanup failed: {name!r} still on device"


def test_install_and_read_back(helix, installed):
    code, out, err = helix("device", "read", installed["cid"], "--json")
    assert code == 0, err or out
    ref = json.loads(out)
    assert ref.get("name") == installed["name"]
    assert ref.get("posi") == installed["pos"]


def test_load_installed_preset(helix, installed):
    code, out, err = helix("device", "load", installed["cid"])
    assert code == 0, err or out


def test_create_copies_and_autonames(helix, installed, free_positions):
    """`device create` requires --from/--pos (NOT positionals) and auto-names
    the copy "<Name> (1)" — validated live 2026-07-15."""
    pos = free_positions(1)[0]
    copy_cid = None
    try:
        code, out, err = helix("device", "create",
                               "--from", installed["cid"], "--pos", pos)
        assert code == 0, err or out
        copy = find_user_preset(helix, f"{installed['name']} (1)")
        assert copy is not None, "copy not auto-named '<Name> (1)'"
        assert copy["posi"] == pos
        copy_cid = copy["cid_"]
    finally:
        if copy_cid is not None:
            delete_preset(helix, copy_cid)


def test_rename_and_rename_back(helix, installed):
    renamed = f"{HGTEST} Write Renamed"
    code, out, err = helix("device", "rename", installed["cid"], renamed)
    assert code == 0, err or out
    try:
        code, out, err = helix("device", "read", installed["cid"], "--json")
        assert code == 0, err or out
        assert json.loads(out).get("name") == renamed
    finally:
        code, out, err = helix("device", "rename", installed["cid"],
                               installed["name"])
        assert code == 0, f"rename-back failed: {err or out}"


def test_set_info_notes_and_color(helix, installed):
    code, out, err = helix("device", "set-info", installed["cid"],
                           "--notes", "HGTEST notes", "--color", "red")
    assert code == 0, err or out


def test_pull_push_roundtrip(helix, installed, free_positions, tmp_path):
    sbe = tmp_path / "HGTEST-roundtrip.sbe"
    code, out, err = helix("device", "pull", installed["cid"], sbe)
    assert code == 0, err or out
    assert sbe.stat().st_size > 0
    pos = free_positions(1)[0]
    pushed_name = f"{HGTEST} Write Pushed"
    pushed_cid = None
    try:
        code, out, err = helix("device", "push", sbe, pushed_name,
                               "--pos", pos)
        assert code == 0, err or out
        m = CID_RE.search(out)
        assert m, f"no cid in push output: {out!r}"
        pushed_cid = int(m.group(1))
        code, out, err = helix("device", "read", pushed_cid, "--json")
        assert code == 0, err or out
        assert json.loads(out).get("name") == pushed_name
    finally:
        if pushed_cid is not None:
            delete_preset(helix, pushed_cid)


def test_push_refuses_occupied_slot(helix, installed, tmp_path):
    sbe = tmp_path / "HGTEST-occupied.sbe"
    code, out, err = helix("device", "pull", installed["cid"], sbe)
    assert code == 0, err or out
    code, out, err = helix("device", "push", sbe, f"{HGTEST} Never Lands",
                           "--pos", installed["pos"])
    assert code != 0, "push into an occupied slot must fail safe"
    assert find_user_preset(helix, f"{HGTEST} Never Lands") is None


@pytest.fixture
def dirty_edit_buffer(helix, installed):
    """Leave the ACTIVE preset carrying an UNSAVED edit, and fail loudly if
    that can't be established.

    This has to be a fixture rather than an instruction to the operator:
    `test_load_installed_preset` runs earlier in this module and `device load`
    CLEARS the dirty flag, so by the time the #38 guard runs a hand-dirtied
    buffer is clean again and `/CreateContent` answers 0 — the uninteresting
    path. So dirty it here, immediately before the save.

    `device load` then `device set-param` on the live buffer is the same
    "tweak a knob without saving" a player would do. If no param can be
    written we SKIP rather than run green: a pass against a clean buffer would
    assert nothing about #38.
    """
    code, out, err = helix("device", "load", installed["cid"])
    assert code == 0, err or out
    # output block at path 0, grid slot 13 — the coordinates `device set-param`'s
    # own help documents as hardware-proven (fw 1.3.2).
    code, out, err = helix("device", "params", "0", "13", "--json")
    if code != 0:
        pytest.skip(f"cannot read live params to dirty the edit buffer: "
                    f"{(err or out).strip()}")
    params = json.loads(out).get("params") or []
    target = next((p for p in params
                   if p.get("name") == "gain" and p.get("pid") is not None), None)
    if target is None:
        pytest.skip("no writable 'gain' param on the active output block; "
                    "cannot establish the dirty edit buffer #38 needs")
    # nudge to a value distinct from the current one, staying inside the
    # param's own reported range
    current = float(target.get("value") or 0.0)
    lo = target.get("min")
    hi = target.get("max")
    new = current - 1.0
    if lo is not None and new < float(lo):
        new = current + 1.0
    if hi is not None and new > float(hi):
        pytest.skip("the active output gain has no headroom to nudge; "
                    "cannot establish the dirty edit buffer #38 needs")
    # `--` sentinel: a negative value (the usual case for an output gain in dB)
    # otherwise parses as an option and click rejects it, which would skip this
    # fixture and silently disarm the #38 regression guard below.
    code, out, err = helix("device", "set-param", "0", "13",
                           str(target["pid"]), "--", str(new))
    if code != 0:
        pytest.skip(f"cannot dirty the edit buffer via set-param: "
                    f"{(err or out).strip()}")
    return {"pid": target["pid"], "was": current, "now": new}


def test_save_edit_buffer(helix, installed, free_positions, dirty_edit_buffer):
    """`device save` of the edit buffer, with the buffer deliberately DIRTY.

    This test used to be xfailed under backlog #38. It is now the primary
    regression guard for the #38 root cause (root-caused 2026-07-19): saving
    while the active preset is DIRTY is exactly the condition that made
    /CreateContent report status 1 and the old client destroy the write. The
    `dirty_edit_buffer` fixture establishes that condition rather than
    trusting the module docstring's manual setup note — a clean buffer makes
    this test pass while exercising none of the fix.
    """
    name = f"{HGTEST} Save Probe"
    pos = free_positions(1)[0]
    try:
        code, out, err = helix("device", "save", name, "--pos", pos)
        assert code == 0, err or out
        saved = find_user_preset(helix, name)
        assert saved is not None
    finally:
        # whether the save half-worked or fully worked, remove any HGTEST stub
        saved = find_user_preset(helix, name)
        if saved is not None:
            delete_preset(helix, saved["cid_"])
