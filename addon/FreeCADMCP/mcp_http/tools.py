"""Tool schemas and the tools/call handler for the HTTP endpoint.

The schemas describe the same methods the XML-RPC endpoint exposes on
FreeCADRPC, so both endpoints answer the same call with the same result. This
module holds no FreeCAD import: the dispatcher that reaches the GUI thread is
injected, which keeps the schema and the routing testable headlessly.
"""

from typing import Any, Callable

# JSON Schema for one tool, derived from the FreeCADRPC signature and docstring.
# Kept deliberately small: enough for a client to call the tool correctly,
# without restating types the addon validates anyway.
_STRING = {"type": "string"}
_BOOL = {"type": "boolean"}


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


_TOOLS: list[dict] = [
    {
        "name": "ping",
        "description": "Check that the addon's MCP HTTP endpoint is alive.",
        "inputSchema": _schema({}, []),
    },
    {
        "name": "get_rpc_status",
        "description": "Report the addon's version and protocol number.",
        "inputSchema": _schema({}, []),
    },
    {
        "name": "list_documents",
        "description": "List every open document with its object count.",
        "inputSchema": _schema({}, []),
    },
    {
        "name": "get_objects",
        "description": "List the objects in a document.",
        "inputSchema": _schema({"doc_name": _STRING}, ["doc_name"]),
    },
    {
        "name": "get_object",
        "description": "Read one object's type and properties.",
        "inputSchema": _schema(
            {"doc_name": _STRING, "obj_name": _STRING}, ["doc_name", "obj_name"]
        ),
    },
    {
        "name": "create_document",
        "description": "Create a new document.",
        "inputSchema": _schema({"name": _STRING}, ["name"]),
    },
    {
        "name": "create_object",
        "description": "Add an object to a document from a type and properties.",
        "inputSchema": _schema(
            {"doc_name": _STRING, "obj_data": {"type": "object"}},
            ["doc_name", "obj_data"],
        ),
    },
    {
        "name": "edit_object",
        "description": "Change properties on an existing object.",
        "inputSchema": _schema(
            {
                "doc_name": _STRING,
                "obj_name": _STRING,
                "properties": {"type": "object"},
            },
            ["doc_name", "obj_name", "properties"],
        ),
    },
    {
        "name": "delete_object",
        "description": "Remove an object from a document.",
        "inputSchema": _schema(
            {"doc_name": _STRING, "obj_name": _STRING}, ["doc_name", "obj_name"]
        ),
    },
    {
        "name": "reload_document",
        "description": "Close and reopen a document, picking up external edits.",
        "inputSchema": _schema({"doc_name": _STRING}, ["doc_name"]),
    },
    {
        "name": "execute_code",
        "description": "Run Python on the FreeCAD GUI thread. This is the only "
        "thread that may touch App::Document, ViewObject or Coin3D.",
        "inputSchema": _schema({"code": _STRING, "timeout": {"type": "number"}}, ["code"]),
    },
    {
        "name": "execute_code_async",
        "description": "Run Python on a background thread for pure geometry "
        "work. Document writes must go through its commit() callback.",
        "inputSchema": _schema({"code": _STRING}, ["code"]),
    },
    {
        "name": "get_async_status",
        "description": "Report the state of background jobs.",
        "inputSchema": _schema({"job_id": _STRING}, []),
    },
    {
        "name": "get_active_screenshot",
        "description": "Capture the displayed 3D view as a PNG image.",
        "inputSchema": _schema(
            {
                "view_name": _STRING,
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "focus_object": _STRING,
            },
            [],
        ),
    },
    {
        "name": "insert_part_from_library",
        "description": "Insert a part from the FreeCAD parts library.",
        "inputSchema": _schema({"relative_path": _STRING}, ["relative_path"]),
    },
    {
        "name": "get_parts_list",
        "description": "List the parts available in the FreeCAD parts library.",
        "inputSchema": _schema({}, []),
    },
    {
        "name": "run_fem_analysis",
        "description": "Run CalculiX on an existing FEM analysis. Blocks the GUI "
        "thread for its whole duration.",
        "inputSchema": _schema(
            {"doc_name": _STRING, "analysis_name": _STRING, "timeout": {"type": "integer"}},
            ["doc_name", "analysis_name"],
        ),
    },
]

TOOLS_BY_NAME: dict[str, dict] = {tool["name"]: tool for tool in _TOOLS}


def list_tools() -> list[dict]:
    """Every tool this endpoint offers."""
    return [dict(tool) for tool in _TOOLS]


def _coerce_arguments(tool_name: str, arguments: dict) -> dict:
    """Validate against the schema, keeping only declared parameters.

    The addon methods have their own defaults; passing an unknown keyword would
    raise TypeError deep inside the GUI thread, so they are dropped here where
    the mistake can still be reported to the caller.
    """
    spec = TOOLS_BY_NAME.get(tool_name)
    if spec is None:
        return {}
    schema = spec["inputSchema"]
    properties = schema["properties"]

    missing = [
        name
        for name in schema["required"]
        if name not in arguments or arguments[name] in (None, "")
    ]
    if missing:
        raise ValueError(f"{tool_name}: missing required argument(s): {', '.join(missing)}")

    return {
        key: value
        for key, value in arguments.items()
        if key in properties and value is not None
    }


class ToolDispatcher:
    """Routes a tools/call to an injected callable and shapes the result.

    ``invoke`` is expected to run the call on whatever thread is correct for
    it -- in the addon that is ``dispatch_to_gui``. Keeping that behind one
    argument is what lets this class be tested without FreeCAD.
    """

    def __init__(self, invoke: Callable[[str, dict], Any]) -> None:
        self._invoke = invoke

    def list_tools(self) -> list[dict]:
        return list_tools()

    def __call__(self, payload: dict) -> dict:
        params = payload.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if name not in TOOLS_BY_NAME:
            return error_result(f"unknown tool: {name}")

        try:
            kwargs = _coerce_arguments(name, arguments)
        except ValueError as exc:
            return error_result(str(exc))

        try:
            value = self._invoke(name, kwargs)
        except Exception as exc:
            # The GUI thread reports failures as a value rather than raising,
            # so both shapes have to end up as a tool error, not a transport
            # error: the call itself succeeded, the operation did not.
            return error_result(f"{type(exc).__name__}: {exc}")

        return to_result(value)


def to_result(value: Any) -> dict:
    """Shape an arbitrary addon return value as an MCP tool result."""
    if isinstance(value, dict) and "content" in value:
        return value
    if isinstance(value, dict):
        if value.get("success") is False:
            return error_result(str(value.get("error", "operation failed")))
        return text_result(_stringify(value))
    if isinstance(value, list):
        return text_result(_stringify(value))
    if value is None:
        return text_result("")
    return text_result(str(value))


def text_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _stringify(value: Any) -> str:
    import json

    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)