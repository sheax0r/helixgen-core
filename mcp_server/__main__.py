"""Entry point for `python -m mcp_server`.

Two transports, picked by the MCP_TRANSPORT env var:

- `stdio` (Claude Code spawning the server as a subprocess) — no host/port,
  no allow-list config. The harness pipes JSON-RPC over stdin/stdout. This is
  what the shipped plugin uses.
- `streamable-http` (default; the Render deploy) — binds MCP_HOST:$PORT. The
  bind host defaults to the loopback `127.0.0.1`; a deploy that must accept
  external connections (e.g. Render) opts in explicitly with MCP_HOST=0.0.0.0.
  DNS-rebinding protection (TransportSecuritySettings) is installed
  unconditionally and is fail-closed: with no MCP_ALLOWED_HOSTS set it defaults
  to loopback Host headers only, so the default 127.0.0.1 bind works out of the
  box while a stray public Host header is rejected. A public deploy
  (MCP_HOST=0.0.0.0) must therefore also set MCP_ALLOWED_HOSTS (CSV, e.g. its
  external hostname); MCP_ALLOWED_ORIGINS (CSV) further restricts Origins.

In mcp 1.27.2 the host/port live on app.settings (not on app.run()), so we
mutate the settings before run().
"""
from __future__ import annotations

import os

from mcp.server.transport_security import TransportSecuritySettings

from mcp_server.server import app


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")

    if transport == "stdio":
        app.run(transport="stdio")
        return

    # Default to loopback; opt in to a public bind with MCP_HOST=0.0.0.0.
    app.settings.host = os.environ.get("MCP_HOST", "127.0.0.1")
    app.settings.port = int(os.environ.get("PORT", "10000"))

    # Always install DNS-rebinding protection (enable_dns_rebinding_protection
    # defaults to True). The middleware is fail-closed: an empty allowed_hosts
    # rejects *every* Host header, so we supply a loopback default that keeps
    # the default 127.0.0.1 bind working. A public deploy must set
    # MCP_ALLOWED_HOSTS to its external hostname.
    allowed_hosts = _csv_env("MCP_ALLOWED_HOSTS") or [
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
    ]
    app.settings.transport_security = TransportSecuritySettings(
        allowed_hosts=allowed_hosts,
        allowed_origins=_csv_env("MCP_ALLOWED_ORIGINS"),
    )

    app.run(transport="streamable-http")


if __name__ == "__main__":
    main()
