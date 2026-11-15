"""embed.start() must not leak the device socket when the editor fails to boot
(the common case: device busy/asleep makes session.load() raise)."""
from __future__ import annotations

import pytest

from helixgen.device.client import HelixError
from helixgen.liveedit import embed


class _FakeClient:
    def __init__(self):
        self.closed = False
        self.ip = "1.2.3.4"

    def connect(self):
        return self

    def product_info(self):
        return {"firmware": "1.4"}

    def close(self):
        self.closed = True


class _FakeLease:
    def __init__(self, dev_ip, enabled=True):
        self.released = False

    def release(self, final=False):
        self.released = True


def _patch(monkeypatch, *, load_raises):
    import helixgen.device.client as clientmod
    import helixgen.liveedit.lease as leasemod
    import helixgen.liveedit.session as sessmod

    client = _FakeClient()
    lease = _FakeLease(None)
    monkeypatch.setattr(clientmod, "HelixClient", lambda ip, port: client)
    monkeypatch.setattr(leasemod, "EditLease", lambda dev_ip, enabled=True: lease)

    class _FakeSession:
        def __init__(self, c, lease=None, liveness=False):
            pass

        def load(self):
            if load_raises:
                raise HelixError("device is busy")

    monkeypatch.setattr(sessmod, "LiveSession", _FakeSession)
    return client, lease


def test_start_closes_client_when_load_fails(monkeypatch):
    client, lease = _patch(monkeypatch, load_raises=True)
    with pytest.raises(HelixError):
        embed.start("1.2.3.4", 1)
    assert client.closed is True   # no leaked DEALER socket
    assert lease.released is True   # lease let go too
