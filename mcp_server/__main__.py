"""Entry point for `python -m mcp_server`.

Binds the FastMCP app over Streamable HTTP on 0.0.0.0:$PORT (Render injects
PORT at runtime; defaults to 10000 locally). In mcp 1.27.2 the host/port live
on app.settings (not on app.run()), so we mutate the settings before run().
"""
from __future__ import annotations

import os

from mcp_server.server import app


def main() -> None:
    app.settings.host = "0.0.0.0"
    app.settings.port = int(os.environ.get("PORT", "10000"))
    app.run(transport="streamable-http")


if __name__ == "__main__":
    main()
