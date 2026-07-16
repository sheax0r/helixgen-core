"""CLI tests for the `helixgen device slots` group (list / --verify / restore)."""
import json
from pathlib import Path

from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device import observations as _obs
from helixgen.device.manifest import SetlistManifest

HSP_MAGIC = b"rpshnosj"


def _seed(*, name="White Limo Lead", slot="4A", path="/x/white-limo.hsp",
          source="import-local", cid=147, posi=12, setlist=None):
    """Write a manifest with one on-device tone to the isolated path. Its
    observed cid/posi (v3: per-device, not in the manifest) goes into a
    devices/<serial>.json so lookups/restore-fallback find it."""
    m = SetlistManifest.load()
    m.tones[name] = {"path": path, "content_hash": None,
                     "source": source, "slot": slot}
    if setlist:
        m.setlists_map.setdefault(setlist, {"tones": [], "synced": True})["tones"].append(name)
    m.save()
    if cid:
        obs = _obs.load_observations("legacy")
        obs.tones[name] = {"cid": cid, "posi": posi}
        _obs.save_observations(obs)
    return m


class FakeClient:
    def __init__(self, *a, **k):
        self.calls = []
        self.presets = getattr(type(self), "PRESETS", [])

    @property
    def _raw(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def list_presets(self, container=-2):
        self.calls.append(("list_presets", container))
        return self.presets

    def find_by_pos(self, container, pos, *, strict=False):
        return None

    def load_preset(self, cid):
        return True

    def get_edit_buffer(self):
        return b"_sbepgsm-template"

    def mutating(self):
        import contextlib
        return contextlib.nullcontext(self)

    def push_to_slot(self, container, pos, name, blob):
        self.calls.append(("push_to_slot", container, pos, name))
        return 900


def _patch_client(monkeypatch, cls=FakeClient):
    import helixgen.device as device_mod
    created = []

    def factory(*a, **k):
        inst = cls(*a, **k)
        created.append(inst)
        return inst

    monkeypatch.setattr(device_mod, "HelixClient", factory)
    return created


# -- list (offline) -----------------------------------------------------------

def test_slots_list_bare_prints_entries(monkeypatch):
    _seed()
    r = CliRunner().invoke(cli, ["device", "slots"])
    assert r.exit_code == 0, r.output
    assert "4A" in r.output
    assert "White Limo Lead" in r.output


def test_slots_list_explicit_and_json(monkeypatch):
    _seed()
    r = CliRunner().invoke(cli, ["device", "slots", "list", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data[0]["name"] == "White Limo Lead"
    assert data[0]["slot"] == "4A"


def test_slots_list_empty_is_graceful(monkeypatch):
    r = CliRunner().invoke(cli, ["device", "slots", "list"])
    assert r.exit_code == 0, r.output


# -- verify (needs device) ----------------------------------------------------

def test_slots_verify_flags_missing(monkeypatch):
    _seed()

    class Empty(FakeClient):
        PRESETS = []

    _patch_client(monkeypatch, Empty)
    r = CliRunner().invoke(cli, ["device", "slots", "list", "--verify"])
    assert r.exit_code == 0, r.output
    assert "missing" in r.output.lower()


def test_slots_verify_ok(monkeypatch):
    _seed()

    class Match(FakeClient):
        PRESETS = [{"posi": 12, "name": "White Limo Lead", "cid_": 147}]

    _patch_client(monkeypatch, Match)
    r = CliRunner().invoke(cli, ["device", "slots", "list", "--verify", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data[0]["status"] == "ok"


# -- restore ------------------------------------------------------------------

def test_slots_restore_sbe_source_repushes(monkeypatch, tmp_path):
    sbe = tmp_path / "lead.sbe"
    sbe.write_bytes(b"_sbepgsm-blob")
    _seed(name="Lead", slot="2B", path=str(sbe), source="push", cid=None)

    holder = {}

    class Rec(FakeClient):
        def push_to_slot(self, container, pos, name, blob):
            holder["push"] = (container, pos, name)
            return 901

    _patch_client(monkeypatch, Rec)
    r = CliRunner().invoke(cli, ["device", "slots", "restore", "Lead"])
    assert r.exit_code == 0, r.output
    assert holder["push"][1] == 5  # "2B" -> posi 5


def test_slots_restore_hsp_source_reinstalls(monkeypatch, tmp_path):
    hsp = tmp_path / "white-limo.hsp"
    hsp.write_bytes(HSP_MAGIC + json.dumps({"meta": {"name": "t"},
                                            "preset": {"flow": []}}).encode())
    _seed(name="White Limo Lead", slot="4A", path=str(hsp), source="authored")

    import helixgen.device.bridge as bridge
    monkeypatch.setattr(bridge, "check_irs", lambda h, body: {"missing": set()})
    monkeypatch.setattr("helixgen.device.transcode.hsp_to_sbepgsm",
                        lambda body, strict=True: b"XCODED")

    created = _patch_client(monkeypatch)
    r = CliRunner().invoke(cli, ["device", "slots", "restore", "White Limo Lead"])
    assert r.exit_code == 0, r.output
    pushes = [c for inst in created for c in inst.calls if c[0] == "push_to_slot"]
    assert pushes and pushes[-1][2] == 12  # (op, container, pos, name)


def test_slots_restore_no_local_source_errors(monkeypatch):
    _seed(name="Live Tweak", slot="1D", path=None, source="save", cid=None)
    _patch_client(monkeypatch)
    r = CliRunner().invoke(cli, ["device", "slots", "restore", "Live Tweak"])
    assert r.exit_code != 0
    assert "no local source" in r.output.lower()


def test_slots_restore_unknown_name_errors(monkeypatch):
    _seed(name="Known", slot="1A")
    _patch_client(monkeypatch)
    r = CliRunner().invoke(cli, ["device", "slots", "restore", "Nonexistent"])
    assert r.exit_code != 0


def test_slots_restore_hsp_occupied_slot_refused_without_force(monkeypatch, tmp_path):
    """#25a: an occupied slot is refused for an .hsp source (as for .sbe)."""
    hsp = tmp_path / "t.hsp"
    hsp.write_bytes(HSP_MAGIC + json.dumps({"meta": {"name": "t"},
                                            "preset": {"flow": []}}).encode())
    _seed(name="Occupied Tone", slot="4A", path=str(hsp), source="authored")

    import helixgen.device.bridge as bridge
    monkeypatch.setattr(bridge, "check_irs", lambda h, body: {"missing": set()})
    monkeypatch.setattr("helixgen.device.transcode.hsp_to_sbepgsm",
                        lambda body, strict=True: b"XCODED")

    class Occupied(FakeClient):
        def find_by_pos(self, container, pos, *, strict=False):
            return {"cid_": 5, "posi": pos}  # slot taken

    _patch_client(monkeypatch, Occupied)
    r = CliRunner().invoke(cli, ["device", "slots", "restore", "Occupied Tone"])
    assert r.exit_code != 0
    assert "not empty" in r.output.lower()


def test_slots_restore_hsp_force_pushes_into_occupied_posi(monkeypatch, tmp_path):
    """#25a: --force lets an .hsp restore proceed at an occupied posi — it
    skips the emptiness check and pushes there (the occupant is NOT deleted),
    matching the .sbe path's --force semantics."""
    hsp = tmp_path / "t.hsp"
    hsp.write_bytes(HSP_MAGIC + json.dumps({"meta": {"name": "t"},
                                            "preset": {"flow": []}}).encode())
    _seed(name="Force Tone", slot="4A", path=str(hsp), source="authored")

    import helixgen.device.bridge as bridge
    monkeypatch.setattr(bridge, "check_irs", lambda h, body: {"missing": set()})
    monkeypatch.setattr("helixgen.device.transcode.hsp_to_sbepgsm",
                        lambda body, strict=True: b"XCODED")

    class Occupied(FakeClient):
        def find_by_pos(self, container, pos, *, strict=False):
            return {"cid_": 5, "posi": pos}  # slot taken

    created = _patch_client(monkeypatch, Occupied)
    r = CliRunner().invoke(
        cli, ["device", "slots", "restore", "Force Tone", "--force"])
    assert r.exit_code == 0, r.output
    pushes = [c for inst in created for c in inst.calls if c[0] == "push_to_slot"]
    assert pushes and pushes[-1][2] == 12  # 4A -> posi 12


def test_slots_restore_falls_back_to_observed_posi(monkeypatch, tmp_path):
    """#25b: a tone whose ``slot`` doesn't resolve but whose ``device.posi``
    is known restores at that posi (no 'no recorded slot' error)."""
    sbe = tmp_path / "lead.sbe"
    sbe.write_bytes(b"_sbepgsm-blob")
    # slot left as "auto" (unresolved) but the device posi was observed
    _seed(name="Synced Tone", slot="auto", path=str(sbe), source="push",
          cid=910, posi=7)

    holder = {}

    class Rec(FakeClient):
        def push_to_slot(self, container, pos, name, blob):
            holder["push"] = (container, pos, name)
            return 911

    _patch_client(monkeypatch, Rec)
    r = CliRunner().invoke(cli, ["device", "slots", "restore", "Synced Tone"])
    assert r.exit_code == 0, r.output
    assert holder["push"][1] == 7  # fell back to observed device.posi
