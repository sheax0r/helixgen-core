"""CLI tests for `helixgen device setlist import-hss` (backlog #31, EXPERIMENTAL).

Never touches a real device: monkeypatches ``helixgen.device.HelixClient``
with a fake, like ``tests/test_device_cli.py``. Bundle bytes are synthesized
via ``tests.test_hss._build_hss`` (same builder the reader unit tests use) so
these tests only exercise CLI wiring, not the container-format parsing
(covered by ``tests/test_hss.py``).
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

pytest.importorskip("msgpack")

from helixgen.cli import cli  # noqa: E402
from helixgen.device import HelixError  # noqa: E402

from tests.test_hss import _build_hss, _real_sbepgsm_blob  # noqa: E402


@pytest.fixture(autouse=True)
def _configured_device_ip(monkeypatch):
    """#74: device verbs no longer have a built-in default IP. These tests
    exercise verb logic against fakes, so simulate a configured user."""
    monkeypatch.setenv("HELIXGEN_HELIX_IP", "10.0.0.99")


def _fresh_manifest_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "setlists.json"))
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(tmp_path / "device-slots.json"))


class HssClient:
    """Fake HelixClient exercising the import-hss write path."""

    SETLISTS: dict = {}
    installed: list = []
    referenced: list = []
    created: list = []
    fail_install_names: set = set()
    fail_reference_names: set = set()
    # setlist_cid -> pre-seeded [{"cctp": 1003, "posi": N, "rcid": pool_cid}, ...]
    # simulating references that existed BEFORE this import ran.
    EXISTING_REFS: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def resolve_setlist_cid(self, name):
        return type(self).SETLISTS.get(name)

    def create_setlist(self, name, pos=None):
        type(self).created.append(name)
        cid = 9000 + len(type(self).created)
        type(self).SETLISTS = dict(type(self).SETLISTS, **{name: cid})
        return cid

    def list_container(self, cid, **kw):
        from helixgen.device.client import Cctp
        return [dict(m, cctp=Cctp.REFERENCE)
                for m in type(self).EXISTING_REFS.get(cid, [])]

    def install_into_pool(self, blob, name, **kw):
        if name in type(self).fail_install_names:
            return None
        cid = 5000 + len(type(self).installed)
        type(self).installed.append((name, blob))
        return cid

    def reference_into_setlist(self, setlist_cid, pool_cid, pos):
        # find the name we just installed for pool_cid via installed order
        if any(n in type(self).fail_reference_names
               for n, _ in type(self).installed):
            # simplistic: fail whichever install matches a flagged name
            for n, _ in type(self).installed:
                if n in type(self).fail_reference_names:
                    type(self).fail_reference_names.discard(n)
                    return None
        type(self).referenced.append((setlist_cid, pool_cid, pos))
        return 7000 + len(type(self).referenced)


class RaisingHssClient(HssClient):
    def resolve_setlist_cid(self, name):
        raise HelixError("boom: device unreachable")


def _reset(cls=HssClient):
    cls.SETLISTS = {}
    cls.installed = []
    cls.referenced = []
    cls.created = []
    cls.fail_install_names = set()
    cls.fail_reference_names = set()
    cls.EXISTING_REFS = {}


def _patch_client(monkeypatch, cls):
    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", cls)


# --- --list (offline) -----------------------------------------------------

def test_import_hss_list_empty_bundle(tmp_path):
    data = _build_hss(setlist_name="Empty One")
    path = tmp_path / "empty.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path), "--list"])
    assert result.exit_code == 0, result.output
    assert "Empty One" in result.output
    assert "0/128 slots filled" in result.output


def test_import_hss_list_filled_bundle(tmp_path):
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(setlist_name="Gigs", filled={1: ("Clean Machine", blob),
                                                     4: ("Lead Tone", blob)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path), "--list"])
    assert result.exit_code == 0, result.output
    assert "2/128 slots filled" in result.output
    assert "Clean Machine" in result.output
    assert "Lead Tone" in result.output


def test_import_hss_list_rejects_bad_file(tmp_path):
    path = tmp_path / "bad.hss"
    path.write_bytes(b"not a valid hss file at all")
    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path), "--list"])
    assert result.exit_code != 0
    assert "GGGY" in result.output or "header" in result.output.lower()


# --- --dry-run --------------------------------------------------------------

def test_import_hss_dry_run_writes_nothing(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    _patch_client(monkeypatch, HssClient)
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(setlist_name="Gigs", filled={2: ("Only Tone", blob)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(
        cli, ["device", "setlist", "import-hss", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "Only Tone" in result.output
    assert HssClient.installed == []
    assert HssClient.created == []


def test_import_hss_empty_bundle_nothing_to_import(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    _patch_client(monkeypatch, HssClient)
    data = _build_hss(setlist_name="Empty One")
    path = tmp_path / "empty.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path)])
    assert result.exit_code == 0, result.output
    assert "nothing to import" in result.output
    assert HssClient.installed == []


# --- device write path -------------------------------------------------------

def test_import_hss_creates_setlist_and_installs_in_order(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    _patch_client(monkeypatch, HssClient)
    blob1 = _real_sbepgsm_blob("preset_151")
    blob2 = _real_sbepgsm_blob("preset_152")
    data = _build_hss(setlist_name="Gigs",
                      filled={1: ("First", blob1), 5: ("Second", blob2)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path)])
    assert result.exit_code == 0, result.output
    assert "2/2" in result.output
    assert "Gigs" in result.output
    assert HssClient.created == ["Gigs"]
    assert [n for n, _ in HssClient.installed] == ["First", "Second"]
    # references were added in slot order (posi 0, 1) against the new setlist cid
    setlist_cid = HssClient.SETLISTS["Gigs"]
    assert [r[0] for r in HssClient.referenced] == [setlist_cid, setlist_cid]
    assert [r[2] for r in HssClient.referenced] == [0, 1]

    # CRITICAL invariant: the manifest's membership matches the references the
    # import wrote (in order) — otherwise the next targeted `device sync Gigs`
    # computes desired=[] and strips them all from the device.
    from helixgen.device.manifest import SetlistManifest
    m = SetlistManifest.load()
    assert "Gigs" in m.setlists()
    assert m.tones_in("Gigs") == ["First", "Second"]
    for name in ("First", "Second"):
        assert m.tones[name]["path"] is None
        assert m.tones[name]["source"] == "import-hss"


def test_import_hss_reuses_existing_setlist(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    HssClient.SETLISTS = {"Gigs": 4242}
    _patch_client(monkeypatch, HssClient)
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(setlist_name="Gigs", filled={1: ("Only", blob)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path)])
    assert result.exit_code == 0, result.output
    assert HssClient.created == []  # setlist already existed; not (re)created
    assert HssClient.referenced[0][0] == 4242


def test_import_hss_explicit_setlist_overrides_bundle_name(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    _patch_client(monkeypatch, HssClient)
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(setlist_name="BundleName", filled={1: ("Only", blob)})
    path = tmp_path / "b.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(
        cli, ["device", "setlist", "import-hss", str(path), "--setlist", "Override"])
    assert result.exit_code == 0, result.output
    assert "Override" in result.output
    assert HssClient.created == ["Override"]


def test_import_hss_reports_per_slot_failures_without_aborting(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    HssClient.fail_install_names = {"Bad One"}
    _patch_client(monkeypatch, HssClient)
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(setlist_name="Gigs",
                      filled={1: ("Bad One", blob), 2: ("Good One", blob)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path)])
    assert result.exit_code != 0
    assert "1/2" in result.output
    assert [n for n, _ in HssClient.installed] == ["Good One"]


def test_import_hss_no_setlist_name_requires_explicit_flag(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    _patch_client(monkeypatch, HssClient)
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(setlist_name="", filled={1: ("Only", blob)})
    path = tmp_path / "noname.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path)])
    assert result.exit_code != 0
    assert "--setlist" in result.output


def test_import_hss_device_error_reported(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset(RaisingHssClient)
    _patch_client(monkeypatch, RaisingHssClient)
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(setlist_name="Gigs", filled={1: ("Only", blob)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path)])
    assert result.exit_code != 0
    assert "unreachable" in result.output.lower()


# -- adversarial-review fixes: collision-safe append + malformed-blob guard --

def test_import_hss_appends_after_existing_references_without_colliding(
        monkeypatch, tmp_path):
    """Importing into an already-populated setlist must not overwrite/collide
    with its existing references (finding: raw enumerate() index collided)."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    HssClient.SETLISTS = {"Gigs": 4242}
    HssClient.EXISTING_REFS = {4242: [{"posi": 0, "rcid": 111},
                                       {"posi": 1, "rcid": 222}]}
    _patch_client(monkeypatch, HssClient)
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(setlist_name="Gigs",
                      filled={1: ("New One", blob), 2: ("New Two", blob)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path)])
    assert result.exit_code == 0, result.output
    # new references land at 2, 3 — never touching the pre-existing 0, 1
    positions = [r[2] for r in HssClient.referenced]
    assert positions == [2, 3]


def test_import_hss_skips_slot_that_does_not_look_like_content(monkeypatch, tmp_path):
    """A filled slot whose payload isn't recognizable content (the unconfirmed
    filled-slot framing bit us) is skipped with a clear error, never sent to
    install_into_pool, and never aborts the rest of the import."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    _patch_client(monkeypatch, HssClient)
    good_blob = _real_sbepgsm_blob("preset_151")
    bad_blob = b"not a recognized content blob at all"
    data = _build_hss(setlist_name="Gigs",
                      filled={1: ("Bad Payload", bad_blob), 2: ("Good", good_blob)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path)])
    assert result.exit_code != 0
    assert "1/2" in result.output
    assert [n for n, _ in HssClient.installed] == ["Good"]
    assert "Bad Payload" in result.output


def test_import_hss_reference_failure_does_not_leave_a_permanent_position_gap(
        monkeypatch, tmp_path):
    """A slot that installs but fails to reference doesn't waste its position —
    the next successful slot reuses it instead of leaving a hole."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    HssClient.fail_reference_names = {"First"}
    _patch_client(monkeypatch, HssClient)
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(setlist_name="Gigs",
                      filled={1: ("First", blob), 2: ("Second", blob)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path)])
    assert result.exit_code != 0  # First's reference failure is still an error
    assert [n for n, _ in HssClient.installed] == ["First", "Second"]
    # Second's reference lands at position 0 — the position First failed at —
    # not position 1 (which would leave 0 permanently empty).
    assert [r[2] for r in HssClient.referenced] == [0]


# -- second review round: strict listing + dry-run honesty ---------------------

def test_import_hss_strict_listing_failure_aborts_before_write(monkeypatch, tmp_path):
    """A flaky-network listing of the destination setlist aborts the import
    BEFORE anything is installed — a timeout must never silently read as
    'empty setlist' and land colliding references at posi 0,1,2."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    HssClient.SETLISTS = {"Gigs": 4242}

    class StrictFailClient(HssClient):
        def list_container(self, cid, **kw):
            if kw.get("strict"):
                raise HelixError("no reply listing container (timeout)")
            return []

    _patch_client(monkeypatch, StrictFailClient)
    blob = _real_sbepgsm_blob("preset_151")
    data = _build_hss(setlist_name="Gigs", filled={1: ("Only", blob)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(cli, ["device", "setlist", "import-hss", str(path)])
    assert result.exit_code != 0
    assert "timeout" in result.output.lower()
    assert HssClient.installed == []
    assert HssClient.referenced == []


def test_import_hss_dry_run_flags_would_skip_payloads(monkeypatch, tmp_path):
    """--dry-run runs the same content-blob check the real import uses and
    says so, instead of promising a slot the import would skip."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset()
    _patch_client(monkeypatch, HssClient)
    good = _real_sbepgsm_blob("preset_151")
    bad = b"unrecognizable payload"
    data = _build_hss(setlist_name="Gigs",
                      filled={1: ("Bad Payload", bad), 2: ("Good", good)})
    path = tmp_path / "gigs.hss"
    path.write_bytes(data)

    result = CliRunner().invoke(
        cli, ["device", "setlist", "import-hss", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    bad_line = next(l for l in result.output.splitlines() if "Bad Payload" in l)
    good_line = next(l for l in result.output.splitlines() if "Good" in l and "Bad" not in l)
    assert "would SKIP" in bad_line
    assert "would SKIP" not in good_line
    assert HssClient.installed == []
