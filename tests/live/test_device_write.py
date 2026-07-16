"""Live device WRITE verbs: install/load/create/rename/delete/set-info/
pull/push/save. Every artifact is HGTEST-prefixed, placed in a discovered
EMPTY slot, and deleted in a finalizer even on failure.

`device restore` is deliberately NOT exercised (recovery-only: it overwrites
an existing preset in place; pull→push covers the same content plumbing on
fresh HGTEST slots).
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


@pytest.mark.xfail(strict=False,
                   reason="backlog #38: /CreateContent intermittently returns "
                          "status 1 while still allocating (episodes observed "
                          "2026-07-14 and 2026-07-15 on fw 1.3.2; the CLI "
                          "self-cleans its stub on this path). `device save` "
                          "fails during an episode and XPASSes when the "
                          "device is healthy — strict=False so a healthy "
                          "device celebrates the fix instead of failing.")
def test_save_edit_buffer(helix, installed, free_positions):
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
