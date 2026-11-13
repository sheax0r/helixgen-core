"""liveedit.server / liveedit.lease — the localhost HTTP + SSE front of
`device live` (hgc-58a). Real sockets on an ephemeral port, fake device."""
import http.client
import json
import threading

import pytest

from helixgen import locks
from helixgen.device.client import HelixError
from helixgen.device.subscribe import Event
from helixgen.liveedit.lease import EditLease
from helixgen.liveedit.server import make_server, run_subscriber
from helixgen.liveedit.session import LiveError, LiveSession
from tests.test_liveedit_session import DRIVE, FakeClient, FakeLease

IP = "192.0.2.1"  # TEST-NET; never a real device


@pytest.fixture
def srv():
    c = FakeClient()
    s = LiveSession(c, lease=FakeLease())
    s.load()
    server = make_server(s, port=0)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02),
                     daemon=True).start()
    yield c, s, server
    server.stopping = True
    server.shutdown()
    server.server_close()


def req(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    h = {"Content-Type": "application/json"} if method == "POST" else {}
    h.update(headers or {})
    conn.request(method, path, json.dumps(body) if body is not None else None, h)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, r.getheader("Content-Type"), data


def _j(data):
    return json.loads(data)


# --- routes ------------------------------------------------------------------

def test_page_is_served_self_contained(srv):
    code, ctype, data = req(srv[2], "GET", "/")
    assert code == 200 and ctype.startswith("text/html")
    html = data.decode()
    assert "<script" in html
    # one self-contained file: nothing fetched from anywhere else
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in html
    assert 'src="' not in html and "@import" not in html


def test_state_endpoint(srv):
    code, ctype, data = req(srv[2], "GET", "/state")
    j = _j(data)
    assert code == 200 and ctype == "application/json"
    assert j["preset"]["name"] == "Venom" and j["connected"] is True
    assert j["paths"][0]["segments"][1]["block"]["name"] == "Scream 808 Mono"


def test_param_write(srv):
    c, s, server = srv
    code, _, data = req(server, "POST", "/param",
                        {"path": 0, "block": 2, "pid": DRIVE, "value": 9})
    assert code == 200 and _j(data) == {"ok": True, "value": 1.0}   # clamped
    assert c.params == [(0, 2, DRIVE, 1.0, True, True)]


def test_client_id_is_passed_through_and_junk_ids_are_dropped(srv):
    c, s, server = srv
    v, r = s.version, s.rev
    req(server, "POST", "/param", {"path": 0, "block": 1, "pid": 1, "value": 0.5, "client": "tab-a"})
    req(server, "POST", "/param", {"path": 0, "block": 1, "pid": 1, "value": 0.6, "client": {"x": 1}})
    req(server, "POST", "/param", {"path": 0, "block": 1, "pid": 1, "value": 0.7, "client": "z" * 500})
    msgs = [m for m in s.changes_since(v, r)[2] if m["t"] == "param"]
    assert [m.get("client") for m in msgs] == ["tab-a", None, None]


def test_stale_rev_is_409_with_the_value_to_show(srv):
    c, s, server = srv
    old = s.rev
    s.reread()
    code, _, data = req(server, "POST", "/param",
                        {"path": 0, "block": 1, "pid": 1, "value": 0.9, "rev": old})
    assert code == 409 and _j(data)["value"] == pytest.approx(0.3)
    assert c.params == []
    code, _, data = req(server, "POST", "/param",
                        {"path": 0, "block": 1, "pid": 1, "value": 0.9, "rev": s.rev})
    assert code == 200


def test_bypass_write(srv):
    c, s, server = srv
    code, _, data = req(server, "POST", "/bypass", {"path": 0, "block": 1, "enabled": False})
    assert code == 200 and _j(data) == {"ok": True, "enabled": False}
    assert c.enables == [(0, 1, False, True)]


def test_save_and_reread(srv):
    c, s, server = srv
    code, _, data = req(server, "POST", "/save", {})
    assert code == 200 and _j(data) == {"ok": True, "saved": "Venom", "cid": 1863}
    reads = c.reads
    code, _, data = req(server, "POST", "/reread", {})
    assert code == 200 and _j(data) == {"ok": True} and c.reads == reads + 1


def test_bad_requests_are_400_and_write_nothing(srv):
    c, s, server = srv
    assert req(server, "POST", "/param", {"path": 0, "block": 2, "pid": DRIVE})[0] == 400
    assert req(server, "POST", "/param", {"path": "x", "block": 2, "pid": DRIVE, "value": 1})[0] == 400
    assert req(server, "POST", "/param", {"path": 0, "block": 2, "pid": DRIVE, "value": "loud"})[0] == 400
    assert req(server, "POST", "/bypass", {"path": 0, "block": 1})[0] == 400
    assert req(server, "POST", "/bypass", {"path": 0, "block": 1, "enabled": "yes"})[0] == 400
    assert req(server, "POST", "/param", [1, 2])[0] == 400
    assert c.params == [] and c.enables == []


def test_unknown_control_is_404(srv):
    code, _, data = req(srv[2], "POST", "/param", {"path": 0, "block": 9, "pid": 1, "value": 0.5})
    assert code == 404 and "error" in _j(data)


def test_failed_write_reports_the_value_to_revert_to(srv):
    c, s, server = srv
    c.ack = False
    code, _, data = req(server, "POST", "/param", {"path": 0, "block": 1, "pid": 1, "value": 0.9})
    j = _j(data)
    assert code == 502 and j["value"] == pytest.approx(0.3) and "acknowledge" in j["error"]
    code, _, data = req(server, "POST", "/bypass", {"path": 0, "block": 1, "enabled": False})
    assert code == 502 and _j(data)["value"] is True


def test_busy_device_is_409(srv):
    c, s, server = srv
    s.lease.busy = "device 10.0.0.99 scope 'editbuffer' is locked by agent-x"
    code, _, data = req(server, "POST", "/param", {"path": 0, "block": 1, "pid": 1, "value": 0.9})
    assert code == 409 and "agent-x" in _j(data)["error"]


def test_device_exception_does_not_leak_a_traceback(srv, monkeypatch):
    c, s, server = srv
    monkeypatch.setattr(s, "save", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    code, ctype, data = req(server, "POST", "/save", {})
    assert code == 500 and ctype == "application/json"
    assert "Traceback" not in data.decode()


def test_unknown_routes(srv):
    assert req(srv[2], "GET", "/nope")[0] == 404
    assert req(srv[2], "POST", "/nope", {})[0] == 404
    assert req(srv[2], "GET", "/state?x=1")[0] == 200


# --- cross-origin / DNS-rebind guards ----------------------------------------

def test_binds_localhost_only():
    for host in ("0.0.0.0", "::", "192.168.1.5"):
        with pytest.raises(ValueError):
            make_server(None, host=host)


def test_foreign_origin_and_host_are_refused(srv):
    c, s, server = srv
    body = {"path": 0, "block": 1, "pid": 1, "value": 0.9}
    assert req(server, "POST", "/param", body, {"Origin": "http://evil.example"})[0] == 403
    assert req(server, "POST", "/param", body, {"Host": "evil.example:80"})[0] == 403
    assert req(server, "GET", "/state", headers={"Host": "evil.example:80"})[0] == 403
    assert req(server, "GET", "/events", headers={"Host": "evil.example:80"})[0] == 403
    assert req(server, "POST", "/param", body, {"Content-Type": "text/plain"})[0] == 415
    assert c.params == []


def test_same_origin_is_accepted(srv):
    port = srv[2].server_address[1]
    code, *_ = req(srv[2], "POST", "/param", {"path": 0, "block": 1, "pid": 1, "value": 0.9},
                   {"Origin": f"http://localhost:{port}", "Host": f"localhost:{port}"})
    assert code == 200


# --- SSE ---------------------------------------------------------------------

class _Stream:
    def __init__(self, server):
        self.conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        self.conn.request("GET", "/events")
        self.r = self.conn.getresponse()
        assert self.r.status == 200
        assert self.r.getheader("Content-Type") == "text/event-stream"

    def next(self):
        while True:
            line = self.r.fp.readline().decode()
            assert line, "stream closed"
            if line.startswith("data: "):
                return json.loads(line[6:])

    def close(self):
        self.conn.close()


def test_events_stream_opens_with_full_state_then_sends_deltas(srv):
    c, s, server = srv
    st = _Stream(server)
    try:
        first = st.next()
        assert first["t"] == "state" and first["paths"]
        req(server, "POST", "/param", {"path": 0, "block": 1, "pid": 1, "value": 0.5})
        msg = st.next()
        assert msg == {"t": "param", "path": 0, "block": 1, "pid": 1, "value": 0.5}
        assert st.next()["t"] == "meta"                    # dirty flipped
        # a change made on the hardware reaches the page as ONE small delta
        s.handle_events([Event(2001, "/setBlockEnable", [1, 77, 0, 1, 0])])
        assert st.next() == {"t": "bypass", "path": 0, "block": 1, "enabled": False}
        # a structural change sends the whole state again
        s.reread()
        assert st.next()["t"] == "state"
    finally:
        st.close()


# --- subscriber loop ---------------------------------------------------------

class _FakeSub:
    def __init__(self, server, batches):
        self.server, self.batches, self.polls = server, list(batches), 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def poll(self, timeout):
        self.polls += 1
        if not self.batches:
            self.server.stopping = True
            return []
        b = self.batches.pop(0)
        if isinstance(b, Exception):
            raise b
        return b


def test_run_subscriber_feeds_the_session_and_survives_a_flaky_stream(srv):
    c, s, server = srv
    sub = _FakeSub(server, [
        HelixError("poll failed"),                          # must not kill the loop
        [Event(2001, "/setParamValue", [1, 77, 0, 1, 0, 1, 0.61])],
        [Event(2001, "/activateSnapshot", [1, 2, 3])],
    ])
    # the fake DEVICE holds the hardware-side change too, so the snapshot
    # re-read that follows returns it
    c.eb["sfg_"]["flow"][0]["blks"][3]["mdls"][0]["parm"][0]["valu"] = 0.61
    reads = c.reads
    run_subscriber(server, sub, error_pause=0)
    assert s.param(0, 1, 1)["value"] == pytest.approx(0.61)
    assert c.reads == reads + 1                             # the snapshot re-read
    assert s.lease.ticks >= 3                               # tick() every pass


# --- the write lease -----------------------------------------------------------

@pytest.fixture
def lockroot(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_LOCKS", str(tmp_path / "locks"))
    monkeypatch.delenv("HELIXGEN_LOCK_TOKEN", raising=False)
    monkeypatch.delenv("HELIXGEN_LOCK_TIMEOUT", raising=False)
    return tmp_path / "locks"


def _held(root, scope):
    return (root / IP / f"{scope}.lock").exists()


def test_lease_is_taken_on_first_write_and_released_when_idle(lockroot):
    now = [0.0]
    lease = EditLease(IP, idle=2.0, clock=lambda: now[0])
    assert not _held(lockroot, "editbuffer")                 # nothing until a write
    lease.ensure()
    assert _held(lockroot, "editbuffer")
    now[0] = 1.0
    lease.ensure()
    lease.tick()
    assert _held(lockroot, "editbuffer")                     # still in use
    now[0] = 3.5
    lease.tick()
    assert not _held(lockroot, "editbuffer")                 # idle -> others may drive
    lease.ensure()
    assert _held(lockroot, "editbuffer")
    lease.release()
    assert not _held(lockroot, "editbuffer")


def test_lease_refuses_fast_when_someone_else_holds_the_device(lockroot):
    other = locks.acquire(IP, ("editbuffer",), label="agent-x", token="tok-other", pid=1)
    try:
        lease = EditLease(IP)
        with pytest.raises(LiveError) as e:
            lease.ensure()
        assert e.value.status == 409 and "agent-x" in str(e.value)
        with pytest.raises(LiveError):
            with lease.transient(("editbuffer", "library")):
                pass
    finally:
        other.release()
    lease.ensure()                                           # free again -> fine
    lease.release()


def test_transient_scopes_are_held_only_inside_the_block(lockroot):
    lease = EditLease(IP)
    with lease.transient(("editbuffer", "library")):
        assert _held(lockroot, "library")
    assert not _held(lockroot, "library")
    lease.release()


def test_disabled_lease_touches_nothing(lockroot):
    lease = EditLease(IP, enabled=False)
    lease.ensure()
    with lease.transient(("library",)):
        pass
    lease.tick()
    assert not lockroot.exists() or not any(lockroot.rglob("*.lock"))
