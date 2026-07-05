from helixgen.generate import _wrap_value_with_snapshots


def test_wrap_densifies_enabled_array():
    # base True (block enabled), disabled only in snapshot 0
    overrides = [False, None, None, None, None, None, None, None]
    wrapped = _wrap_value_with_snapshots(True, overrides)
    assert wrapped["snapshots"] == [False, True, True, True, True, True, True, True]


def test_wrap_densifies_param_array():
    overrides = [None, 0.30, None, None, None, None, None, None]
    wrapped = _wrap_value_with_snapshots(0.12, overrides)
    assert wrapped["snapshots"] == [0.12, 0.30, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12]


def test_wrap_no_variation_stays_plain():
    assert _wrap_value_with_snapshots(0.5, [None] * 8) == {"value": 0.5}
    assert _wrap_value_with_snapshots(0.5, None) == {"value": 0.5}
