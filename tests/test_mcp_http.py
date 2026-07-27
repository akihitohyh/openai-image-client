from __future__ import annotations

import base64
import io
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import anyio
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl
from PIL import Image

from openai_image_client.assets import AssetStore
from openai_image_client.http_server import (
    RequestConcurrencyLimitMiddleware,
    create_http_application,
    load_http_settings,
)
from openai_image_client.profiles import ConfigurationError, MCPSettings, Profile


TOKEN_A = "tenant-a-token-0123456789abcdef0123456789abcdef"
TOKEN_B = "tenant-b-token-0123456789abcdef0123456789abcdef"
TOKEN_LIMITED = "limited-token-0123456789abcdef0123456789abcdef"
TOKEN_RATE = "rate-token-0123456789abcdef0123456789abcdef0123"
TOKEN_OPERATION = "operation-token-0123456789abcdef0123456789abcdef"
ALL_SCOPES = [
    "mcp:use",
    "image:generate",
    "image:upload",
    "image:read",
    "image:cancel",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def png_bytes(color: tuple[int, int, int] = (20, 40, 60)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (6, 5), color).save(output, format="PNG")
    return output.getvalue()


def large_png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (700, 700), (10, 20, 30)).save(
        output,
        format="PNG",
        compress_level=0,
    )
    return output.getvalue()


def _write_json(path: Path, payload: dict, *, mode: int | None = None) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


@pytest.fixture
def http_application(tmp_path: Path):
    profile_config = tmp_path / "profiles.json"
    _write_json(
        profile_config,
        {
            "profiles": {
                "tenant-a-profile": {
                    "base_url": "https://provider-a.example/v1",
                    "api_key_env": "PROVIDER_A_KEY",
                    "protocol": "openai",
                    "image_model": "gpt-image-2",
                    "allowed_models": ["gpt-image-2"],
                },
                "tenant-b-profile": {
                    "base_url": "https://provider-b.example/v1",
                    "api_key_env": "PROVIDER_B_KEY",
                    "protocol": "openai",
                    "image_model": "gpt-image-2",
                    "allowed_models": ["gpt-image-2"],
                },
            },
            "default_profile": "tenant-a-profile",
            "output_dir": str(tmp_path / "assets"),
            "allowed_input_roots": [str(tmp_path)],
            "max_reference_bytes": 1024,
            "max_output_bytes": 2 * 1024 * 1024,
            "max_asset_store_bytes": 10 * 1024 * 1024,
        },
    )
    auth_config = tmp_path / "http-auth.json"
    _write_json(
        auth_config,
        {
            "tenants": [
                {
                    "tenant_id": "tenant-a",
                    "token_env": "TENANT_A_TOKEN",
                    "allowed_profiles": ["tenant-a-profile"],
                    "default_profile": "tenant-a-profile",
                    "scopes": ALL_SCOPES,
                    "requests_per_minute": 10,
                    "operations_per_minute": 100,
                    "expires_at": None,
                },
                {
                    "tenant_id": "tenant-b",
                    "token_env": "TENANT_B_TOKEN",
                    "allowed_profiles": ["tenant-b-profile"],
                    "default_profile": "tenant-b-profile",
                    "scopes": ALL_SCOPES,
                    "requests_per_minute": 10,
                    "operations_per_minute": 100,
                    "expires_at": None,
                },
                {
                    "tenant_id": "limited",
                    "token_env": "TENANT_LIMITED_TOKEN",
                    "allowed_profiles": ["tenant-a-profile"],
                    "default_profile": "tenant-a-profile",
                    "scopes": ["mcp:use"],
                    "requests_per_minute": 1,
                    "operations_per_minute": 100,
                    "expires_at": None,
                },
                {
                    "tenant_id": "rate-limited",
                    "token_env": "TENANT_RATE_TOKEN",
                    "allowed_profiles": ["tenant-a-profile"],
                    "default_profile": "tenant-a-profile",
                    "scopes": ["mcp:use", "image:generate", "image:read"],
                    "requests_per_minute": 1,
                    "operations_per_minute": 100,
                    "expires_at": None,
                },
                {
                    "tenant_id": "operation-limited",
                    "token_env": "TENANT_OPERATION_TOKEN",
                    "allowed_profiles": ["tenant-a-profile"],
                    "default_profile": "tenant-a-profile",
                    "scopes": ["mcp:use", "image:read"],
                    "requests_per_minute": 1,
                    "operations_per_minute": 1,
                    "expires_at": None,
                },
            ]
        },
        mode=0o600,
    )
    environ = {
        "OPENAI_IMAGE_MCP_CONFIG": str(profile_config),
        "OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG": str(auth_config),
        "OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL": "https://mcp.example.com/mcp",
        "OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS": "mcp.example.com",
        "OPENAI_IMAGE_MCP_HTTP_ALLOWED_ORIGINS": "https://console.example.com",
        "OPENAI_IMAGE_MCP_HTTP_MAX_BODY_BYTES": "70000",
        "OPENAI_IMAGE_MCP_HTTP_MAX_RESOURCE_BYTES": str(1024 * 1024),
        "TENANT_A_TOKEN": TOKEN_A,
        "TENANT_B_TOKEN": TOKEN_B,
        "TENANT_LIMITED_TOKEN": TOKEN_LIMITED,
        "TENANT_RATE_TOKEN": TOKEN_RATE,
        "TENANT_OPERATION_TOKEN": TOKEN_OPERATION,
        "PROVIDER_A_KEY": "provider-a-secret",
        "PROVIDER_B_KEY": "provider-b-secret",
    }
    encoded = base64.b64encode(png_bytes((80, 100, 120))).decode("ascii")

    def processor(payload: dict) -> dict:
        return {
            "media_type": "image",
            "images": [f"data:image/png;base64,{encoded}"],
            "raw": {
                "api_key": payload["api_key"],
                "base_url": payload["base_url"],
            },
        }

    application = create_http_application(
        environ=environ,
        project_root=tmp_path,
        processor=processor,
    )
    try:
        yield application
    finally:
        application.close(wait=True)


@asynccontextmanager
async def mcp_session(application, token: str):
    transport = httpx.ASGITransport(app=application.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://mcp.example.com",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ) as client:
        async with streamable_http_client(
            "https://mcp.example.com/mcp",
            http_client=client,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


def initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "http-integration-test", "version": "1.0"},
        },
    }


def base_http_settings(tmp_path: Path) -> MCPSettings:
    return MCPSettings(
        profiles={
            "default": Profile(
                name="default",
                base_url="https://provider.example/v1",
                api_key="test-key",
            )
        },
        default_profile="default",
        output_dir=tmp_path / "assets",
        allowed_input_roots=(tmp_path,),
    )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"OPENAI_IMAGE_MCP_HTTP_HOST": "0.0.0.0"}, "ALLOW_NON_LOOPBACK"),
        ({"OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL": "https://mcp.example.com/not-mcp"}, "end with /mcp"),
        ({"OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS": "*.example.com"}, "exact host"),
        (
            {"OPENAI_IMAGE_MCP_HTTP_ALLOWED_ORIGINS": "http://console.example.com"},
            "must use HTTPS",
        ),
        ({"OPENAI_IMAGE_MCP_HTTP_ALLOW_NON_LOOPBACK": "sometimes"}, "true or false"),
    ],
)
def test_http_settings_fail_closed(
    tmp_path: Path,
    overrides: dict[str, str],
    message: str,
) -> None:
    environ = {
        "OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG": str(tmp_path / "auth.json"),
        "OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL": "https://mcp.example.com/service/mcp",
        **overrides,
    }
    with pytest.raises(ConfigurationError, match=message):
        load_http_settings(
            environ=environ,
            project_root=tmp_path,
            base_settings=base_http_settings(tmp_path),
        )


def test_http_settings_allow_explicit_non_loopback_proxy_binding(tmp_path: Path) -> None:
    settings = load_http_settings(
        environ={
            "OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG": str(tmp_path / "auth.json"),
            "OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL": "https://mcp.example.com/service/mcp",
            "OPENAI_IMAGE_MCP_HTTP_HOST": "0.0.0.0",
            "OPENAI_IMAGE_MCP_HTTP_ALLOW_NON_LOOPBACK": "true",
        },
        project_root=tmp_path,
        base_settings=base_http_settings(tmp_path),
    )
    assert settings.host == "0.0.0.0"
    assert settings.allow_non_loopback
    assert settings.public_url.endswith("/service/mcp")


@pytest.mark.anyio
async def test_http_request_concurrency_is_bounded() -> None:
    active = 0
    peak = 0

    async def endpoint(scope, receive, send):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await anyio.sleep(0.03)
        active -= 1
        body = b'{"ok":true}'
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    app = RequestConcurrencyLimitMiddleware(endpoint, max_inflight=1)
    transport = httpx.ASGITransport(app=app)
    statuses: list[int] = []
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://mcp.example.com",
    ) as client:
        async def request() -> None:
            response = await client.post("/mcp")
            statuses.append(response.status_code)

        async with anyio.create_task_group() as task_group:
            for _ in range(5):
                task_group.start_soon(request)

    assert statuses == [200] * 5
    assert peak == 1


@pytest.mark.anyio
async def test_authenticated_http_tenants_are_isolated(http_application) -> None:
    async with http_application.app.router.lifespan_context(http_application.app):
        async with mcp_session(http_application, TOKEN_A) as tenant_a:
            tools = {tool.name for tool in (await tenant_a.list_tools()).tools}
            assert "upload_reference_image" in tools
            assert "import_reference_image" not in tools

            profiles = await tenant_a.read_resource(AnyUrl("image-client://profiles"))
            profiles_text = profiles.contents[0].text
            assert "tenant-a-profile" in profiles_text
            assert "tenant-b-profile" not in profiles_text
            capabilities = await tenant_a.read_resource(
                AnyUrl("image-client://capabilities")
            )
            capability_payload = json.loads(capabilities.contents[0].text)
            assert capability_payload["transport"] == "streamable-http"
            assert "allowed_input_roots" not in capability_payload["security"]
            assert capability_payload["security"]["max_resource_response_bytes"] == 1024 * 1024

            upload = await tenant_a.call_tool(
                "upload_reference_image",
                {
                    "data_base64": base64.b64encode(png_bytes()).decode("ascii"),
                    "filename": "reference.png",
                },
            )
            assert not upload.isError
            uploaded_asset_id = upload.structuredContent["asset"]["asset_id"]

            generated = await tenant_a.call_tool(
                "generate_image",
                {"prompt": "A tenant-isolated test image"},
            )
            assert not generated.isError
            generated_asset = generated.structuredContent["assets"][0]
            generated_job_id = generated.structuredContent["job_id"]

            large_asset = http_application.registry.get("tenant-a").assets.store_data_url(
                "data:image/png;base64,"
                + base64.b64encode(large_png_bytes()).decode("ascii"),
                media_type="image",
                name="large-test-image.png",
            )
            assert large_asset.size > 1024 * 1024
            with pytest.raises(Exception, match="remote resource response limit"):
                await tenant_a.read_resource(AnyUrl(large_asset.uri))

            denied_profile = await tenant_a.call_tool(
                "generate_image",
                {
                    "prompt": "This profile is not entitled",
                    "profile": "tenant-b-profile",
                },
            )
            assert denied_profile.isError

        async with mcp_session(http_application, TOKEN_B) as tenant_b:
            cross_job = await tenant_b.call_tool(
                "get_job",
                {"job_id": generated_job_id},
            )
            assert cross_job.isError
            cross_cancel = await tenant_b.call_tool(
                "cancel_job",
                {"job_id": generated_job_id},
            )
            assert cross_cancel.isError
            cross_edit = await tenant_b.call_tool(
                "edit_image",
                {
                    "prompt": "Attempt a cross-tenant edit",
                    "reference_asset_ids": [uploaded_asset_id],
                },
            )
            assert cross_edit.isError
            with pytest.raises(Exception, match="asset does not exist"):
                await tenant_b.read_resource(AnyUrl(generated_asset["uri"]))

        async with mcp_session(http_application, TOKEN_LIMITED) as limited:
            denied_upload = await limited.call_tool(
                "upload_reference_image",
                {"data_base64": base64.b64encode(png_bytes()).decode("ascii")},
            )
            assert denied_upload.isError
            assert "image:upload" in str(denied_upload.content)

        async with mcp_session(http_application, TOKEN_RATE) as rate_limited:
            first = await rate_limited.call_tool(
                "generate_image",
                {"prompt": "The one allowed request"},
            )
            assert not first.isError
            second = await rate_limited.call_tool(
                "generate_image",
                {"prompt": "This request exceeds the tenant RPM"},
            )
            assert second.isError
            assert "rate limit exceeded" in str(second.content)

    assert http_application.registry._closed


@pytest.mark.anyio
async def test_general_operation_limit_covers_every_authenticated_mcp_request(
    http_application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_tenants: list[str] = []
    original_check = http_application.registry.check_operation

    def observed_check(tenant_id: str) -> None:
        checked_tenants.append(tenant_id)
        original_check(tenant_id)

    monkeypatch.setattr(http_application.registry, "check_operation", observed_check)
    common_headers = {
        "Accept": "application/json, text/event-stream",
        "Mcp-Protocol-Version": "2025-06-18",
    }

    async with http_application.app.router.lifespan_context(http_application.app):
        transport = httpx.ASGITransport(app=http_application.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://mcp.example.com",
            timeout=10,
        ) as client:
            missing = await client.post(
                "/mcp",
                json=initialize_payload(),
                headers=common_headers,
            )
            invalid = await client.post(
                "/mcp",
                json=initialize_payload(),
                headers={
                    **common_headers,
                    "Authorization": "Bearer " + "x" * 40,
                },
            )
            oversized = await client.post(
                "/mcp",
                content=b"x" * 70_001,
                headers={
                    **common_headers,
                    "Authorization": f"Bearer {TOKEN_A}",
                    "Content-Type": "application/json",
                },
            )
            assert [missing.status_code, invalid.status_code, oversized.status_code] == [
                401,
                401,
                413,
            ]
            assert checked_tenants == []

            tenant_a_headers = {
                **common_headers,
                "Authorization": f"Bearer {TOKEN_A}",
            }
            protocol_payloads = [
                initialize_payload(),
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "not/a-real-method",
                    "params": {},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "resources/read",
                    "params": {"uri": "image-client://profiles"},
                },
            ]
            protocol_responses = [
                await client.post("/mcp", json=payload, headers=tenant_a_headers)
                for payload in protocol_payloads
            ]
            assert all(response.status_code == 200 for response in protocol_responses)
            assert '"error"' in protocol_responses[3].text
            assert checked_tenants == ["tenant-a"] * len(protocol_payloads)

            missing_scope = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "upload_reference_image",
                        "arguments": {
                            "data_base64": base64.b64encode(png_bytes()).decode("ascii")
                        },
                    },
                },
                headers={
                    **common_headers,
                    "Authorization": f"Bearer {TOKEN_LIMITED}",
                },
            )
            assert missing_scope.status_code == 200
            assert "image:upload" in missing_scope.text
            assert checked_tenants == ["tenant-a"] * len(protocol_payloads) + [
                "limited"
            ]

            operation_headers = {
                **common_headers,
                "Authorization": f"Bearer {TOKEN_OPERATION}",
            }
            first = await client.post(
                "/mcp",
                json=initialize_payload(),
                headers=operation_headers,
            )
            second = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 7, "method": "ping"},
                headers=operation_headers,
            )
            limited = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/list",
                    "params": {},
                },
                headers=operation_headers,
            )
            assert first.status_code == 200
            assert second.status_code == 429
            assert limited.status_code == 429
            assert "operation rate limit exceeded" in second.text
            assert second.headers["retry-after"].isdigit()
            assert checked_tenants[-3:] == ["operation-limited"] * 3

    assert http_application.registry._closed


@pytest.mark.anyio
async def test_http_auth_transport_guards_health_and_upload_limits(
    http_application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread = threading.get_ident()
    upload_threads: list[int] = []
    original_upload = AssetStore.upload_reference

    def observed_upload(self, *args, **kwargs):
        upload_threads.append(threading.get_ident())
        return original_upload(self, *args, **kwargs)

    monkeypatch.setattr(AssetStore, "upload_reference", observed_upload)
    async with http_application.app.router.lifespan_context(http_application.app):
        transport = httpx.ASGITransport(app=http_application.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://mcp.example.com",
            timeout=10,
        ) as client:
            health = await client.get("/healthz")
            assert health.status_code == 200
            assert health.json() == {
                "ok": True,
                "service": "openai-image-client-mcp",
                "transport": "streamable-http",
            }
            assert health.headers["cache-control"] == "no-store"

            missing = await client.post("/mcp", json=initialize_payload())
            assert missing.status_code == 401
            assert missing.headers["www-authenticate"].startswith("Bearer ")
            invalid = await client.post(
                "/mcp",
                json=initialize_payload(),
                headers={"Authorization": "Bearer " + "x" * 40},
            )
            assert invalid.status_code == 401

            authenticated_headers = {"Authorization": f"Bearer {TOKEN_A}"}
            invalid_host = await client.post(
                "/mcp",
                json=initialize_payload(),
                headers={**authenticated_headers, "Host": "attacker.example"},
            )
            assert invalid_host.status_code == 421
            invalid_origin = await client.post(
                "/mcp",
                json=initialize_payload(),
                headers={**authenticated_headers, "Origin": "https://attacker.example"},
            )
            assert invalid_origin.status_code == 403
            preflight = await client.options(
                "/mcp",
                headers={
                    "Origin": "https://console.example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type,mcp-protocol-version",
                },
            )
            assert preflight.status_code == 200
            assert (
                preflight.headers["access-control-allow-origin"]
                == "https://console.example.com"
            )
            assert "authorization" in preflight.headers[
                "access-control-allow-headers"
            ].lower()

            oversized_body = await client.post(
                "/mcp",
                content=b"x" * 70001,
                headers={
                    **authenticated_headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert oversized_body.status_code == 413

            class ChunkedBody(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b"x" * 35_000
                    yield b"x" * 35_001

            chunked_body = await client.post(
                "/mcp",
                content=ChunkedBody(),
                headers={
                    **authenticated_headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert chunked_body.status_code == 413

        async with mcp_session(http_application, TOKEN_A) as tenant_a:
            invalid_base64 = await tenant_a.call_tool(
                "upload_reference_image",
                {"data_base64": "not-valid-base64"},
            )
            assert invalid_base64.isError
            data_url = await tenant_a.call_tool(
                "upload_reference_image",
                {
                    "data_base64": "data:image/png;base64,"
                    + base64.b64encode(png_bytes()).decode("ascii")
                },
            )
            assert data_url.isError
            too_large = await tenant_a.call_tool(
                "upload_reference_image",
                {"data_base64": base64.b64encode(os.urandom(1025)).decode("ascii")},
            )
            assert too_large.isError

    assert upload_threads
    assert all(thread_id != event_loop_thread for thread_id in upload_threads)
    assert http_application.registry._closed
