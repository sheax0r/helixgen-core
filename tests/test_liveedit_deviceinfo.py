"""The read-only Device Info pane (81v.11): LiveSession.device_info() returns
the device's curated identity / firmware / storage (``/ProductInfoGet``).

The ``product_info`` decode lives in (and is tested with) the client; what's new
here is the thin session wrapper — lease-free device read, curation (drop the
large ``raw`` map), and the disconnected / read-failure guards.
"""
from __future__ import annotations

import pytest

from helixgen.device.client import HelixError
from helixgen.liveedit.session import LiveError, LiveSession


class _InfoClient:
    def __init__(self, info=None, exc=None):
        self._info = info
        self._exc = exc

    def product_info(self):
        if self._exc is not None:
            raise self._exc
        return self._info


_FULL = {
    "model": "stadium", "device_id": 2490368, "helixgen_model": "stadium_xl",
    "serial": "47292244582131381", "firmware": "1.4.1", "firmware_build": 1410,
    "firmware_date": "2026-05-01", "sd_total_bytes": 32_000_000_000,
    "sd_available_bytes": 28_500_000_000, "raw": {"big": "blob"},
}


def test_device_info_curates_fields_and_drops_raw():
    sess = LiveSession(_InfoClient(_FULL))
    out = sess.device_info()
    assert out["helixgen_model"] == "stadium_xl" and out["serial"] == "47292244582131381"
    assert out["firmware"] == "1.4.1" and out["firmware_build"] == 1410
    assert out["sd_available_bytes"] == 28_500_000_000
    assert "raw" not in out            # the large map is not forwarded to the pane


def test_device_info_missing_fields_are_none_not_crash():
    sess = LiveSession(_InfoClient({"firmware": "1.4.1"}))
    out = sess.device_info()
    assert out["firmware"] == "1.4.1" and out["serial"] is None
    assert set(out) == set(LiveSession._INFO_FIELDS)


def test_device_info_disconnected_is_refused():
    # no client -> not connected -> a clear error, no device touched
    with pytest.raises(LiveError):
        LiveSession().device_info()


def test_device_info_read_failure_is_502():
    sess = LiveSession(_InfoClient(exc=HelixError("no reply")))
    with pytest.raises(LiveError) as ei:
        sess.device_info()
    assert ei.value.status == 502
