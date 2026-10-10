"""Content modes: text, image and multimodal, and every way they degrade.

Runs without FreeCAD: capture is a plain callable returning a base64 string.
"""

import base64
from pathlib import Path
import sys

import pytest


ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_http.content import (  # noqa: E402
    CONTENT_MODES,
    DEFAULT_CONTENT_MODE,
    MAX_IMAGE_BYTES,
    MULTIMODAL_VIEWS,
    TEXT,
    attach_images,
    decode_image,
    image_block,
    normalise_mode,
)
from mcp_http.tools import ToolDispatcher, list_tools  # noqa: E402


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 512
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


def call(name="get_objects", arguments=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


def blocks(result):
    return result.get("content", [])


def types_of(result):
    return [b.get("type") for b in blocks(result)]


def dispatch(capture=None, invoke_value=None, **kwargs):
    def default_invoke(name, args):
        return invoke_value if invoke_value is not None else {"success": True, "docs": []}

    return ToolDispatcher(default_invoke, capture=capture, **kwargs)


# ------------------------------------------------------------------- mode set


def test_the_three_modes_are_offered():
    assert set(CONTENT_MODES) == {"text", "image", "multimodal"}
    assert DEFAULT_CONTENT_MODE == TEXT


def test_every_tool_schema_offers_content_mode():
    for tool in list_tools():
        prop = tool["inputSchema"]["properties"].get("content_mode")
        assert prop is not None, f"{tool['name']} lacks content_mode"
        assert prop["enum"] == list(CONTENT_MODES)
        # Offered, not required: an omitted mode must stay valid.
        assert "content_mode" not in tool["inputSchema"]["required"]


def test_normalise_mode_falls_back_instead_of_failing():
    assert normalise_mode("image") == "image"
    assert normalise_mode("MULTIMODAL") == "multimodal"
    assert normalise_mode(None) == TEXT
    assert normalise_mode("nonsense") == TEXT
    assert normalise_mode(7) == TEXT


# ----------------------------------------------------------------- text mode


def test_text_mode_returns_no_image():
    result = dispatch(capture=lambda v: PNG_B64)(call(arguments={"content_mode": "text"}))

    assert types_of(result) == ["text"]
    assert "contentDegraded" not in result


def test_omitting_the_mode_defaults_to_text():
    result = dispatch(capture=lambda v: PNG_B64)(call())

    assert types_of(result) == ["text"]


def test_unknown_mode_still_returns_the_answer():
    result = dispatch(capture=lambda v: PNG_B64)(call(arguments={"content_mode": "hologram"}))

    assert types_of(result) == ["text"]


# ----------------------------------------------------------------- image mode


def test_image_mode_adds_exactly_one_image():
    result = dispatch(capture=lambda v: PNG_B64)(
        call("list_documents", {"content_mode": "image"})
    )

    assert types_of(result) == ["text", "image"]
    image = blocks(result)[1]
    assert image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == PNG_BYTES


def test_image_mode_captures_the_isometric_view():
    seen = []

    def capture(view):
        seen.append(view)
        return PNG_B64

    # list_documents has no required arguments, so the call reaches capture.
    dispatch(capture=capture)(call("list_documents", {"content_mode": "image"}))

    assert seen == ["Isometric"]


def test_multimodal_adds_three_views():
    seen = []

    def capture(view):
        seen.append(view)
        return PNG_B64

    result = dispatch(capture=capture)(call("list_documents", {"content_mode": "multimodal"}))

    assert seen == list(MULTIMODAL_VIEWS)
    assert types_of(result) == ["text", "image", "image", "image"]


def test_screenshot_request_is_never_answered_with_a_screenshot():
    """Attaching a capture to a capture request would spend a second
    viewport render to illustrate the first."""
    seen = []

    result = dispatch(capture=lambda v: seen.append(v) or PNG_B64)(
        call("get_active_screenshot", {"content_mode": "multimodal"})
    )

    assert seen == []
    assert types_of(result) == ["text"]


# --------------------------------------------------------------- degradation


def test_no_gui_degrades_with_an_explanation():
    result = attach_images(
        {"content": [{"type": "text", "text": "ok"}]},
        "image",
        lambda v: PNG_B64,
        gui_available=False,
    )

    assert result["contentDegraded"] is True
    assert result["contentMode"] == "image"
    assert types_of(result) == ["text", "text"]
    assert "without a GUI" in blocks(result)[1]["text"]


def test_capture_returning_nothing_degrades():
    result = attach_images(
        {"content": [{"type": "text", "text": "ok"}]}, "image", lambda v: None
    )

    assert result["contentDegraded"] is True
    assert "capture returned nothing" in blocks(result)[1]["text"]


def test_oversized_capture_is_refused_and_explained():
    oversized = base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode()

    result = attach_images(
        {"content": [{"type": "text", "text": "ok"}]}, "image", lambda v: oversized
    )

    assert result["contentDegraded"] is True
    assert "size budget" in blocks(result)[1]["text"]


def test_capture_raising_is_reported_per_view():
    def capture(view):
        if view == "Front":
            raise RuntimeError("no active view")
        return PNG_B64

    result = attach_images(
        {"content": [{"type": "text", "text": "ok"}]}, "multimodal", capture
    )

    # The other two views still come back, and the failure is stated.
    assert types_of(result) == ["text", "image", "image", "text"]
    assert "no active view" in blocks(result)[-1]["text"]


def test_all_views_failing_degrades_rather_than_returning_nothing():
    def capture(view):
        raise RuntimeError("boom")

    result = attach_images(
        {"content": [{"type": "text", "text": "ok"}]}, "multimodal", capture
    )

    assert types_of(result) == ["text", "text"]
    assert result["contentDegraded"] is True


def test_missing_capture_callable_degrades_instead_of_failing():
    result = dispatch(capture=None)(call("list_documents", {"content_mode": "image"}))

    assert types_of(result) == ["text", "text"]
    assert result["contentDegraded"] is True


def test_error_results_keep_their_is_error_flag_when_images_are_requested():
    def failing(name, args):
        return {"success": False, "error": "nope"}

    result = ToolDispatcher(failing, capture=lambda v: PNG_B64)(
        call("ping", {"content_mode": "image"})
    )

    assert result["isError"] is True
    assert types_of(result) == ["text", "image"]


# ------------------------------------------------------------------- helpers


def test_decode_image_rejects_junk():
    assert decode_image(PNG_B64) == PNG_BYTES
    assert decode_image("") is None
    assert decode_image(None) is None
    assert decode_image("not base64!!") is None


def test_image_block_is_none_for_unusable_data():
    assert image_block(PNG_B64)["type"] == "image"
    assert image_block("") is None
    assert image_block("not base64!!") is None


def test_attach_does_not_mutate_the_original_result():
    original = {"content": [{"type": "text", "text": "ok"}]}
    attach_images(original, "image", lambda v: PNG_B64)

    assert types_of(original) == ["text"]


def test_content_mode_is_not_forwarded_to_the_rpc_method():
    """It is this layer's parameter; FreeCADRPC has no such keyword."""
    seen = []

    def invoke(name, kwargs):
        seen.append((name, kwargs))
        return {"success": True}

    ToolDispatcher(invoke, capture=lambda v: PNG_B64)(
        call("get_objects", {"doc_name": "box", "content_mode": "image"})
    )

    assert seen == [("get_objects", {"doc_name": "box"})]

# ------------------------------------------------------- screenshot tool shape


def test_screenshot_tool_returns_an_image_not_base64_text():
    """Its entire result is a PNG; rendering it as text would hand the caller
    a wall of base64 to decode itself."""
    result = dispatch(capture=lambda v: PNG_B64, invoke_value=PNG_B64)(
        call("get_active_screenshot", {"view_name": "Isometric"})
    )

    assert types_of(result) == ["image"]
    assert blocks(result)[0]["mimeType"] == "image/png"


def test_screenshot_tool_without_a_mode_still_returns_an_image():
    result = dispatch(capture=lambda v: PNG_B64, invoke_value=PNG_B64)(
        call("get_active_screenshot")
    )

    assert types_of(result) == ["image"]


def test_screenshot_tool_failure_stays_text():
    """A view that cannot be captured yields an error message, not an image."""
    result = dispatch(capture=lambda v: PNG_B64, invoke_value={"success": False, "error": "no view"})(
        call("get_active_screenshot")
    )

    assert types_of(result) == ["text"]
    assert result["isError"] is True


def test_screenshot_tool_none_result_stays_text():
    result = dispatch(capture=lambda v: PNG_B64, invoke_value=None)(
        call("get_active_screenshot")
    )

    assert types_of(result) == ["text"]


def test_is_png_rejects_non_images():
    from mcp_http.content import is_png, image_result

    # The shared fixture carries the PNG magic prefix, so it is a PNG.
    assert is_png(PNG_B64) is True

    not_png = base64.b64encode(b"JUST-TEXT-NOT-A-PNG" * 8).decode()
    assert is_png(not_png) is False
    assert is_png("not base64") is False
    assert is_png(None) is False
    assert image_result("nope") is None


def test_oversized_screenshot_reports_rather_than_silently_dropping():
    from mcp_http.content import image_result

    oversized = base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES
    ).decode()

    result = image_result(oversized)

    assert result["contentDegraded"] is True
    assert types_of(result) == ["text"]
