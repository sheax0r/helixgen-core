"""Setlist verbs — device-side (`device setlist create/rename/delete/
duplicate`, export-hss/import-hss) and the local manifest side
(`device setlist list/add/remove/create-local/sync-on/sync-off`).

All device setlists are HGTEST-prefixed and deleted in finalizers (deleting
a setlist never orphans pool presets — references die, pool survives — and
these test setlists never reference anything anyway).

`import-hss` is NOT idempotent on retry, so it only ever targets a FRESH
HGTEST setlist created by the test itself (validated live 2026-07-15).
"""
from __future__ import annotations

import json

import pytest

from .conftest import HGTEST, create_device_setlist, delete_hgtest_setlists

pytestmark = [pytest.mark.live, pytest.mark.setlists]


def _setlist_names(helix) -> set[str]:
    code, out, err = helix("device", "setlists", "--json")
    assert code == 0, err or out
    return {m.get("name") for m in json.loads(out)}


def test_device_setlist_lifecycle(helix):
    """create → rename → duplicate → delete, verified via `device setlists`."""
    a, b, c = (f"{HGTEST}-SL-A", f"{HGTEST}-SL-B", f"{HGTEST}-SL-C")
    try:
        create_device_setlist(helix, a)
        assert a in _setlist_names(helix)

        code, out, err = helix("device", "setlist", "rename", a, b)
        assert code == 0, err or out
        names = _setlist_names(helix)
        assert b in names and a not in names

        code, out, err = helix("device", "setlist", "duplicate", b, c)
        assert code == 0, err or out
        assert {b, c} <= _setlist_names(helix)

        for name in (b, c):
            code, out, err = helix("device", "setlist", "delete", name, "--yes")
            assert code == 0, err or out
        names = _setlist_names(helix)
        assert not ({a, b, c} & names)
    finally:
        delete_hgtest_setlists(helix)


def test_local_manifest_membership(cli, scratch, hgtest_hsp):
    """create-local / add / list / remove / sync-on / sync-off — pure local
    manifest ops against the SCRATCH manifest (no device writes)."""
    sl = f"{HGTEST}-LOCAL"
    code, out, err = cli("device", "setlist", "create-local", sl)
    assert code == 0, err or out

    code, out, err = cli("device", "setlist", "add", sl, hgtest_hsp)
    assert code == 0, err or out

    code, out, err = cli("device", "setlist", "list", "--json")
    assert code == 0, err or out
    doc = json.loads(out)  # full manifest document: {version, tones, setlists, observed}
    assert f"{HGTEST} Base Tone" in doc["setlists"][sl]["tones"]

    code, out, err = cli("device", "setlist", "sync-on", sl)
    assert code == 0, err or out
    code, out, err = cli("device", "setlist", "sync-off", sl)
    assert code == 0, err or out

    code, out, err = cli("device", "setlist", "remove", sl,
                         f"{HGTEST} Base Tone")
    assert code == 0, err or out
    code, out, err = cli("device", "setlist", "list", "--json")
    assert code == 0, err or out
    doc = json.loads(out)
    assert f"{HGTEST} Base Tone" not in doc["setlists"][sl]["tones"]


def test_hss_export_import_roundtrip(helix, tmp_path):
    """export-hss a fresh (empty) HGTEST device setlist → offline --list
    decode → dry-run → import-hss targeting a SECOND fresh HGTEST setlist.

    The bundle is deliberately kept EMPTY: importing filled slots mints NEW
    pool presets by design (import-hss is not idempotent) and imported
    presets are pathless tones with no scoped delete verb, so a filled
    round-trip can't clean up after itself. An empty-bundle import is a
    documented no-op ({"ok": true, "created": false} — the destination
    setlist is NOT created), which is exactly what this asserts; the filled
    install path shares its primitives with `device sync`, which
    test_sync_lifecycle covers end-to-end."""
    src, dst = f"{HGTEST}-HSS-SRC", f"{HGTEST}-HSS-DST"
    bundle = tmp_path / "HGTEST-roundtrip.hss"
    try:
        create_device_setlist(helix, src)

        code, out, err = helix("device", "setlist", "export-hss", src, bundle)
        assert code == 0, err or out
        assert bundle.exists() and bundle.stat().st_size > 0

        # offline decode (no device writes)
        code, out, err = helix("device", "setlist", "import-hss", bundle,
                               "--list")
        assert code == 0, err or out

        # dry-run, then the real import — an empty bundle is a clean no-op
        code, out, err = helix("device", "setlist", "import-hss", bundle,
                               "--setlist", dst, "--dry-run")
        assert code == 0, err or out
        assert dst not in _setlist_names(helix), "--dry-run must not create"

        code, out, err = helix("device", "setlist", "import-hss", bundle,
                               "--setlist", dst)
        assert code == 0, err or out
        assert dst not in _setlist_names(helix), \
            "empty-bundle import must not create the destination setlist"
    finally:
        delete_hgtest_setlists(helix)
    assert not ({src, dst} & _setlist_names(helix))
