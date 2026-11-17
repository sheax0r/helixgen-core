"""The `device live` runtime discovery loop (task 9): poll for a device, and
on the first sighting connect exactly once. A later drop (connected() flips
back to False) resumes polling."""
from helixgen.liveedit.server import run_discovery


class _Stop(Exception):
    """Breaks the loop from inside the fake sleep."""


def _driver(discover, connect, connected):
    """Run run_discovery with a fake clock that stops after a bounded number
    of polls, so the loop never spins forever."""
    sleeps = {"n": 0}
    stop = {"v": False}

    def sleep(_):
        sleeps["n"] += 1
        if sleeps["n"] >= 8:
            stop["v"] = True

    run_discovery(discover, connect, connected=connected,
                  should_stop=lambda: stop["v"], interval=0, sleep=sleep)
    return sleeps["n"]


def test_discovery_connects_once_after_the_device_appears():
    seq = iter([None, None, "10.0.0.5"])          # found on the third poll
    connected = {"v": False}
    calls = []

    def discover():
        return next(seq, "10.0.0.5")              # keeps answering afterwards

    def connect(ip):
        calls.append(ip)
        connected["v"] = True                     # the real connect flips this

    _driver(discover, connect, lambda: connected["v"])
    assert calls == ["10.0.0.5"]                  # connected ONCE, not every poll


def test_discovery_does_not_connect_while_already_connected():
    calls = []
    _driver(lambda: "10.0.0.5", lambda ip: calls.append(ip),
            connected=lambda: True)               # already up: never connect
    assert calls == []


def test_a_drop_resumes_discovery():
    # the device is up, then drops (connected() goes False on its own, as the
    # session does when its stream dies); the loop must reconnect.
    state = {"connected": True, "ticks": 0}
    calls = []

    def sleep(_):
        state["ticks"] += 1
        if state["ticks"] == 3:
            state["connected"] = False            # external drop
        if state["ticks"] >= 8:
            raise _Stop

    def connect(ip):
        calls.append(ip)
        state["connected"] = True

    try:
        run_discovery(lambda: "10.0.0.7", connect,
                      connected=lambda: state["connected"],
                      should_stop=lambda: False, interval=0, sleep=sleep)
    except _Stop:
        pass
    assert calls == ["10.0.0.7"]                  # reconnected after the drop


def test_a_discover_error_is_swallowed_and_polling_continues():
    seq = iter([RuntimeError("scan failed"), None, "10.0.0.9"])
    connected = {"v": False}
    calls = []

    def discover():
        v = next(seq, "10.0.0.9")
        if isinstance(v, Exception):
            raise v
        return v

    def connect(ip):
        calls.append(ip)
        connected["v"] = True

    _driver(discover, connect, lambda: connected["v"])
    assert calls == ["10.0.0.9"]                  # a flaky scan never kills the loop
