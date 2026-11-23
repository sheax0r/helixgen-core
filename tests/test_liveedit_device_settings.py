"""The Device pane's Global Settings surface (81v.5): LiveSession read/write
methods over the device-property channel, and their /device/* server routes.

Pure UI over the already-shipped ``device settings`` verbs — no new on-the-wire
behaviour — so a fake property client exercises the lock + catalog + guard
plumbing without a device.
"""
import threading

import pytest

from helixgen.device import settings as S
from helixgen.device.client import HelixError
from helixgen.liveedit.server import make_server
from helixgen.liveedit.session import LiveError, LiveSession
from tests.test_liveedit_server import req, _j
from tests.test_liveedit_session import FakeLease


class FakeSettingsClient:
    """Answers ``get_property_def`` / ``get_property`` / ``set_property`` for a
    handful of keys, mirroring the real client (which guards dangerous keys)."""
    ip = "10.0.0.99"

    def __init__(self):
        self.sock = object()
        self.sets = []            # (key, typ, value) written
        self.die_after = None     # key after which the socket "dies" (sock=None)
        self.read_fail = set()    # keys whose read raises
        self.set_fail = None      # exception set_property raises (NAK or OSError)
        self.def_fail = set()     # keys whose get_property_def raises
        self.values = {
            "global.out.level.qtr": 0.0,
            "global.tuner.type": 1,
            "global.clock.year": 2026,
            # snapshot-panel context keys (81v.2 snapshot_globals)
            "global.snapshot.edits": 0,
            "global.snapshot.reselect": 0,
            "global.tempo.select": 1,
            "global.tempo.bpm": 120.0,
            "global.send.snapshot.cc": 0,
            "global.midi.channel": 1,
            "global.midi.pc.send": 3,
        }
        self.defs = {
            "global.out.level.qtr": S.PropertyDef(
                key="global.out.level.qtr", name="1/4\" Out Level", short="",
                type="f", vmin=-30.0, vmax=0.0, default=0.0, enum=[], units=0,
                id=None),
            "global.tuner.type": S.PropertyDef(
                key="global.tuner.type", name="Tuner Type", short="", type="i",
                vmin=0, vmax=2, default=0, enum=["Fluid", "Strobe", "Needle"],
                units=0, id=None),
            "global.clock.year": S.PropertyDef(
                key="global.clock.year", name="Year", short="", type="i",
                vmin=2000, vmax=2099, default=2026, enum=[], units=0, id=None),
            "global.snapshot.edits": S.PropertyDef(
                key="global.snapshot.edits", name="Snapshot Edits", short="",
                type="i", vmin=0, vmax=1, default=0, enum=["Save", "Discard"],
                units=0, id=None),
            "global.snapshot.reselect": S.PropertyDef(
                key="global.snapshot.reselect", name="Snapshot Reselect",
                short="", type="i", vmin=0, vmax=1, default=0,
                enum=["Reload", "Toggle Prev"], units=0, id=None),
            "global.tempo.select": S.PropertyDef(
                key="global.tempo.select", name="Tempo Select", short="",
                type="i", vmin=0, vmax=2, default=1,
                enum=["Snapshot", "Per Preset", "Global"], units=0, id=None),
            "global.tempo.bpm": S.PropertyDef(
                key="global.tempo.bpm", name="Global Tempo", short="", type="f",
                vmin=20.0, vmax=240.0, default=120.0, enum=[], units=0, id=None),
            "global.send.snapshot.cc": S.PropertyDef(
                key="global.send.snapshot.cc", name="Send Snapshot CC", short="",
                type="i", vmin=0, vmax=1, default=0, enum=["Off", "On"],
                units=0, id=None),
            "global.midi.channel": S.PropertyDef(
                key="global.midi.channel", name="Global MIDI Channel", short="",
                type="i", vmin=1, vmax=16, default=1, enum=[], units=0, id=None),
            "global.midi.pc.send": S.PropertyDef(
                key="global.midi.pc.send", name="Send PC w/ Preset", short="",
                type="i", vmin=0, vmax=3, default=3,
                enum=["Off", "MIDI", "USB", "MIDI+USB"], units=0, id=None),
        }

    def get_property_def(self, key):
        if key in self.def_fail or key not in self.defs:
            raise HelixError(f"no def for {key}")
        return self.defs[key]

    def get_property(self, key):
        if key in self.read_fail:
            raise HelixError(f"read failed for {key}")
        d = self.defs[key]
        v = S.PropertyValue(key=key, type=d.type, value=self.values.get(key, 0))
        if self.die_after == key:
            self.sock = None
        return v

    def set_property(self, key, typ, value):
        S.guard_key(key)          # the real client refuses DANGEROUS_KEYS here
        if self.set_fail is not None:
            raise self.set_fail
        self.sets.append((key, typ, value))
        self.values[key] = value
        return True


def _sess():
    return LiveSession(FakeSettingsClient(), lease=FakeLease())


# --- device_pages: offline catalog structure --------------------------------

def test_device_pages_is_offline_and_ordered():
    s = LiveSession(None, lease=FakeLease())      # no client at all
    out = s.device_pages()
    pages = out["pages"]
    ids = [p["page"] for p in pages]
    # manual order (spec 81v.5 §7.1), tuner + date-time last
    assert ids[:8] == ["ins-outs", "switches-pedals", "displays", "preferences",
                       "songs", "tempo-click", "midi", "tuner"]
    assert "date-time" in ids
    # every catalog page is present exactly once
    assert set(ids) == set(S.page_names())
    labels = {p["page"]: p["label"] for p in pages}
    assert labels["ins-outs"] == "Ins / Outs"
    assert labels["midi"] == "MIDI"


def test_device_pages_carries_keys_and_readonly_flag():
    s = LiveSession(None, lease=FakeLease())
    pages = {p["page"]: p for p in s.device_pages()["pages"]}
    assert pages["ins-outs"]["keys"] == S.keys_for_page("ins-outs")
    assert pages["ins-outs"]["readonly"] is False
    # date-time is clock entry — shown, not editable (spec §8 won't-do)
    assert pages["date-time"]["readonly"] is True
    assert pages["date-time"]["note"]
    # hardware-only pages carry a "takes effect on the device" note
    assert pages["displays"]["note"]
    assert pages["switches-pedals"]["note"]


def test_device_pages_omits_dangerous_keys():
    s = LiveSession(None, lease=FakeLease())
    all_keys = [k for p in s.device_pages()["pages"] for k in p["keys"]]
    assert "global.wifi.enable" not in all_keys
    assert "global.remote.access" not in all_keys


# --- device_values: live read of one page -----------------------------------

def test_device_values_reads_def_and_value():
    s = _sess()
    rows, aborted = s._read_keys(["global.tuner.type"])
    assert aborted is None
    row = rows[0]
    assert row["key"] == "global.tuner.type"
    assert row["value"] == 1
    assert row["enum"] == ["Fluid", "Strobe", "Needle"]
    assert row["display"] == "Strobe (1)"
    assert row["type"] == "i" and row["min"] == 0 and row["max"] == 2


def test_device_values_captures_per_key_error():
    s = _sess()
    s.client.read_fail = {"global.tuner.type"}
    rows, _ = s._read_keys(["global.tuner.type", "global.out.level.qtr"])
    assert "error" in rows[0] and rows[0]["key"] == "global.tuner.type"
    assert rows[1]["key"] == "global.out.level.qtr" and "error" not in rows[1]


def test_device_values_stops_on_dead_socket():
    s = _sess()
    s.client.die_after = "global.tuner.type"
    rows, aborted = s._read_keys(["global.tuner.type", "global.out.level.qtr"])
    assert aborted == "global.tuner.type"
    assert len(rows) == 1            # stopped after the socket died


def test_device_values_unknown_page_404():
    s = _sess()
    with pytest.raises(LiveError) as e:
        s.device_values("nope")
    assert e.value.status == 404


def test_device_values_requires_connection():
    s = _sess()
    s._connected = False
    with pytest.raises(LiveError) as e:
        s.device_values("tuner")
    assert e.value.status == 503


# --- device_write: coerce, guard, lease, readback ---------------------------

def test_device_write_coerces_and_reads_back():
    s = _sess()
    out = s.device_write("global.tuner.type", 2)     # enum index
    assert s.client.sets == [("global.tuner.type", "i", 2)]
    assert out == {"key": "global.tuner.type", "value": 2,
                   "display": "Needle (2)"}
    # a write takes the 'globals' scope transiently (mirrors `settings set`)
    assert s.lease.transients == [("globals",)]


def test_device_write_float_clamped_by_def():
    s = _sess()
    with pytest.raises(LiveError) as e:
        s.device_write("global.out.level.qtr", 5.0)   # above max 0.0
    assert e.value.status == 400


def test_device_write_refuses_offcatalog_key():
    s = _sess()
    with pytest.raises(LiveError) as e:
        s.device_write("global.wifi.enable", 0)       # dangerous + off-catalog
    assert e.value.status in (403, 404)
    assert s.client.sets == []


def test_device_write_refuses_readonly_page():
    s = _sess()
    with pytest.raises(LiveError) as e:
        s.device_write("global.clock.year", 2030)     # date-time is read-only
    assert e.value.status == 403
    assert s.client.sets == []


def test_device_write_nak_does_not_disconnect():
    # the device refusing a write (NAK) is not a transport loss: the editor must
    # stay connected (a HelixError wrongly flipping _connected froze the pane).
    s = _sess()
    s.client.set_fail = HelixError("device rejected /PropertyValueSet")
    with pytest.raises(LiveError) as e:
        s.device_write("global.tuner.type", 2)
    assert e.value.status == 502
    assert s._connected is True


def test_device_write_missing_firmware_key_does_not_disconnect():
    s = _sess()
    s.client.def_fail = {"global.tuner.type"}
    with pytest.raises(LiveError) as e:
        s.device_write("global.tuner.type", 2)
    assert e.value.status == 502
    assert s._connected is True


def test_device_write_transport_loss_disconnects():
    s = _sess()
    s.client.set_fail = OSError("connection reset")
    with pytest.raises(LiveError) as e:
        s.device_write("global.tuner.type", 2)
    assert e.value.status == 502
    assert s._connected is False


def test_device_write_readback_failure_returns_coerced():
    # the write landed (ok=True); a failing readback must not revert the control
    # or mark the device down — reflect the value we wrote.
    s = _sess()
    s.client.read_fail = {"global.tuner.type"}
    out = s.device_write("global.tuner.type", 2)
    assert out == {"key": "global.tuner.type", "value": 2,
                   "display": "Needle (2)"}
    assert s._connected is True
    assert s.client.sets == [("global.tuner.type", "i", 2)]


# --- snapshot_globals: the snapshot panel's slice of Global Settings ---------

def test_snapshot_globals_reads_the_panel_keys_in_order():
    s = _sess()
    out = s.snapshot_globals()
    keys = [r["key"] for r in out["keys"]]
    assert keys == list(LiveSession._SNAPSHOT_GLOBAL_KEYS)
    edits = next(r for r in out["keys"] if r["key"] == "global.snapshot.edits")
    assert edits["enum"] == ["Save", "Discard"] and edits["value"] == 0
    assert edits["display"] == "Save (0)"


def test_snapshot_globals_tolerates_a_missing_key():
    s = _sess()
    s.client.read_fail = {"global.send.snapshot.cc"}
    out = s.snapshot_globals()
    bad = next(r for r in out["keys"] if r["key"] == "global.send.snapshot.cc")
    assert "error" in bad and "value" not in bad
    # one missing key never blanks the rest
    good = [r for r in out["keys"] if r["key"] != "global.send.snapshot.cc"]
    assert good and all("value" in r for r in good)


def test_snapshot_globals_requires_connection():
    s = _sess()
    s._connected = False
    with pytest.raises(LiveError) as e:
        s.snapshot_globals()
    assert e.value.status == 503


def test_snapshot_globals_is_read_only_takes_no_lease():
    s = _sess()
    s.snapshot_globals()
    assert s.lease.transients == []          # browsing globals locks nothing out


def test_discard_edits_is_writable_through_device_set():
    # the panel's one interactive control toggles global.snapshot.edits; it must
    # be on a writable page (switches-pedals), not refused as read-only.
    s = _sess()
    out = s.device_write("global.snapshot.edits", 1)       # Discard
    assert s.client.sets == [("global.snapshot.edits", "i", 1)]
    assert out["value"] == 1 and out["display"] == "Discard (1)"


# --- /device/* routes --------------------------------------------------------

@pytest.fixture
def srv():
    s = _sess()
    s._connected = True
    server = make_server(s, port=0)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02),
                     daemon=True).start()
    yield server
    server.stopping = True
    server.shutdown()
    server.server_close()


def test_device_state_route_is_offline_catalog(srv):
    code, _, data = req(srv, "GET", "/device/state")
    assert code == 200
    pages = _j(data)["pages"]
    assert pages[0]["page"] == "ins-outs"
    assert any(p["readonly"] for p in pages)


def test_device_values_route(srv):
    code, _, data = req(srv, "POST", "/device/values", {"page": "tuner"})
    assert code == 200 and _j(data)["page"] == "tuner"


def test_device_values_route_unknown_page(srv):
    code, _, _ = req(srv, "POST", "/device/values", {"page": "nope"})
    assert code == 404


def test_device_set_route(srv):
    code, _, data = req(srv, "POST", "/device/set",
                        {"key": "global.tuner.type", "value": 2})
    assert code == 200
    assert _j(data)["display"] == "Needle (2)"


def test_device_set_route_rejects_readonly(srv):
    code, _, _ = req(srv, "POST", "/device/set",
                     {"key": "global.clock.year", "value": 2030})
    assert code == 403


def test_unknown_device_route_404(srv):
    assert req(srv, "POST", "/device/bogus", {})[0] == 404
    assert req(srv, "GET", "/device/bogus")[0] == 404


def test_snapshot_globals_route(srv):
    code, _, data = req(srv, "GET", "/snapshot/globals")
    assert code == 200
    rows = _j(data)["keys"]
    assert any(r["key"] == "global.snapshot.edits" for r in rows)
    assert any(r["key"] == "global.tempo.bpm" for r in rows)
