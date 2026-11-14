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


def test_intents_endpoint_serves_the_adjective_map(srv):
    code, ctype, data = req(srv[2], "GET", "/intents")
    j = _j(data)
    assert code == 200 and ctype == "application/json"
    assert "crunchier" in [i["id"] for i in j["intents"]]
    assert "crunchy" in [a.lower() for a in j["aliases"]]       # type-ahead term
    crunch = next(i for i in j["intents"] if i["id"] == "crunchier")
    assert {"role", "param", "dir"} == set(crunch["moves"][0])   # which-params only


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


def test_snapshot_switch_activates_on_the_device_under_the_lease(srv):
    c, s, server = srv
    ensured = s.lease.ensured
    code, _, data = req(server, "POST", "/snapshot", {"index": 3})
    assert code == 200 and _j(data) == {"ok": True, "snapshot": 3}
    assert c.snapshots_activated == [3]
    assert s.lease.ensured == ensured + 1 and s.lease.dones >= 1


def test_snapshot_out_of_range_is_refused_and_writes_nothing(srv):
    c, s, server = srv
    assert req(server, "POST", "/snapshot", {"index": 9})[0] == 400
    assert req(server, "POST", "/snapshot", {"index": "two"})[0] == 400
    assert c.snapshots_activated == []


def test_preset_load_resolves_the_cid_and_loads_under_the_lease(srv):
    c, s, server = srv
    ensured = s.lease.ensured
    code, _, data = req(server, "POST", "/preset", {"cid": 1950})
    assert code == 200 and _j(data) == {"ok": True, "cid": 1950}
    assert c.loaded == [1950]              # the setlist REFERENCE cid, as sent
    assert s.lease.ensured == ensured + 1 and s.lease.dones >= 1


def test_preset_load_needs_an_integer_cid(srv):
    c, s, server = srv
    assert req(server, "POST", "/preset", {})[0] == 400
    assert req(server, "POST", "/preset", {"cid": "x"})[0] == 400
    assert c.loaded == []


def test_preset_list_groups_setlists_then_pool_with_the_active_cid(srv):
    c, s, server = srv
    code, ctype, data = req(server, "GET", "/presets")
    j = _j(data)
    assert code == 200 and ctype == "application/json"
    assert j["active"] == 1863          # Venom is loaded in the fixture
    assert [g["name"] for g in j["groups"]] == ["Rock", "Pool"]
    assert j["groups"][0]["presets"] == [{"cid": 1950, "name": "Venom Live"}]
    assert j["groups"][1]["presets"] == [{"cid": 1863, "name": "Venom"}]


def test_preset_list_reports_a_device_read_failure_as_502(srv):
    c, s, server = srv
    c.fail = HelixError("timeout")
    code, _, data = req(server, "GET", "/presets")
    assert code == 502 and "error" in _j(data)


def test_bad_requests_are_400_and_write_nothing(srv):
    c, s, server = srv
    assert req(server, "POST", "/param", {"path": 0, "block": 2, "pid": DRIVE})[0] == 400
    assert req(server, "POST", "/param", {"path": "x", "block": 2, "pid": DRIVE, "value": 1})[0] == 400
    assert req(server, "POST", "/param", {"path": 0, "block": 2, "pid": DRIVE, "value": "loud"})[0] == 400
    assert req(server, "POST", "/bypass", {"path": 0, "block": 1})[0] == 400
    assert req(server, "POST", "/bypass", {"path": 0, "block": 1, "enabled": "yes"})[0] == 400
    assert req(server, "POST", "/param", [1, 2])[0] == 400
    assert c.params == [] and c.enables == []


def test_a_boolean_is_not_a_param_value(srv):
    c, s, server = srv
    code, *_ = req(server, "POST", "/param", {"path": 0, "block": 1, "pid": 1, "value": True})
    assert code == 400 and c.params == []


def test_huge_numbers_are_400_not_500(srv):
    c, s, server = srv
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    body = '{"path": 0, "block": 1, "pid": 1, "value": 1' + "0" * 400 + "}"
    conn.request("POST", "/param", body, {"Content-Type": "application/json"})
    r = conn.getresponse()
    r.read()
    assert r.status == 400 and c.params == []


def test_oversized_and_chunked_bodies_are_refused_unread(srv):
    c, s, server = srv
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.putrequest("POST", "/param")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(64 * 1024 * 1024))
    conn.endheaders()
    r = conn.getresponse()
    assert r.status == 413 and r.getheader("Connection") == "close"
    conn.close()
    # a chunked body would otherwise sit unread on the socket and be parsed as
    # a SECOND request — one whose headers the sender chooses freely
    smuggled = (b'POST /param HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                b'Content-Type: application/json\r\nContent-Length: 47\r\n\r\n'
                b'{"path": 0, "block": 1, "pid": 1, "value": 0.9}' % server.server_address[1])
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.putrequest("POST", "/param")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    conn.send(b"%x\r\n%s\r\n0\r\n\r\n" % (len(smuggled), smuggled))
    r = conn.getresponse()
    assert r.status == 400 and r.getheader("Connection") == "close"
    conn.close()
    assert c.params == []


def test_a_refused_cross_origin_post_closes_the_connection(srv):
    c, s, server = srv
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request("POST", "/param", json.dumps({"path": 0, "block": 1, "pid": 1, "value": 0.9}),
                 {"Content-Type": "application/json", "Origin": "http://evil.example"})
    r = conn.getresponse()
    assert r.status == 403 and r.getheader("Connection") == "close"


def test_the_page_cannot_be_framed(srv):
    conn = http.client.HTTPConnection("127.0.0.1", srv[2].server_address[1], timeout=5)
    conn.request("GET", "/")
    r = conn.getresponse()
    r.read()
    assert r.getheader("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in r.getheader("Content-Security-Policy")


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
    lease.done()
    now[0] = 1.5
    lease.ensure()                                           # a second write...
    lease.done()
    now[0] = 3.0
    lease.tick()
    assert _held(lockroot, "editbuffer")                     # ...restarts the idle clock
    now[0] = 3.6
    lease.tick()
    assert not _held(lockroot, "editbuffer")                 # idle -> others may drive
    lease.ensure()
    assert _held(lockroot, "editbuffer")
    lease.release()
    assert not _held(lockroot, "editbuffer")


def test_lease_is_never_dropped_under_a_write_in_progress(lockroot):
    now = [0.0]
    lease = EditLease(IP, idle=2.0, clock=lambda: now[0])
    lease.ensure()                                           # a write starts...
    now[0] = 30.0                                            # ...and takes forever (reconnect loop)
    lease.tick()
    assert _held(lockroot, "editbuffer")
    lease.done()
    now[0] = 31.0
    lease.tick()
    assert _held(lockroot, "editbuffer")                     # idle is counted from the END of the write
    now[0] = 33.0
    lease.tick()
    assert not _held(lockroot, "editbuffer")


def test_a_long_unbroken_session_retakes_the_lease_before_its_ttl(lockroot):
    now = [0.0]
    lease = EditLease(IP, idle=2.0, max_hold=300.0, clock=lambda: now[0])
    lease.ensure(); lease.done()
    first = (lockroot / IP / "editbuffer.lock").read_text()
    for t in range(1, 400):                                  # a write a second, never idle
        now[0] = float(t)
        lease.ensure(); lease.done(); lease.tick()
    assert _held(lockroot, "editbuffer")
    assert (lockroot / IP / "editbuffer.lock").read_text() != first   # a fresh lease, not the 400 s old one
    lease.release()


def test_no_write_can_retake_the_lease_after_shutdown(lockroot):
    lease = EditLease(IP)
    lease.ensure(); lease.done()
    lease.release(final=True)
    with pytest.raises(LiveError) as e:
        lease.ensure()
    assert e.value.status == 503 and not _held(lockroot, "editbuffer")


def test_a_dangling_lock_token_stops_the_page_like_any_other_verb(lockroot, monkeypatch):
    # $HELIXGEN_LOCK_TOKEN says "I am inside a held session". If that session
    # is gone, taking a fresh lease under the token would quietly re-arm it
    # for every sibling verb.
    monkeypatch.setenv("HELIXGEN_LOCK_TOKEN", "tok-reclaimed")
    lease = EditLease(IP)
    with pytest.raises(LiveError) as e:
        lease.ensure()
    assert e.value.status == 409 and not _held(lockroot, "editbuffer")
    with pytest.raises(LiveError):
        with lease.transient(("editbuffer", "library")):
            pass


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
    lease.done()
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
