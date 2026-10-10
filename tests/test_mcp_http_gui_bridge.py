"""The HTTP bridge must not wrap FreeCADRPC calls in a second GUI dispatch.

Regression guard. The RPC methods already dispatch themselves; an outer
dispatch deadlocks, because the outer task occupies the only thread that
drains the queue while the inner one waits for a task that can never run. A
real FreeCAD hung for the full 60 s timeout before this was found.

Stubs the addon modules so the guard runs without FreeCAD.
"""

from pathlib import Path
import sys
import types

import pytest


ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))


@pytest.fixture
def fake_rpc(monkeypatch):
    """A stub FreeCADRPC plus a dispatch_to_gui that fails loudly if called."""
    calls = []

    class FreeCADRPC:
        def list_documents(self):
            calls.append("list_documents")
            return ["doc"]

        def execute_code(self, code, timeout=None):
            calls.append("execute_code")
            return {"stdout": code}

        def ping(self):
            calls.append("ping")
            return True

    def boom(*args, **kwargs):
        raise AssertionError(
            "gui_bridge must not call dispatch_to_gui: FreeCADRPC already "
            "dispatches internally and a second dispatch deadlocks"
        )

    gui_dispatch = types.ModuleType("rpc_server.gui_dispatch")
    gui_dispatch.dispatch_to_gui = boom
    monkeypatch.setitem(sys.modules, "rpc_server.gui_dispatch", gui_dispatch)

    rpc_server = types.ModuleType("rpc_server.rpc_server")
    rpc_server.FreeCADRPC = FreeCADRPC
    monkeypatch.setitem(sys.modules, "rpc_server.rpc_server", rpc_server)

    return calls


def test_rpc_call_does_not_go_through_dispatch_to_gui(fake_rpc):
    from mcp_http.gui_bridge import invoke_rpc

    assert invoke_rpc("list_documents", {}) == ["doc"]
    assert fake_rpc == ["list_documents"]


def test_tools_call_does_not_go_through_dispatch_to_gui(fake_rpc):
    from mcp_http.gui_bridge import handle_tools_call

    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "list_documents", "arguments": {}}}

    result = handle_tools_call(payload)

    assert fake_rpc == ["list_documents"]
    assert "doc" in result["content"][0]["text"]


def test_double_dispatch_would_have_been_caught(fake_rpc):
    """The guard is real: if the bridge did dispatch, this fails."""
    from mcp_http.gui_bridge import invoke_rpc

    # Simulate the bug that was fixed: wrapping the same call again.
    from rpc_server.gui_dispatch import dispatch_to_gui

    with pytest.raises(AssertionError, match="must not call dispatch_to_gui"):
        dispatch_to_gui(lambda: invoke_rpc("ping", {}))


def test_unknown_method_is_reported_not_raised(fake_rpc):
    from mcp_http.gui_bridge import handle_tools_call

    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "no_such_method", "arguments": {}}}

    result = handle_tools_call(payload)

    assert result["isError"] is True
    assert "no_such_method" in result["content"][0]["text"]


def test_rpc_fault_becomes_a_tool_error(monkeypatch, fake_rpc):
    """The RPC layer reports dispatch failure by raising xmlrpc Fault."""
    import xmlrpc.client

    def failing(self):
        raise xmlrpc.client.Fault(1, "GUI_DISPATCH_FAILED: gave up")

    rpc_class = type(sys.modules["rpc_server.rpc_server"].FreeCADRPC())
    monkeypatch.setattr(rpc_class, "list_documents", failing)

    from mcp_http.gui_bridge import handle_tools_call

    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "list_documents", "arguments": {}}}

    result = handle_tools_call(payload)

    assert result["isError"] is True
    assert "GUI_DISPATCH_FAILED" in result["content"][0]["text"]