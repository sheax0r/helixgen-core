"""Entry point for `python -m mcp_server`.

Binds the FastMCP app over Streamable HTTP on 0.0.0.0:$PORT (Render injects
PORT at runtime; defaults to 10000 locally). In mcp 1.27.2 the host/port live
on app.settings (not on app.run()), so we mutate the settings before run().

The MCP SDK enables DNS-rebinding protection by default, which rejects any
request whose Host or Origin header isn't on an allow-list. For public
deployments behind a custom domain, set MCP_ALLOWED_HOSTS (CSV) and
MCP_ALLOWED_ORIGINS (CSV) env vars; unset values keep the SDK defaults.
"""
from __future__ import annotations

import os

from mcp.server.transport_security import TransportSecuritySettings

from mcp_server.server import app


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def main() -> None:
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
