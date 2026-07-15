"""Canonical irhash <-> device ``irmd`` conversion (resolver pattern, #14).

An IR is identified by its ``irhash`` — a 32-char lowercase hex string
(``mapping.json``'s key). Device content references the same IR by the raw
**16-byte** blob stored as ``mdls[*].irmd`` (mental model #3). These two
one-liners are the single source of truth for that hex<->bytes conversion,
so every device-layer call site shares one implementation instead of inlining
``bytes.fromhex`` / ``bytes(...).hex()`` ad hoc.

Deliberately **minimal** — no length or content validation. Several call sites
use their own ``len == 16`` / ``len == 32`` checks as control-flow *filters*
(deciding whether a value even IS an irmd), not as conversion guards; baking a
length assert in here would silently change that behavior. Keep guards at the
call site. :func:`irhash_to_irmd` raises ``ValueError`` on non-hex input,
exactly like ``bytes.fromhex``, so existing ``try/except ValueError`` sites keep
working unchanged.
"""
from __future__ import annotations

from typing import Optional, Union


def normalize_hash_string(s: str) -> Optional[str]:
    """Normalize an **already-string** device IR hash to a canonical ``irhash``.

    The single home (#53) for the two device-hash *string* branches that used to
    disagree: ``client._hex_hash`` lowercased with no length check, while
    ``sftp._addcontent_hash`` required exact ``len == 32`` but preserved case.
    The reconciled contract is the safer union — **validate length (32) and
    lowercase** — returning ``None`` for anything not exactly 32 chars. (Device
    IR hashes arrive over the wire as 16 raw msgpack bytes, handled by
    :func:`irmd_to_irhash`; this string path is the defensive fallback.)
    """
    return s.lower() if len(s) == 32 else None


def irhash_to_irmd(irhash: str) -> bytes:
    """32-char hex ``irhash`` -> the raw 16-byte ``irmd`` blob.

    Raises ``ValueError`` for non-hex input (same contract as
    ``bytes.fromhex``).
    """
    return bytes.fromhex(irhash)


def irmd_to_irhash(irmd: Union[bytes, bytearray]) -> str:
    """Raw ``irmd`` blob -> its lowercase hex ``irhash`` string.

    No length validation — the caller owns any ``len == 16`` filter.
    """
    return bytes(irmd).hex()
