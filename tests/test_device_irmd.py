"""Unit tests for the canonical irhash <-> device-irmd conversion pair
(``helixgen.device.irmd``) — the resolver-pattern (#14) single source of truth
for the 16-byte IR-hash blob the device stores as ``mdls[*].irmd``."""
from __future__ import annotations

import pytest

from helixgen.device import irmd


HEX = "0123456789abcdef0123456789abcdef"  # 32 hex chars -> 16 bytes
BLOB = bytes.fromhex(HEX)


def test_irhash_to_irmd_roundtrips():
    assert irmd.irhash_to_irmd(HEX) == BLOB
    assert len(irmd.irhash_to_irmd(HEX)) == 16


def test_irmd_to_irhash_roundtrips():
    assert irmd.irmd_to_irhash(BLOB) == HEX
    assert irmd.irmd_to_irhash(bytearray(BLOB)) == HEX


def test_pair_is_inverse():
    assert irmd.irmd_to_irhash(irmd.irhash_to_irmd(HEX)) == HEX


def test_irhash_to_irmd_raises_valueerror_on_bad_hex():
    # must raise ValueError (like bytes.fromhex) so existing try/except sites
    # in client.py keep catching it — minimal helper, no swallowing.
    with pytest.raises(ValueError):
        irmd.irhash_to_irmd("nothex!!")


def test_irmd_to_irhash_no_length_validation():
    # minimal helper: callers own their own length filters (maintenance.py's
    # len==16 test stays at the call site), so a short blob still converts.
    assert irmd.irmd_to_irhash(b"\x01\x02") == "0102"
