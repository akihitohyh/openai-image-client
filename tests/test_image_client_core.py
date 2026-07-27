"""Tests for the logic shared by web_app.py and generate_image.py.

These helpers previously existed as byte-identical copies in both entry points, and
fixes kept landing in only one of them. ``test_shared_helpers_are_not_redefined``
is the guard that keeps them from drifting apart again.
"""

from __future__ import annotations

import ast
import base64
import pathlib

import pytest

from image_client_core import endpoints, models, responses

ROOT = pathlib.Path(__file__).resolve().parent.parent

SHARED_NAMES = {
    "candidate_endpoints",
    "closest_grok_ratio",
    "closest_grok_video_ratio",
    "decode_base64_limited",
    "exception_diagnostic",
    "grok_aspect_ratio_from_size",
    "grok_resolution_from_size",
    "grok_video_aspect_ratio_from_size",
    "grok_video_resolution_from_size",
    "is_gpt_image_2",
    "is_grok_image_model",
    "is_grok_video_model",
    "join_url",
    "json_error_type",
    "likely_gateway_or_upstream_error",
    "likely_non_retryable_client_error",
    "normalize_base_url",
    "parse_size",
    "preview_response_body",
    "read_limited_response",
    "response_diagnostic",
    "response_json_limited",
    "video_poll_endpoint_from_generation",
}


class FakeResponse:
    """Minimal stand-in for requests.Response as consumed by responses.py."""

    def __init__(self, body: bytes = b"", *, status: int = 200, content_type: str = "application/json", encoding=None):
        self._body = body
        self.status_code = status
        self.ok = status < 400
        self.headers = {"Content-Type": content_type}
        self.encoding = encoding
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index : index + chunk_size]

    def close(self):
        self.closed = True


@pytest.mark.parametrize("module_name", ["web_app.py", "generate_image.py"])
def test_shared_helpers_are_not_redefined(module_name: str) -> None:
    """Neither entry point may carry its own copy of a shared helper."""
    tree = ast.parse((ROOT / module_name).read_text(encoding="utf-8"))
    redefined = sorted(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in SHARED_NAMES
    )
    assert redefined == [], (
        f"{module_name} redefines shared helpers {redefined}; import them from "
        "image_client_core instead so fixes cannot land in only one entry point."
    )


def test_model_detection_ignores_case_and_padding() -> None:
    # generate_image.py compared verbatim, so "GPT-Image-2" took the wrong branch.
    for value in ("gpt-image-2", "GPT-Image-2", "  gpt-image-2  ", "gpt-image-2-mini"):
        assert models.is_gpt_image_2(value) is True
    for value in ("dall-e-3", "", None, "gpt-image-3"):
        assert models.is_gpt_image_2(value) is False

    assert models.is_grok_image_model("GROK-IMAGINE") is True
    assert models.is_grok_image_model("grok-imagine-image-pro") is True
    assert models.is_grok_video_model("grok-imagine-video-1.5") is True
    assert models.is_grok_video_model("grok-imagine-image") is False


@pytest.mark.parametrize(
    ("size", "ratio", "resolution", "video_ratio", "video_resolution"),
    [
        ("1024x1024", "1:1", "1k", "1:1", "720p"),
        ("1920x1072", "16:9", "2k", "16:9", "1080p"),
        ("720x1280", "9:16", "1k", "9:16", "720p"),
        ("garbage", "1:1", "1k", "16:9", "480p"),
        ("", "1:1", "1k", "16:9", "480p"),
    ],
)
def test_grok_derivation_from_size(size, ratio, resolution, video_ratio, video_resolution) -> None:
    assert models.grok_aspect_ratio_from_size(size) == ratio
    assert models.grok_resolution_from_size(size) == resolution
    assert models.grok_video_aspect_ratio_from_size(size) == video_ratio
    assert models.grok_video_resolution_from_size(size) == video_resolution


def test_derived_ratios_are_always_supported_values() -> None:
    for width in (1, 7, 100, 640, 1920, 3840, 9999):
        for height in (1, 9, 100, 480, 1080, 2160, 7777):
            assert models.closest_grok_ratio(width, height) in models.GROK_SUPPORTED_RATIOS
            assert models.closest_grok_video_ratio(width, height) in models.GROK_VIDEO_SUPPORTED_RATIOS


def test_candidate_endpoints_do_not_double_up_versions() -> None:
    assert endpoints.candidate_endpoints("https://api.x.com/v1", "images/generations") == [
        "https://api.x.com/v1/images/generations"
    ]
    assert endpoints.candidate_endpoints("https://api.x.com/v1/images/generations", "images/edits") == [
        "https://api.x.com/v1/images/edits"
    ]
    assert endpoints.candidate_endpoints("https://api.x.com", "images/generations") == [
        "https://api.x.com/v1/images/generations",
        "https://api.x.com/images/generations",
    ]
    assert endpoints.candidate_endpoints("", "images/generations") == []


def test_video_poll_endpoint_derivation() -> None:
    assert (
        endpoints.video_poll_endpoint_from_generation("https://a.b/v1/videos/generations", "rid")
        == "https://a.b/v1/videos/rid"
    )
    assert (
        endpoints.video_poll_endpoint_from_generation("https://a.b/v1/videos", "rid")
        == "https://a.b/v1/videos/videos/rid"
    )


def test_base64_decoder_rejects_non_base64_payloads() -> None:
    """An HTML error page must never decode into a file that looks like an image."""
    nginx_504 = (
        "<!DOCTYPE html><html><head><title>504 Gateway Time-out</title></head>"
        "<body><h1>504 Gateway Time-out</h1></body></html>"
    )
    # Without validate=True this silently decodes to 66 bytes of garbage.
    assert len(base64.b64decode(nginx_504)) > 0
    with pytest.raises(ValueError, match="合法"):
        responses.decode_base64_limited(nginx_504, 1_000_000, "媒体文件")

    assert responses.decode_base64_limited("YWJj", 3, "test") == b"abc"
    with pytest.raises(ValueError, match="超过"):
        responses.decode_base64_limited(base64.b64encode(b"abcd").decode(), 3, "test")


def test_base64_decoder_tolerates_mime_line_wrapping() -> None:
    payload = b"hello world" * 8
    encoded = base64.b64encode(payload).decode()
    wrapped = "\n".join(encoded[index : index + 16] for index in range(0, len(encoded), 16))
    assert responses.decode_base64_limited(wrapped, 1_000, "test") == payload


def test_error_body_preview_decodes_as_utf8() -> None:
    """response.text falls back to ISO-8859-1 for charset-less text/*, mangling Chinese."""
    message = "上游模型通道异常，请稍后重试"
    response = FakeResponse(message.encode("utf-8"), status=500, content_type="text/plain")
    assert responses.preview_response_body(response) == message


def test_retry_classification() -> None:
    # 5xx: try the next candidate endpoint.
    assert responses.likely_gateway_or_upstream_error(FakeResponse(b"{}", status=502)) is True
    assert responses.likely_gateway_or_upstream_error(FakeResponse(b"{}", status=200)) is False
    # 4xx: an explicit refusal; re-POSTing a 50MB multipart body would just repeat it.
    assert responses.likely_non_retryable_client_error(FakeResponse(b"{}", status=400)) is True
    assert responses.likely_non_retryable_client_error(FakeResponse(b"{}", status=422)) is True
    # 404/405 stay retryable because they are how route probing discovers the path.
    assert responses.likely_non_retryable_client_error(FakeResponse(b"{}", status=404)) is False
    assert responses.likely_non_retryable_client_error(FakeResponse(b"{}", status=405)) is False


def test_upstream_error_type_marks_gateway_failures() -> None:
    body = b'{"error": {"type": "upstream_error", "message": "boom"}}'
    assert responses.likely_gateway_or_upstream_error(FakeResponse(body, status=400)) is True


def test_response_reading_is_bounded_and_cached() -> None:
    response = FakeResponse(b"x" * 100)
    with pytest.raises(ValueError, match="超过"):
        responses.read_limited_response(response, 10)
    assert response.closed is True

    cached = FakeResponse(b'{"ok": true}')
    assert responses.response_json_limited(cached) == {"ok": True}
    # Second read must come from the cache rather than re-consuming the stream.
    assert responses.response_json_limited(cached) == {"ok": True}


def test_non_object_json_is_rejected() -> None:
    with pytest.raises(ValueError, match="顶层"):
        responses.response_json_limited(FakeResponse(b"[1, 2, 3]"))
    with pytest.raises(ValueError, match="不合法"):
        responses.response_json_limited(FakeResponse(b"<html>login</html>"))
