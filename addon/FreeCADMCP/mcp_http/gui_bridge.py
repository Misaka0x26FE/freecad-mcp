"""Bridge from an HTTP tools/call to the FreeCAD GUI thread.

The only module here that imports FreeCAD. Everything above it -- transport,
security, tools -- stays importable without it, which is what lets the
protocol and routing be tested headlessly.

Nothing here improves on the XML-RPC path's threading: App::Document,
ViewObject and Coin3D can only be touched from the main thread, so a call
arriving over HTTP dispatches exactly the way a call arriving over XML-RPC
does. Changing the transport changes nothing about that.
"""

from typing import Any

from mcp_http.tools import ToolDispatcher, to_result

# How long a single tool call may occupy the GUI thread, in seconds. Matches
# the XML-RPC endpoint's default so behaviour is comparable between them.
DEFAULT_GUI_TIMEOUT = 60


def build_dispatcher(gui_timeout: int = DEFAULT_GUI_TIMEOUT) -> ToolDispatcher:
    """A ToolDispatcher whose calls run on the GUI thread.

    ``FreeCADRPC`` is imported lazily and reached through its module instance
    rather than a fresh one, so the HTTP endpoint shares the same object the
    XML-RPC endpoint serves.
    """
    return ToolDispatcher(lambda name, kwargs: invoke_on_gui(name, kwargs, gui_timeout))


def invoke_on_gui(name: str, kwargs: dict, gui_timeout: int = DEFAULT_GUI_TIMEOUT) -> Any:
    """Run one RPC method on the GUI thread and return its value."""
    from rpc_server import rpc_server
    from rpc_server.gui_dispatch import dispatch_to_gui

    rpc = rpc_server.FreeCADRPC()
    method = getattr(rpc, name, None)
    if method is None or not callable(method):
        raise AttributeError(f"FreeCADRPC has no method {name}")

    return dispatch_to_gui(
        lambda: method(**kwargs),
        timeout=gui_timeout,
        operation_name=name,
    )


def handle_tools_call(payload: dict, gui_timeout: int = DEFAULT_GUI_TIMEOUT) -> dict:
    """Entry point for HTTPServerTransport: a tools/call request in, MCP tool
    result out.

    Never raises. A GUI-dispatch timeout comes back as an error result rather
    than an exception, because the HTTP call did succeed -- the operation did
    not, and the client needs to be told which.
    """
    return build_dispatcher(gui_timeout)(payload)