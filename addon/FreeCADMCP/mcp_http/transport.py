"""Streamable HTTP transport for MCP, served from inside the addon.

Pure standard library, no asyncio and no ``mcp`` SDK: FreeCAD's main loop is a
Qt event loop, so both an asyncio loop and a threaded HTTP server are things
the addon has to live with. ThreadingHTTPServer keeps the structure identical
to the XML-RPC endpoint that already runs in production.

The request handler is injected, so this module never imports FreeCAD and can
be exercised headlessly. FreeCAD supplies the handler that routes tools/call
through ``dispatch_to_gui``.

Endpoints on one listener:
    POST /mcp   the JSON-RPC reply comes back inline as application/json;
                a notification (no id) is answered 202 with an empty body
    GET  /mcp   405 -- this server offers no server-to-client stream
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from mcp_http import protocol
from mcp_http.security import (
    ALLOWED,
    DENIED,
    MAX_REQUEST_BODY,
    UNAUTHORIZED,
    check_bind_host,
    check_body_length,
    check_content_type,
    classify,
    default_allowed_hosts,
)

# A handler receives the parsed JSON-RPC request and returns either the result
# payload for a request, or None for a notification.
ToolHandler = Callable[[dict], Any]

MAX_JSON_DEPTH_NOTE = "request nesting is bounded by json.loads recursion limits"


class HTTPError(Exception):
    """A failure to answer at the HTTP layer rather than the JSON-RPC layer.

    ``headers`` are emitted on the response; ``payload`` is the JSON body. They
    are separate because a challenge such as WWW-Authenticate has to reach the
    client as a header, not as content it would have to parse to find out that
    it needs a credential.
    """

    def __init__(
        self,
        status: int,
        reason: str,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.payload = payload
        self.headers = headers or {}


class HTTPServerTransport:
    """Server-side Streamable HTTP transport on a single listener.

    Sized for one connected client at a time, which is what a desktop-app MCP
    server attached to a running FreeCAD is.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9876,
        handler: ToolHandler | None = None,
        allowed_ips: str = "127.0.0.1",
        allowed_hosts: tuple[str, ...] | None = None,
        allowed_origins: tuple[str, ...] = (),
        auth_token: str | None = None,
        loopback_only: bool | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._handler = handler
        self._allowed_ips = allowed_ips
        # Falsy means "no token configured". Never enforce an empty token:
        # that would refuse every request with no way to present a valid one.
        self._auth_token = auth_token or None
        if loopback_only is None:
            loopback_only = host.lower() in ("127.0.0.1", "localhost", "::1")
        self._loopback_only = loopback_only
        if allowed_hosts is None:
            check_bind_host(host)
            self._allowed_hosts = default_allowed_hosts(host, loopback_only)
        else:
            self._allowed_hosts = tuple(name.lower() for name in allowed_hosts)
        self._allowed_origins = frozenset(allowed_origins)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._serving = False

    # ------------------------------------------------------------------ setup

    @property
    def bound_port(self) -> int:
        """The port actually bound, which differs from ``port`` when 0 was asked
        for."""
        if self._httpd is None:
            return self._port
        return self._httpd.server_address[1]

    def set_handler(self, handler: ToolHandler) -> None:
        self._handler = handler

    def bind(self) -> None:
        """Bind the listening socket, reporting a clash immediately.

        Binding is deliberately synchronous and separate from serve() so an
        EADDRINUSE surfaces on the caller's thread, where it can be reported,
        rather than disappearing into a background one.
        """
        if self._httpd is not None:
            raise RuntimeError("already bound; call stop() first")

        # The name bound in the class body must not also be assigned there:
        # a class body resolves reads against its own namespace first and only
        # then globals, skipping the enclosing function entirely.
        owner = self

        class Handler(_RequestHandler):
            transport = owner

        self._httpd = ThreadingHTTPServer((self._host, self._port), Handler)
        self._httpd.daemon_threads = True

    def serve(self) -> None:
        """Serve until stop(). Blocks; use the thread-safe helper for the
        background case."""
        if self._httpd is None:
            raise RuntimeError("bind() first")
        self._serving = True
        try:
            self._httpd.serve_forever(poll_interval=0.2)
        finally:
            self._serving = False

    def serve_in_background(self) -> None:
        """Serve on a daemon thread and return once the loop is up."""
        if self._httpd is None:
            raise RuntimeError("bind() first")
        self._thread = threading.Thread(
            target=self.serve, name="mcp-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket. Idempotent.

        shutdown() only when a serve loop is actually running: it waits on an
        event that serve_forever is the only thing to set, so calling it after
        a bare bind() would block forever.
        """
        if self._httpd is not None:
            if self._serving:
                self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._serving = False
        self._thread = None

    # --------------------------------------------------------------- admission

    def admit(self, handler: "_RequestHandler") -> None:
        """Raise HTTPError unless this request may proceed."""
        verdict, reason = classify(
            client_ip=handler.client_address[0],
            host_header=handler.headers.get("Host"),
            origin_header=handler.headers.get("Origin"),
            authorization=handler.headers.get("Authorization"),
            allowed_ips=self._allowed_ips,
            allowed_hosts=self._allowed_hosts,
            allowed_origins=self._allowed_origins,
            auth_token=self._auth_token,
            loopback_only=self._loopback_only,
        )
        if verdict == ALLOWED:
            return
        if verdict == DENIED:
            raise HTTPError(403, reason)
        raise HTTPError(401, reason, headers={"WWW-Authenticate": "Bearer"})

    # ---------------------------------------------------------------- routing

    def dispatch(self, payload: Any) -> tuple[int, dict | None]:
        """Route one parsed JSON-RPC message.

        Returns ``(status, body)``; body is None for a notification, which is
        answered 202 with no content per the Streamable HTTP rules.
        """
        if isinstance(payload, list):
            return self._dispatch_batch(payload)
        if not isinstance(payload, dict):
            raise HTTPError(
                400,
                "request must be a JSON-RPC object",
                protocol.error_response(None, protocol.INVALID_REQUEST, "invalid request"),
            )

        msg_id = payload.get("id")
        is_notification = "id" not in payload

        if payload.get("jsonrpc") != "2.0":
            if is_notification:
                raise HTTPError(
                    400,
                    "jsonrpc must be \"2.0\"",
                    protocol.error_response(None, protocol.INVALID_REQUEST, "invalid request"),
                )
            return 200, protocol.error_response(
                msg_id, protocol.INVALID_REQUEST, "jsonrpc must be \"2.0\""
            )

        version = handler_version(payload)
        if not protocol.version_supported(version):
            body = protocol.error_response(
                None if is_notification else msg_id,
                protocol.UNSUPPORTED_PROTOCOL_VERSION,
                f"unsupported MCP protocol version: {version}",
                {"supported": list(protocol.SUPPORTED_PROTOCOL_VERSIONS)},
            )
            if is_notification:
                return 202, None
            raise HTTPError(400, "unsupported protocol version", body)

        method = payload.get("method")
        if method is None:
            body = protocol.error_response(
                None if is_notification else msg_id,
                protocol.INVALID_REQUEST,
                "method is required",
            )
            if is_notification:
                return 202, None
            return 200, body

        params = payload.get("params") or {}

        if method == "initialize":
            result = protocol.initialize_result(protocol.negotiated_version(version))
        elif method in ("notifications/initialized", "notifications/cancelled"):
            if is_notification:
                return 202, None
            result = {}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self._list_tools()}
        elif method == "tools/call":
            if self._handler is None:
                body = protocol.error_response(
                    None if is_notification else msg_id,
                    protocol.METHOD_NOT_FOUND,
                    "no tool handler is registered",
                )
                if is_notification:
                    return 202, None
                return 200, body
            result = self._handler(payload)
            if isinstance(result, dict) and result.get("isError"):
                result = dict(result)
        else:
            if is_notification:
                return 202, None
            return 200, protocol.error_response(
                msg_id, protocol.METHOD_NOT_FOUND, f"unknown method: {method}"
            )

        if is_notification:
            return 202, None
        return 200, protocol.result_response(msg_id, result)

    def _dispatch_batch(self, payloads: list) -> tuple[int, dict | None]:
        """Handle a JSON-RPC batch. Notifications contribute no responses."""
        responses = []
        for item in payloads:
            status, body = self.dispatch(item)
            if status == 200 and body is not None:
                responses.append(body)
        if not responses:
            return 202, None
        return 200, responses

    def _list_tools(self) -> list[dict]:
        if self._handler is None:
            return []
        lister = getattr(self._handler, "list_tools", None)
        if callable(lister):
            return list(lister())
        return []


def handler_version(payload: dict) -> str | None:
    """The protocol version a request declares, if any."""
    meta = payload.get("_meta")
    if isinstance(meta, dict):
        value = meta.get("io.modelcontextprotocol/protocolVersion")
        if isinstance(value, str):
            return value
    params = payload.get("params")
    if isinstance(params, dict):
        value = params.get("protocolVersion")
        if isinstance(value, str):
            return value
    return None


class _RequestHandler(BaseHTTPRequestHandler):
    """Maps HTTP onto the transport. Logging stays off: a blocked GUI thread
    would otherwise block the HTTP worker too."""

    protocol_version = "HTTP/1.1"
    transport: HTTPServerTransport

    # Keep the console readable when a client misbehaves.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/mcp":
            self._send(404, {"error": "not found"})
            return

        try:
            self.transport.admit(self)
            refusal = check_content_type(self.headers.get("Content-Type"))
            if refusal is not None:
                self._send(refusal[0], {"error": refusal[1]})
                return
            refusal = check_body_length(self.headers.get("Content-Length"))
            if refusal is not None:
                self._send(refusal[0], {"error": refusal[1]})
                return

            raw = self._read_body()
            if raw is None:
                self._send(400, {"error": "request body exceeds the size limit"})
                return

            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send(
                    400,
                    protocol.error_response(None, protocol.PARSE_ERROR, f"parse error: {exc}"),
                )
                return

            status, body = self.transport.dispatch(payload)
            if status == 202:
                self._send_empty(202)
            else:
                self._send(200, body)
        except HTTPError as exc:
            self._send(exc.status, exc.payload or {"error": exc.reason}, exc.headers)

    def do_GET(self) -> None:
        # The spec allows a server to expose a stream here; this one does not,
        # and saying so plainly beats hanging a connection open.
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ----------------------------------------------------------------- helpers

    def _read_body(self) -> bytes | None:
        """Read exactly the bytes the request declared, or None if we refuse.

        The read length comes from Content-Length, never from the cap: asking
        a socket for more bytes than the peer will send blocks until the
        timeout with no response written, which is the hang the cap is meant to
        prevent.
        """
        raw_length = self.headers.get("Content-Length")
        if raw_length in (None, ""):
            # No declared length: there is no body to read. Chunked uploads
            # are not accepted; refusing them here beats blocking on them.
            if (self.headers.get("Transfer-Encoding") or "").strip():
                raise HTTPError(411, "a Content-Length is required")
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            return None
        if length < 0 or length > MAX_REQUEST_BODY:
            return None
        return self.rfile.read(length)

    def _send(self, status: int, payload: Any, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


__all__ = ["HTTPServerTransport", "HTTPError", "ToolHandler", "MAX_JSON_DEPTH_NOTE"]