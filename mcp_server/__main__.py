"""Entry point for `python -m mcp_server`.

Two transports, picked by the MCP_TRANSPORT env var:

- `stdio` (Claude Code spawning the server as a subprocess) — no host/port,
  no allow-list config. The harness pipes JSON-RPC over stdin/stdout.
- `streamable-http` (default; the Render deploy) — binds 0.0.0.0:$PORT, with
  DNS-rebinding protection configurable via MCP_ALLOWED_HOSTS (CSV) and
  MCP_ALLOWED_ORIGINS (CSV).

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

    app.settings.host = "0.0.0.0"
    app.settings.port = int(os.environ.get("PORT", "10000"))

    hosts = _csv_env("MCP_ALLOWED_HOSTS")
    origins = _csv_env("MCP_ALLOWED_ORIGINS")
    if hosts or origins:
        app.settings.transport_security = TransportSecuritySettings(
            allowed_hosts=hosts,
            allowed_origins=origins,
        )

    app.run(transport="streamable-http")


if __name__ == "__main__":
    main()
