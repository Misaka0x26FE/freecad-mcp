"""Text and image content for HTTP tool results.

The three modes are a per-call choice rather than a server setting, because the
cost differs by two orders of magnitude: a tool result is a few kilobytes of
text, and each PNG is tens to hundreds of kilobytes once base64-encoded.

    text        the result alone
    image       the result plus one isometric view
    multimodal  the result plus three views

Degradation is always explicit. Silently dropping an image would leave the
client believing the model looks like the text describes it, which is worse than
saying the capture failed.
"""

import base64
from typing import Any, Callable

TEXT = "text"
IMAGE = "image"
MULTIMODAL = "multimodal"
CONTENT_MODES = (TEXT, IMAGE, MULTIMODAL)
DEFAULT_CONTENT_MODE = TEXT

# Views captured for multimodal. Kept to three: enough to read a shape's form,
# small enough that the payload stays inside a normal model context.
MULTIMODAL_VIEWS = ("Isometric", "Front", "Top")

# Ceiling on the decoded PNG. base64 adds a third on top, so this bounds an
# image content block at roughly 2.7 MB.
MAX_IMAGE_BYTES = 2 * 1024 * 1024

IMAGE_MIME = "image/png"

# Returned instead of an image when a capture cannot be produced. The client
# gets a text block either way, so the shape of the response does not change.
UNAVAILABLE_NOTE = "screenshot unavailable: {reason}"


def normalise_mode(value: Any) -> str:
    """Coerce a requested mode to a supported one, defaulting to text.

    An unrecognised value falls back rather than failing: a client sending a
    mode from a newer protocol revision should still get its answer.
    """
    if isinstance(value, str) and value.lower() in CONTENT_MODES:
        return value.lower()
    return DEFAULT_CONTENT_MODE


def decode_image(data: str) -> bytes | None:
    """Decode a base64 PNG, or None if it is not usable."""
    if not isinstance(data, str) or not data:
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        return None
    return raw or None


def image_block(data: str, note: str | None = None) -> dict | None:
    """An MCP image content block, or None when it must be degraded away.

    A note is returned instead of the image when the payload is over budget, so
    the caller can say why rather than just omitting it.
    """
    raw = decode_image(data)
    if raw is None:
        return None
    if len(raw) > MAX_IMAGE_BYTES:
        return None
    block = {"type": "image", "data": data, "mimeType": IMAGE_MIME}
    if note:
        block["_note"] = note
    return block


def is_png(data: Any) -> bool:
    """Whether ``data`` is a base64 string carrying a real PNG.

    Used to keep the screenshot tool returning an image rather than a wall of
    base64 the caller would have to decode itself.
    """
    raw = decode_image(data) if isinstance(data, str) else None
    return raw is not None and raw[:8] == b"\x89PNG\r\n\x1a\n"


def image_result(data: Any) -> dict | None:
    """Shape a screenshot tool's return value as an MCP result.

    Returns None when the value is not a usable PNG, so the caller can fall
    back to rendering it as text rather than losing it.
    """
    if not is_png(data):
        return None
    block = image_block(data)
    if block is None:
        return {
            "content": [{
                "type": "text",
                "text": UNAVAILABLE_NOTE.format(reason="image exceeded the size budget"),
            }],
            "contentDegraded": True,
        }
    return {"content": [block]}


def attach_images(
    result: dict,
    mode: str,
    capture: Callable[[str], Any],
    *,
    gui_available: bool = True,
) -> dict:
    """Add image content to a text result according to ``mode``.

    ``capture`` receives a view name and returns the addon's base64 PNG, or
    None when that view cannot be captured. The original result is never
    mutated.
    """
    mode = normalise_mode(mode)
    if mode == TEXT:
        return result

    if not gui_available:
        return _degrade(
            result,
            mode,
            "FreeCAD is running without a GUI, so there is no viewport to capture",
        )

    views = MULTIMODAL_VIEWS if mode == MULTIMODAL else (MULTIMODAL_VIEWS[0],)
    blocks: list[dict] = []
    notes: list[str] = []

    for view in views:
        try:
            data = capture(view)
        except Exception as exc:
            notes.append(f"{view}: {type(exc).__name__}: {exc}")
            continue

        block = image_block(data)
        if block is None:
            reason = (
                "capture returned nothing" if not data
                else "image exceeded the size budget"
            )
            notes.append(f"{view}: {reason}")
            continue
        blocks.append(block)

    if not blocks:
        detail = "; ".join(notes) if notes else "no view could be captured"
        return _degrade(result, mode, detail)

    merged = dict(result)
    merged["content"] = list(result.get("content", [])) + blocks
    if notes:
        merged["content"].append(
            {"type": "text", "text": UNAVAILABLE_NOTE.format(reason="; ".join(notes))}
        )
    return merged


def _degrade(result: dict, mode: str, reason: str) -> dict:
    """Keep the text and say the image was not produced."""
    merged = dict(result)
    merged["content"] = list(result.get("content", [])) + [
        {"type": "text", "text": UNAVAILABLE_NOTE.format(reason=reason)}
    ]
    merged["contentMode"] = mode
    merged["contentDegraded"] = True
    return merged