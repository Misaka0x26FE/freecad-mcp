"""Start/stop lifecycle for the addon's HTTP endpoint.

Exercised without FreeCAD by pointing the controller at a stub settings module
and a stub handler, so bind conflicts and the already-running path are covered
without needing the addon loaded.
"""

from pathlib import Path
import socket
import sys
import types

import pytest


ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_http import server as http_server  # noqa: E402
from mcp_http.security import BindHostError  # noqa: E402


@pytest.fixture(autouse=True)
def stub_settings(monkeypatch):
    """Point load_settings at a fixed dict and drop the FreeCAD-dependent
    handler, so start/stop can run outside FreeCAD."""
    fake = types.ModuleType("rpc_server.settings")
    fake.load_settings = lambda: {
        "allowed_ips": "127.0.0.1",
        "auth_token": "",
        "auto_start_rpc": False,
    }
    fake.save_settings = lambda settings: None
    monkeypatch.setitem(sys.modules, "rpc_server.settings", fake)

    bridge = types.ModuleType("mcp_http.gui_bridge")
    # Use the real dispatcher so tools/list is exercised the way it is in
    # FreeCAD, rather than against a stub with no list_tools of its own.
    from mcp_http.tools import ToolDispatcher

    bridge.build_dispatcher = lambda *a, **k: ToolDispatcher(
        lambda name, kwargs: {"content": [{"type": "text", "text": "stub"}]}
    )
    monkeypatch.setitem(sys.modules, "mcp_http.gui_bridge", bridge)

    http_server.http_transport = None
    yield
    if http_server.http_transport is not None:
        http_server.http_transport.stop()
    http_server.http_transport = None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_start_binds_and_reports_its_address():
    port = free_port()

    message = http_server.start_http_server(port)

    assert str(port) in message
    assert "/mcp" in message
    assert http_server.is_running()
    assert http_server.http_transport.bound_port == port


def test_start_twice_is_reported_not_attempted():
    port = free_port()
    http_server.start_http_server(port)

    second = http_server.start_http_server(port)

    assert "already running" in second


def test_stop_releases_and_is_idempotent():
    http_server.start_http_server(free_port())

    first = http_server.stop_http_server()
    second = http_server.stop_http_server()

    assert "stopped" in first
    assert "not running" in second
    assert http_server.is_running() is False


def test_stopped_port_can_be_bound_again():
    """A stop that leaks the socket would make the command unusable without
    restarting FreeCAD."""
    port = free_port()
    http_server.start_http_server(port)
    http_server.stop_http_server()

    again = http_server.start_http_server(port)

    assert str(port) in again


def test_port_clash_raises_and_leaves_nothing_running():
    with socket.socket() as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]

        with pytest.raises(RuntimeError) as excinfo:
            http_server.start_http_server(taken)

    assert "could not bind" in str(excinfo.value)
    assert http_server.is_running() is False
    assert http_server.http_transport is None


def test_wildcard_bind_is_refused_with_an_actionable_message():
    """Binding every interface makes the Host allowlist unusable, so the
    refusal has to name the address the user should dial instead."""
    with pytest.raises(BindHostError) as excinfo:
        http_server.build_transport(free_port(), host="0.0.0.0")

    message = str(excinfo.value)
    assert "403" in message
    assert "MCP_HTTP_HOST" in message


def test_bind_reports_the_error_before_starting():
    with pytest.raises(BindHostError):
        http_server.start_http_server(free_port(), host="0.0.0.0")


def test_auth_token_from_settings_is_reported_when_set(monkeypatch):
    fake = sys.modules["rpc_server.settings"]
    monkeypatch.setattr(
        fake, "load_settings",
        lambda: {"allowed_ips": "127.0.0.1", "auth_token": "tok", "auto_start_rpc": False},
    )

    message = http_server.start_http_server(free_port())

    assert "Auth token required" in message
    assert http_server.http_transport._auth_token == "tok"


def test_endpoint_answers_a_ping_over_http():
    """End to end on the controller path, without FreeCAD."""
    import http.client
    import json

    port = free_port()
    http_server.start_http_server(port)

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "ping", "arguments": {}}})
        conn.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read().decode())
    finally:
        conn.close()

    assert response.status == 200
    assert payload["result"]["content"][0]["text"] == "stub"


def test_tools_list_reports_the_full_surface_over_http():
    import http.client
    import json

    port = free_port()
    http_server.start_http_server(port)

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        conn.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read().decode())
    finally:
        conn.close()

    names = {tool["name"] for tool in payload["result"]["tools"]}
    assert "execute_code" in names
    assert "get_objects" in names