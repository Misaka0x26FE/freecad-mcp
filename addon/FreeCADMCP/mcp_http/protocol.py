"""JSON-RPC framing and MCP protocol-version handling for the HTTP endpoint.

Kept free of FreeCAD imports so the transport can be exercised headlessly.
"""

from typing import Any

# Protocol revisions this endpoint understands. Newest first: the first entry
# is what an ``initialize`` without an explicit version negotiates.
#
# 2026-07-28 is deliberately absent. It removes both sessions and the GET
# stream, and this endpoint offers neither yet; refusing it is honest, while
# half-implementing it would be neither.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")

# What every currently deployed client negotiates when it says nothing.
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]

# From 2025-06-18 on the client repeats the negotiated version in a header on
# every later request.
HEADER_PROTOCOL_VERSION = "MCP-Protocol-Version"

# JSON-RPC error codes, including the two the MCP spec reserves for transport
# level problems.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# The spec defines -32000..-32099 for implementation-defined server errors.
# freecad-mcp uses this one for a rejected protocol version.
UNSUPPORTED_PROTOCOL_VERSION = -32022

SERVER_INFO = {"name": "freecad-mcp", "version": "0.1.26"}


def version_supported(version: str | None) -> bool:
    """True when ``version`` is one this endpoint speaks.

    An absent version means the client is speaking 2025-03-26 semantics, which
    the spec says to assume; the spec uses SHOULD, so a client is allowed to
    omit it.
    """
    if version is None or version == "":
        return True
    return version in SUPPORTED_PROTOCOL_VERSIONS


def negotiated_version(version: str | None) -> str:
    """The version to answer with for a request that carried ``version``."""
    if version is None or version == "":
        return DEFAULT_PROTOCOL_VERSION
    return version


def error_response(msg_id: Any, code: int, message: str, data: Any = None) -> dict:
    """A JSON-RPC error object, shaped so the id is preserved even when null."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def result_response(msg_id: Any, result: Any) -> dict:
    """A JSON-RPC success object."""
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def initialize_result(version: str) -> dict:
    """The ``initialize`` result for ``version``.

    Capabilities are deliberately minimal: this endpoint is a plain tool
    server. It serves no server-to-client stream, so it advertises neither
    logging nor sampling and asks for no roots.
    """
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": dict(SERVER_INFO),
    }