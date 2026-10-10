"""Streamable HTTP transport: protocol, admission and routing.

Runs without FreeCAD. The transport takes an injected handler precisely so
this suite can drive a real socket without the addon loaded.
"""

from contextlib import contextmanager
import base64
import http.client
import json
from pathlib import Path
import sys
import threading
from typing import Iterator

import pytest


ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_http import protocol  # noqa: E402
from mcp_http.security import (  # noqa: E402
    ALLOWED,
    DENIED,
    MAX_REQUEST_BODY,
    UNAUTHORIZED,
    BindHostError,
    check_bind_host,
    check_body_length,
    classify,
    hostname_of,
    token_matches,
)
from mcp_http.transport import HTTPError, HTTPServerTransport  # noqa: E402


def echo_handler(payload: dict):
    """A tools/call handler that reports what it was asked for."""
    params = payload.get("params") or {}
    return {"content": [{"type": "text", "text": json.dumps(params)}]}


def list_tools_handler(payload: dict):
    return {"content": []}


def tool_transport(**kwargs) -> HTTPServerTransport:
    def handler(payload: dict):
        if payload.get("method") == "tools/list":
            return {"content": []}
        return echo_handler(payload)

    handler.list_tools = lambda: [
        {"name": "get_objects", "description": "list objects", "inputSchema": {}},
        {"name": "execute_code", "description": "run python", "inputSchema": {}},
    ]
    return HTTPServerTransport(handler=handler, **kwargs)


@contextmanager
def serving(transport: HTTPServerTransport) -> Iterator[HTTPServerTransport]:
    transport.bind()
    transport.serve_in_background()
    try:
        yield transport
    finally:
        transport.stop()


def post(
    transport: HTTPServerTransport,
    payload,
    headers: dict | None = None,
    raw: bytes | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict | bytes | None, dict]:
    """POST /mcp and return (status, parsed-or-raw body, response headers)."""
    conn = http.client.HTTPConnection(transport._host, transport.bound_port, timeout=10)
    try:
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        sent = dict(headers or {})
        if content_type is not None:
            sent.setdefault("Content-Type", content_type)
        conn.request("POST", "/mcp", body=body, headers=sent)
        response = conn.getresponse()
        data = response.read()
        received = {k.lower(): v for k, v in response.getheaders()}
        parsed: dict | bytes | None
        if data and received.get("content-type", "").startswith("application/json"):
            parsed = json.loads(data.decode("utf-8"))
        else:
            parsed = data
        return response.status, parsed, received
    finally:
        conn.close()


def request(transport: HTTPServerTransport, method: str) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection(transport._host, transport.bound_port, timeout=10)
    try:
        conn.request(method, "/mcp")
        response = conn.getresponse()
        body = response.read()
        received = {k.lower(): v for k, v in response.getheaders()}
        return response.status, received, body
    finally:
        conn.close()


def rpc(msg_id, method: str, params: dict | None = None, version: str | None = None):
    payload = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        payload["params"] = params
    if version is not None:
        payload["params"] = dict(params or {})
        payload["params"]["protocolVersion"] = version
    return payload


# --------------------------------------------------------------------- basics


def test_initialize_reports_negotiated_version():
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, rpc(1, "initialize", version="2025-06-18"))

    assert status == 200
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["serverInfo"]["name"] == "freecad-mcp"
    assert "tools" in body["result"]["capabilities"]


def test_initialize_without_version_defaults_to_legacy_revision():
    with serving(tool_transport()) as transport:
        _, body, _ = post(transport, rpc(1, "initialize"))

    assert body["result"]["protocolVersion"] == protocol.DEFAULT_PROTOCOL_VERSION


@pytest.mark.parametrize("version", protocol.SUPPORTED_PROTOCOL_VERSIONS)
def test_every_supported_version_is_accepted(version):
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, rpc(1, "initialize", version=version))

    assert status == 200
    assert body["result"]["protocolVersion"] == version


def test_unsupported_version_is_refused_with_400():
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, rpc(1, "initialize", version="2026-07-28"))

    assert status == 400
    assert body["error"]["code"] == protocol.UNSUPPORTED_PROTOCOL_VERSION
    assert "2025-06-18" in body["error"]["data"]["supported"]


def test_ping_returns_empty_result():
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, rpc(7, "ping"))

    assert status == 200
    assert body["result"] == {}


def test_tools_list_is_served():
    with serving(tool_transport()) as transport:
        _, body, _ = post(transport, rpc(2, "tools/list"))

    names = [tool["name"] for tool in body["result"]["tools"]]
    assert names == ["get_objects", "execute_code"]


def test_tools_call_reaches_the_handler():
    with serving(tool_transport()) as transport:
        status, body, _ = post(
            transport, rpc(3, "tools/call", {"name": "get_objects", "arguments": {"doc": "x"}})
        )

    assert status == 200
    assert body["result"]["content"][0]["type"] == "text"
    assert json.loads(body["result"]["content"][0]["text"])["name"] == "get_objects"


def test_unknown_method_is_method_not_found():
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, rpc(4, "does/not/exist"))

    assert status == 200
    assert body["error"]["code"] == protocol.METHOD_NOT_FOUND


# --------------------------------------------------------------- notifications


def test_notification_is_accepted_with_202_and_no_body():
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert status == 202
    assert not body


def test_batch_of_only_notifications_is_202():
    payload = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
    ]
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, payload)

    assert status == 202
    assert not body


def test_batch_returns_only_responses_for_requests():
    payload = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc(1, "ping"),
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
        rpc(2, "tools/list"),
    ]
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, payload)

    assert status == 200
    assert isinstance(body, list)
    assert [item["id"] for item in body] == [1, 2]


# ------------------------------------------------------------------- routing


def test_get_is_405_and_advertises_post():
    with serving(tool_transport()) as transport:
        status, headers, body = request(transport, "GET")

    assert status == 405
    assert headers["allow"] == "POST"
    assert body == b""


def test_delete_is_405():
    with serving(tool_transport()) as transport:
        status, _, _ = request(transport, "DELETE")

    assert status == 405


def test_unknown_path_is_404():
    with serving(tool_transport()) as transport:
        status, _, _ = post(transport, rpc(1, "ping"), raw=b"{}")
        conn = http.client.HTTPConnection(transport._host, transport.bound_port, timeout=10)
        try:
            conn.request("POST", "/nope", body=b"{}", headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            assert response.status == 404
        finally:
            conn.close()


def test_malformed_json_is_parse_error():
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, None, raw=b"{not json")

    assert status == 400
    assert body["error"]["code"] == protocol.PARSE_ERROR


def test_non_jsonrpc_version_is_rejected():
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, {"jsonrpc": "1.0", "id": 1, "method": "ping"})

    assert body["error"]["code"] == protocol.INVALID_REQUEST


# ------------------------------------------------------------------- admission


def test_xml_content_type_is_refused():
    with serving(tool_transport()) as transport:
        status, body, _ = post(transport, rpc(1, "ping"), content_type="text/xml")

    assert status == 415


def test_json_content_type_with_charset_is_accepted():
    with serving(tool_transport()) as transport:
        status, _, _ = post(transport, rpc(1, "ping"), content_type="application/json; charset=utf-8")

    assert status == 200


def test_request_without_token_is_served():
    with serving(tool_transport()) as transport:
        status, _, _ = post(transport, rpc(1, "ping"))

    assert status == 200


def test_missing_token_is_401_with_challenge():
    with serving(tool_transport(auth_token="s3cret")) as transport:
        status, _, headers = post(transport, rpc(1, "ping"))

    assert status == 401
    assert headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_401():
    with serving(tool_transport(auth_token="s3cret")) as transport:
        status, _, _ = post(transport, rpc(1, "ping"), headers={"Authorization": "Bearer nope"})

    assert status == 401


def test_correct_token_is_served():
    with serving(tool_transport(auth_token="s3cret")) as transport:
        status, _, _ = post(transport, rpc(1, "ping"), headers={"Authorization": "Bearer s3cret"})

    assert status == 200


def test_non_ascii_authorization_header_is_401_not_a_crash():
    """A latin-1 header byte above 0x80 makes compare_digest raise TypeError.

    Without the guard in security.token_matches this takes the handler thread
    down and the caller gets no response at all.
    """
    with serving(tool_transport(auth_token="s3cret")) as transport:
        status, _, _ = post(transport, rpc(1, "ping"), headers={"Authorization": "Bearer éé"})

    assert status == 401


def test_non_ascii_token_is_401_not_a_crash():
    """A token outside latin-1 cannot leave through http.client at all, so the
    guard is exercised at the function the handler actually calls."""
    assert token_matches("Bearer 中文字符", "abc") is False
    assert token_matches("Bearer \U0001f600", "abc") is False


def test_origin_header_is_403():
    with serving(tool_transport()) as transport:
        status, _, _ = post(
            transport, rpc(1, "ping"), headers={"Origin": "https://evil.example"}
        )

    assert status == 403


def test_foreign_host_is_403():
    with serving(tool_transport()) as transport:
        status, _, _ = post(transport, rpc(1, "ping"), headers={"Host": "evil.example:9876"})

    assert status == 403


def test_denied_is_403_even_with_a_valid_token():
    """Policy refusal and bad credential are different answers.

    A caller that is not allowed here at all should not be invited to retry,
    which is what returning 401 here would do.
    """
    with serving(tool_transport(auth_token="s3cret")) as transport:
        status, _, _ = post(
            transport,
            rpc(1, "ping"),
            headers={"Authorization": "Bearer s3cret", "Origin": "https://evil.example"},
        )

    assert status == 403


# ----------------------------------------------------------------- body limits


def test_oversized_content_length_is_413():
    with serving(tool_transport()) as transport:
        conn = http.client.HTTPConnection(transport._host, transport.bound_port, timeout=10)
        try:
            conn.putrequest("POST", "/mcp", skip_accept_encoding=True)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(MAX_REQUEST_BODY + 1))
            conn.endheaders()
            response = conn.getresponse()
            response.read()
            assert response.status == 413
        finally:
            conn.close()


def test_negative_content_length_is_400():
    """Trusting it would turn the read into a block until the socket timeout."""
    assert check_body_length("-1") == (400, "Content-Length must not be negative")

    with serving(tool_transport()) as transport:
        conn = http.client.HTTPConnection(transport._host, transport.bound_port, timeout=10)
        try:
            conn.putrequest("POST", "/mcp", skip_accept_encoding=True)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "-1")
            conn.endheaders()
            response = conn.getresponse()
            response.read()
            assert response.status == 400
        finally:
            conn.close()


def test_non_numeric_content_length_is_400():
    assert check_body_length("lots") == (400, "Content-Length is not a number")


def test_body_within_cap_is_accepted():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    raw = json.dumps(payload).encode("utf-8").ljust(MAX_REQUEST_BODY // 2, b" ")
    with serving(tool_transport()) as transport:
        status, _, _ = post(transport, None, raw=raw)

    # Padded whitespace stays valid JSON, so this exercises the cap, not the parser.
    assert status == 200


# ------------------------------------------------------------- policy functions


def test_wildcard_bind_is_refused():
    with pytest.raises(BindHostError):
        check_bind_host("0.0.0.0")
    with pytest.raises(BindHostError):
        check_bind_host("::")


def test_wildcard_bind_can_be_overridden_with_explicit_hosts():
    transport = HTTPServerTransport(host="0.0.0.0", allowed_hosts=("192.168.1.10",))
    assert transport._allowed_hosts == ("192.168.1.10",)


def test_hostname_of_strips_port_and_brackets():
    assert hostname_of("localhost:9876") == "localhost"
    assert hostname_of("[::1]:9876") == "[::1]"
    assert hostname_of("LOCALHOST") == "localhost"
    assert hostname_of(None) is None


def test_token_comparison_is_constant_time_and_crash_safe():
    assert token_matches("Bearer abc", "abc") is True
    assert token_matches("Bearer abc", "abd") is False
    # Basic auth carries the token in the password field; username is ignored.
    assert token_matches("Basic " + base64.b64encode(b":abc").decode(), "abc") is True
    assert token_matches("Basic " + base64.b64encode(b"user:abc").decode(), "abc") is True
    assert token_matches("Basic " + base64.b64encode(b"user:abd").decode(), "abc") is False
    assert token_matches("Basic !!!!not-base64", "abc") is False
    assert token_matches(None, "abc") is False
    assert token_matches("Bearer \udcff", "abc") is False


def test_classify_separates_denied_from_unauthorized():
    common = {
        "client_ip": "127.0.0.1",
        "host_header": "localhost:9876",
        "origin_header": None,
        "authorization": None,
        "allowed_ips": "127.0.0.1",
        "allowed_hosts": ("localhost",),
        "auth_token": "tok",
    }

    assert classify(**common)[0] == UNAUTHORIZED

    assert classify(**{**common, "origin_header": "https://evil.example"})[0] == DENIED
    assert classify(**{**common, "host_header": "evil.example"})[0] == DENIED
    assert classify(**{**common, "client_ip": "10.0.0.5"})[0] == DENIED

    assert classify(**{**common, "authorization": "Bearer tok"})[0] == ALLOWED


def test_empty_token_configured_does_not_refuse_everything():
    """An empty token means auth is off, not that every request is invalid."""
    with serving(tool_transport(auth_token="")) as transport:
        status, _, _ = post(transport, rpc(1, "ping"))

    assert status == 200


# ------------------------------------------------------------------ lifecycle


def test_bind_reports_port_conflict_immediately():
    first = tool_transport(port=0)
    first.bind()
    try:
        # Port 0 asked twice must not collide; use the real port for the clash.
        second = tool_transport(port=first.bound_port)
        with pytest.raises(OSError):
            second.bind()
    finally:
        first.stop()


def test_stop_is_idempotent():
    transport = tool_transport()
    transport.bind()
    transport.stop()
    transport.stop()


def test_serve_without_bind_is_an_error():
    with pytest.raises(RuntimeError):
        tool_transport().serve_in_background()


def test_double_bind_is_an_error():
    transport = tool_transport()
    transport.bind()
    try:
        with pytest.raises(RuntimeError):
            transport.bind()
    finally:
        transport.stop()


def test_handler_can_be_replaced_after_binding():
    transport = tool_transport()
    with serving(transport):
        transport.set_handler(lambda payload: {"content": [{"type": "text", "text": "swapped"}]})
        _, body, _ = post(transport, rpc(1, "tools/call", {"name": "x"}))
        assert body["result"]["content"][0]["text"] == "swapped"


def test_protocol_error_helpers_preserve_the_id():
    body = protocol.error_response(42, protocol.INTERNAL_ERROR, "boom")
    assert body["id"] == 42
    assert body["error"]["code"] == protocol.INTERNAL_ERROR
    assert "data" not in body["error"]


def test_http_error_carries_status_and_payload():
    err = HTTPError(401, "nope", {"headers": {"WWW-Authenticate": "Bearer"}})
    assert err.status == 401
    assert err.payload["headers"]["WWW-Authenticate"] == "Bearer"