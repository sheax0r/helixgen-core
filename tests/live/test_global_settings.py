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


#: Pinned explicitly: the ONLY write in the whole suite must never silently
#: retarget to a self-changing key (e.g. global.clock.minute, where
#: set-same-value would actually perturb the clock and the verify would
#: flake). global.tuner.type is a stable enum setting.
SAFE_KEY = "global.tuner.type"


def test_settings_set_same_value_roundtrip(helix):
    code, out, err = helix("device", "settings", "list", "--json")
    assert code == 0, err or out
    catalog = json.loads(out)
    if not any(SAFE_KEY in keys for keys in catalog.values()):
        pytest.skip(f"{SAFE_KEY} not in the settings catalog")
    key = SAFE_KEY

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
