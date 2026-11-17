from __future__ import annotations

from helixgen.liveedit.reconcile import run_reconcile


class _FakeDrawer:
    """Duck-typed drawer: public_state with pre-stamped badges + a counting
    resolve_diff + a cache that records save()."""

    def __init__(self, variants):
        self._variants = variants
        self.resolved = []
        self.saved = 0
        self.cache = self  # self.save() records

    def public_state(self):
        return {"tones": [{"variants": self._variants}]}

    def resolve_diff(self, cid, hsp=None):
        self.resolved.append(cid)
        return "changed"

    def save(self):
        self.saved += 1


def _v(cid, diff):
    return {"presence": "both", "device_cid": cid, "hsp": "tones/a.hsp",
            "diff": diff}


def test_reconcile_settles_only_stale():
    d = _FakeDrawer([_v(1, "same"), _v(2, "verifying")])
    updates = []
    run_reconcile(d, on_update=lambda cid, verdict: updates.append((cid, verdict)),
                  should_stop=lambda: False)
    assert d.resolved == [2]           # only the stale one was read
    assert updates == [(2, "changed")]
    assert d.saved == 1                 # cache persisted once at the end


def test_reconcile_stops_cooperatively():
    d = _FakeDrawer([_v(1, "verifying"), _v(2, "verifying")])
    run_reconcile(d, on_update=lambda *a: None,
                  should_stop=lambda: len(d.resolved) >= 1)
    assert d.resolved == [1]           # halted before the second
