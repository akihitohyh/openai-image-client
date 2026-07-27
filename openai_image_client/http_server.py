from __future__ import annotations

import ipaddress
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .http_auth import (
    HTTPAuthConfig,
    StaticTenantTokenVerifier,
    load_http_auth_config,
)
from .profiles import ConfigurationError, MCPSettings, load_settings
from .server import build_server
from .service import Processor
from .tenancy import TenantRateLimitExceeded, TenantRuntimeRegistry


MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
REQUIRED_TRANSPORT_SCOPE = "mcp:use"
LOCAL_PUBLIC_HOSTS = {"127.0.0.1", "localhost", "::1"}
DNS_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class MCPHTTPSettings:
    host: str
    port: int
    allow_non_loopback: bool
    public_url: str
    issuer_url: str
    auth_config_path: Path
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    max_body_bytes: int
    max_resource_bytes: int
    global_workers: int
    global_queue: int
    global_io_concurrency: int
    tenant_io_concurrency: int
    max_inflight_requests: int
    global_asset_store_bytes: int
    log_level: str


@dataclass
class HTTPMCPApplication:
    app: Starlette
    registry: TenantRuntimeRegistry
    settings: MCPHTTPSettings

    def close(self, wait: bool = False) -> None:
        self.registry.close(wait=wait)


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int, path: str = MCP_PATH) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != self.path
        ):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_encoding = headers.get("content-encoding", "").strip().lower()
        if content_encoding not in {"", "identity"}:
            await self._error(scope, receive, send, 415, "content encoding is not supported")
            return

        content_length = headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError:
                await self._error(scope, receive, send, 400, "invalid content length")
                return
            if declared_length < 0:
                await self._error(scope, receive, send, 400, "invalid content length")
                return
            if declared_length > self.max_body_bytes:
                await self._error(scope, receive, send, 413, "request body is too large")
                return

        received_bytes = 0
        overflowed = False

        async def limited_receive() -> Message:
            nonlocal overflowed, received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    overflowed = True
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }
            return message

        async def guarded_send(message: Message) -> None:
            if not overflowed:
                await send(message)

        await self.app(scope, limited_receive, guarded_send)
        if overflowed:
            await self._error(scope, receive, send, 413, "request body is too large")

    @staticmethod
    async def _error(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        message: str,
    ) -> None:
        response = JSONResponse(
            {"error": message},
            status_code=status_code,
            headers={"Connection": "close"},
        )
        await response(scope, receive, send)


class RequestConcurrencyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_inflight: int, path: str = MCP_PATH) -> None:
        self.app = app
        self.path = path
        self._limiter = anyio.Semaphore(max_inflight)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self.path:
            await self.app(scope, receive, send)
            return
        async with self._limiter:
            await self.app(scope, receive, send)


class TenantOperationRateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        verifier: StaticTenantTokenVerifier,
        registry: TenantRuntimeRegistry,
        path: str = MCP_PATH,
    ) -> None:
        self.app = app
        self.verifier = verifier
        self.registry = registry
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self.path:
            await self.app(scope, receive, send)
            return

        authorization = Headers(scope=scope).get("authorization", "")
        if authorization.lower().startswith("bearer "):
            access_token = await self.verifier.verify_token(authorization[7:])
            if access_token is not None:
                try:
                    self.registry.check_operation(access_token.subject)
                except TenantRateLimitExceeded as exc:
                    message = str(exc)
                    retry_after = re.search(
                        r"retry after approximately (\d+) seconds",
                        message,
                    )
                    headers = (
                        {"Retry-After": retry_after.group(1)} if retry_after else None
                    )
                    response = JSONResponse(
                        {"error": message},
                        status_code=429,
                        headers=headers,
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _bounded_int(
    raw: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    name: str,
) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(raw: str | None, *, default: bool, name: str) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _hostname(value: str) -> str:
    normalized = value.strip().lower()
    try:
        ipaddress.ip_address(normalized)
        return normalized
    except ValueError:
        pass
    labels = normalized[:-1].split(".") if normalized.endswith(".") else normalized.split(".")
    if (
        not normalized
        or len(normalized) > 253
        or not labels
        or any(not DNS_LABEL_PATTERN.fullmatch(label) for label in labels)
    ):
        raise ConfigurationError("host contains an invalid hostname or IP address")
    return normalized


def _authority(parsed: Any) -> str:
    hostname = _hostname(parsed.hostname or "")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("host contains an invalid port") from exc
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{display_host}:{port}" if port is not None else display_host


def _public_url(raw: str) -> tuple[str, str]:
    raw_value = raw.strip().rstrip("/")
    if not raw_value:
        raise ConfigurationError("OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL is required")
    parsed = urlparse(raw_value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ConfigurationError(
            "OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL must be an absolute MCP endpoint URL"
        )
    if parsed.path != MCP_PATH and not parsed.path.endswith(MCP_PATH):
        raise ConfigurationError("the public MCP URL path must end with /mcp")
    authority = _authority(parsed)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and hostname not in LOCAL_PUBLIC_HOSTS:
        raise ConfigurationError("the public MCP URL must use HTTPS")
    origin = f"{parsed.scheme.lower()}://{authority}"
    value = f"{origin}{parsed.path}"
    return value, origin


def _host(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized or "*" in normalized or any(
        character in normalized for character in "/?#@"
    ):
        raise ConfigurationError("allowed hosts must contain exact host[:port] values")
    parsed = urlparse(f"http://{normalized}")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("allowed hosts must contain exact host[:port] values")
    return _authority(parsed)


def _origin(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized or "*" in normalized or normalized == "null":
        raise ConfigurationError("allowed origins must contain exact HTTP(S) origins")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ConfigurationError("allowed origins must contain exact HTTP(S) origins")
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and hostname not in LOCAL_PUBLIC_HOSTS:
        raise ConfigurationError("non-loopback browser origins must use HTTPS")
    return f"{parsed.scheme.lower()}://{_authority(parsed)}"


def _comma_separated(raw: str, parser: Any) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(parser(item) for item in raw.split(",") if item.strip()))
    return values


def load_http_settings(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    base_settings: MCPSettings | None = None,
) -> MCPHTTPSettings:
    env = os.environ if environ is None else environ
    root = (project_root or Path(__file__).resolve().parent.parent).resolve()
    raw_host = str(env.get("OPENAI_IMAGE_MCP_HTTP_HOST", "127.0.0.1")).strip()
    if raw_host.startswith("[") and raw_host.endswith("]"):
        raw_host = raw_host[1:-1]
    host = _hostname(raw_host)
    allow_non_loopback = _boolean(
        env.get("OPENAI_IMAGE_MCP_HTTP_ALLOW_NON_LOOPBACK"),
        default=False,
        name="OPENAI_IMAGE_MCP_HTTP_ALLOW_NON_LOOPBACK",
    )
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if not is_loopback and not allow_non_loopback:
        raise ConfigurationError(
            "non-loopback HTTP binding requires OPENAI_IMAGE_MCP_HTTP_ALLOW_NON_LOOPBACK=true"
        )
    port = _bounded_int(
        env.get("OPENAI_IMAGE_MCP_HTTP_PORT"),
        default=8765,
        minimum=1,
        maximum=65535,
        name="OPENAI_IMAGE_MCP_HTTP_PORT",
    )
    public_url, issuer_url = _public_url(
        str(env.get("OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL", ""))
    )
    parsed_public = urlparse(public_url)
    raw_allowed_hosts = str(env.get("OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS", "")).strip()
    allowed_hosts = _comma_separated(raw_allowed_hosts, _host) if raw_allowed_hosts else (
        _host(parsed_public.netloc),
    )
    raw_allowed_origins = str(
        env.get("OPENAI_IMAGE_MCP_HTTP_ALLOWED_ORIGINS", "")
    ).strip()
    allowed_origins = (
        _comma_separated(raw_allowed_origins, _origin) if raw_allowed_origins else ()
    )

    auth_path_value = str(env.get("OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG", "")).strip()
    if not auth_path_value:
        raise ConfigurationError("OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG is required")
    auth_config_path = Path(os.path.expandvars(os.path.expanduser(auth_path_value)))
    if not auth_config_path.is_absolute():
        auth_config_path = root / auth_config_path
    auth_config_path = auth_config_path.resolve()

    resolved_base_settings = base_settings or load_settings(environ=env, project_root=root)
    reference_limit = resolved_base_settings.max_reference_bytes
    default_body_limit = ((reference_limit + 2) // 3) * 4 + 1024 * 1024
    max_body_bytes = _bounded_int(
        env.get("OPENAI_IMAGE_MCP_HTTP_MAX_BODY_BYTES"),
        default=default_body_limit,
        minimum=64 * 1024,
        maximum=160 * 1024 * 1024,
        name="OPENAI_IMAGE_MCP_HTTP_MAX_BODY_BYTES",
    )
    if max_body_bytes < ((reference_limit + 2) // 3) * 4 + 64 * 1024:
        raise ConfigurationError(
            "OPENAI_IMAGE_MCP_HTTP_MAX_BODY_BYTES is too small for max_reference_bytes"
        )

    log_level = str(env.get("OPENAI_IMAGE_MCP_HTTP_LOG_LEVEL", "INFO")).strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("OPENAI_IMAGE_MCP_HTTP_LOG_LEVEL is invalid")
    return MCPHTTPSettings(
        host=host,
        port=port,
        allow_non_loopback=allow_non_loopback,
        public_url=public_url,
        issuer_url=issuer_url,
        auth_config_path=auth_config_path,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        max_body_bytes=max_body_bytes,
        max_resource_bytes=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_HTTP_MAX_RESOURCE_BYTES"),
            default=16 * 1024 * 1024,
            minimum=1024 * 1024,
            maximum=32 * 1024 * 1024,
            name="OPENAI_IMAGE_MCP_HTTP_MAX_RESOURCE_BYTES",
        ),
        global_workers=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_HTTP_GLOBAL_WORKERS"),
            default=8,
            minimum=1,
            maximum=64,
            name="OPENAI_IMAGE_MCP_HTTP_GLOBAL_WORKERS",
        ),
        global_queue=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_HTTP_GLOBAL_QUEUE"),
            default=100,
            minimum=0,
            maximum=1000,
            name="OPENAI_IMAGE_MCP_HTTP_GLOBAL_QUEUE",
        ),
        global_io_concurrency=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_HTTP_GLOBAL_IO_CONCURRENCY"),
            default=8,
            minimum=1,
            maximum=64,
            name="OPENAI_IMAGE_MCP_HTTP_GLOBAL_IO_CONCURRENCY",
        ),
        tenant_io_concurrency=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_HTTP_TENANT_IO_CONCURRENCY"),
            default=2,
            minimum=1,
            maximum=16,
            name="OPENAI_IMAGE_MCP_HTTP_TENANT_IO_CONCURRENCY",
        ),
        max_inflight_requests=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_HTTP_MAX_INFLIGHT_REQUESTS"),
            default=128,
            minimum=1,
            maximum=1024,
            name="OPENAI_IMAGE_MCP_HTTP_MAX_INFLIGHT_REQUESTS",
        ),
        global_asset_store_bytes=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_HTTP_GLOBAL_ASSET_STORE_BYTES"),
            default=max(
                10 * 1024 * 1024 * 1024,
                resolved_base_settings.max_asset_store_bytes,
            ),
            minimum=1024 * 1024,
            maximum=1024 * 1024 * 1024 * 1024,
            name="OPENAI_IMAGE_MCP_HTTP_GLOBAL_ASSET_STORE_BYTES",
        ),
        log_level=log_level,
    )


def _validate_tenant_config(
    auth_config: HTTPAuthConfig,
    base_settings: MCPSettings,
) -> None:
    profile_names = set(base_settings.profiles)
    for grant in auth_config.grants_by_tenant.values():
        if REQUIRED_TRANSPORT_SCOPE not in grant.scopes:
            raise ConfigurationError(
                f"tenant {grant.tenant_id!r} must include scope {REQUIRED_TRANSPORT_SCOPE!r}"
            )
        unknown_profiles = set(grant.allowed_profiles) - profile_names
        if unknown_profiles:
            raise ConfigurationError(
                f"tenant {grant.tenant_id!r} references unknown profiles"
            )


def create_http_application(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    processor: Processor | None = None,
) -> HTTPMCPApplication:
    env = os.environ if environ is None else environ
    root = (project_root or Path(__file__).resolve().parent.parent).resolve()
    base_settings = load_settings(environ=env, project_root=root)
    settings = load_http_settings(
        environ=env,
        project_root=root,
        base_settings=base_settings,
    )
    auth_config = load_http_auth_config(settings.auth_config_path, env)
    _validate_tenant_config(auth_config, base_settings)
    verifier = StaticTenantTokenVerifier(
        auth_config,
        issuer=settings.issuer_url,
        resource=settings.public_url,
    )
    registry = TenantRuntimeRegistry(
        base_settings,
        auth_config,
        global_workers=settings.global_workers,
        global_queue=settings.global_queue,
        global_asset_store_bytes=settings.global_asset_store_bytes,
        processor=processor,
    )

    global_io_limiter = anyio.CapacityLimiter(settings.global_io_concurrency)
    tenant_io_limiters = {
        tenant_id: anyio.CapacityLimiter(settings.tenant_io_concurrency)
        for tenant_id in auth_config.grants_by_tenant
    }

    def authenticated_tenant_id() -> str:
        access_token = get_access_token()
        tenant_id = access_token.subject if access_token is not None else None
        if (
            access_token is None
            or not tenant_id
            or access_token.client_id != f"tenant:{tenant_id}"
            or tenant_id not in auth_config.grants_by_tenant
        ):
            raise PermissionError("authenticated tenant identity is unavailable")
        return tenant_id

    def resolve_runtime(required_scope: str):
        access_token = get_access_token()
        tenant_id = authenticated_tenant_id()
        assert access_token is not None
        if required_scope and required_scope not in access_token.scopes:
            raise PermissionError(f"required scope is missing: {required_scope}")
        try:
            return registry.get_runtime(tenant_id)
        except OSError as exc:  # pragma: no cover - registry normalizes storage failures
            raise RuntimeError("tenant storage is unavailable") from exc

    async def run_tenant_blocking(_active, operation):
        tenant_id = authenticated_tenant_id()
        async with tenant_io_limiters[tenant_id]:
            async with global_io_limiter:
                return await anyio.to_thread.run_sync(operation)

    try:
        mcp = build_server(
            runtime_resolver=resolve_runtime,
            blocking_runner=run_tenant_blocking,
            remote=True,
            max_resource_bytes=settings.max_resource_bytes,
            fastmcp_options={
                "host": settings.host,
                "port": settings.port,
                "streamable_http_path": MCP_PATH,
                "json_response": True,
                "stateless_http": True,
                "log_level": settings.log_level,
                "auth": AuthSettings(
                    issuer_url=settings.issuer_url,
                    resource_server_url=None,
                    required_scopes=[REQUIRED_TRANSPORT_SCOPE],
                ),
                "token_verifier": verifier,
                "transport_security": TransportSecuritySettings(
                    enable_dns_rebinding_protection=True,
                    allowed_hosts=list(settings.allowed_hosts),
                    allowed_origins=list(settings.allowed_origins),
                ),
            },
        )

        @mcp.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)
        async def healthz(_: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "ok": True,
                    "service": "openai-image-client-mcp",
                    "transport": "streamable-http",
                }
            )

        app = mcp.streamable_http_app()
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def application_lifespan(starlette_app: Starlette):
            try:
                async with original_lifespan(starlette_app) as state:
                    yield state
            finally:
                registry.close(wait=False)

        app.router.lifespan_context = application_lifespan
        app.add_middleware(
            TenantOperationRateLimitMiddleware,
            verifier=verifier,
            registry=registry,
            path=MCP_PATH,
        )
        app.add_middleware(
            RequestBodyLimitMiddleware,
            max_body_bytes=settings.max_body_bytes,
            path=MCP_PATH,
        )
        app.add_middleware(
            RequestConcurrencyLimitMiddleware,
            max_inflight=settings.max_inflight_requests,
            path=MCP_PATH,
        )
        if settings.allowed_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=list(settings.allowed_origins),
                allow_methods=["GET", "POST", "DELETE"],
                allow_headers=[
                    "Authorization",
                    "Content-Type",
                    "Last-Event-ID",
                    "Mcp-Protocol-Version",
                    "Mcp-Session-Id",
                ],
                expose_headers=["Mcp-Session-Id"],
                allow_credentials=False,
                max_age=600,
            )
        app.add_middleware(SecurityHeadersMiddleware)
    except BaseException:
        registry.close(wait=False)
        raise
    return HTTPMCPApplication(app=app, registry=registry, settings=settings)


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        description="Run the authenticated OpenAI Image Client Streamable HTTP MCP server."
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate profiles, tenants, and HTTP settings without starting the server",
    )
    arguments = parser.parse_args()
    try:
        application = create_http_application()
    except ConfigurationError as exc:
        parser.error(str(exc))
    if arguments.check_config:
        application.close(wait=False)
        print("HTTP MCP configuration is valid")
        return
    try:
        uvicorn.run(
            application.app,
            host=application.settings.host,
            port=application.settings.port,
            log_level=application.settings.log_level.lower(),
            workers=1,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1,::1",
            server_header=False,
            timeout_graceful_shutdown=30,
            limit_concurrency=application.settings.max_inflight_requests + 1,
        )
    finally:
        application.close(wait=False)
