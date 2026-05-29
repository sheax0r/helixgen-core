"""User-IR registration: maps Helix `irhash` slot values to local .wav paths."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def default_irs_path() -> Path:
    """Return the IRs directory path, honoring HELIXGEN_IRS env var."""
    env = os.environ.get("HELIXGEN_IRS")
    if env:
        return Path(env)
    return Path(os.environ["HOME"]) / ".helixgen" / "irs"


@dataclass
class IrMapping:
    """Hash→wav-path mapping for user IRs registered with helixgen."""

    irs_dir: Path
    entries: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, irs_dir: Path | None = None) -> "IrMapping":
        irs_dir = irs_dir if irs_dir is not None else default_irs_path()
        mapping_file = irs_dir / "mapping.json"
        if not mapping_file.exists():
            return cls(irs_dir=irs_dir, entries={})
        data = json.loads(mapping_file.read_text())
        return cls(irs_dir=irs_dir, entries=dict(data))

    def save(self) -> None:
        """Write mapping.json atomically. Creates irs_dir if needed."""
        self.irs_dir.mkdir(parents=True, exist_ok=True)
        target = self.irs_dir / "mapping.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.entries, indent=2, sort_keys=True))
        os.replace(tmp, target)
