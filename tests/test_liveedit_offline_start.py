"""`device live` cold-start: the server binds and serves with NO device
attached, so the page (and the library drawer) open before — or without — a
Stadium ever being found (task 8). Device bring-up is deferred to
``LiveSession.connect_device``."""
import threading

import pytest

from helixgen.liveedit.drawer import LibraryDrawer
from helixgen.liveedit.server import make_server
from helixgen.liveedit.session import LiveError, LiveSession
from tests.test_liveedit_server import req, _j
from tests.test_liveedit_session import FakeClient, FakeLease


def _offline_drawer():
    # a drawer with no device and empty local sources: pure offline read model
    return LibraryDrawer(None, client_factory=None,
                         tone_loader=lambda: [], guitar_loader=lambda: [],
                         ir_local_loader=lambda: [])


@pytest.fixture
def srv():
    s = LiveSession(None, lease=FakeLease())        # NO device
    server = make_server(s, port=0, drawer=_offline_drawer())
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02),
                     daemon=True).start()
    yield s, server
    server.stopping = True
    server.shutdown()
    server.server_close()


def test_server_serves_without_device(srv):
    s, server = srv
    code, _, data = req(server, "GET", "/state")
    assert code == 200 and _j(data)["connected"] is False
    assert _j(data)["paths"] == []                  # empty chain, no crash
    code, _, data = req(server, "GET", "/library/state")
    assert code == 200 and _j(data)["connected"] is False


def test_offline_session_reports_disconnected_and_refuses_writes():
    s = LiveSession(None, lease=FakeLease())
    assert s.connected is False
    st = s.public_state()
    assert st["connected"] is False and st["paths"] == []
    with pytest.raises(LiveError) as e:
        s.set_param(0, 1, 1, 0.5)
    assert e.value.status == 503                     # disconnected, not a crash


def test_load_without_a_client_is_a_clean_error_not_a_crash():
    s = LiveSession(None, lease=FakeLease())
    with pytest.raises(LiveError):
        s.load()                                     # no AttributeError on None
    # list_presets must not blow up on a None client either
    with pytest.raises(LiveError):
        s.list_presets()


def test_connect_device_attaches_a_client_and_loads_the_buffer():
    s = LiveSession(None, lease=FakeLease())
    c = FakeClient()
    s.connect_device(c)
    assert s.connected is True
    assert c.reads == 1                              # the edit buffer was read
    assert s.public_state()["preset"]["name"] == "Venom"
    # now writes go through
    assert s.set_param(0, 1, 1, 0.4) == {"value": pytest.approx(0.4)}
