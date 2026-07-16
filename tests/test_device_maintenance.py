"""Unit tests for device/maintenance.py — IR delete/rename/prune planning and
the preset color/notes drivers.

Device-free throughout: the pure planning functions take plain data; the
device-driving entry points run against a FakeClient (the
``test_setlist_sync.py`` pattern) with ``maintenance.HelixClient``
monkeypatched.
"""
from __future__ import annotations

import contextlib

import pytest

msgpack = pytest.importorskip("msgpack")

from helixgen.device import content as _content  # noqa: E402
from helixgen.device import maintenance as mt  # noqa: E402
from helixgen.device.client import Container, HelixError  # noqa: E402


# ---------------------------------------------------------------------------
# content_ir_hashes
# ---------------------------------------------------------------------------

H1 = "aa" * 16
H2 = "bb" * 16


def test_content_ir_hashes_collects_nested_irmd():
    doc = {
        "sfg_": {"flow": [
            {"blks": {"b1": {"mdls": [{"irmd": bytes.fromhex(H1)}]}}},
            {"blks": {"b2": {"mdls": [
                {"irmd": bytes.fromhex(H2)},
                {"irmd": bytes.fromhex(H1)},  # dual-cab second slot
            ]}}},
        ]},
        "pm__": [{"key_": "preset.meta.info", "val_": "notes"}],
    }
    assert mt.content_ir_hashes(doc) == {H1, H2}


def test_content_ir_hashes_ignores_non_16_byte_and_non_bytes():
    doc = {"irmd": b"short", "x": {"irmd": "aa" * 16}}  # str, not bytes
    assert mt.content_ir_hashes(doc) == set()


# ---------------------------------------------------------------------------
# resolve_device_ir
# ---------------------------------------------------------------------------

IRS = [
    {"cid_": 1159, "name": "YA KW 412 M25 Mix 05", "hash": H1, "posi": 0},
    {"cid_": 1160, "name": "ZZC-test", "hash": H2, "posi": 1},
]


def test_resolve_by_exact_hash():
    assert mt.resolve_device_ir(IRS, H1)["cid_"] == 1159


def test_resolve_by_name_case_insensitive():
    assert mt.resolve_device_ir(IRS, "zzc-TEST")["cid_"] == 1160


def test_resolve_tolerates_wav_suffix():
    # device names have no extension; a user pasting the filename still works
    assert mt.resolve_device_ir(IRS, "ZZC-test.wav")["cid_"] == 1160


def test_resolve_absent_raises_with_candidates():
    with pytest.raises(ValueError, match="no device IR"):
        mt.resolve_device_ir(IRS, "nope")


def test_resolve_ambiguous_raises():
    dup = IRS + [{"cid_": 9, "name": "ZZC-test", "hash": "cc" * 16, "posi": 2}]
    with pytest.raises(ValueError, match="ambiguous"):
        mt.resolve_device_ir(dup, "ZZC-test")


# ---------------------------------------------------------------------------
# plan_ir_prune
# ---------------------------------------------------------------------------

def test_plan_ir_prune_buckets():
    irs = [
        {"cid_": 1, "name": "used-on-device", "hash": "11" * 16},
        {"cid_": 2, "name": "used-locally", "hash": "22" * 16},
        {"cid_": 3, "name": "orphan", "hash": "33" * 16},
    ]
    plan = mt.plan_ir_prune(
        irs,
        device_ref={"11" * 16: ["Some Preset"]},
        local_ref={"22" * 16: ["Off Device Tone"]},
    )
    assert [m["name"] for m in plan["referenced"]] == ["used-on-device"]
    assert [m["name"] for m in plan["protected"]] == ["used-locally"]
    assert plan["protected"][0]["local_tones"] == ["Off Device Tone"]
    assert [m["name"] for m in plan["orphans"]] == ["orphan"]


def test_plan_ir_prune_device_ref_wins_over_local():
    irs = [{"cid_": 1, "name": "both", "hash": "11" * 16}]
    plan = mt.plan_ir_prune(
        irs,
        device_ref={"11" * 16: ["P"]},
        local_ref={"11" * 16: ["T"]},
    )
    assert plan["referenced"] and not plan["protected"] and not plan["orphans"]


# ---------------------------------------------------------------------------
# color_index
# ---------------------------------------------------------------------------

def test_color_index_by_token_and_label():
    assert mt.color_index("red") == 2
    assert mt.color_index("Turquoise") == 7
    assert mt.color_index("dark orange") == 3
    assert mt.color_index("AUTO") == 0


def test_color_index_int_passthrough_and_range():
    assert mt.color_index(5) == 5
    assert mt.color_index("11") == 11
    with pytest.raises(ValueError, match="color"):
        mt.color_index(12)
    with pytest.raises(ValueError, match="color"):
        mt.color_index("chartreuse")


# ---------------------------------------------------------------------------
# fake client for the driver tests
# ---------------------------------------------------------------------------

def _blob(doc) -> bytes:
    return _content.encode_content_data(doc)


class FakeClient:
    """In-memory pool presets (with content) + user IRs + setlists."""

    def __init__(self, *args, **kwargs):
        self.pool = list(type(self).POOL)
        self.contents = dict(type(self).CONTENTS)  # cid -> decoded doc
        self.irs = list(type(self).IRS)
        self.setlists = list(type(self).SETLISTS)
        self.setlist_refs = {k: list(v) for k, v in type(self).SETLIST_REFS.items()}
        self.edit_buffer_hashes = list(type(self).EDIT_BUFFER_HASHES)
        self.deleted_irs = []
        self.set_content_calls = []
        self.set_attrs_calls = []
        self.loaded = []
        self.strict_calls = []

    POOL: list = []
    CONTENTS: dict = {}
    IRS: list = []
    SETLISTS: list = []
    SETLIST_REFS: dict = {}
    EDIT_BUFFER_HASHES: list = []
    #: cids that the device confirms exist (beyond the pool) when probed via
    #: ``get_ref`` — a reference to a cid NOT here reads as dangling (#32b).
    EXISTING_EXTRA_CIDS: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def mutating(self):
        return contextlib.nullcontext(self)

    @property
    def _raw(self):
        return self

    def list_presets(self, container=Container.POOL, strict=False):
        self.strict_calls.append(("presets", strict))
        return list(self.pool)

    def list_irs(self, strict=False):
        self.strict_calls.append(("irs", strict))
        return list(self.irs)

    def list_setlists(self, strict=False):
        self.strict_calls.append(("setlists", strict))
        return list(self.setlists)

    def list_container(self, cid, strict=False):
        self.strict_calls.append(("container", strict))
        return list(self.setlist_refs.get(cid, []))

    def get_ref(self, cid):
        # A pool preset (or an EXISTING_EXTRA cid) resolves; anything else is
        # gone — a dangling reference (#32b).
        known = {m["cid_"] for m in self.pool} | set(
            type(self).EXISTING_EXTRA_CIDS)
        return {"cid_": cid} if cid in known else None

    def get_edit_buffer(self):
        doc = {"pm__": [], "sfg_": {"flow": [{"blks": {"b0": {"mdls": [
            {"irmd": bytes.fromhex(h)} for h in self.edit_buffer_hashes]}}}]}}
        return _content.encode_content(doc)

    def get_content(self, cid):
        return _blob(self.contents[cid])

    def load_preset(self, cid):  # must never be called (non-activating!)
        self.loaded.append(cid)
        raise AssertionError("maintenance ops must never activate a preset")

    def delete_irs(self, cids):
        self.deleted_irs.extend(cids)
        self.irs = [m for m in self.irs if m["cid_"] not in set(cids)]
        return True

    def rename(self, cid, name):
        for m in self.irs:
            if m["cid_"] == cid:
                m["name"] = name
        return True

    def set_attrs(self, cid, attrs):
        self.set_attrs_calls.append((cid, dict(attrs)))
        return True

    def set_content_data(self, cid, blob):
        self.set_content_calls.append((cid, bytes(blob)))
        self.contents[cid] = _content.decode_any(bytes(blob))
        return True


class FakeManifest:
    def __init__(self, tones=None):
        self.tones = tones or {}


@pytest.fixture
def fake_client(monkeypatch, tmp_path):
    """Arm maintenance.HelixClient with a canned FakeClient class."""
    FakeClient.POOL = [
        {"cid_": 10, "name": "Uses H1", "cctp": 1000, "posi": 0},
    ]
    FakeClient.CONTENTS = {
        10: {"sfg_": {"flow": [{"blks": {"b0": {"mdls": [
            {"irmd": bytes.fromhex(H1)}]}}}]},
            "pm__": [{"key_": "preset.meta.info", "type": "s", "val_": "old"}]},
    }
    FakeClient.IRS = [
        {"cid_": 100, "name": "on-device-ref", "hash": H1, "posi": 0},
        {"cid_": 101, "name": "local-only", "hash": H2, "posi": 1},
        {"cid_": 102, "name": "ZZC-orphan", "hash": "33" * 16, "posi": 2},
    ]
    FakeClient.SETLISTS = []
    FakeClient.SETLIST_REFS = {}
    FakeClient.EDIT_BUFFER_HASHES = []
    FakeClient.EXISTING_EXTRA_CIDS = []
    monkeypatch.setattr(mt, "HelixClient", FakeClient)

    # keep the backing-file removal hermetic: no real SFTP in unit tests
    class _NoopSftp:
        removed = []

        def __init__(self, ip, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def remove_ir_file(self, name):
            type(self).removed.append(name)

    from helixgen.device import sftp as sftp_mod
    _NoopSftp.removed = []
    monkeypatch.setattr(sftp_mod, "HelixSFTP", _NoopSftp)
    return FakeClient


def _manifest_with_local_h2(tmp_path):
    """A manifest whose one tone references H2 via a real .hsp on disk."""
    import json

    hsp = tmp_path / "t.hsp"
    body = {"meta": {"name": "Local Tone"}, "preset": {"flow": [
        {"b0": {"slot": [{"irhash": H2}]}}]}}
    hsp.write_bytes(b"rpshnosj" + json.dumps(body).encode())
    return FakeManifest(tones={"Local Tone": {"path": str(hsp)}})


# ---------------------------------------------------------------------------
# ir_prune driver
# ---------------------------------------------------------------------------

def test_ir_prune_dry_run_by_default(fake_client, tmp_path):
    res = mt.ir_prune(ip="x", manifest=_manifest_with_local_h2(tmp_path))
    assert res["ok"] is True
    assert res["dry_run"] is True
    assert [m["name"] for m in res["orphans"]] == ["ZZC-orphan"]
    assert [m["name"] for m in res["protected"]] == ["local-only"]
    assert res["deleted"] == []


def test_ir_prune_execute_deletes_orphans_only(fake_client, tmp_path):
    res = mt.ir_prune(ip="x", execute=True,
                      manifest=_manifest_with_local_h2(tmp_path))
    assert res["dry_run"] is False
    assert [m["name"] for m in res["deleted"]] == ["ZZC-orphan"]
    # protected (locally referenced) IR was NOT deleted
    assert [m["name"] for m in res["protected"]] == ["local-only"]


def test_ir_prune_force_also_deletes_protected(fake_client, tmp_path):
    res = mt.ir_prune(ip="x", execute=True, force=True,
                      manifest=_manifest_with_local_h2(tmp_path))
    assert sorted(m["name"] for m in res["deleted"]) == [
        "ZZC-orphan", "local-only"]


def test_ir_prune_only_narrows_to_one_ir(fake_client, tmp_path):
    res = mt.ir_prune(ip="x", execute=True, force=True, only="ZZC-orphan",
                      manifest=_manifest_with_local_h2(tmp_path))
    assert [m["name"] for m in res["deleted"]] == ["ZZC-orphan"]


def test_ir_prune_only_never_matches_referenced(fake_client, tmp_path):
    # `only` naming a device-referenced IR is an error, not a deletion
    with pytest.raises(ValueError, match="referenced"):
        mt.ir_prune(ip="x", execute=True, only="on-device-ref",
                    manifest=_manifest_with_local_h2(tmp_path))


def test_ir_prune_never_activates(fake_client, tmp_path):
    res = mt.ir_prune(ip="x", execute=True,
                      manifest=_manifest_with_local_h2(tmp_path))
    assert res["ok"]  # FakeClient.load_preset raises if ever called


def test_ir_prune_aborts_on_content_read_failure(fake_client, tmp_path, monkeypatch):
    # an unreadable pool preset means the device reference set is INCOMPLETE —
    # deleting anything then would be unsafe, so the run must fail closed.
    def boom(self, cid):
        raise HelixError("flaky")

    monkeypatch.setattr(FakeClient, "get_content", boom)
    with pytest.raises(HelixError):
        mt.ir_prune(ip="x", execute=True,
                    manifest=_manifest_with_local_h2(tmp_path))


# ---------------------------------------------------------------------------
# notes / color drivers
# ---------------------------------------------------------------------------

def test_set_preset_notes_updates_existing_entry(fake_client):
    c = FakeClient()
    assert mt.set_preset_notes(c, 10, "new notes") is True
    pm = c.contents[10]["pm__"]
    hit = [e for e in pm if e.get("key_") == "preset.meta.info"]
    assert hit[0]["val_"] == "new notes"
    assert hit[0]["type"] == "s"


def test_set_preset_notes_inserts_when_absent(fake_client):
    FakeClient.CONTENTS = {10: {"pm__": [
        {"key_": "preset.tempo.bpm", "type": "f", "val_": 120.0},
        {"key_": "preset.xyctrl.x", "type": "i", "val_": 0},
    ], "sfg_": {}}}
    c = FakeClient()
    assert mt.set_preset_notes(c, 10, "hello") is True
    pm = c.contents[10]["pm__"]
    keys = [e["key_"] for e in pm]
    assert "preset.meta.info" in keys
    # pm__ stays sorted by key (the device's observed ordering)
    assert keys == sorted(keys)


def test_get_preset_notes(fake_client):
    c = FakeClient()
    assert mt.get_preset_notes(c, 10) == "old"


def test_set_preset_info_color_and_notes(fake_client):
    c = FakeClient()
    out = mt.set_preset_info(c, 10, color="red", notes="n")
    assert out == {"color": True, "notes": True}
    assert c.set_attrs_calls == [(10, {"colr": 2})]


def test_set_preset_info_requires_something(fake_client):
    c = FakeClient()
    with pytest.raises(ValueError, match="color"):
        mt.set_preset_info(c, 10)


# ---------------------------------------------------------------------------
# delete_device_ir — registry delete + backing-file removal
# ---------------------------------------------------------------------------

def test_delete_device_ir_removes_registry_and_file(fake_client, monkeypatch):
    removed = []

    class FakeSftp:
        def __init__(self, ip, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def remove_ir_file(self, name):
            removed.append(name)

    from helixgen.device import sftp as sftp_mod
    monkeypatch.setattr(sftp_mod, "HelixSFTP", FakeSftp)
    c = FakeClient()
    c.ir_paths = {"33" * 16: "/data/stadium-family-fw/ir/ZZC-orphan.wav"}
    c.ir_path_for_hash = lambda h: c.ir_paths.get(h)
    res = mt.delete_device_ir(c, "ZZC-orphan", ip="x")
    assert res["ok"] is True
    assert c.deleted_irs == [102]
    assert res["file_removed"] is True
    assert removed == ["ZZC-orphan.wav"]


def test_delete_device_ir_file_removal_best_effort(fake_client, monkeypatch):
    from helixgen.device import sftp as sftp_mod

    class BoomSftp:
        def __init__(self, ip, **kw):
            raise HelixError("no ssh key")

    monkeypatch.setattr(sftp_mod, "HelixSFTP", BoomSftp)
    c = FakeClient()
    c.ir_path_for_hash = lambda h: "/data/stadium-family-fw/ir/ZZC-orphan.wav"
    res = mt.delete_device_ir(c, "ZZC-orphan", ip="x")
    assert res["ok"] is True          # the registry delete still succeeded
    assert res["file_removed"] is False
    assert c.deleted_irs == [102]


def test_delete_device_ir_wedged_file_only_state(fake_client, monkeypatch):
    """A hash with no -11 entry but a resolving path index (the observed
    delete->quick-reimport wedge) is cleaned by removing the file alone."""
    removed = []

    class FakeSftp:
        def __init__(self, ip, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def remove_ir_file(self, name):
            removed.append(name)

    from helixgen.device import sftp as sftp_mod
    monkeypatch.setattr(sftp_mod, "HelixSFTP", FakeSftp)
    c = FakeClient()
    wedged = "dd" * 16
    c.ir_path_for_hash = lambda h: (
        "/data/stadium-family-fw/ir/ZZC-wedged.wav" if h == wedged else None)
    res = mt.delete_device_ir(c, wedged, ip="x", force_wedge=True)
    assert res == {"ok": True, "cid": None, "name": "ZZC-wedged",
                   "hash": wedged, "file_removed": True}
    assert removed == ["ZZC-wedged.wav"]
    assert c.deleted_irs == []  # nothing in the registry to delete


def test_delete_device_ir_unknown_name_still_raises(fake_client):
    c = FakeClient()
    with pytest.raises(ValueError, match="no device IR"):
        mt.delete_device_ir(c, "totally-unknown", ip="x")


# ---------------------------------------------------------------------------
# review #37 gating fixes
# ---------------------------------------------------------------------------

def test_ir_prune_lists_strictly(fake_client, tmp_path):
    """Every listing the prune plan trusts must be strict (finding 1a)."""
    c_holder = {}
    orig_init = FakeClient.__init__

    def spy_init(self, *a, **k):
        orig_init(self, *a, **k)
        c_holder["client"] = self

    FakeClient.__init__ = spy_init
    try:
        mt.ir_prune(ip="x", manifest=_manifest_with_local_h2(tmp_path))
    finally:
        FakeClient.__init__ = orig_init
    calls = c_holder["client"].strict_calls
    assert calls, "no listings recorded"
    assert all(strict is True for _kind, strict in calls), calls


def test_ir_prune_cross_check_catches_incomplete_pool(fake_client, tmp_path):
    """A setlist reference whose rcid is missing from the pool listing but
    which the device still HAS (get_ref resolves) means the pool listing is
    incomplete — abort with the retry error (finding 1b)."""
    FakeClient.SETLISTS = [{"cid_": 900, "name": "user", "cctp": 1001}]
    FakeClient.SETLIST_REFS = {900: [
        {"cid_": 901, "cctp": 1003, "rcid": 999, "posi": 0}]}  # 999 not in pool
    FakeClient.EXISTING_EXTRA_CIDS = [999]  # but the device still has it
    with pytest.raises(HelixError, match="incomplete"):
        mt.ir_prune(ip="x", execute=True,
                    manifest=_manifest_with_local_h2(tmp_path))


def test_ir_prune_detects_dangling_reference(fake_client, tmp_path):
    """A setlist reference whose rcid the device no longer HAS (get_ref returns
    None) is a DANGLING reference — abort with an actionable error naming the
    stale reference, not the misleading 'incomplete/reboot' one (#32b)."""
    FakeClient.SETLISTS = [{"cid_": 900, "name": "user", "cctp": 1001}]
    FakeClient.SETLIST_REFS = {900: [
        {"cid_": 901, "cctp": 1003, "rcid": 999, "posi": 0}]}  # 999 gone
    FakeClient.EXISTING_EXTRA_CIDS = []  # get_ref(999) -> None => dangling
    with pytest.raises(HelixError, match="dangling") as ei:
        mt.ir_prune(ip="x", execute=True,
                    manifest=_manifest_with_local_h2(tmp_path))
    msg = str(ei.value)
    assert "999" in msg and "user" in msg  # names the stale reference
    assert "reboot" not in msg.lower()     # not the old misleading advice


def test_ir_prune_execute_rescans_and_aborts_on_disagreement(
        fake_client, tmp_path):
    """Execute mode re-scans right before deleting; a differing plan aborts
    with nothing deleted (finding 1c)."""
    c_holder = {}
    orig_init = FakeClient.__init__

    def spy_init(self, *a, **k):
        orig_init(self, *a, **k)
        c_holder["client"] = self
        # first IR listing shows the orphan; the re-scan does not
        self._ir_listings = [
            list(type(self).IRS),
            [m for m in type(self).IRS if m["name"] != "ZZC-orphan"],
        ]

        def unstable_list_irs(strict=False):
            self.strict_calls.append(("irs", strict))
            return self._ir_listings.pop(0) if self._ir_listings else []

        self.list_irs = unstable_list_irs

    FakeClient.__init__ = spy_init
    try:
        with pytest.raises(HelixError, match="changed between"):
            mt.ir_prune(ip="x", execute=True,
                        manifest=_manifest_with_local_h2(tmp_path))
    finally:
        FakeClient.__init__ = orig_init
    assert c_holder["client"].deleted_irs == []


def test_ir_prune_counts_edit_buffer_references(fake_client, tmp_path):
    """An IR referenced only by the live edit buffer is NOT an orphan
    (finding 9)."""
    FakeClient.EDIT_BUFFER_HASHES = ["33" * 16]  # ZZC-orphan's hash
    res = mt.ir_prune(ip="x", manifest=_manifest_with_local_h2(tmp_path))
    assert res["orphans"] == []
    ref = [m for m in res["referenced"] if m["hash"] == "33" * 16]
    assert ref and ref[0]["presets"] == ["(edit buffer)"]


def test_ir_prune_edit_buffer_read_failure_fails_closed(fake_client, tmp_path,
                                                        monkeypatch):
    def boom(self):
        raise HelixError("no blob")

    monkeypatch.setattr(FakeClient, "get_edit_buffer", boom)
    with pytest.raises(HelixError, match="edit buffer"):
        mt.ir_prune(ip="x", execute=True,
                    manifest=_manifest_with_local_h2(tmp_path))


def test_local_refs_warn_and_fail_closed_on_unreadable_paths(fake_client,
                                                             tmp_path):
    """A tone with a recorded but missing/unreadable .hsp can't prove which
    IRs it protects — surface a warning; execute requires force (finding 4)."""
    import json

    hsp = tmp_path / "t.hsp"
    body = {"meta": {"name": "Local Tone"}, "preset": {"flow": [
        {"b0": {"slot": [{"irhash": H2}]}}]}}
    hsp.write_bytes(b"rpshnosj" + json.dumps(body).encode())
    manifest = FakeManifest(tones={
        "Local Tone": {"path": str(hsp)},
        "Ghost Tone": {"path": str(tmp_path / "missing.hsp")},
    })
    hashes, warnings = mt.local_referenced_ir_hashes(manifest)
    assert H2 in hashes
    assert warnings and "Ghost Tone" in warnings[0]

    # dry-run reports the warning
    res = mt.ir_prune(ip="x", manifest=manifest)
    assert res["warnings"] and "Ghost Tone" in res["warnings"][0]
    # execute without ignore_warnings refuses (fail closed)
    with pytest.raises(ValueError, match="Ghost Tone"):
        mt.ir_prune(ip="x", execute=True, manifest=manifest)
    # ignore_warnings overrides the warning gate (deletes the orphan)
    res = mt.ir_prune(ip="x", execute=True, ignore_warnings=True,
                      manifest=manifest)
    assert [m["name"] for m in res["deleted"]] != []


def test_local_refs_decode_sbe_sources(fake_client, tmp_path):
    """#68i: a tone whose recorded source is a .sbe (device content, the
    record `device push` writes) is decoded as device content — its irmd
    hashes protect IRs, and there is no bogus "missing rpshnosj magic"
    warning for a perfectly normal push flow."""
    sbe = tmp_path / "pushed.sbe"
    sbe.write_bytes(_content.encode_content_data(
        {"sfg_": {"flow": [{"blks": {"b1": {
            "mdls": [{"irmd": bytes.fromhex(H2)}]}}}]}}))
    manifest = FakeManifest(tones={"Pushed Tone": {"path": str(sbe)}})
    hashes, warnings = mt.local_referenced_ir_hashes(manifest)
    assert warnings == []
    assert hashes.get(H2) == ["Pushed Tone"]


def test_local_refs_unreadable_sbe_warns_accurately(fake_client, tmp_path):
    sbe = tmp_path / "corrupt.sbe"
    sbe.write_bytes(b"not a content blob at all")
    manifest = FakeManifest(tones={"Bad Push": {"path": str(sbe)}})
    hashes, warnings = mt.local_referenced_ir_hashes(manifest)
    assert hashes == {}
    assert warnings and ".sbe device-content source" in warnings[0]
    assert "rpshnosj" not in warnings[0]


def test_ir_prune_force_and_ignore_warnings_are_independent(fake_client,
                                                            tmp_path):
    """#32a: ``force`` (delete protected) and ``ignore_warnings`` (proceed over
    unverifiable-local-tone warnings) are SEPARATE consents.

    * ``force`` alone does NOT bypass a warning;
    * ``ignore_warnings`` alone does NOT delete a protected IR.
    """
    import json

    hsp = tmp_path / "t.hsp"
    body = {"meta": {"name": "Local Tone"}, "preset": {"flow": [
        {"b0": {"slot": [{"irhash": H2}]}}]}}
    hsp.write_bytes(b"rpshnosj" + json.dumps(body).encode())
    manifest = FakeManifest(tones={
        "Local Tone": {"path": str(hsp)},                       # protects H2
        "Ghost Tone": {"path": str(tmp_path / "missing.hsp")},  # warning
    })

    # force but NOT ignore_warnings: still blocked by the warning
    with pytest.raises(ValueError, match="Ghost Tone"):
        mt.ir_prune(ip="x", execute=True, force=True, manifest=manifest)

    # ignore_warnings but NOT force: proceeds, deletes only the orphan
    # (the protected local-only IR is left alone)
    res = mt.ir_prune(ip="x", execute=True, ignore_warnings=True,
                      manifest=manifest)
    names = [m["name"] for m in res["deleted"]]
    assert "ZZC-orphan" in names and "local-only" not in names

    # both consents: deletes the protected IR too
    res = mt.ir_prune(ip="x", execute=True, force=True, ignore_warnings=True,
                      manifest=manifest)
    assert sorted(m["name"] for m in res["deleted"]) == ["ZZC-orphan",
                                                         "local-only"]


def test_delete_device_ir_wedge_needs_force_wedge(fake_client, monkeypatch):
    """Without force_wedge, an unresolvable hash raises even when the path
    index resolves — protects a healthy-but-listing-lagged IR (finding 3)."""
    removed = []

    class FakeSftp:
        def __init__(self, ip, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def remove_ir_file(self, name):
            removed.append(name)

    from helixgen.device import sftp as sftp_mod
    monkeypatch.setattr(sftp_mod, "HelixSFTP", FakeSftp)
    c = FakeClient()
    wedged = "dd" * 16
    c.ir_path_for_hash = lambda h: "/data/stadium-family-fw/ir/ZZC-w.wav"
    with pytest.raises(ValueError, match="force"):
        mt.delete_device_ir(c, wedged, ip="x")
    assert removed == [] and c.deleted_irs == []


def test_resolve_device_ir_live_lists_strictly(fake_client):
    """#32c: the wedge-path resolution lists strictly, so a dropped/partial
    -11 listing raises HelixError instead of resolving as 'no such IR' and
    silently falling into the file-only wedge cleanup."""
    seen = []
    c = FakeClient()

    def strict_irs(strict=False):
        seen.append(strict)
        return list(c.irs)

    c.list_irs = strict_irs
    mt.resolve_device_ir_live(c, "on-device-ref")
    assert seen and all(s is True for s in seen), seen


def test_delete_device_ir_strict_listing_failure_propagates(fake_client,
                                                            monkeypatch):
    """#32c: a strict-listing HelixError during resolution propagates (fail
    closed) — it is NOT swallowed into the wedge file-removal path (which
    catches only ValueError)."""
    from helixgen.device import sftp as sftp_mod

    class BoomSftp:
        def __init__(self, ip, **kw):
            raise AssertionError("wedge file removal must not run")

    monkeypatch.setattr(sftp_mod, "HelixSFTP", BoomSftp)
    c = FakeClient()
    c.ir_path_for_hash = lambda h: "/data/stadium-family-fw/ir/ZZC-w.wav"

    def boom(strict=False):
        raise HelixError("no listing reply")

    c.list_irs = boom
    with pytest.raises(HelixError, match="no listing reply"):
        mt.delete_device_ir(c, "dd" * 16, ip="x", force_wedge=True)
    assert c.deleted_irs == []
