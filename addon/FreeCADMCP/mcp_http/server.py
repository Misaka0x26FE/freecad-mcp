"""Lifecycle for the addon's Streamable HTTP endpoint.

Owns the single transport instance and the start/stop the toolbar commands
drive. Split out from transport.py so that binding -- which has to report a
port clash on the caller's thread, while the GUI is still able to show it --
stays callable without a handler being wired up first.
"""

import os
from typing import Any

from mcp_http.security import BindHostError
from mcp_http.transport import HTTPServerTransport

DEFAULT_HTTP_PORT = 9876

# The single instance, mirroring rpc_server.rpc_server_instance.
http_transport: HTTPServerTransport | None = None


def build_transport(port: int = DEFAULT_HTTP_PORT, **overrides: Any) -> HTTPServerTransport:
    """A transport configured from settings, with the handler attached.

    Imported here rather than at module scope so that starting the endpoint
    without a GUI -- and building one for a test -- does not drag FreeCAD in.
    """
    from mcp_http.gui_bridge import build_dispatcher

    from rpc_server.settings import load_settings

    settings = load_settings()
    transport = HTTPServerTransport(
        host=overrides.pop("host", "127.0.0.1"),
        port=port,
        allowed_ips=overrides.pop("allowed_ips", settings.get("allowed_ips", "127.0.0.1")),
        auth_token=overrides.pop("auth_token", settings.get("auth_token", "")),
        **overrides,
    )
    transport.set_handler(build_dispatcher())
    return transport


def start_http_server(port: int = DEFAULT_HTTP_PORT, **overrides: Any) -> str:
    """Start the endpoint. Returns a status line for the Report View."""
    global http_transport

    if http_transport is not None:
        host, bound = http_transport._host, http_transport.bound_port
        return f"MCP HTTP Server already running at {host}:{bound}."

    transport = build_transport(port, **overrides)

    try:
        # Synchronous on purpose: a port clash has to surface here, where the
        # caller can report it, rather than inside a background thread.
        transport.bind()
    except BindHostError as exc:
        raise
    except OSError as exc:
        # A failed start must not leave a socket behind or appear running,
        # so the toolbar command can be retried without restarting FreeCAD.
        raise RuntimeError(f"could not bind {transport._host}:{port}: {exc}") from exc

    transport.serve_in_background()
    http_transport = transport

    message = f"MCP HTTP Server started at {transport._host}:{transport.bound_port}/mcp (PID {os.getpid()})."
    if transport._auth_token:
        message += " Auth token required."
    return message


def stop_http_server() -> str:
    """Stop the endpoint. Safe to call when it is not running."""
    global http_transport

    if http_transport is None:
        return "MCP HTTP Server is not running."

    transport, http_transport = http_transport, None
    transport.stop()
    return "MCP HTTP Server stopped."


def is_running() -> bool:
    return http_transport is not None