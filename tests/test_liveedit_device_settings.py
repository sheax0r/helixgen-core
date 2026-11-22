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
        self.values = {
            "global.out.level.qtr": 0.0,
            "global.tuner.type": 1,
            "global.clock.year": 2026,
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
        }

    def get_property_def(self, key):
        if key not in self.defs:
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
