"""Live device READ verbs (never mutate the device): info/list/setlists/read/
blocks/local-list/library/slots list/settings list+get/globaleq list/tuner/
meters/measure/watch/backup/list-irs.

Reference values (e.g. "4 setlists / 29 pool presets / 24 IRs" on the
2026-07-15 validation day) are deliberately NOT hardcoded — everything is
captured dynamically; these tests assert shapes and invariants only.
"""
from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.live, pytest.mark.device_read]


def test_info_json(helix):
    code, out, err = helix("device", "info", "--json")
    assert code == 0, err or out
    info = json.loads(out)
    assert info  # identity fields come from /ProductInfoGet
    text = json.dumps(info).lower()
    assert "stadium" in text or "helix" in text


def test_list_user_json_shape(helix):
    code, out, err = helix("device", "list", "--json")
    assert code == 0, err or out
    presets = json.loads(out)
    assert isinstance(presets, list)
    for m in presets:
        assert "cid_" in m and "posi" in m


def test_setlists_json(helix):
    code, out, err = helix("device", "setlists", "--json")
    assert code == 0, err or out
    setlists = json.loads(out)
    assert isinstance(setlists, list) and setlists
    names = {m.get("name") for m in setlists}
    assert names  # device always has at least the stock setlists


def test_read_first_user_preset(helix):
    code, out, err = helix("device", "list", "--json")
    assert code == 0, err or out
    presets = json.loads(out)
    if not presets:
        pytest.skip("user setlist is empty; nothing to read")
    cid = presets[0]["cid_"]
    code, out, err = helix("device", "read", cid, "--json")
    assert code == 0, err or out
    ref = json.loads(out)
    assert ref.get("cid_") == cid


def test_blocks_edit_buffer(helix):
    code, out, err = helix("device", "blocks", "--json")
    assert code == 0, err or out
    blocks = json.loads(out)
    assert isinstance(blocks, list)
    for b in blocks:
        assert {"path", "block", "enabled"} <= set(b)


def test_list_irs_device_json(helix):
    code, out, err = helix("device", "list-irs", "--json")
    assert code == 0, err or out
    irs = json.loads(out)
    assert isinstance(irs, list)
    for m in irs:
        assert "hash" in m and "name" in m


def test_backup_and_local_list(helix, device_backup):
    # the upfront session backup already ran; verify its manifest is readable
    assert (device_backup / "manifest.json").exists()
    code, out, err = helix("device", "local-list", "--dir", device_backup,
                           "--json")
    assert code == 0, err or out
    entries = json.loads(out)
    assert isinstance(entries, list)


def test_library_and_slots_list_offline(helix):
    code, out, err = helix("device", "library", "--json")
    assert code == 0, err or out
    code, out, err = helix("device", "slots", "list")
    assert code == 0, err or out


def test_settings_list_offline_catalog(helix):
    code, out, err = helix("device", "settings", "list", "--json")
    assert code == 0, err or out
    catalog = json.loads(out)
    assert isinstance(catalog, dict) and catalog
    assert all(isinstance(keys, list) for keys in catalog.values())


def test_settings_get_first_key(helix):
    code, out, err = helix("device", "settings", "list", "--json")
    assert code == 0, err or out
    catalog = json.loads(out)
    key = sorted(catalog.items())[0][1][0]
    code, out, err = helix("device", "settings", "get", key, "--json")
    assert code == 0, err or out
    got = json.loads(out)
    assert got["key"] == key and "value" in got


def test_globaleq_list_offline(helix):
    code, out, err = helix("device", "globaleq", "list")
    assert code == 0, err or out
    assert out.strip()


def test_tuner_timeboxed(helix):
    code, out, err = helix("device", "tuner", "--seconds", "2", "--json",
                           timeout=30)
    assert code == 0, err or out


def test_meters_timeboxed(helix):
    code, out, err = helix("device", "meters", "--seconds", "2", "--json",
                           timeout=30)
    assert code == 0, err or out


def test_watch_timeboxed(helix):
    code, out, err = helix("device", "watch", "--seconds", "2", timeout=30)
    assert code == 0, err or out


def test_measure_not_playing_is_clean_failure(helix):
    """Nothing is playing during CI-style runs, so `measure`'s PASS condition
    for plumbing is the clean not-enough-playing result: JSON with ok:false +
    a reason, exit code 1 (validated live 2026-07-15). If someone IS playing,
    ok:true / exit 0 is equally valid plumbing — accept both."""
    code, out, err = helix("device", "measure", "--seconds", "3", "--json",
                           timeout=60)
    assert code in (0, 1), err or out
    result = json.loads(out)
    assert "ok" in result
    if code == 1:
        assert result["ok"] is False
        assert result.get("reason")
    else:
        assert result["ok"] is True
