"""User-IR registration: maps Helix `irhash` slot values to local .wav paths."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


class IrMappingError(ValueError):
    """Raised when an IR mapping operation is rejected (conflict, ambiguity, etc.)."""


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

    def register(self, hash_: str, wav_path: Path, *, force: bool = False) -> None:
        """Bind hash → wav_path. Idempotent for same (hash, file); see Task 3 for conflicts."""
        wav_path = Path(wav_path)
        if not wav_path.is_file():
            raise FileNotFoundError(f"wav file not found: {wav_path}")
        canonical = self._canonical(wav_path)
        existing = self.entries.get(hash_)
        if existing is not None:
            if existing == canonical:
                return  # idempotent
            if not force:
                raise IrMappingError(
                    f"hash {hash_} is already mapped to {existing}; "
                    f"refusing to overwrite with {canonical} (use force=True)"
                )
        self.entries[hash_] = canonical

    def resolve_by_hash(self, hash_: str) -> Path:
        """Return absolute Path for hash. Raises IrMappingError on miss."""
        if hash_ not in self.entries:
            raise IrMappingError(f"unknown IR hash {hash_}")
        return self._absolute(self.entries[hash_])

    def resolve_by_basename(self, basename: str) -> tuple[str, Path]:
        """Return (hash, absolute_path) for unique basename match.

        Case-sensitive. Raises IrMappingError on ambiguous or missing.
        """
        matches = [
            (h, p) for h, p in self.entries.items() if os.path.basename(p) == basename
        ]
        if not matches:
            raise IrMappingError(f"no registered IR matches basename {basename!r}")
        if len(matches) > 1:
            paths = ", ".join(p for _, p in matches)
            raise IrMappingError(
                f"ambiguous IR basename {basename!r}; matches: {paths}"
            )
        h, p = matches[0]
        return h, self._absolute(p)

    def _absolute(self, stored: str) -> Path:
        p = Path(stored)
        if p.is_absolute():
            return p
        return (self.irs_dir / p).resolve()

    def _canonical(self, wav_path: Path) -> str:
        """Return path relative to irs_dir if under it, else absolute."""
        wav_abs = wav_path.resolve()
        irs_abs = self.irs_dir.resolve()
        try:
            return str(wav_abs.relative_to(irs_abs))
        except ValueError:
            return str(wav_abs)


IR_MODEL_PREFIX = "HX2_ImpulseResponse"


def extract_ir_hashes(preset_body: dict) -> list[str]:
    """Return slot-level irhash values from a .hsp body dict, in (path, position) order.

    Blocks whose `slot[0].model` does not start with HX2_ImpulseResponse are ignored.
    """
    hashes: list[tuple[int, int, str]] = []
    for path_obj in preset_body.get("preset", {}).get("flow", []):
        if not isinstance(path_obj, dict):
            continue
        for v in path_obj.values():
            if not isinstance(v, dict) or "slot" not in v:
                continue
            slot = v["slot"][0]
            if not str(slot.get("model", "")).startswith(IR_MODEL_PREFIX):
                continue
            if "irhash" not in slot:
                continue
            hashes.append((int(v.get("path", 0)), int(v.get("position", 0)), slot["irhash"]))
    hashes.sort()
    return [h for _, _, h in hashes]
