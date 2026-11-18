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


# --- per-action advisory lease: the drawer no longer sits on library+irs for
#     its whole life (hgc); each write takes only its own scope, transiently,
#     so an agent pushing tones while the page is open is not locked out. ------

def test_copy_takes_library_and_irs_transiently(srv):
    server, d = srv
    req(server, "POST", "/library/copy", {"hsp": "a.hsp"})
    assert server.session.lease.transients == [("library", "irs")]


def test_setlist_action_takes_library_transiently(srv):
    server, d = srv
    req(server, "POST", "/library/setlist_create", {"name": "Gigs"})
    assert server.session.lease.transients == [("library",)]


def test_push_ir_takes_irs_transiently(srv):
    server, d = srv
    req(server, "POST", "/library/push_ir", {"wav": "x.wav"})
    assert server.session.lease.transients == [("irs",)]


def test_setlist_add_with_hsp_takes_library_and_irs(srv):
    # adding a tone by `hsp` copies it in IRs-and-all (session.setlist_add ->
    # copy_to_device, with_irs=True), so it needs `irs` as well as `library`,
    # exactly like `copy` / the CLI `device copy`.
    server, d = srv
    req(server, "POST", "/library/setlist_add", {"to": "Gigs", "hsp": "a.hsp"})
    assert server.session.lease.transients == [("library", "irs")]


def test_remaining_write_actions_take_their_scope(srv):
    server, d = srv
    for name, scope in [("setlist_remove", ("library",)),
                        ("setlist_move", ("library",)),
                        ("setlist_rename", ("library",)),
                        ("setlist_duplicate", ("library",)),
                        ("delete_ir", ("irs",)),
                        ("rename_ir", ("irs",))]:
        server.session.lease.transients.clear()
        req(server, "POST", f"/library/{name}", {})
        assert server.session.lease.transients == [scope], name


def test_local_only_actions_take_no_lease(srv):
    # content_diff reads; update_local overwrites the LOCAL .hsp — no device write
    server, d = srv
    for name in ("content_diff", "update_local"):
        server.session.lease.transients.clear()
        req(server, "POST", f"/library/{name}", {})
        assert server.session.lease.transients == [], name


def test_pull_takes_no_device_lease(srv):
    # pull reads the device + writes a LOCAL .hsp — no device write, no lease
    # (matches the CLI `device pull`, which takes no lock).
    server, d = srv
    req(server, "POST", "/library/pull", {"cid": 1})
    assert server.session.lease.transients == []
    assert d.calls == [("pull", {"cid": 1})]


def test_write_action_refused_when_the_device_is_busy(srv):
    # another helixgen process holds `library`: the drawer's write is refused
    # (409) and never touches the library, rather than racing it.
    server, d = srv
    server.session.lease.busy = "device scope 'library' is locked by someone"
    code, _, _ = req(server, "POST", "/library/copy", {"hsp": "a.hsp"})
    assert code == 409
    assert d.calls == []
