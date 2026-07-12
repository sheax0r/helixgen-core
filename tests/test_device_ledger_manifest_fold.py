"""The slot ledger is folded into the setlist manifest: ONE local file.

After the fold, ``SlotLedger`` stores its per-slot placements inside the same
``setlists.json`` document the ``SetlistManifest`` owns (as an ``entries``
section), so a single physical file carries both a setlist-membership entry
(manifest) and a slot placement (ledger). ``device-slots.json`` is no longer
written — it is only read once, for migration.
"""
import json
from pathlib import Path

from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.hsp import write_hsp
from helixgen.device.ledger import SlotLedger
from helixgen.device.manifest import SetlistManifest, default_setlists_path

NOW = "2026-07-12T00:00:00+00:00"


def _env(monkeypatch, tmp_path):
    """Isolate the single manifest file + point the legacy ledger at an absent
    path (so nothing migrates and nothing writes device-slots.json)."""
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "setlists.json"))
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(tmp_path / "device-slots.json"))


def test_ledger_default_storage_is_the_manifest_file(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    led = SlotLedger.load()
    assert led.path == default_setlists_path() == tmp_path / "setlists.json"


def test_one_file_holds_both_ledger_and_manifest(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    setlists_file = tmp_path / "setlists.json"
    device_slots_file = tmp_path / "device-slots.json"

    # (1) ledger side: record a slot placement.
    led = SlotLedger.load()
    led.record(setlist="user", posi=3, name="White Limo Lead", cid=147,
               source_kind="hsp", source_path="/x/wl.hsp", now=NOW)
    led.save()

    # (2) manifest side: register the same tone into a setlist.
    hsp = tmp_path / "wl.hsp"
    write_hsp(hsp, {"meta": {"name": "White Limo Lead"}})
    m = SetlistManifest.load()
    m.add_tone("helixgen", hsp)
    m.save()

    # ONE physical file — the ledger no longer writes device-slots.json.
    assert setlists_file.exists()
    assert not device_slots_file.exists()

    doc = json.loads(setlists_file.read_text())
    # ledger placement is present as the folded-in `entries` section...
    assert any(e["name"] == "White Limo Lead" and e["posi"] == 3
               for e in doc["entries"])
    # ...alongside the manifest's own membership + registry sections.
    assert doc["setlists"]["helixgen"] == ["White Limo Lead"]
    assert "White Limo Lead" in doc["tones"]

    # A later ledger write must preserve the manifest sections (bidirectional).
    led2 = SlotLedger.load()
    led2.record(setlist="user", posi=4, name="Second", cid=200,
                source_kind="hsp", source_path="/x/two.hsp", now=NOW)
    led2.save()
    doc2 = json.loads(setlists_file.read_text())
    assert doc2["setlists"]["helixgen"] == ["White Limo Lead"]
    assert {e["name"] for e in doc2["entries"]} == {"White Limo Lead", "Second"}

    # Both views reload cleanly from the single file.
    assert SlotLedger.load().find(setlist="user", posi=3)["cid"] == 147
    assert SetlistManifest.load().tones_in("helixgen") == ["White Limo Lead"]


def test_cli_install_and_setlist_add_share_one_file(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        @property
        def _raw(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def find_by_pos(self, container, pos):
            return None

        def get_edit_buffer(self):
            return b"_sbepgsm-template"

        def load_preset(self, cid):
            return True

        def save_edit_buffer_to(self, container, pos, name):
            return 501

    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", FakeClient)

    # placement via CLI `device save`
    r = CliRunner().invoke(cli, ["device", "save", "Clean Verse",
                                 "--setlist", "user", "--pos", "2"])
    assert r.exit_code == 0, r.output

    # membership via CLI `device setlist add`
    hsp = tmp_path / "tone.hsp"
    write_hsp(hsp, {"meta": {"name": "Clean Verse"}})
    r2 = CliRunner().invoke(cli, ["device", "setlist", "add", "helixgen", str(hsp)])
    assert r2.exit_code == 0, r2.output

    setlists_file = tmp_path / "setlists.json"
    assert setlists_file.exists()
    assert not (tmp_path / "device-slots.json").exists()
    doc = json.loads(setlists_file.read_text())
    assert any(e["name"] == "Clean Verse" for e in doc["entries"])
    assert doc["setlists"]["helixgen"] == ["Clean Verse"]
