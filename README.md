# freecad-mcp-shttp

An experiment in serving MCP **from inside the FreeCAD addon** over
Streamable HTTP, rather than from a separate `freecad-mcp` server process.

> This is **not** the upstream project. It is a fork of
> [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp) carrying one
> line of work. For the real thing, use the upstream repository.

## Why

`freecad-mcp` ships as two artifacts that must be kept in step by hand:

```
Agent ──MCP/stdio──> freecad-mcp server ──XML-RPC──> FreeCAD addon
                      (pip / uvx)      (:9875)      (copy into Mod/)
```

The MCP server is installed from a package index; the addon is a directory
copy into FreeCAD's Mod directory, whose path depends on the FreeCAD version
and packaging. When they disagree, the version handshake in `version.py`
detects it and tells the user which side to update — but the copy still has to
happen.

Serving MCP from the addon collapses that to one artifact.

## What this is not

It does **not** remove FreeCAD's GUI-thread constraint. `App::Document`,
`ViewObject` and the Coin3D scene graph can only be touched from the main
thread, so `tools/call` still dispatches through `dispatch_to_gui`. Changing
the transport changes nothing about that.

## Status

**Phases 1–3 — transport, wiring and content modes, verified against a real
FreeCAD 1.1.3 GUI.**

Present:

- `addon/FreeCADMCP/mcp_http/protocol.py` — JSON-RPC framing, protocol-version
  whitelist
- `addon/FreeCADMCP/mcp_http/security.py` — IP allowlist, bearer token,
  browser guard, body limits
- `addon/FreeCADMCP/mcp_http/transport.py` — the HTTP listener
- `addon/FreeCADMCP/mcp_http/tools.py` — tool schemas and `content_mode`
- `addon/FreeCADMCP/mcp_http/content.py` — image content and its degradation
- `addon/FreeCADMCP/mcp_http/gui_bridge.py` — the layer that reaches FreeCAD
- `addon/FreeCADMCP/mcp_http/server.py` — start/stop and settings
- `docs/http-endpoint.md` — starting it, client JSON, content modes

Endpoints on one listener:

| Route | Behaviour |
|---|---|
| `POST /mcp` | reply inline as `application/json`; a notification gets `202` and an empty body |
| `GET /mcp` | `405`, with `Allow: POST` — this server offers no server-to-client stream |
| `DELETE /mcp` | `405`, there are no sessions to end |

**Phase 3 — content modes, verified against a real FreeCAD 1.1.3 GUI.**

Tool calls have been exercised end to end under FreeCAD 1.1.3 with `GuiUp=1`:
`execute_code` builds real geometry (a 40×30×20 box returns its exact 24 000 mm³
volume and 6 faces), documents are created and listed, and the three content
modes return real PNG captures. 334 tests pass.

Three bugs were only reachable this way and are fixed:

- the transport blocked on a read longer than the peer would ever send, which
  pinned a worker thread until the socket timeout instead of answering
- `stop()` deadlocked when no serve loop was running, because `shutdown()`
  waits on an event `serve_forever` is the only thing to set
- the bridge deadlocked the GUI thread by dispatching work the RPC layer had
  already dispatched itself

`fc_gui_e2e.py` and `fc_mm_test.py` in the repository root carry the GUI
checks; they need a display but nothing else.

## Documents

[docs/http-endpoint.md](docs/http-endpoint.md) covers starting the endpoint,
client configuration in JSON, the content modes, and what it does not change.

## Design notes

**Port 9876 alongside 9875, not instead of it.** A fault in an unreleased
endpoint should be switchable without touching anyone on the current path, and
having both ports gives a way to compare behaviour between them.

**Admission is three-valued.** A policy refusal answers `403` and carries no
challenge; a missing or wrong credential answers `401` with
`WWW-Authenticate: Bearer`. Collapsing the two invites a client to retry
against a server that will never accept it.

**Bodies are bounded by `Content-Length`, not by a read cap.** Asking a socket
for more bytes than the peer will send blocks until the timeout with nothing
written — a negative `Content-Length` is refused for the same reason, and one
above the cap answers `413`.

**Token comparison is guarded against `TypeError`.** HTTP headers decode as
latin-1, so a byte above `0x80` makes `hmac.compare_digest` raise. Without the
guard an unauthenticated caller could take the handler thread down with a
malformed header and get no response at all. This gap is still open on the
upstream XML-RPC path in `ip_filter.authorization_ok`.

**Protocol versions** `2025-11-25`, `2025-06-18` and `2025-03-26` are
accepted; anything else is refused with `400`. `2026-07-28` is deliberately not
implemented — it removes both sessions and the GET stream, and half of that
would be worse than refusing it.

## Running the tests

No FreeCAD required:

```bash
uv run pytest tests/test_mcp_http_transport.py -v
```

## Licence

MIT, inherited from [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp).

Copyright (c) 2025 Shirokuma (k tanaka) — see [`LICENSE`](./LICENSE).