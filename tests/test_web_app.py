from __future__ import annotations

import base64
import ipaddress
from io import BytesIO
import json
import time
from types import MethodType, SimpleNamespace

import pytest
from requests import HTTPError, RequestException

import web_app


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


def test_redirect_is_revalidated_before_following(monkeypatch):
    redirect = FakeResponse(status=302, headers={"Location": "http://127.0.0.1/admin"})
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return redirect

    monkeypatch.setattr(web_app, "_request_with_pinned_dns", fake_request)
    with pytest.raises(RequestException, match="非公网地址"):
        web_app.safe_http_request("GET", "https://8.8.8.8/start")
    assert len(calls) == 1
    assert redirect.closed


def test_cross_origin_redirect_does_not_receive_api_credentials(monkeypatch):
    redirect = FakeResponse(status=307, headers={"Location": "https://1.1.1.1/next"})
    monkeypatch.setattr(web_app, "_request_with_pinned_dns", lambda *args, **kwargs: redirect)

    with pytest.raises(RequestException, match="跨源"):
        web_app.safe_http_request(
            "POST",
            "https://8.8.8.8/v1/images/generations",
            headers={"Authorization": "Bearer secret"},
        )
    assert redirect.closed


def test_pinned_dns_adapter_connects_to_validated_ip_with_original_tls_identity(
    monkeypatch,
):
    monkeypatch.setattr(
        web_app,
        "_resolved_addresses",
        lambda hostname, port: {ipaddress.ip_address("8.8.8.8")},
    )
    adapter = web_app.PinnedDNSHTTPAdapter()
    request = web_app.requests.Request(
        "GET",
        "https://Provider.Example:8443/v1/models",
    ).prepare()
    _, parsed, addresses = web_app._validated_outbound_target(request.url)
    adapter._request_target.value = (
        parsed.scheme,
        parsed.hostname,
        parsed.port,
        addresses[0].compressed,
    )
    request.headers["Host"] = web_app._request_authority(parsed)
    try:
        pool = adapter.get_connection_with_tls_context(
            request,
            verify=True,
        )
    finally:
        adapter._request_target.value = None

    assert request.headers["Host"] == "provider.example:8443"
    assert pool.host == "8.8.8.8"
    assert pool.port == 8443
    assert pool.conn_kw["server_hostname"] == "provider.example"
    assert pool.assert_hostname == "provider.example"


def test_pinned_dns_adapter_blocks_rebinding_between_validation_and_connection(
    monkeypatch,
):
    resolutions = iter(
        [
            {ipaddress.ip_address("8.8.8.8")},
            {ipaddress.ip_address("127.0.0.1")},
        ]
    )
    monkeypatch.setattr(
        web_app,
        "_resolved_addresses",
        lambda hostname, port: next(resolutions),
    )

    with pytest.raises(RequestException, match="非公网地址"):
        web_app.safe_http_request("GET", "https://provider.example/v1/models")


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
    ("overrides", "message"),
    [
        ({"n": 9}, "n 必须在"),
        ({"timeout": 601}, "timeout 必须在"),
        ({"timeout": 1}, "timeout 必须在"),
        ({"partial_images": 4}, "partial_images 必须在"),
        ({"prompt": "x" * (web_app.MAX_PROMPT_CHARS + 1)}, "提示词不能超过"),
        ({"model": "m" * (web_app.MAX_MODEL_CHARS + 1)}, "模型名不能超过"),
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
    with pytest.raises(ValueError):
        web_app.request_grok_video_api(
            "https://8.8.8.8/v1",
            "key",
            30,
            json_body={"model": "grok-imagine-video", "prompt": "test"},
        )


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
