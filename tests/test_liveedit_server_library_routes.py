"""liveedit server mounts the library drawer: GET /library/state, POST /library/<action>."""
import threading

import pytest

from helixgen.liveedit.server import make_server
from helixgen.liveedit.session import LiveSession
from helixgen.libraryui.session import LibraryError
from tests.test_liveedit_server import req, _j
from tests.test_liveedit_session import FakeClient, FakeLease


class FakeDrawer:
    def __init__(self):
        self.calls = []

    def public_state(self):
        return {"connected": False, "tones": []}

    def action(self, name, payload):
        self.calls.append((name, payload))
        if name == "nope":
            raise LibraryError("unknown action", status=404)
        return {"did": name}


@pytest.fixture
def srv():
    s = LiveSession(FakeClient(), lease=FakeLease())
    s.load()
    d = FakeDrawer()
    server = make_server(s, port=0, drawer=d)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02),
                     daemon=True).start()
    yield server, d
    server.stopping = True
    server.shutdown()
    server.server_close()


def test_library_state_route(srv):
    server, _ = srv
    code, _, data = req(server, "GET", "/library/state")
    assert code == 200 and _j(data) == {"connected": False, "tones": []}
    assert req(server, "GET", "/state")[0] == 200


def test_library_action_route(srv):
    server, d = srv
    code, _, data = req(server, "POST", "/library/copy", {"hsp": "a.hsp"})
    assert code == 200 and _j(data) == {"ok": True, "did": "copy"}
    assert d.calls == [("copy", {"hsp": "a.hsp"})]
    code, _, _ = req(server, "POST", "/library/copy", {},
                     headers={"Content-Type": "text/plain"})
    assert code == 415
    assert req(server, "POST", "/library/nope", {})[0] == 404
