"""Shared Host / Origin / Content-Type guard for the localhost HTTP servers
(``liveedit`` and ``libraryui``): anti DNS-rebind, anti cross-origin."""
from __future__ import annotations


def local_hosts(port: int) -> set:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def bad_host(headers, port: int) -> bool:
    return headers.get("Host") not in local_hosts(port)


def guard_post(headers, port: int):
    """``(status, message)`` to refuse a POST, or ``None`` if it may proceed."""
    if bad_host(headers, port):
        return 403, "bad Host"
    origin = headers.get("Origin")
    if origin is not None and origin not in {"http://" + h for h in local_hosts(port)}:
        return 403, "bad Origin"
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return 415, "Content-Type must be application/json"
    return None
