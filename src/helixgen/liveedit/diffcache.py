"""Persistent per-device cache of preset diff verdicts.

A preset's cid is stable across an in-place save, so a verdict is keyed to the
library-side .hsp hash; device-side staleness is the reconcile's job.
"""
import json
import os
from pathlib import Path
from typing import Optional

from helixgen.home import helixgen_home


def default_path(device_serial: str) -> Path:
    return helixgen_home() / "diffcache" / f"{device_serial}.json"


class DiffCache:
    """cid -> {device_fp, hsp_hash, verdict, ts}."""

    # ponytail: whole-file JSON cache, fine for ~100 presets
    def __init__(self, path):
        self.path = Path(path)
        self._d: dict = {}
        self.load()

    def load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
            # keep only dict entries — a hand-edited/partial file must not make
            # verdict() raise on a non-dict value
            self._d = ({k: v for k, v in d.items() if isinstance(v, dict)}
                       if isinstance(d, dict) else {})
        except (OSError, ValueError):
            self._d = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        # snapshot: the subscriber thread may invalidate/put concurrently, and
        # a plain dict() copy is GIL-atomic (no "changed size during iteration")
        tmp.write_text(json.dumps(dict(self._d)))
        os.replace(tmp, self.path)

    def get(self, cid) -> Optional[dict]:
        return self._d.get(cid)

    def put(self, cid, entry: dict) -> None:
        self._d[cid] = entry

    def invalidate(self, cid) -> None:
        self._d.pop(cid, None)

    def verdict(self, cid, hsp_hash) -> Optional[str]:
        e = self._d.get(cid)
        return e.get("verdict") if e and e.get("hsp_hash") == hsp_hash else None
