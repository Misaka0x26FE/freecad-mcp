"""Bridge from an HTTP tools/call to FreeCAD.

The only module here that imports FreeCAD, and it does so lazily inside the
call, which keeps the transport, security and tools layers importable without
FreeCAD so the protocol and routing stay testable headlessly.

Deliberately does *not* dispatch to the GUI thread itself. The FreeCADRPC
methods already do: create_document, get_objects, execute_code and the rest
each queue their own work on the GUI thread and wait for it. Wrapping them in
another dispatch_to_gui deadlocks -- the outer task occupies the only thread
that drains the queue while the inner one waits for a task that can never be
reached. A real FreeCAD hung for the full 60 s timeout before this was caught.
"""

from typing import Any

from mcp_http.tools import ToolDispatcher, to_result


def build_dispatcher() -> ToolDispatcher:
    """A ToolDispatcher whose calls reach FreeCAD.

    ``FreeCADRPC`` is reached through its class, matching the instance the
    XML-RPC endpoint registers; both endpoints therefore run the same code.
    """
    return ToolDispatcher(invoke_rpc, capture=capture_view)


def invoke_rpc(name: str, kwargs: dict) -> Any:
    """Call one FreeCADRPC method and return its value.

    Called from the HTTP worker thread. The method moves itself onto the GUI
    thread as needed, so there is nothing to wrap it in here.
    """
    from rpc_server.rpc_server import FreeCADRPC

    method = getattr(FreeCADRPC, name, None)
    if method is None or not callable(method):
        raise AttributeError(f"FreeCADRPC has no method {name}")

    return method(FreeCADRPC(), **kwargs)


def capture_view(view_name: str) -> Any:
    """A base64 PNG of the named view, or None when it cannot be captured."""
    import FreeCAD

    if not FreeCAD.GuiUp:
        return None
    return invoke_rpc("get_active_screenshot", {"view_name": view_name})


def handle_tools_call(payload: dict) -> dict:
    """Entry point for HTTPServerTransport: a tools/call request in, an MCP
    tool result out.

    Never raises. dispatch_to_gui signals failure by returning a value rather
    than by raising, and the RPC methods turn that into a Fault, so both
    shapes end up as an MCP tool error: the HTTP call did succeed even when the
    operation did not, and the client needs to be able to tell them apart.
    """
    try:
        return build_dispatcher()(payload)
    except Exception as exc:
        return {
            "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            "isError": True,
        }


__all__ = ["build_dispatcher", "invoke_rpc", "handle_tools_call", "to_result"]