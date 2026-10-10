# HTTP MCP endpoint (experimental)

The addon can serve MCP itself over Streamable HTTP, on **port 9876**, in
addition to the XML-RPC endpoint on 9875 that the `freecad-mcp` package talks
to. Both run at the same time; neither replaces the other.

This is experimental and not the default path. See the repository README for
what it does and does not change.

## Starting it

1. Open FreeCAD with a GUI (`GuiUp` must be true — there is no viewport to
   capture or dispatch to otherwise).
2. **MCP Addon** workbench → **Start MCP HTTP Server**.

The Report View prints something like:

```
MCP HTTP Server started at 127.0.0.1:9876/mcp (PID 12345).
```

**Stop MCP HTTP Server** shuts it down again. If the port is taken, the bind
fails immediately and says so — nothing is left half-started, so you can retry
without restarting FreeCAD.

## Client configuration

```json
{
  "mcpServers": {
    "freecad-http": {
      "type": "http",
      "url": "http://127.0.0.1:9876/mcp"
    }
  }
}
```

Field names differ between clients. Cursor and VS Code's Copilot use
`serverUrl`; some older clients only accept `type: "sse"` and cannot reach a
Streamable HTTP endpoint at all.

### Constraints

These follow from how the endpoint is configured by default:

| Constraint | Why | What happens otherwise |
|---|---|---|
| URL must be `127.0.0.1`, `localhost` or `[::1]` | the Host allowlist defaults to loopback names | any other Host answers `403` |
| Only send `Authorization` if a token is set | an empty configured token means auth is off | `401` |
| Path is `/mcp`, method is `POST` | the endpoint serves no stream | `GET` answers `405` with `Allow: POST` |

With a token configured (workbench → **Set Auth Token**):

```json
{
  "mcpServers": {
    "freecad-http": {
      "type": "http",
      "url": "http://127.0.0.1:9876/mcp",
      "headers": { "Authorization": "Bearer your-token" }
    }
  }
}
```

Binding to a LAN address is refused by default, because no client's `Host`
header ever names a wildcard address and every LAN client would get a `403`.
The error names the address to dial instead.

## Content modes

Every tool takes a `content_mode` argument:

| Mode | Result |
|---|---|
| `"text"` (default) | the result alone |
| `"image"` | the result plus one isometric screenshot |
| `"multimodal"` | the result plus isometric, front and top screenshots |

Images are returned as MCP image content blocks (`type: "image"`,
`mimeType: "image/png"`), so a client renders them rather than printing base64.

```json
{"method": "tools/call",
 "params": {"name": "execute_code",
            "arguments": {"code": "...", "content_mode": "multimodal"}}}
```

The default is `text` because the modes differ by roughly two orders of
magnitude in size — a few kilobytes of text against tens or hundreds of
kilobytes per PNG.

**A missing image is always reported, never dropped.** If FreeCAD has no GUI,
the view cannot be captured, or the capture exceeds the size budget, the
response still contains the text and adds a line saying why the picture is
absent. Silently returning text alone would leave a client believing the model
looks the way the text describes.

`get_active_screenshot` returns its PNG directly as an image block, and asking
it for `content_mode` does not attach further views.

## Checking it by hand

```powershell
netstat -ano | findstr :9876
```

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:9876/mcp -Method Post `
  -ContentType 'application/json' `
  -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Known limitations

- **Needs a GUI.** Started under `freecadcmd`, `GuiUp` is 0 and every tool call
  fails — importing the addon also trips a pre-existing bug in
  `rpc_server.commands`, where `FreeCADGui.addCommand` is called without a
  command manager. That is upstream issue #14, not something this endpoint
  introduces.
- **Screenshots need a real viewport.** TechDraw and Spreadsheet views have no
  3D view to capture and return `None`.
- **GUI-thread waits still apply.** A long call occupies the main thread, and
  this endpoint inherits the same 60 s dispatch budget as the XML-RPC one.
  Serving over HTTP does not move any work off the GUI thread.
- **Protocol versions** `2025-11-25`, `2025-06-18` and `2025-03-26` are
  accepted. `2026-07-28` is refused with `400`: it removes sessions and the GET
  stream, and this endpoint offers neither.