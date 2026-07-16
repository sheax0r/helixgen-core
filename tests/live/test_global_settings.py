"""EXTRA-gated global-settings write test (HELIXGEN_LIVE_GLOBAL=1).

`device settings set` mutates the device's GLOBAL configuration (not preset
state), so it is excluded from the ordinary live run. It has read-back,
though, which admits a provably-safe pattern: read the current value, set it
to the SAME value, verify the read-back is unchanged. That is the only write
this test ever performs.

`device globaleq set` has NO network read-back (write-only), so no such safe
pattern exists — it stays excluded entirely (see the package conftest
docstring).
"""
from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.live, pytest.mark.live_global,
              pytest.mark.device_write]


def test_settings_set_same_value_roundtrip(helix):
    code, out, err = helix("device", "settings", "list", "--json")
    assert code == 0, err or out
    catalog = json.loads(out)
    key = sorted(catalog.items())[0][1][0]

    code, out, err = helix("device", "settings", "get", key, "--json")
    assert code == 0, err or out
    before = json.loads(out)

    # write the exact current value back (numeric raw value, not the label)
    code, out, err = helix("device", "settings", "set", key, before["value"])
    assert code == 0, err or out

    code, out, err = helix("device", "settings", "get", key, "--json")
    assert code == 0, err or out
    after = json.loads(out)
    assert after["value"] == before["value"], (
        f"set-same-value changed {key}: {before['value']!r} -> "
        f"{after['value']!r}")
