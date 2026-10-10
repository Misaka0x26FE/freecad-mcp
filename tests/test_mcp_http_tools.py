"""Tool schemas, argument validation and result shaping for the HTTP endpoint.

Runs without FreeCAD: the dispatcher takes its GUI-callable as an argument, so
routing and error shaping are exercised directly.
"""

from pathlib import Path
import sys

import pytest


ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_http import tools as tools_mod  # noqa: E402
from mcp_http.tools import (  # noqa: E402
    TOOLS_BY_NAME,
    ToolDispatcher,
    error_result,
    list_tools,
    text_result,
    to_result,
)


def call(name: str, arguments: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


def dispatcher_returning(value=None, error: Exception | None = None, seen: list | None = None):
    def invoke(name, kwargs):
        if seen is not None:
            seen.append((name, kwargs))
        if error is not None:
            raise error
        return value

    return ToolDispatcher(invoke)


# ------------------------------------------------------------------- schemas


def test_every_tool_has_a_name_description_and_object_schema():
    for tool in list_tools():
        assert tool["name"], "tool without a name"
        assert tool["description"], f"{tool['name']} without a description"
        assert tool["inputSchema"]["type"] == "object"
        assert isinstance(tool["inputSchema"]["properties"], dict)


def test_tool_names_are_unique():
    names = [tool["name"] for tool in list_tools()]
    assert len(names) == len(set(names))


def test_every_required_name_is_a_declared_property():
    for tool in list_tools():
        schema = tool["inputSchema"]
        for required in schema["required"]:
            assert required in schema["properties"], f"{tool['name']}.{required}"


def test_tools_cover_the_rpc_surface():
    """The endpoint must reach the same operations the XML-RPC one does."""
    for expected in ("ping", "execute_code", "get_objects", "create_document",
                     "run_fem_analysis", "get_active_screenshot"):
        assert expected in TOOLS_BY_NAME


def test_list_tools_returns_copies():
    first = list_tools()
    first[0]["name"] = "mutated"
    assert list_tools()[0]["name"] != "mutated"


# --------------------------------------------------------------- dispatching


def test_call_reaches_the_injected_callable_with_coerced_arguments():
    seen: list = []
    dispatch = dispatcher_returning(value={"success": True}, seen=seen)

    result = dispatch(call("get_objects", {"doc_name": "box"}))

    assert seen == [("get_objects", {"doc_name": "box"})]
    assert result["content"][0]["type"] == "text"


def test_unknown_arguments_are_dropped_rather_than_forwarded():
    """An unrecognised keyword would raise TypeError on the GUI thread, where
    the mistake is far harder to attribute."""
    seen: list = []
    dispatch = dispatcher_returning(value={"success": True}, seen=seen)

    dispatch(call("get_objects", {"doc_name": "box", "bogus": 1}))

    assert seen == [("get_objects", {"doc_name": "box"})]


def test_none_valued_optional_arguments_are_dropped():
    seen: list = []
    dispatch = dispatcher_returning(value={"success": True}, seen=seen)

    dispatch(call("get_active_screenshot", {"view_name": "Top", "width": None}))

    assert seen == [("get_active_screenshot", {"view_name": "Top"})]


def test_missing_required_argument_is_a_tool_error_not_an_exception():
    dispatch = dispatcher_returning(value={"success": True})

    result = dispatch(call("get_objects", {}))

    assert result["isError"] is True
    assert "doc_name" in result["content"][0]["text"]


def test_blank_required_argument_is_rejected():
    dispatch = dispatcher_returning(value={"success": True})

    result = dispatch(call("get_objects", {"doc_name": ""}))

    assert result["isError"] is True


def test_unknown_tool_is_a_tool_error():
    dispatch = dispatcher_returning(value={"success": True})

    result = dispatch(call("no_such_tool", {}))

    assert result["isError"] is True
    assert "no_such_tool" in result["content"][0]["text"]


def test_gui_thread_exception_becomes_a_tool_error():
    """The HTTP call succeeded; the operation did not. That distinction has to
    survive into the response or a client cannot tell them apart."""
    dispatch = dispatcher_returning(error=RuntimeError("GUI thread is stuck"))

    result = dispatch(call("execute_code", {"code": "1"}))

    assert result["isError"] is True
    assert "GUI thread is stuck" in result["content"][0]["text"]
    assert "RuntimeError" in result["content"][0]["text"]


def test_dispatch_timeout_value_is_reported_as_an_error():
    """dispatch_to_gui signals a timeout by returning a failure dict."""
    dispatch = dispatcher_returning(
        value={"success": False, "error": "timed out after 60 s"}
    )

    result = dispatch(call("execute_code", {"code": "1"}))

    assert result["isError"] is True
    assert "timed out" in result["content"][0]["text"]


def test_dispatcher_exposes_list_tools_for_the_transport():
    assert [t["name"] for t in dispatcher_returning().list_tools()] == [
        t["name"] for t in list_tools()
    ]


# ------------------------------------------------------------ result shaping


def test_dict_payload_is_rendered_as_json():
    result = to_result({"doc": "box", "objects": 3})

    assert result["content"][0]["type"] == "text"
    assert '"doc": "box"' in result["content"][0]["text"]


def test_list_payload_is_rendered_as_json():
    result = to_result([1, 2, 3])
    assert "[1, 2, 3]" in result["content"][0]["text"] or "1," in result["content"][0]["text"]


def test_failure_dict_is_an_error_result():
    result = to_result({"success": False, "error": "document not found"})
    assert result["isError"] is True


def test_prebuilt_content_result_passes_through_unchanged():
    """A handler that already shaped its own result, e.g. one returning image
    content, must not be re-encoded."""
    prebuilt = {"content": [{"type": "image", "data": "AAA", "mimeType": "image/png"}]}
    assert to_result(prebuilt) is prebuilt


def test_none_result_is_empty_text_not_a_crash():
    result = to_result(None)
    assert result["content"] == [{"type": "text", "text": ""}]
    assert "isError" not in result


def test_non_serialisable_value_falls_back_to_str():
    class Opaque:
        def __str__(self):
            return "opaque"

    assert "opaque" in to_result(Opaque())["content"][0]["text"]


def test_text_and_error_helpers():
    assert text_result("hi") == {"content": [{"type": "text", "text": "hi"}]}
    assert error_result("bad")["isError"] is True