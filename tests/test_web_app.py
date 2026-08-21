from __future__ import annotations

import base64
from io import BytesIO
import json
import time
from types import MethodType, SimpleNamespace

import pytest
from requests import HTTPError, RequestException

import web_app
from image_client_core import responses


class FakeResponse:
    def __init__(self, data=None, *, status=200, headers=None, body=None, lines=None):
        if body is None:
            body = json.dumps(data if data is not None else {}).encode("utf-8")
        self._body = body
        self._lines = lines
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.encoding = "utf-8"
        self.closed = False

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset : offset + chunk_size]

    def iter_lines(self, decode_unicode=False):
        if isinstance(self._lines, BaseException):
            raise self._lines
        for line in self._lines or []:
            yield line

    def raise_for_status(self):
        if not self.ok:
            raise HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_IMAGE_CLIENT_ALLOW_PRIVATE_UPSTREAMS", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_CLIENT_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(web_app, "ASSET_ROOT", tmp_path / "assets")
    with web_app.JOBS_LOCK:
        web_app.JOBS.clear()
        web_app.JOB_FUTURES.clear()
    yield
    with web_app.JOBS_LOCK:
        futures = list(web_app.JOB_FUTURES.values())
        web_app.JOBS.clear()
        web_app.JOB_FUTURES.clear()
    for future in futures:
        future.cancel()


def valid_payload(**overrides):
    payload = {
        "base_url": "https://8.8.8.8/v1",
        "api_key": "test-key",
        "prompt": "draw a lighthouse",
        "model": "gpt-image-2",
        "n": 1,
        "timeout": 30,
        "mode": "generate",
    }
    payload.update(overrides)
    return payload


def test_private_upstream_requires_explicit_trust(monkeypatch):
    with pytest.raises(ValueError, match="非公网地址"):
        web_app.validate_outbound_url("http://127.0.0.1:8080/v1")

    monkeypatch.setenv("OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS", "127.0.0.1")
    assert web_app.validate_outbound_url("http://127.0.0.1:8080/v1").endswith("/v1")


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "https://user:secret@8.8.8.8/v1", "ftp://8.8.8.8/file"],
)
def test_outbound_url_rejects_unsafe_forms(url):
    with pytest.raises(ValueError):
        web_app.validate_outbound_url(url)


def test_iframe_embedding_is_enabled_by_default():
    assert web_app.resolve_embed_policy(None) == ("*", None)
    csp = web_app.build_content_security_policy("*")
    assert csp.count("frame-ancestors") == 1
    assert csp.endswith("frame-ancestors *")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ("'none'", "DENY")),
        ("none", ("'none'", "DENY")),
        ("self", ("'self'", "SAMEORIGIN")),
        ("*", ("*", None)),
        (
            "self, HTTPS://Example.COM.:443/,https://example.com",
            ("'self' https://example.com", None),
        ),
        ("http://127.0.0.1:3000,https://[::1]:8443", ("http://127.0.0.1:3000 https://[::1]:8443", None)),
    ],
)
def test_iframe_embed_policy_is_canonical_and_consistent(raw, expected):
    assert web_app.resolve_embed_policy(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "*,self",
        "none,self",
        "file:///tmp/embed.html",
        "null",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com?",
        "https://example.com#fragment",
        "https://*.example.com",
        "https://example.com:invalid",
        "https://example.com;script-src *",
        "https://example.com\nhttps://evil.example",
    ],
)
def test_iframe_embed_policy_rejects_unsafe_sources(raw):
    with pytest.raises(ValueError):
        web_app.resolve_embed_policy(raw)


def test_iframe_embed_policy_limits_source_count():
    raw = ",".join(f"https://embed-{index}.example" for index in range(web_app.MAX_EMBED_ORIGINS + 1))
    with pytest.raises(ValueError, match="最多允许"):
        web_app.resolve_embed_policy(raw)


def _render_security_headers(frame_ancestors: str, x_frame_options: str | None) -> str:
    handler = object.__new__(web_app.AppHandler)
    handler.path = "/index.html"
    handler.request_version = "HTTP/1.1"
    handler.server = SimpleNamespace(
        frame_ancestors=frame_ancestors,
        x_frame_options=x_frame_options,
    )
    handler._headers_buffer = []
    handler.wfile = BytesIO()
    handler.end_headers()
    return handler.wfile.getvalue().decode("latin-1")


def test_iframe_response_headers_allow_default_embedding_and_support_lockdown():
    default_headers = _render_security_headers("*", None)
    assert "Content-Security-Policy:" in default_headers
    assert "frame-ancestors *" in default_headers
    assert "X-Frame-Options:" not in default_headers

    locked_headers = _render_security_headers("'none'", "DENY")
    assert "frame-ancestors 'none'" in locked_headers
    assert "X-Frame-Options: DENY" in locked_headers


def _stub_outbound_session(monkeypatch, handler):
    """拦截 safe_http_request 的出网调用，确保测试不会真的发起网络请求。"""
    monkeypatch.setattr(
        web_app, "outbound_session", lambda: SimpleNamespace(request=handler)
    )


def test_redirect_is_revalidated_before_following(monkeypatch):
    redirect = FakeResponse(status=302, headers={"Location": "http://127.0.0.1/admin"})
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return redirect

    _stub_outbound_session(monkeypatch, fake_request)
    with pytest.raises(RequestException, match="非公网地址"):
        web_app.safe_http_request("GET", "https://8.8.8.8/start")
    assert len(calls) == 1
    assert redirect.closed


def test_cross_origin_redirect_does_not_receive_api_credentials(monkeypatch):
    redirect = FakeResponse(status=307, headers={"Location": "https://1.1.1.1/next"})
    _stub_outbound_session(monkeypatch, lambda *args, **kwargs: redirect)

    with pytest.raises(RequestException, match="跨源"):
        web_app.safe_http_request(
            "POST",
            "https://8.8.8.8/v1/images/generations",
            headers={"Authorization": "Bearer secret"},
        )
    assert redirect.closed


def test_limited_response_rejects_declared_and_streamed_oversize():
    declared = FakeResponse(body=b"x", headers={"Content-Length": "11"})
    with pytest.raises(ValueError, match="超过"):
        web_app.read_limited_response(declared, 10)
    assert declared.closed

    streamed = FakeResponse(body=b"12345678901")
    with pytest.raises(ValueError, match="超过"):
        web_app.read_limited_response(streamed, 10)
    assert streamed.closed


def test_base64_decoder_is_strict_and_bounded():
    assert web_app.decode_base64_limited("YWJj", 3, "test") == b"abc"
    with pytest.raises(ValueError, match="合法"):
        web_app.decode_base64_limited("@@@", 10, "test")
    with pytest.raises(ValueError, match="超过"):
        web_app.decode_base64_limited(base64.b64encode(b"abcd").decode(), 3, "test")


def test_generated_images_have_a_total_memory_limit(monkeypatch):
    monkeypatch.setattr(web_app, "MAX_GENERATED_MEDIA_BYTES", 3)
    encoded = base64.b64encode(b"ab").decode()
    with pytest.raises(ValueError, match="总大小"):
        web_app.parse_image_api_response(
            {"data": [{"b64_json": encoded}, {"b64_json": encoded}]},
            timeout=5,
            client_request_id="request-id",
            last_endpoint="https://8.8.8.8/v1/images/generations",
        )


@pytest.mark.parametrize(
    ("base_url", "model", "expected"),
    [
        (
            "https://generativelanguage.googleapis.com",
            "gemini-2.5-flash-image",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent",
        ),
        (
            "https://gemini.example/v1beta/",
            "models/custom image",
            "https://gemini.example/v1beta/models/custom%20image:generateContent",
        ),
        (
            "https://gemini.example/v1/models/already-set:generateContent",
            "ignored-model",
            "https://gemini.example/v1/models/already-set:generateContent",
        ),
    ],
)
def test_gemini_generate_endpoint(base_url, model, expected):
    assert web_app.gemini_generate_endpoint(base_url, model) == expected


def test_gemini_request_body_supports_native_image_config_and_references():
    reference = base64.b64encode(b"reference-image").decode()
    payload = valid_payload(
        protocol="gemini",
        model="gemini-image-model",
        mode="edit",
        aspect_ratio="16:9",
        resolution="4K",
        edit_images=[{"data_url": f"data:image/png;base64,{reference}"}],
    )

    body = web_app.build_gemini_request_body(payload, payload["prompt"])
    assert body["generationConfig"] == {
        "responseModalities": ["TEXT", "IMAGE"],
        "imageConfig": {"aspectRatio": "16:9", "imageSize": "4K"},
    }
    assert body["contents"][0]["parts"] == [
        {"text": "draw a lighthouse"},
        {"inlineData": {"mimeType": "image/png", "data": reference}},
    ]


def test_gemini_response_parser_accepts_camel_and_snake_case_and_redacts_raw():
    first = base64.b64encode(b"first-image").decode()
    second = base64.b64encode(b"second-image").decode()
    data = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "generated"},
                        {"inlineData": {"mimeType": "image/png", "data": first}},
                        {"inline_data": {"mime_type": "image/webp", "data": second}},
                    ]
                }
            }
        ]
    }
    images = web_app.parse_gemini_image_response(data, "request-id", "https://gemini.example")
    assert images == [
        f"data:image/png;base64,{first}",
        f"data:image/webp;base64,{second}",
    ]
    compact = web_app.compact_gemini_raw_response(data)
    serialized = json.dumps(compact)
    assert first not in serialized
    assert second not in serialized
    assert serialized.count("<omitted>") == 2


def test_uhd_inline_image_limits_cover_large_gemini_responses():
    assert web_app.MAX_REMOTE_MEDIA_BYTES >= 64 * 1024 * 1024
    assert web_app.MAX_GENERATED_MEDIA_BYTES >= 96 * 1024 * 1024
    assert responses.MAX_UPSTREAM_JSON_BYTES >= 128 * 1024 * 1024


def test_gemini_response_parser_rejects_missing_invalid_and_oversized_images(monkeypatch):
    with pytest.raises(RuntimeError, match="没有可用的图片"):
        web_app.parse_gemini_image_response(
            {"candidates": [{"finishReason": "SAFETY"}]},
            "request-id",
            "https://gemini.example",
        )

    invalid = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": "@@@"}}]}}]}
    with pytest.raises(ValueError, match="Base64"):
        web_app.parse_gemini_image_response(invalid, "request-id", "https://gemini.example")

    monkeypatch.setattr(web_app, "MAX_REMOTE_MEDIA_BYTES", 2)
    oversized = base64.b64encode(b"abc").decode()
    data = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": oversized}}]}}]}
    with pytest.raises(ValueError, match="超过"):
        web_app.parse_gemini_image_response(data, "request-id", "https://gemini.example")


def test_gemini_multiple_outputs_use_sequential_native_requests(monkeypatch):
    encoded = base64.b64encode(b"generated-image").decode()
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(
            {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": encoded}}]}}]}
        )

    monkeypatch.setattr(web_app, "safe_http_request", fake_request)
    result = web_app.request_gemini_image_api(
        "https://gemini.example/v1beta",
        "secret-key",
        30,
        "gemini-image-model",
        json_body={"contents": []},
        count=2,
    )

    assert len(calls) == 2
    assert len(result["images"]) == 2
    for method, endpoint, kwargs in calls:
        assert method == "POST"
        assert endpoint.endswith("/models/gemini-image-model:generateContent")
        assert kwargs["headers"]["x-goog-api-key"] == "secret-key"
        assert kwargs["headers"]["Authorization"] == "Bearer secret-key"
        assert kwargs["json"] == {"contents": []}


def test_gemini_multiple_outputs_honor_cancellation_between_requests(monkeypatch):
    encoded = base64.b64encode(b"generated-image").decode()
    monkeypatch.setattr(
        web_app,
        "safe_http_request",
        lambda *args, **kwargs: FakeResponse(
            {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": encoded}}]}}]}
        ),
    )
    checks = iter([False, True])
    with pytest.raises(web_app.JobCancelledError):
        web_app.request_gemini_image_api(
            "https://gemini.example/v1beta",
            "secret-key",
            30,
            "gemini-image-model",
            json_body={"contents": []},
            count=2,
            should_cancel=lambda: next(checks),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"n": 9}, "n 必须在"),
        ({"timeout": 601}, "timeout 必须在"),
        ({"timeout": 1}, "timeout 必须在"),
        ({"partial_images": 4}, "partial_images 必须在"),
        ({"prompt": "x" * (web_app.MAX_PROMPT_CHARS + 1)}, "提示词不能超过"),
        ({"model": "m" * (web_app.MAX_MODEL_CHARS + 1)}, "模型名不能超过"),
        ({"protocol": "gemini", "model": ""}, "Gemini 模型名称不能为空"),
        ({"protocol": "gemini", "model": "gemini-image", "aspect_ratio": "4:5"}, "aspect_ratio"),
        ({"protocol": "gemini", "model": "gemini-image", "resolution": "8K"}, "resolution"),
    ],
)
def test_request_validation_enforces_bounds(overrides, message):
    with pytest.raises(ValueError, match=message):
        web_app.validate_request_payload(valid_payload(**overrides))


def test_reference_image_total_is_bounded(monkeypatch):
    monkeypatch.setattr(web_app, "MAX_REFERENCE_IMAGES_BYTES", 3)
    data_url = "data:image/png;base64," + base64.b64encode(b"ab").decode()
    payload = valid_payload(
        mode="edit",
        edit_images=[{"data_url": data_url}, {"data_url": data_url}],
    )
    with pytest.raises(ValueError, match="总大小"):
        web_app.validate_request_payload(payload)


def test_video_done_without_url_fails_even_after_another_video_succeeded(monkeypatch):
    responses = iter(
        [
            FakeResponse({"url": "https://8.8.8.8/video-1.mp4"}),
            FakeResponse({"request_id": "task-2"}),
            FakeResponse({"status": "done"}),
        ]
    )
    monkeypatch.setattr(web_app, "safe_http_request", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(web_app.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="完成但没有返回"):
        web_app.request_grok_video_api(
            "https://8.8.8.8/v1",
            "key",
            30,
            json_body={"model": "grok-imagine-video", "prompt": "test"},
            count=2,
            poll_interval=1,
        )


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/private.mp4"])
def test_video_result_url_cannot_target_local_resources(monkeypatch, url):
    monkeypatch.setattr(web_app, "safe_http_request", lambda *args, **kwargs: FakeResponse({"url": url}))
    # 拒绝行为不变；异常类型从裸 ValueError 改为带诊断的 RuntimeError，
    # 这样报错里会带上上游原始响应，而不是只剩一句 scheme 不合法。
    with pytest.raises(RuntimeError) as excinfo:
        web_app.request_grok_video_api(
            "https://8.8.8.8/v1",
            "key",
            30,
            json_body={"model": "grok-imagine-video", "prompt": "test"},
        )
    payload = json.loads(str(excinfo.value))
    assert "视频结果 URL" in payload["message"]
    assert payload["raw"] == {"url": url}


def test_image_remote_fallback_rejects_non_http_url():
    with pytest.raises(RuntimeError, match="没有可保存"):
        web_app.parse_image_api_response(
            {"data": [{"url": "file:///etc/passwd"}]},
            timeout=5,
            client_request_id="request-id",
            last_endpoint="https://8.8.8.8/v1/images/generations",
        )


def test_cleanup_only_expires_terminal_jobs_and_deadlines_running_job(monkeypatch):
    class OccupiedFuture:
        def done(self):
            return False

        def cancel(self):
            return False

    removed = []
    monkeypatch.setattr(web_app, "_remove_job_assets", removed.append)
    with web_app.JOBS_LOCK:
        web_app.JOBS.update(
            {
                "old-success": {
                    "id": "old-success",
                    "status": "success",
                    "created_at": 1,
                    "updated_at": 1,
                    "deadline_at": 2,
                },
                "still-queued": {
                    "id": "still-queued",
                    "status": "queued",
                    "created_at": 1,
                    "updated_at": 1,
                    "deadline_at": 20_000,
                },
                "overdue-running": {
                    "id": "overdue-running",
                    "status": "running",
                    "created_at": 1,
                    "updated_at": 1,
                    "deadline_at": 9_000,
                },
            }
        )
        web_app.JOB_FUTURES["old-success"] = OccupiedFuture()

    web_app.cleanup_jobs(now=10_000)
    assert "old-success" not in web_app.JOBS
    assert web_app.JOBS["still-queued"]["status"] == "queued"
    assert web_app.JOBS["overdue-running"]["status"] == "error"
    assert web_app.JOBS["overdue-running"]["error"]["code"] == "job_deadline_exceeded"
    assert "old-success" in web_app.JOB_FUTURES
    assert removed == ["old-success"]


def test_job_queue_has_a_hard_limit():
    with web_app.JOBS_LOCK:
        for index in range(web_app.MAX_CONCURRENT_JOBS + web_app.MAX_PENDING_JOBS):
            web_app.JOBS[str(index)] = {
                "id": str(index),
                "status": "queued",
                "created_at": time.time(),
                "updated_at": time.time(),
                "deadline_at": time.time() + 60,
            }
    with pytest.raises(web_app.JobQueueFullError, match="队列已满"):
        web_app.create_async_job(valid_payload())


def test_overdue_job_status_cannot_hide_unfinished_executor_work():
    class OccupiedFuture:
        def done(self):
            return False

        def cancel(self):
            return False

    with web_app.JOBS_LOCK:
        for index in range(web_app.MAX_CONCURRENT_JOBS + web_app.MAX_PENDING_JOBS):
            job_id = f"expired-{index}"
            web_app.JOBS[job_id] = {
                "id": job_id,
                "status": "error",
                "created_at": time.time(),
                "updated_at": time.time(),
                "deadline_at": time.time() - 1,
            }
            web_app.JOB_FUTURES[job_id] = OccupiedFuture()

    with pytest.raises(web_app.JobQueueFullError, match="队列已满"):
        web_app.create_async_job(valid_payload())


def test_cancel_marks_running_job_and_prevents_result_overwrite():
    class FakeFuture:
        cancelled = False

        def cancel(self):
            self.cancelled = True
            return False

    future = FakeFuture()
    with web_app.JOBS_LOCK:
        web_app.JOBS["job-1"] = {
            "id": "job-1",
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
            "deadline_at": time.time() + 60,
        }
        web_app.JOB_FUTURES["job-1"] = future

    job = web_app.cancel_async_job("job-1")
    assert job["status"] == "cancelled"
    assert web_app.JOBS["job-1"]["cancel_requested"] is True
    assert future.cancelled


def test_async_result_materializes_base64_once(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "ASSET_ROOT", tmp_path)
    image_bytes = b"small-png"
    data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode()
    result = {
        "ok": True,
        "images": [data_url],
        "videos": [],
        "media": [{"type": "image", "url": data_url}],
        "raw": {"data": [{"b64_json": "removed upstream"}]},
    }
    stored = web_app.materialize_job_result("job-id", result)
    assert stored["images"] == ["/api/assets/job-id/image-1.png"]
    assert stored["media"] == [{"type": "image", "url": "/api/assets/job-id/image-1.png"}]
    assert (tmp_path / "job-id" / "image-1.png").read_bytes() == image_bytes


def test_sse_failure_after_headers_stays_an_sse_error(monkeypatch):
    response = FakeResponse(headers={"Content-Type": "text/event-stream"}, lines=ValueError("bad stream"))
    monkeypatch.setattr(
        web_app,
        "request_image_stream",
        lambda *args, **kwargs: (response, "https://8.8.8.8/v1/images/generations", "request-id"),
    )

    class Handler:
        def __init__(self):
            self.wfile = BytesIO()
            self.statuses = []

        def send_response(self, status):
            self.statuses.append(status)

        def send_header(self, *_args):
            pass

        def end_headers(self):
            pass

    handler = Handler()
    web_app.stream_images_to_client(handler, valid_payload())
    body = handler.wfile.getvalue().decode("utf-8")
    assert handler.statuses == [200]
    assert "event: error" in body
    assert "bad stream" in body


def test_http_layer_rejects_oversized_body_before_reading():
    handler = object.__new__(web_app.AppHandler)
    handler.path = "/api/generate"
    handler.headers = {"Content-Length": str(web_app.MAX_REQUEST_BODY_BYTES + 1)}
    handler.server = SimpleNamespace(require_auth=False)
    handler.rfile = BytesIO(b"must not be read")
    sent = []

    def send_json(self, status, payload):
        sent.append((status, payload))

    handler._send_json = MethodType(send_json, handler)
    handler.do_POST()
    assert sent[0][0] == 413
    assert handler.rfile.tell() == 0
    assert handler.close_connection is True


def test_non_loopback_listener_requires_auth_token():
    with pytest.raises(ValueError, match="OPENAI_IMAGE_CLIENT_AUTH_TOKEN"):
        web_app.create_server("0.0.0.0", 0, True)


GROK_VIDEO_ENDPOINT = "https://api.x.ai/v1/videos/generations"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 绝对地址原样保留
        ("https://cdn.x.ai/a.mp4", "https://cdn.x.ai/a.mp4"),
        # 协议相对地址按端点补全 scheme
        ("//cdn.x.ai/a.mp4", "https://cdn.x.ai/a.mp4"),
        # 根相对路径按端点补全 scheme + host
        ("/files/a.mp4", "https://api.x.ai/files/a.mp4"),
        # 路径相对地址
        ("a.mp4", "https://api.x.ai/v1/videos/a.mp4"),
    ],
)
def test_video_result_urls_resolve_relative_forms(raw, expected):
    assert web_app.resolve_video_result_urls([raw], GROK_VIDEO_ENDPOINT) == [expected]


def test_video_result_accepts_inline_data_url():
    data_url = "data:video/mp4;base64," + base64.b64encode(b"fake-mp4").decode()
    assert web_app.resolve_video_result_urls([data_url], GROK_VIDEO_ENDPOINT) == [data_url]


def test_video_result_url_still_blocks_local_targets():
    # 补全相对地址不能成为绕过口子：拼出来的绝对地址仍要过出站校验。
    with pytest.raises(ValueError):
        web_app.resolve_video_result_urls(["/a.mp4"], "https://127.0.0.1/v1/videos/generations")


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "blob:https://api.x.ai/abc",
        "data:text/html;base64,PHNjcmlwdD4=",
        "data:video/mp4,not-base64",
        "",
    ],
)
def test_video_result_urls_reject_unloadable_schemes(raw):
    with pytest.raises(ValueError):
        web_app.resolve_video_result_urls([raw], GROK_VIDEO_ENDPOINT)


def test_video_result_total_size_is_bounded(monkeypatch):
    monkeypatch.setattr(web_app, "MAX_GENERATED_MEDIA_BYTES", 4)
    oversized = "data:video/mp4;base64," + base64.b64encode(b"aaaaaaaa").decode()
    with pytest.raises(ValueError, match="总大小"):
        web_app.resolve_video_result_urls([oversized], GROK_VIDEO_ENDPOINT)


def test_inline_video_is_materialized_to_an_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "ASSET_ROOT", tmp_path)
    video_bytes = b"fake-mp4-body"
    data_url = "data:video/mp4;base64," + base64.b64encode(video_bytes).decode()
    stored = web_app.materialize_job_result("job-id", {"images": [], "videos": [data_url]})
    assert stored["videos"] == ["/api/assets/job-id/video-1.mp4"]
    assert stored["media"] == [{"type": "video", "url": "/api/assets/job-id/video-1.mp4"}]
    assert (tmp_path / "job-id" / "video-1.mp4").read_bytes() == video_bytes


def test_remote_video_urls_are_not_materialized(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "ASSET_ROOT", tmp_path)
    stored = web_app.materialize_job_result(
        "job-id", {"images": [], "videos": ["https://cdn.x.ai/a.mp4"]}
    )
    assert stored["videos"] == ["https://cdn.x.ai/a.mp4"]
    assert not (tmp_path / "job-id").exists()

def test_sub2api_relay_video_is_downloaded_with_the_api_key(monkeypatch):
    """sub2api 把 video.url 改写成 /v1/videos/<id>/content，该路由挂了 apiKeyAuth。"""
    seen = {}

    def fake_request(method, url, **kwargs):
        if method == "POST":
            return FakeResponse({"status": "done", "video": {"url": "/v1/videos/req-1/content"}})
        seen["url"] = url
        seen["auth"] = kwargs.get("headers", {}).get("Authorization")
        return FakeResponse(body=b"mp4-bytes", headers={"Content-Type": "video/mp4"})

    monkeypatch.setattr(web_app, "safe_http_request", fake_request)
    result = web_app.request_grok_video_api(
        "https://8.8.8.8/v1",
        "secret-key",
        30,
        json_body={"model": "grok-imagine-video", "prompt": "p"},
    )
    # 相对地址按端点补全成网关的绝对地址
    assert seen["url"] == "https://8.8.8.8/v1/videos/req-1/content"
    # 且带上了 Key，否则网关返回 401
    assert seen["auth"] == "Bearer secret-key"
    # 内容被取回并内联，交给 materialize_job_result 落盘
    assert result["videos"][0] == "data:video/mp4;base64," + base64.b64encode(b"mp4-bytes").decode()


def test_third_party_cdn_video_never_receives_the_api_key(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("headers", {}).get("Authorization")))
        return FakeResponse({"status": "done", "video": {"url": "https://1.1.1.1/signed/a.mp4"}})

    monkeypatch.setattr(web_app, "safe_http_request", fake_request)
    result = web_app.request_grok_video_api(
        "https://8.8.8.8/v1",
        "secret-key",
        30,
        json_body={"model": "grok-imagine-video", "prompt": "p"},
    )
    # 异源签名地址原样交给浏览器，服务端不下载、更不带 Key
    assert result["videos"] == ["https://1.1.1.1/signed/a.mp4"]
    assert [call for call in calls if call[1].startswith("https://1.1.1.1")] == []


def test_relay_video_download_failure_reports_diagnostics(monkeypatch):
    def fake_request(method, url, **kwargs):
        if method == "POST":
            return FakeResponse({"status": "done", "video": {"url": "/v1/videos/req-1/content"}})
        return FakeResponse({"error": "unauthorized"}, status=401)

    monkeypatch.setattr(web_app, "safe_http_request", fake_request)
    with pytest.raises(RuntimeError) as excinfo:
        web_app.request_grok_video_api(
            "https://8.8.8.8/v1",
            "secret-key",
            30,
            json_body={"model": "grok-imagine-video", "prompt": "p"},
        )
    payload = json.loads(str(excinfo.value))
    assert "HTTP 401" in payload["message"]
    assert payload["raw"]["video"]["url"] == "/v1/videos/req-1/content"


@pytest.mark.parametrize(
    "header, expected",
    [
        ("", None),                       # 无 Range -> 整体响应
        ("bytes=0-1", (0, 1)),            # Safari 的探测请求
        ("bytes=0-", (0, 99)),
        ("bytes=50-", (50, 99)),
        ("bytes=10-20", (10, 20)),
        ("bytes=90-500", (90, 99)),       # 末端截断到资源大小
        ("bytes=-10", (90, 99)),          # 末尾 N 字节
        ("bytes=0-1,5-6", None),          # 多段不处理，按整体响应
        ("items=0-1", None),              # 非 bytes 单位
        ("bytes=abc", None),              # 语法错误
    ],
)
def test_parse_byte_range(header, expected):
    assert web_app.parse_byte_range(header, 100) == expected


@pytest.mark.parametrize("header", ["bytes=100-", "bytes=200-300", "bytes=-0"])
def test_parse_byte_range_rejects_unsatisfiable(header):
    with pytest.raises(ValueError):
        web_app.parse_byte_range(header, 100)


def _serve_asset(tmp_path, monkeypatch, body, *, headers):
    monkeypatch.setattr(web_app, "ASSET_ROOT", tmp_path)
    job_id = "11111111-1111-1111-1111-111111111111"
    (tmp_path / job_id).mkdir(parents=True, exist_ok=True)
    (tmp_path / job_id / "video-1.mp4").write_bytes(body)
    monkeypatch.setattr(web_app, "get_async_job", lambda _id: {"status": "success"})

    handler = object.__new__(web_app.AppHandler)
    handler.path = f"/api/assets/{job_id}/video-1.mp4"
    handler.request_version = "HTTP/1.1"
    handler.server = SimpleNamespace(frame_ancestors="*", x_frame_options=None)
    handler.headers = headers
    handler._headers_buffer = []
    handler.wfile = BytesIO()
    handler.send_response = MethodType(
        lambda self, code, message=None: self._headers_buffer.append(f"STATUS {code}"), handler
    )
    handler.send_header = MethodType(
        lambda self, key, value: self._headers_buffer.append(f"{key}: {value}"), handler
    )
    handler.end_headers = MethodType(lambda self: None, handler)
    handler._send_asset(job_id, "video-1.mp4")
    return handler._headers_buffer, handler.wfile.getvalue()


def test_video_asset_serves_range_requests(tmp_path, monkeypatch):
    body = bytes(range(100))
    sent, payload = _serve_asset(tmp_path, monkeypatch, body, headers={"Range": "bytes=10-19"})
    assert "STATUS 206" in sent
    assert "Content-Range: bytes 10-19/100" in sent
    assert "Accept-Ranges: bytes" in sent
    assert "Content-Length: 10" in sent
    assert payload == body[10:20]


def test_video_asset_without_range_still_returns_whole_body(tmp_path, monkeypatch):
    body = bytes(range(100))
    sent, payload = _serve_asset(tmp_path, monkeypatch, body, headers={})
    assert "STATUS 200" in sent
    # 即使整体响应也要声明支持 Range，否则 Safari 不会再发探测请求
    assert "Accept-Ranges: bytes" in sent
    assert payload == body


def test_video_asset_rejects_unsatisfiable_range(tmp_path, monkeypatch):
    sent, payload = _serve_asset(
        tmp_path, monkeypatch, bytes(range(100)), headers={"Range": "bytes=500-600"}
    )
    assert "STATUS 416" in sent
    assert "Content-Range: bytes */100" in sent
    assert payload == b""
