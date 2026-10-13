"""`device backup` / `restore` — the device photograph (2026-09-09 design).

Drives ``helixgen.device.snapshot`` against a stub client (the
``tests/test_device_copy.py`` / ``test_device_reorder.py`` pattern — no socket,
no real HelixClient, no hardware). SFTP is stubbed at the module attribute so
the IR pull path is exercised without a network.
"""
from __future__ import annotations

import hashlib
import json
import struct
from types import SimpleNamespace

import pytest

from helixgen.device import snapshot as S
from helixgen.device.client import Cctp, Container, HelixError


# ---------------------------------------------------------------------------
# fixtures: real-enough WAV + content blobs
# ---------------------------------------------------------------------------

def wav(payload: bytes) -> bytes:
    """A minimal RIFF/WAVE whose `data` chunk is exactly ``payload``."""
    body = b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 3, 1, 48000, 192000, 4, 32)
    body += b"data" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def irhash_of(payload: bytes) -> str:
    return hashlib.md5(payload).hexdigest()


def content(irmds=()) -> bytes:
    """An encoded native content blob referencing the given 16-byte irmds."""
    from helixgen.device import content as _content
    doc = {"mdls": [{"irmd": m} for m in irmds]}
    return _content.encode_content(doc)


def irmd_for(hash_hex: str) -> bytes:
    from helixgen.device import irmd as _irmd
    return _irmd.irhash_to_irmd(hash_hex)


class StubSFTP:
    """Stands in for `device pull-ir`'s SFTP transport."""

    def __init__(self, files, fail=()):
        self.files, self.fail = files, set(fail)
        self.downloaded = []

    def __call__(self, ip, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download_ir(self, name, local):
        self.downloaded.append(name)
        if name in self.fail:
            raise OSError("transport blew up")
        with open(local, "wb") as fh:
            fh.write(self.files[name])
        return local


class StubClient:
    ip = "10.0.0.99"

    def __init__(self, *, pool=None, setlists=None, refs=None, irs=None,
                 ir_paths=None, blobs=None, list_irs_error=None):
        self.pool = [dict(m) for m in (pool or [])]
        self.setlists = [dict(m) for m in (setlists or [])]
        self.refs = {k: [dict(m) for m in v] for k, v in (refs or {}).items()}
        self.irs = [dict(m) for m in (irs or [])]
        self.ir_paths = dict(ir_paths or {})
        self.blobs = dict(blobs or {})
        self.list_irs_error = list_irs_error
        self.calls = []
        self.strict = []
        self.loaded = []

    # -- reads ----------------------------------------------------------
    def list_presets(self, container=Container.POOL, *, strict=False):
        self.strict.append(("pool", strict))
        return [dict(m) for m in self.pool]

    def list_setlists(self, *, strict=False):
        self.strict.append(("setlists", strict))
        return [dict(m) for m in self.setlists]

    def list_container(self, cid, *, strict=False):
        self.strict.append((cid, strict))
        return [dict(m) for m in self.refs.get(cid, [])]

    def get_content(self, cid):
        self.calls.append(("get_content", cid))
        return self.blobs[cid]

    def load_preset(self, cid):  # must never be called by a backup
        self.loaded.append(cid)
        return True

    def list_irs(self, *, strict=False, **kw):
        if self.list_irs_error:
            raise HelixError(self.list_irs_error)
        return [dict(m) for m in self.irs]

    def ir_path_for_hash(self, h, *, strict=False):
        return self.ir_paths.get(h)

    def product_info(self):
        return {"serial": "SN-TEST-1", "model": "Stadium XL",
                "firmware": "1.3.2"}

    def get_property(self, key):
        # A real device answers every global; returning a value keeps errors[]
        # empty, which matters because `take` deliberately refuses to prune
        # after ANY failed read (a preset it couldn't fetch must not look
        # deleted and take its previous backup down with it).
        return SimpleNamespace(value=f"v:{key}")


@pytest.fixture
def two_presets():
    return dict(
        pool=[{"cid_": 101, "name": "Dream On", "posi": 0},
              {"cid_": 102, "name": "Back In Black", "posi": 1}],
        setlists=[{"cid_": 5001, "name": "Gigs"}],
        refs={5001: [{"cctp": Cctp.REFERENCE, "rcid": 102, "posi": 0},
                     {"cctp": Cctp.REFERENCE, "rcid": 101, "posi": 1}]},
        blobs={101: content(), 102: content()},
    )


# ---------------------------------------------------------------------------
# take: layout, order, idempotence
# ---------------------------------------------------------------------------

def test_take_writes_the_documented_tree(tmp_path, two_presets):
    res = S.take(StubClient(**two_presets), tmp_path)
    base = tmp_path / "SN-TEST-1"
    assert (base / "pool" / "Dream-On.sbe").is_file()
    assert (base / "pool" / "Back-In-Black.sbe").is_file()
    assert (base / "setlists" / "Gigs.json").is_file()
    assert (base / "device.json").is_file()
    assert res["serial"] == "SN-TEST-1"
    assert not res["errors"]


def test_take_captures_setlist_ORDER_not_pool_order(tmp_path, two_presets):
    """The setlist JSON is the reference order, which differs from pool order."""
    S.take(StubClient(**two_presets), tmp_path)
    order = json.loads(
        (tmp_path / "SN-TEST-1" / "setlists" / "Gigs.json").read_text())
    assert order == [{"name": "Back In Black", "posi": 0},
                     {"name": "Dream On", "posi": 1}]


def test_take_is_non_activating(tmp_path, two_presets):
    """A backup must never disturb the player's live tone."""
    c = StubClient(**two_presets)
    S.take(c, tmp_path)
    assert c.loaded == []
    assert all(k == "get_content" for k, _ in c.calls)


def test_take_uses_strict_listings(tmp_path, two_presets):
    """#40: a listing timeout must abort, never read as an empty device."""
    c = StubClient(**two_presets)
    S.take(c, tmp_path)
    assert all(strict for _, strict in c.strict), c.strict


def test_rerun_rewrites_nothing(tmp_path, two_presets):
    S.take(StubClient(**two_presets), tmp_path)
    stamps = {p: p.stat().st_mtime_ns
              for p in (tmp_path / "SN-TEST-1").rglob("*") if p.is_file()}
    res = S.take(StubClient(**two_presets), tmp_path)
    assert res["changed"] == []
    assert res["pool"]["written"] == []
    assert {p: p.stat().st_mtime_ns
            for p in (tmp_path / "SN-TEST-1").rglob("*") if p.is_file()} == stamps


def test_dry_run_writes_nothing_but_reports_accurately(tmp_path, two_presets):
    res = S.take(StubClient(**two_presets), tmp_path, dry_run=True)
    assert not (tmp_path / "SN-TEST-1").exists()
    assert any("Dream-On.sbe" in c for c in res["changed"])
    # and the report matches what a real run then produces
    real = S.take(StubClient(**two_presets), tmp_path)
    assert sorted(real["changed"]) == sorted(res["changed"])


def test_changed_marks_new_vs_modified(tmp_path, two_presets):
    S.take(StubClient(**two_presets), tmp_path)
    edited = dict(two_presets)
    edited["blobs"] = {101: content(), 102: b"_sbepgsm-different"}
    res = S.take(StubClient(**edited), tmp_path, dry_run=True)
    assert [c for c in res["changed"] if c.startswith("~")]
    assert not [c for c in res["changed"] if c.startswith("+")]


def test_deleted_preset_is_pruned_from_the_photograph(tmp_path, two_presets):
    S.take(StubClient(**two_presets), tmp_path)
    gone = dict(two_presets)
    gone["pool"] = [two_presets["pool"][0]]
    gone["blobs"] = {101: content()}
    gone["refs"] = {5001: [{"cctp": Cctp.REFERENCE, "rcid": 101, "posi": 0}]}
    S.take(StubClient(**gone), tmp_path)
    assert not (tmp_path / "SN-TEST-1" / "pool" / "Back-In-Black.sbe").exists()


def test_a_partial_read_never_prunes(tmp_path, two_presets):
    """A preset we failed to fetch must not look deleted and take its backup
    down with it."""
    S.take(StubClient(**two_presets), tmp_path)

    class Flaky(StubClient):
        def get_content(self, cid):
            if cid == 102:
                raise HelixError("dropped")
            return super().get_content(cid)

    res = S.take(Flaky(**two_presets), tmp_path)
    assert res["errors"]
    assert (tmp_path / "SN-TEST-1" / "pool" / "Back-In-Black.sbe").is_file()


# ---------------------------------------------------------------------------
# naming: filesystem-safe AND collision-safe
# ---------------------------------------------------------------------------

def test_colliding_names_do_not_overwrite_each_other():
    names = ["A/B", "A:B"]
    got = S.assign_filenames(names, ".sbe")
    assert len(set(got)) == 2, got


def test_case_differing_names_do_not_collide_on_a_case_insensitive_fs():
    """macOS and Windows filesystems are case-insensitive by default, so
    "Lead" and "lead" are ONE file. Comparing with a case-SENSITIVE set said
    these were distinct, and the second silently clobbered the first
    (adversarial review, HIGH 4)."""
    got = S.assign_filenames(["Lead", "lead"], ".sbe")
    assert len({g.casefold() for g in got}) == 2, got


def test_case_differing_presets_both_survive_a_real_backup(tmp_path):
    c = StubClient(
        pool=[{"cid_": 1, "name": "Lead", "posi": 0},
              {"cid_": 2, "name": "lead", "posi": 1}],
        blobs={1: content(), 2: b"_sbepgsm-two"},
    )
    S.take(c, tmp_path)
    files = list((tmp_path / "SN-TEST-1" / "pool").glob("*.sbe"))
    assert len(files) == 2, [f.name for f in files]
    assert len({f.read_bytes() for f in files}) == 2


def test_two_presets_that_sanitise_alike_both_survive(tmp_path):
    c = StubClient(
        pool=[{"cid_": 1, "name": "A/B", "posi": 0},
              {"cid_": 2, "name": "A:B", "posi": 1}],
        blobs={1: content(), 2: b"_sbepgsm-two"},
    )
    S.take(c, tmp_path)
    files = list((tmp_path / "SN-TEST-1" / "pool").glob("*.sbe"))
    assert len(files) == 2
    assert len({f.read_bytes() for f in files}) == 2


# ---------------------------------------------------------------------------
# the IR cross-check (#38)
# ---------------------------------------------------------------------------

@pytest.fixture
def ir_setup(monkeypatch):
    payload = b"\x01\x02\x03\x04" * 8
    h = irhash_of(payload)
    files = {"cab.wav": wav(payload)}
    sftp = StubSFTP(files)
    monkeypatch.setattr(S, "HelixSFTP", sftp, raising=False)
    import helixgen.device.sftp as _s
    monkeypatch.setattr(_s, "HelixSFTP", sftp)
    return h, payload, sftp


def test_ir_referenced_but_UNLISTED_is_still_collected(tmp_path, ir_setup):
    """The -11 listing cache goes stale (#38) — a preset's own reference is the
    other half of the cross-check, and it must win."""
    h, _payload, sftp = ir_setup
    c = StubClient(
        pool=[{"cid_": 1, "name": "Tone", "posi": 0}],
        blobs={1: content([irmd_for(h)])},
        irs=[],                       # listing says there are NO IRs
        ir_paths={h: "/data/ir/cab.wav"},
    )
    res = S.take(c, tmp_path)
    assert h in res["irs"]["pulled"], res["irs"]
    assert (tmp_path / "SN-TEST-1" / "irs" / f"{h}.wav").is_file()


def test_md5_mismatch_lands_in_missing_not_on_disk(tmp_path, monkeypatch):
    payload = b"\x09" * 32
    h = irhash_of(payload)
    sftp = StubSFTP({"cab.wav": wav(b"\x00" * 32)})   # wrong bytes
    import helixgen.device.sftp as _s
    monkeypatch.setattr(_s, "HelixSFTP", sftp)
    c = StubClient(
        pool=[{"cid_": 1, "name": "Tone", "posi": 0}],
        blobs={1: content([irmd_for(h)])},
        ir_paths={h: "/data/ir/cab.wav"},
    )
    res = S.take(c, tmp_path)
    assert [m for m in res["irs"]["missing"] if m["irhash"] == h]
    assert not (tmp_path / "SN-TEST-1" / "irs" / f"{h}.wav").exists()


def test_unregistered_ir_is_reported_not_silently_dropped(tmp_path, monkeypatch):
    h = irhash_of(b"nope")
    import helixgen.device.sftp as _s
    monkeypatch.setattr(_s, "HelixSFTP", StubSFTP({}))
    c = StubClient(
        pool=[{"cid_": 1, "name": "Tone", "posi": 0}],
        blobs={1: content([irmd_for(h)])},
        ir_paths={},                  # device cannot resolve it at all
    )
    res = S.take(c, tmp_path)
    assert [m for m in res["irs"]["missing"] if m["irhash"] == h]


def test_verified_ir_is_not_re_pulled(tmp_path, ir_setup):
    """IR files are content-addressed, so an existing verified copy is provably
    identical — re-pulling it would be pure waste."""
    h, _payload, sftp = ir_setup
    kw = dict(pool=[{"cid_": 1, "name": "Tone", "posi": 0}],
              blobs={1: content([irmd_for(h)])},
              ir_paths={h: "/data/ir/cab.wav"})
    S.take(StubClient(**kw), tmp_path)
    assert sftp.downloaded == ["cab.wav"]
    res = S.take(StubClient(**kw), tmp_path)
    assert sftp.downloaded == ["cab.wav"]          # no second pull
    assert h in res["irs"]["unchanged"]


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def test_plan_restore_is_additive_without_prune(tmp_path, two_presets):
    S.take(StubClient(**two_presets), tmp_path)
    wiped = StubClient(pool=[], setlists=two_presets["setlists"], refs={5001: []},
                       blobs={})
    plan = S.plan_restore(wiped, tmp_path, serial="SN-TEST-1")
    assert {o["op"] for o in plan["ops"]} <= {"create", "setlist_order",
                                              "reference", "move", "push_ir"}
    assert not [o for o in plan["ops"] if "delete" in o["op"]]


def test_plan_restore_prune_deletes_only_what_the_snapshot_lacks(
        tmp_path, two_presets):
    S.take(StubClient(**two_presets), tmp_path)
    extra = dict(two_presets)
    extra["pool"] = two_presets["pool"] + [
        {"cid_": 103, "name": "Stray", "posi": 2}]
    extra["blobs"] = {**two_presets["blobs"], 103: content()}
    plan = S.plan_restore(StubClient(**extra), tmp_path, serial="SN-TEST-1",
                          prune=True)
    deletes = [o for o in plan["ops"] if "delete" in o["op"]]
    assert len(deletes) == 1
    assert "Stray" in json.dumps(deletes)


def test_plan_restore_reports_no_work_when_device_matches(tmp_path, two_presets):
    S.take(StubClient(**two_presets), tmp_path)
    plan = S.plan_restore(StubClient(**two_presets), tmp_path,
                          serial="SN-TEST-1")
    assert not [o for o in plan["ops"] if o["op"] in ("create", "update")]


def test_plan_restore_reports_a_missing_snapshot(tmp_path):
    """A restore pointed at a snapshot that isn't there must say so, not
    quietly plan zero ops and look like a clean no-op run."""
    plan = S.plan_restore(StubClient(), tmp_path, serial="NOPE")
    assert plan["ops"] == []
    assert plan["errors"] and "no snapshot" in plan["errors"][0]


def test_plan_restore_refuses_an_ambiguous_root(tmp_path):
    (tmp_path / "SN-A").mkdir()
    (tmp_path / "SN-B").mkdir()
    with pytest.raises(HelixError):
        S.plan_restore(StubClient(), tmp_path)


# ---------------------------------------------------------------------------
# git textconv
# ---------------------------------------------------------------------------

def test_ensure_git_textconv_is_idempotent(tmp_path):
    S.ensure_git_textconv(tmp_path)
    first = (tmp_path / ".gitattributes").read_text()
    S.ensure_git_textconv(tmp_path)
    assert (tmp_path / ".gitattributes").read_text() == first
    assert "*.sbe" in first


def test_ensure_git_textconv_keeps_unrelated_lines(tmp_path):
    (tmp_path / ".gitattributes").write_text("*.png binary\n")
    S.ensure_git_textconv(tmp_path)
    text = (tmp_path / ".gitattributes").read_text()
    assert "*.png binary" in text
    assert "*.sbe" in text


# ---------------------------------------------------------------------------
# adversarial-review regressions (2026-09-09)
# ---------------------------------------------------------------------------

def test_a_failed_IR_pull_never_prunes_an_already_backed_up_wav(tmp_path,
                                                                monkeypatch):
    """CRITICAL 2: IR failures land in irs["missing"], not errors[], so the
    `if not errors` prune guard let one flaky /IrPathForHashGet delete a
    verified .wav this backup may be the only copy of."""
    payload = b"\x01\x02\x03\x04" * 8
    h = irhash_of(payload)
    sftp = StubSFTP({"cab.wav": wav(payload)})
    import helixgen.device.sftp as _s
    monkeypatch.setattr(_s, "HelixSFTP", sftp)
    kw = dict(pool=[{"cid_": 1, "name": "Tone", "posi": 0}],
              blobs={1: content([irmd_for(h)])},
              ir_paths={h: "/data/ir/cab.wav"})
    S.take(StubClient(**kw), tmp_path)
    stored = tmp_path / "SN-TEST-1" / "irs" / f"{h}.wav"
    assert stored.is_file()

    class Flaky(StubClient):
        def ir_path_for_hash(self, hh, *, strict=False):
            raise HelixError("timed out")

    res = S.take(Flaky(**kw), tmp_path)
    assert res["irs"]["missing"]
    assert stored.is_file(), "a transient IR failure deleted the backup"
    assert not any(c.startswith("- irs/") for c in res["changed"])


def test_prune_refuses_a_snapshot_with_no_index(tmp_path, two_presets):
    """CRITICAL 3: a missing/corrupt index.json read as "the snapshot has no
    presets", so --prune emitted a delete for every live preset and reported
    zero errors."""
    S.take(StubClient(**two_presets), tmp_path)
    (tmp_path / "SN-TEST-1" / "index.json").unlink()
    plan = S.plan_restore(StubClient(**two_presets), tmp_path,
                          serial="SN-TEST-1", prune=True)
    assert plan["ops"] == []
    assert plan["errors"] and "index.json" in plan["errors"][0]


def test_prune_refuses_an_empty_pool_index(tmp_path, two_presets):
    S.take(StubClient(**two_presets), tmp_path)
    idx = tmp_path / "SN-TEST-1" / "index.json"
    idx.write_text(json.dumps({"pool": {}, "setlists": {}}))
    plan = S.plan_restore(StubClient(**two_presets), tmp_path,
                          serial="SN-TEST-1", prune=True)
    assert not [o for o in plan["ops"] if "delete" in o["op"]]
    assert plan["errors"]


def test_restore_refuses_a_snapshot_from_another_device(tmp_path, two_presets):
    """HIGH 5: nothing compared the snapshot's serial to the connected device,
    so `restore --prune --yes` could wipe and repopulate the WRONG Helix."""
    S.take(StubClient(**two_presets), tmp_path)
    dev = tmp_path / "SN-TEST-1" / "device.json"
    doc = json.loads(dev.read_text())
    doc["serial"] = "SOME-OTHER-HELIX"
    dev.write_text(json.dumps(doc))
    plan = S.plan_restore(StubClient(**two_presets), tmp_path,
                          serial="SN-TEST-1", prune=True)
    assert plan["ops"] == []
    assert any("across devices" in e for e in plan["errors"])


def test_backup_wires_the_git_textconv(tmp_path, two_presets):
    """MEDIUM 8: ensure_git_textconv was never called, so the documented
    readable .sbe diffs did not actually happen."""
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True)
    S.take(StubClient(**two_presets), tmp_path)
    assert "*.sbe" in (tmp_path / ".gitattributes").read_text()
    got = subprocess.run(
        ["git", "-C", str(tmp_path), "config", "--get",
         "diff.helixgen-sbe.textconv"], capture_output=True, text=True)
    assert "device decode" in got.stdout


class _CapClient:
    ip = "10.0.0.99"

    def __init__(self, n):
        self.n = n
        self.applied = []

    def list_irs(self, *, strict=False, settle=True, include_unusable=False):
        return [{"hash": None}] * self.n

    def mutating(self):
        import contextlib
        return contextlib.nullcontext()


def test_apply_restore_refuses_irs_past_the_cap_before_uploading(monkeypatch):
    """hgc-6r5.8: restore is an IR uploader too — refuse before any write."""
    monkeypatch.setattr(S, "_apply_op",
                        lambda client, op: client.applied.append(op))
    plan = {"ops": [{"op": "push_ir", "irhash": "a" * 32, "file": "x"},
                    {"op": "push_ir", "irhash": "b" * 32, "file": "y"},
                    {"op": "create", "name": "Tone", "file": "z"}]}
    c = _CapClient(2047)
    res = S.apply_restore(c, plan)
    assert c.applied == []
    assert res["ok"] is False and res["applied"] == 0
    assert len(res["errors"]) == 1 and "1 more" in res["errors"][0]


def test_apply_restore_within_cap_runs(monkeypatch):
    monkeypatch.setattr(S, "_apply_op",
                        lambda client, op: client.applied.append(op))
    plan = {"ops": [{"op": "push_ir", "irhash": "a" * 32, "file": "x"}]}
    c = _CapClient(2047)
    assert S.apply_restore(c, plan)["ok"]
    assert len(c.applied) == 1

def test_a_global_absent_from_this_firmware_is_not_a_failed_read(tmp_path, two_presets):
    """fw 1.4 dropped some catalog keys (e.g. global.clip.tm.gap, renamed
    global.timemachine.gap). The device answers them status -21 with an empty
    blob — the key does not exist, so it must neither be backed up nor count
    as a failed read (which would block pruning forever)."""
    from helixgen.device.client import PropertyUnavailable

    class Fw14(StubClient):
        def get_property(self, key):
            if key == "global.clip.tm.gap":
                raise PropertyUnavailable("not on this firmware")
            return super().get_property(key)

    res = S.take(Fw14(**two_presets), tmp_path)
    assert res["errors"] == []
    dev = json.loads(next(tmp_path.glob("*/device.json")).read_text())
    assert "global.clip.tm.gap" not in dev["globals"]


def test_firmware_specific_globals_are_skipped_whatever_the_failure(tmp_path, two_presets):
    """A 1.3 device's reply to the 1.4-only keys is uncharacterized (could be
    /error or a timeout). Failures on the firmware-specific catalog keys must
    never block pruning; failures on any other key still count."""
    class Old(StubClient):
        def get_property(self, key):
            if key in ("global.exp.2.pedal.position", "global.clip.tm.gap",
                       "global.tuner.type"):
                raise HelixError("no blob in reply")
            return super().get_property(key)

    res = S.take(Old(**two_presets), tmp_path)
    assert res["errors"] == ["global global.tuner.type: no blob in reply"]


# ---------------------------------------------------------------------------
# hgc-6r5.14: fw 1.4 setlists keep real gaps — backups record posi, restore
# puts each reference back at it
# ---------------------------------------------------------------------------

def _gapped(two_presets, posis=(0, 3)):
    g = dict(two_presets)
    g["refs"] = {5001: [{"cctp": Cctp.REFERENCE, "rcid": 102, "posi": posis[0], "cid_": 9001},
                        {"cctp": Cctp.REFERENCE, "rcid": 101, "posi": posis[1], "cid_": 9002}]}
    return g


def _setlist_ops(plan):
    return [o for o in plan["ops"] if o["op"] in ("reference", "remove_ref", "move")]


def test_take_records_the_posi_of_a_gapped_setlist(tmp_path, two_presets):
    S.take(StubClient(**_gapped(two_presets)), tmp_path)
    order = json.loads((tmp_path / "SN-TEST-1" / "setlists" / "Gigs.json").read_text())
    assert order == [{"name": "Back In Black", "posi": 0},
                     {"name": "Dream On", "posi": 3}]


def test_restore_puts_references_back_at_their_recorded_posi(tmp_path, two_presets):
    S.take(StubClient(**_gapped(two_presets)), tmp_path)
    emptied = dict(two_presets, refs={5001: []})
    ops = _setlist_ops(S.plan_restore(StubClient(**emptied), tmp_path, serial="SN-TEST-1"))
    assert [(o["op"], o["name"], o.get("pos")) for o in ops] == [
        ("reference", "Back In Black", 0), ("reference", "Dream On", 3)]


def test_restore_of_a_matching_gapped_setlist_is_a_no_op(tmp_path, two_presets):
    S.take(StubClient(**_gapped(two_presets)), tmp_path)
    plan = S.plan_restore(StubClient(**_gapped(two_presets)), tmp_path, serial="SN-TEST-1")
    assert _setlist_ops(plan) == [] and not plan["errors"]


def test_restore_moves_a_misplaced_reference_by_remove_then_reference(tmp_path, two_presets):
    S.take(StubClient(**_gapped(two_presets)), tmp_path)        # want 0, 3
    live = StubClient(**_gapped(two_presets, posis=(0, 1)))    # have 0, 1
    ops = _setlist_ops(S.plan_restore(live, tmp_path, serial="SN-TEST-1"))
    assert [(o["op"], o["name"], o.get("pos")) for o in ops] == [
        ("remove_ref", "Dream On", None), ("reference", "Dream On", 3)]


def test_restore_never_clobbers_an_incumbent_outside_the_backup(tmp_path, two_presets):
    """fw 1.4: a reference added at an occupied setlist posi REPLACES the
    incumbent reference (HW-observed) — without --prune that would silently
    drop a preset the user added since the backup."""
    S.take(StubClient(**_gapped(two_presets)), tmp_path)        # want BiB@0, DO@3
    live = dict(two_presets)
    live["pool"] = two_presets["pool"] + [{"cid_": 103, "name": "Stray", "posi": 2}]
    live["blobs"] = {**two_presets["blobs"], 103: content()}
    live["refs"] = {5001: [{"cctp": Cctp.REFERENCE, "rcid": 102, "posi": 0, "cid_": 9001},
                           {"cctp": Cctp.REFERENCE, "rcid": 103, "posi": 3, "cid_": 9003}]}
    plan = S.plan_restore(StubClient(**live), tmp_path, serial="SN-TEST-1")
    assert _setlist_ops(plan) == []
    assert any("Stray" in e and "3" in e for e in plan["errors"])
    pruned = S.plan_restore(StubClient(**live), tmp_path, serial="SN-TEST-1", prune=True)
    assert [(o["op"], o["name"], o.get("pos")) for o in _setlist_ops(pruned)] == [
        ("remove_ref", "Stray", None), ("reference", "Dream On", 3)]


def test_restore_reads_a_pre_posi_backup_as_compact_positions(tmp_path, two_presets):
    S.take(StubClient(**two_presets), tmp_path)
    old = tmp_path / "SN-TEST-1" / "setlists" / "Gigs.json"
    old.write_text(json.dumps(["Back In Black", "Dream On"]))   # legacy shape
    emptied = dict(two_presets, refs={5001: []})
    ops = _setlist_ops(S.plan_restore(StubClient(**emptied), tmp_path, serial="SN-TEST-1"))
    assert [(o["name"], o["pos"]) for o in ops] == [("Back In Black", 0), ("Dream On", 1)]


def test_restore_never_removes_a_misplaced_reference_it_cannot_put_back(tmp_path, two_presets):
    S.take(StubClient(**_gapped(two_presets)), tmp_path)        # want BiB@0, DO@3
    live = dict(two_presets)
    live["pool"] = two_presets["pool"] + [{"cid_": 103, "name": "Stray", "posi": 2}]
    live["blobs"] = {**two_presets["blobs"], 103: content()}
    live["refs"] = {5001: [{"cctp": Cctp.REFERENCE, "rcid": 102, "posi": 0, "cid_": 9001},
                           {"cctp": Cctp.REFERENCE, "rcid": 101, "posi": 1, "cid_": 9002},
                           {"cctp": Cctp.REFERENCE, "rcid": 103, "posi": 3, "cid_": 9003}]}
    plan = S.plan_restore(StubClient(**live), tmp_path, serial="SN-TEST-1")
    assert _setlist_ops(plan) == []          # Dream On stays at 1, not dropped
    assert any("Stray" in e for e in plan["errors"])


def test_apply_reference_refuses_an_occupied_posi_at_write_time():
    """A failed earlier remove_ref must not become a silent replace (fw 1.4
    replaces the incumbent of an occupied setlist posi)."""
    class C:
        refs = []

        def resolve_setlist_cid(self, name):
            return 5001

        def list_presets(self, container=Container.POOL, *, strict=False):
            return [{"cid_": 101, "name": "A"}]

        def find_by_pos(self, container, pos, *, strict=False):
            assert strict
            return {"cid_": 9009, "posi": pos}

        def reference_into_setlist(self, *a):
            self.refs.append(a)
            return 1

    c = C()
    with pytest.raises(HelixError, match="occupied"):
        S._apply_op(c, {"op": "reference", "setlist": "Gigs", "name": "A", "pos": 3})
    assert c.refs == []


@pytest.mark.parametrize("doc", [
    [{"name": "A", "posi": 1}, {"name": "B", "posi": 1}],
    [{"name": "A", "posi": 1}, {"name": "B"}],
    [{"name": "A", "posi": 128}],
])
def test_a_posi_file_with_duplicate_or_bad_posi_is_malformed(doc):
    assert S._desired_refs(doc) is None


def test_legacy_names_with_nulls_restore_compact():
    assert S._desired_refs(["A", None, "B"]) == [("A", 0), ("B", 1)]
