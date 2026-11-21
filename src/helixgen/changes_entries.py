"""Concrete tone-affecting change entries, registered onto changes.REGISTRY.

Each entry is a content detector + idempotent in-place migration for one
shipped engine fix. Importing this module registers them all (via
:func:`helixgen.changes.register`). Entries are added here as fixes land;
`library upgrade` and `helixgen changes` pick them up automatically.
"""
from __future__ import annotations

# Entries are appended in the tasks that follow (fs-bypass, factory-info,
# headphones-highcut). Keeping the module importable with no entries yet lets
# the CLI wire `changes` / `upgrade` before any entry exists.
