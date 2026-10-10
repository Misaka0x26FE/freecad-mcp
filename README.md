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

**Phase 1 — transport only, and only tested without FreeCAD.**

Present:

- `addon/FreeCADMCP/mcp_http/protocol.py` — JSON-RPC framing, protocol-version
  whitelist
- `addon/FreeCADMCP/mcp_http/security.py` — IP allowlist, bearer token,
  browser guard, body limits
- `addon/FreeCADMCP/mcp_http/transport.py` — the HTTP listener
- `tests/test_mcp_http_transport.py` — 46 tests, none of which need FreeCAD

Endpoints on one listener:

| Route | Behaviour |
|---|---|
| `POST /mcp` | reply inline as `application/json`; a notification gets `202` and an empty body |
| `GET /mcp` | `405`, with `Allow: POST` — this server offers no server-to-client stream |
| `DELETE /mcp` | `405`, there are no sessions to end |

Not present yet: the GUI-side wiring that routes `tools/call` through
`dispatch_to_gui`, tool schema generation, and the FreeCAD toolbar command.

**Nothing here has been run against a real FreeCAD.** The transport takes an
injected handler precisely so the protocol, admission and routing layers can be
tested in isolation — which also means the integration is still unproven.

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