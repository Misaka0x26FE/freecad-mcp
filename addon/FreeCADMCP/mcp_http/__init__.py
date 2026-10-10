"""Native Streamable HTTP MCP endpoint served from inside the addon.

Relative imports are not safe in FreeCAD addon context; modules here import
each other flat (``from mcp_http import protocol``), matching ``rpc_server``.
"""

from mcp_http.transport import HTTPError, HTTPServerTransport

__all__ = ["HTTPServerTransport", "HTTPError"]