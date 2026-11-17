"""Background diff reconcile for the library drawer.

Run once per device connect on a daemon thread holding the library client:
settle every on-both preset whose badge is still ``verifying`` (no fresh
cached verdict) by reading + diffing it, push each result through
``on_update``, and persist the cache once at the end. Cooperative ``should_stop``
lets a page-close or reconnect abort mid-sweep. IRs need no pass here — their
drift is a cheap hash set-diff already computed in ``public_state``.
"""
from __future__ import annotations

from typing import Callable


def run_reconcile(drawer, *, on_update: Callable[[int, str], None],
                  should_stop: Callable[[], bool]) -> None:
    # Re-verify everything this connect: a persisted verdict is provisional
    # until confirmed against the live device (amp edits while closed).
    if hasattr(drawer, "begin_reconcile"):
        drawer.begin_reconcile()
    did = False
    for tone in drawer.public_state().get("tones", []):
        for v in tone.get("variants", []):
            if should_stop():
                _save(drawer, did)
                return
            if v.get("presence") != "both" or v.get("diff") != "verifying":
                continue
            cid = v.get("device_cid")
            verdict = drawer.resolve_diff(cid, v.get("hsp"))
            did = True
            on_update(cid, verdict)
    _save(drawer, did)


def _save(drawer, did: bool) -> None:
    cache = getattr(drawer, "cache", None)
    if did and cache is not None:
        try:
            cache.save()
        except Exception:  # noqa: BLE001 — a flaky save must not abort the sweep
            pass
