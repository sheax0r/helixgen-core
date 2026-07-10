"""helixgen MCP server — drives the helixgen library from an MCP client."""

# Track the helixgen library version rather than carrying a separate number
# that drifts out of date. The plugin/pip environments always have helixgen
# importable; the fallback only matters in a stripped-down context.
try:
    from helixgen import __version__
except Exception:  # pragma: no cover - helixgen not importable
    __version__ = "0.0.0"
