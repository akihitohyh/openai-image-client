#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from concurrent.futures import Future, ThreadPoolExecutor
import hmac
import ipaddress
import json
import mimetypes
import os
import shutil
import socket
import stat
import threading
import sys
import tempfile
import time
import uuid
import webbrowser
from collections.abc import Callable
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import ParseResult, quote, urljoin, urlparse

import requests
from requests import RequestException
from requests.adapters import HTTPAdapter

from image_client_core.endpoints import (
    candidate_endpoints,
    video_poll_endpoint_from_generation,
)
from image_client_core.models import (
    GROK_RESOLUTION_VALUES,
    GROK_RESPONSE_FORMAT_VALUES,
    GROK_SUPPORTED_RATIOS,
    GROK_VIDEO_RESOLUTION_VALUES,
    GROK_VIDEO_SUPPORTED_RATIOS,
    grok_aspect_ratio_from_size,
    grok_resolution_from_size,
    grok_video_aspect_ratio_from_size,
    grok_video_resolution_from_size,
    is_gpt_image_2,
    is_grok_image_model,
    is_grok_video_model,
)
from image_client_core.responses import (
    decode_base64_limited,
    exception_diagnostic,
    likely_gateway_or_upstream_error,
    likely_non_retryable_client_error,
    preview_response_body,
    read_limited_response,
    response_diagnostic,
    response_json_limited,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def app_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


ROOT = app_root()
WEB_ROOT = ROOT / "web"
OPENAI_IMAGE_EDIT_FILE_FIELD = "image"
OPENAI_IMAGE_EDIT_ARRAY_FIELD = "image[]"
PROTOCOL_VALUES = {"auto", "openai", "grok", "gemini"}
JOB_TTL_SECONDS = 60 * 60
MAX_JOB_RUNTIME_SECONDS = 15 * 60
MAX_CONCURRENT_JOBS = 2
MAX_PENDING_JOBS = 8
MAX_JOBS = 64
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_FUTURES: dict[str, Future] = {}
JOB_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="image-job")
DEFAULT_STREAM_PARTIAL_IMAGES = 2
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_TIMEOUT_SECONDS = 500
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 600
MAX_IMAGE_COUNT = 8
MAX_VIDEO_COUNT = 4
MAX_UPSTREAM_MEDIA_ITEMS = 16
MAX_PARTIAL_IMAGES = 3
MAX_PROMPT_CHARS = 16_000
MAX_MODEL_CHARS = 200
MAX_BASE_URL_CHARS = 2_048
MAX_API_KEY_CHARS = 8_192
MAX_REQUEST_BODY_BYTES = 48 * 1024 * 1024
MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REFERENCE_IMAGES_BYTES = 30 * 1024 * 1024
# 4K Gemini/OpenAI images can exceed 20 MiB after base64 decoding. Keep a
# bounded per-image ceiling that still allows high-detail UHD PNGs.
MAX_REMOTE_MEDIA_BYTES = 64 * 1024 * 1024
MAX_GENERATED_MEDIA_BYTES = 96 * 1024 * 1024
MAX_SSE_EVENT_BYTES = 28 * 1024 * 1024
MAX_RETAINED_RAW_BYTES = 64 * 1024
MAX_REDIRECTS = 3
MAX_EDIT_REFERENCE_IMAGES = 16
MAX_GROK_VIDEO_REFERENCE_IMAGES = 7
GEMINI_ASPECT_RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9"}
GEMINI_IMAGE_SIZES = {"1K", "2K", "4K"}
MAX_EMBED_ORIGINS = 32
MAX_EMBED_ORIGINS_CHARS = 4096
TERMINAL_JOB_STATUSES = {"success", "error", "cancelled"}
ASSET_CACHE_SECONDS = 3600
ASSET_ROOT = Path(tempfile.gettempdir()) / "openai-image-client-assets"


class JobQueueFullError(RuntimeError):
    pass


class JobCancelledError(RuntimeError):
    """任务在执行过程中被取消或超过最大运行时间。"""


CancelCheck = Callable[[], bool] | None
CANCEL_POLL_SLICE_SECONDS = 0.5


def raise_if_cancelled(should_cancel: CancelCheck) -> None:
    if should_cancel is not None and should_cancel():
        raise JobCancelledError("任务已取消")


def cancellable_sleep(duration: float, should_cancel: CancelCheck) -> None:
    """分片休眠，使取消请求最多在 CANCEL_POLL_SLICE_SECONDS 内生效。"""
    raise_if_cancelled(should_cancel)
    if should_cancel is None:
        time.sleep(duration)
        return
    remaining = duration
    while remaining > 0:
        time.sleep(min(CANCEL_POLL_SLICE_SECONDS, remaining))
        remaining -= CANCEL_POLL_SLICE_SECONDS
        raise_if_cancelled(should_cancel)


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _canonical_embed_origin(value: str) -> str:
    if any(ord(character) < 0x20 or character.isspace() for character in value):
        raise ValueError("iframe 嵌入来源不能包含空白或控制字符")
    if any(character in value for character in "'\";\\?#"):
        raise ValueError("iframe 嵌入来源包含不允许的字符")

    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"iframe 嵌入来源不合法: {value}") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError(f"iframe 嵌入来源必须是完整的 http(s) origin: {value}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("iframe 嵌入来源不允许包含用户名或密码")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.params:
        raise ValueError("iframe 嵌入来源只能包含 scheme、host 和 port")

    normalized_host = hostname.lower().rstrip(".")
    if not normalized_host or "*" in normalized_host or "%" in normalized_host:
        raise ValueError("iframe 嵌入来源不允许通配主机或 IPv6 zone ID")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        try:
            normalized_host = normalized_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(f"iframe 嵌入来源主机名不合法: {value}") from exc
        host_text = normalized_host
    else:
        host_text = f"[{address.compressed}]" if address.version == 6 else address.compressed

    default_port = 443 if scheme == "https" else 80
    port_text = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{host_text}{port_text}"


def resolve_embed_policy(raw_value: str | None) -> tuple[str, str | None]:
    if raw_value is None:
        return "*", None
    if len(raw_value) > MAX_EMBED_ORIGINS_CHARS:
        raise ValueError(
            f"OPENAI_IMAGE_CLIENT_EMBED_ORIGINS 不能超过 {MAX_EMBED_ORIGINS_CHARS} 个字符"
        )

    values = [item.strip() for item in raw_value.split(",") if item.strip()]
    if not values:
        return "'none'", "DENY"
    if len(values) > MAX_EMBED_ORIGINS:
        raise ValueError(
            f"OPENAI_IMAGE_CLIENT_EMBED_ORIGINS 最多允许 {MAX_EMBED_ORIGINS} 个来源"
        )

    lowered = [value.lower() for value in values]
    if "*" in lowered:
        if len(values) != 1:
            raise ValueError("iframe 嵌入来源 * 必须单独使用")
        return "*", None
    if "none" in lowered:
        if len(values) != 1:
            raise ValueError("iframe 嵌入来源 none 必须单独使用")
        return "'none'", "DENY"

    sources: list[str] = []
    for value in values:
        source = "'self'" if value.lower() == "self" else _canonical_embed_origin(value)
        if source not in sources:
            sources.append(source)
    x_frame_options = "SAMEORIGIN" if sources == ["'self'"] else None
    return " ".join(sources), x_frame_options


def build_content_security_policy(frame_ancestors: str) -> str:
    return (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: blob: http: https:; media-src 'self' data: blob: http: https:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; "
        f"base-uri 'self'; form-action 'self'; frame-ancestors {frame_ancestors}"
    )


def trusted_private_upstream_hosts() -> set[str]:
    return {
        item.strip().lower().rstrip(".")
        for item in os.environ.get("OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS", "").split(",")
        if item.strip()
    }


def _host_is_explicitly_trusted(hostname: str) -> bool:
    normalized = hostname.lower().rstrip(".")
    return env_truthy("OPENAI_IMAGE_CLIENT_ALLOW_PRIVATE_UPSTREAMS") or normalized in trusted_private_upstream_hosts()


def _resolved_addresses(hostname: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return {ipaddress.ip_address(hostname)}
    except ValueError:
        pass

    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"无法解析上游主机 {hostname}: {exc}") from exc
    addresses = {ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in records}
    if not addresses:
        raise ValueError(f"无法解析上游主机 {hostname}")
    return addresses


def _validated_outbound_target(
    value: str,
    *,
    label: str = "URL",
) -> tuple[str, ParseResult, tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} 不能为空")
    if len(raw) > MAX_BASE_URL_CHARS:
        raise ValueError(f"{label} 过长")

    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"{label} 只允许 http 或 https")
    if not parsed.hostname:
        raise ValueError(f"{label} 缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} 不允许包含用户名或密码")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValueError(f"{label} 端口不合法") from exc

    hostname = parsed.hostname.lower().rstrip(".")
    addresses = _resolved_addresses(hostname, port)
    if not _host_is_explicitly_trusted(hostname):
        blocked = sorted(str(address) for address in addresses if not address.is_global)
        if blocked:
            raise ValueError(
                f"{label} 指向非公网地址 ({', '.join(blocked)})；"
                "如确为受信任的本地网关，请通过 OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS 显式允许"
            )
    ordered_addresses = tuple(
        sorted(addresses, key=lambda address: (address.version != 4, address.compressed))
    )
    return raw, parsed, ordered_addresses


def validate_outbound_url(value: str, *, label: str = "URL") -> str:
    return _validated_outbound_target(value, label=label)[0]


def _request_authority(parsed: ParseResult) -> str:
    hostname = (parsed.hostname or "").rstrip(".")
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    return f"{host_text}:{parsed.port}" if parsed.port is not None else host_text


# 把域名解析一次后钉死到具体 IP 再建连接，TLS 仍按原域名校验。
# 没有这一层的话，SSRF 校验是 TOCTOU：validate 解析一次、requests 再解析一次，
# 一个短 TTL 的域名先返回公网 IP、再返回 127.0.0.1/169.254.169.254 就能绕过。
class PinnedDNSHTTPAdapter(HTTPAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._request_target = threading.local()

    def _target(self) -> tuple[str, str, int, str]:
        target = getattr(self._request_target, "value", None)
        if target is None:
            raise RequestException("出站请求缺少已验证的目标地址")
        return target

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if proxies and any(proxies.values()):
            raise RequestException("Web 出站请求不允许通过可能重新解析域名的代理")
        try:
            _, parsed, addresses = _validated_outbound_target(request.url)
        except ValueError as exc:
            raise RequestException(str(exc)) from exc
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        request.headers["Host"] = _request_authority(parsed)
        self._request_target.value = (
            parsed.scheme.lower(),
            hostname,
            port,
            addresses[0].compressed,
        )
        try:
            return super().send(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )
        finally:
            self._request_target.value = None

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        scheme, hostname, port, address = self._target()
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request,
            verify,
            cert,
        )
        host_params.update({"scheme": scheme, "host": address, "port": port})
        if scheme == "https":
            pool_kwargs["server_hostname"] = hostname
            pool_kwargs["assert_hostname"] = hostname
        return self.poolmanager.connection_from_host(
            **host_params,
            pool_kwargs=pool_kwargs,
        )

    def get_connection(self, url, proxies=None):  # pragma: no cover - requests < 2.32
        scheme, hostname, port, address = self._target()
        pool_kwargs = {}
        if scheme == "https":
            pool_kwargs.update(
                server_hostname=hostname,
                assert_hostname=hostname,
            )
        return self.poolmanager.connection_from_host(
            address,
            port=port,
            scheme=scheme,
            pool_kwargs=pool_kwargs,
        )


def _url_origin(value: str) -> tuple[str, str, int]:
    parsed = urlparse(value)
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip("."), parsed.port or default_port


_SESSION_LOCAL = threading.local()


def outbound_session() -> requests.Session:
    """每线程复用一个挂了 DNS 钉住 adapter 的 Session。

    复用的作用：视频轮询会对同一主机发起几十次请求，而 requests.request() 每次都会
    新建并丢弃一个 Session，也就是每次都重做一遍 TLS 握手。Session 不保证线程安全，
    所以按线程隔离。

    adapter 必须是 PinnedDNSHTTPAdapter：普通 adapter 会让 requests 重新解析域名，
    使 SSRF 校验退化成 TOCTOU。trust_env=False 则确保出站不会被环境代理改道。
    """
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        # 重定向由 safe_http_request 自己按 SSRF 规则逐跳校验，这里不放行。
        session.mount("https://", PinnedDNSHTTPAdapter())
        session.mount("http://", PinnedDNSHTTPAdapter())
        _SESSION_LOCAL.session = session
    return session


def safe_http_request(method: str, url: str, *, allow_cross_origin: bool = False, **kwargs) -> requests.Response:
    current_method = method.upper()
    current_url = validate_outbound_url(url)
    request_kwargs = dict(kwargs)
    request_kwargs["allow_redirects"] = False
    session = outbound_session()

    for redirect_count in range(MAX_REDIRECTS + 1):
        response = session.request(current_method, current_url, **request_kwargs)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response

        location = response.headers.get("Location", "").strip()
        if not location:
            return response
        if redirect_count >= MAX_REDIRECTS:
            response.close()
            raise RequestException(f"重定向次数超过 {MAX_REDIRECTS} 次")

        target = urljoin(current_url, location)
        try:
            target = validate_outbound_url(target, label="重定向 URL")
        except ValueError as exc:
            response.close()
            raise RequestException(str(exc)) from exc
        if not allow_cross_origin and _url_origin(target) != _url_origin(current_url):
            response.close()
            raise RequestException("拒绝向跨源重定向目标转发 API 凭据")

        response.close()
        if response.status_code == 303 or (response.status_code in {301, 302} and current_method == "POST"):
            current_method = "GET"
            for key in ("json", "data", "files"):
                request_kwargs.pop(key, None)
        current_url = target

    raise RequestException("重定向处理失败")


def compact_raw_response(data: dict) -> dict:
    try:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return {"omitted": True, "reason": "unserializable"}
    if len(encoded) <= MAX_RETAINED_RAW_BYTES:
        return data

    def small_value(value) -> bool:
        try:
            return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= 4 * 1024
        except (TypeError, ValueError):
            return False

    compact: dict = {
        key: value
        for key, value in data.items()
        if key not in {"data", "b64_json", "result"} and small_value(value)
    }
    items = data.get("data")
    if isinstance(items, list):
        compact["data"] = [
            {
                key: value
                for key, value in item.items()
                if key not in {"b64_json", "result"} and small_value(value)
            }
            for item in items[:MAX_UPSTREAM_MEDIA_ITEMS]
            if isinstance(item, dict)
        ]
    compact["_omitted_large_fields"] = True
    return compact


def compact_gemini_raw_response(data: dict) -> dict:
    """Keep Gemini diagnostics while never retaining returned image bytes."""

    def redact(value, parent_key: str = ""):
        if isinstance(value, dict):
            return {
                key: "<omitted>"
                if key == "data" and parent_key in {"inlineData", "inline_data"}
                else redact(item, key)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact(item, parent_key) for item in value]
        return value

    sanitized = redact(data)
    return compact_raw_response(sanitized if isinstance(sanitized, dict) else {})


def resolve_image_protocol(protocol: str, model: str, base_url: str) -> str:
    value = str(protocol or "auto").strip().lower()
    if value not in PROTOCOL_VALUES:
        raise ValueError("protocol 必须是 auto、openai、grok 或 gemini")
    if value in {"openai", "grok", "gemini"}:
        return value
    base = str(base_url or "").lower()
    if is_grok_image_model(model) or is_grok_video_model(model) or "api.x.ai" in base or "x.ai" in base:
        return "grok"
    if str(model or "").lower().startswith("gemini-") or "generativelanguage.googleapis.com" in base:
        return "gemini"
    return "openai"


def gemini_generate_endpoint(base_url: str, model: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    model_name = str(model or "").strip()
    if not base:
        raise ValueError("Base URL 不能为空")
    if not model_name:
        raise ValueError("Gemini 模型名称不能为空")
    if base.endswith(":generateContent"):
        return base

    if model_name.startswith("models/"):
        model_name = model_name[7:]
    encoded_model = quote(model_name, safe="-._~")
    parsed = urlparse(base)
    last_segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if last_segment not in {"v1", "v1beta", "v1alpha"}:
        base += "/v1beta"
    return f"{base}/models/{encoded_model}:generateContent"


def data_url_from_bytes(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def data_url_payload_size(value: str) -> int:
    parts = _data_url_parts(value)
    if parts is None:
        return 0
    encoded = parts[1]
    padding = len(encoded) - len(encoded.rstrip("="))
    return max(0, (len(encoded) * 3) // 4 - padding)


def image_mime_from_format(output_format: str) -> str:
    value = str(output_format or "").strip().lower()
    if value in {"jpg", "jpeg"}:
        return "image/jpeg"
    if value == "webp":
        return "image/webp"
    if value == "gif":
        return "image/gif"
    return "image/png"


def data_url_from_remote(image_url: str, timeout: int) -> str:
    response = safe_http_request(
        "GET",
        image_url,
        timeout=timeout,
        stream=True,
        allow_cross_origin=True,
    )
    try:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip() or None
        if content_type and not content_type.lower().startswith("image/"):
            raise ValueError("远程媒体不是图片")
        if not content_type:
            guessed, _ = mimetypes.guess_type(urlparse(image_url).path)
            content_type = guessed or "image/png"
        if not content_type.lower().startswith("image/"):
            raise ValueError("远程媒体不是图片")
        return data_url_from_bytes(read_limited_response(response, MAX_REMOTE_MEDIA_BYTES), content_type)
    finally:
        response.close()


def stream_image_data_url(value: str, output_format: str, timeout: int) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("图像数据为空")
    if raw.startswith("data:"):
        parts = _data_url_parts(raw)
        if parts is None:
            raise ValueError("图像 data URL 格式不正确")
        mime, encoded = parts
        if not mime.startswith("image/"):
            raise ValueError("图像 data URL 的 Content-Type 必须是 image/*")
        decode_base64_limited(encoded, MAX_REMOTE_MEDIA_BYTES, "图像数据")
        return raw
    if raw.startswith(("http://", "https://")):
        try:
            return data_url_from_remote(raw, timeout=timeout)
        except RequestException:
            return raw
    mime = image_mime_from_format(output_format)
    return data_url_from_bytes(decode_base64_limited(raw, MAX_REMOTE_MEDIA_BYTES, "图像数据"), mime)


def image_item_to_data_url(item: dict, timeout: int) -> str:
    if not isinstance(item, dict):
        raise ValueError("图像项格式不正确")
    if item.get("b64_json"):
        output_format = str(item.get("output_format", "")).strip()
        image_bytes = decode_base64_limited(str(item["b64_json"]), MAX_REMOTE_MEDIA_BYTES, "上游图像")
        return data_url_from_bytes(image_bytes, image_mime_from_format(output_format))
    if item.get("url"):
        image_url = str(item["url"])
        try:
            return data_url_from_remote(image_url, timeout=timeout)
        except RequestException:
            return image_url
    if item.get("result"):
        return stream_image_data_url(
            str(item["result"]),
            str(item.get("output_format", "")).strip(),
            timeout,
        )
    raise ValueError("返回里没有可用的图像字段")


def iter_sse_messages(response: requests.Response):
    event_name = "message"
    data_lines: list[str] = []
    data_size = 0

    for raw_line in response.iter_lines(decode_unicode=True):
        line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="replace")
        if len(line.encode("utf-8")) > MAX_SSE_EVENT_BYTES:
            raise ValueError("上游 SSE 事件超过大小限制")
        line = line.rstrip("\r")
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = "message"
            data_lines = []
            data_size = 0
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
            continue
        if line.startswith("data:"):
            data_line = line[5:].lstrip()
            data_size += len(data_line.encode("utf-8"))
            if data_size > MAX_SSE_EVENT_BYTES:
                raise ValueError("上游 SSE 事件超过大小限制")
            data_lines.append(data_line)

    if data_lines:
        yield event_name, "\n".join(data_lines)


def parse_image_api_response(data: dict, timeout: int, client_request_id: str, last_endpoint: str) -> list[str]:
    items = data.get("data")
    if not isinstance(items, list) or not items:
        raise RuntimeError(
            json.dumps(
                {
                    "message": "接口返回异常，未找到 data",
                    "client_request_id": client_request_id,
                    "endpoint": last_endpoint,
                    "raw": compact_raw_response(data),
                },
                ensure_ascii=False,
            )
        )

    images: list[str] = []
    total_media_bytes = 0
    for item in items[:MAX_UPSTREAM_MEDIA_ITEMS]:
        if not isinstance(item, dict):
            continue
        try:
            image = image_item_to_data_url(item, timeout=timeout)
        except ValueError:
            continue
        total_media_bytes += data_url_payload_size(image)
        if total_media_bytes > MAX_GENERATED_MEDIA_BYTES:
            raise ValueError(f"生成结果总大小超过 {MAX_GENERATED_MEDIA_BYTES} 字节限制")
        images.append(image)

    if not images:
        raise RuntimeError(
            json.dumps(
                {
                    "message": "返回里没有可保存的 b64_json、url 或 result",
                    "client_request_id": client_request_id,
                    "endpoint": last_endpoint,
                    "raw": compact_raw_response(data),
                },
                ensure_ascii=False,
            )
        )
    return images


def request_image_api(
    base_url: str,
    api_key: str,
    timeout: int,
    route: str,
    *,
    json_body: dict | None = None,
    form_data: dict | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    should_cancel: CancelCheck = None,
) -> dict:
    endpoints = candidate_endpoints(base_url, route)
    if not endpoints:
        raise ValueError("Base URL 不能为空")
    if not api_key:
        raise ValueError("API Key 不能为空")

    last_response: requests.Response | None = None
    last_endpoint = ""
    diagnostics: list[str] = []
    response: requests.Response | None = None
    client_request_id = str(uuid.uuid4())
    started_at = time.time()

    for endpoint in endpoints:
        raise_if_cancelled(should_cancel)
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "X-Client-Request-Id": client_request_id,
            }
            request_kwargs = {
                "headers": headers,
                "timeout": timeout,
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"
                request_kwargs["json"] = json_body
            else:
                request_kwargs["data"] = form_data or {}
                request_kwargs["files"] = files or []

            current = safe_http_request("POST", endpoint, stream=True, **request_kwargs)
        except RequestException as exc:
            diagnostics.append(exception_diagnostic(endpoint, exc))
            last_endpoint = endpoint
            continue

        content_type = current.headers.get("Content-Type", "")
        if current.ok and "text/html" not in content_type.lower():
            response = current
            last_endpoint = endpoint
            break

        diagnostics.append(response_diagnostic(endpoint, current))
        last_response = current
        last_endpoint = endpoint
        if likely_gateway_or_upstream_error(current) or likely_non_retryable_client_error(current):
            break

    if response is None:
        error_message = "请求失败，未找到可用的 OpenAI 图片接口。"
        if last_response is not None:
            error_message = preview_response_body(last_response) or error_message
        raise RuntimeError(
            json.dumps(
                {
                    "message": error_message,
                    "last_endpoint": last_endpoint,
                    "client_request_id": client_request_id,
                    "diagnostics": diagnostics,
                },
                ensure_ascii=False,
            )
        )

    data = response_json_limited(response)
    images = parse_image_api_response(data, timeout, client_request_id, last_endpoint)
    duration_ms = int((time.time() - started_at) * 1000)
    return {
        "ok": True,
        "media_type": "image",
        "endpoint": last_endpoint,
        "client_request_id": client_request_id,
        "duration_ms": duration_ms,
        "images": images,
        "media": [{"type": "image", "url": image} for image in images],
        "raw": compact_raw_response(data),
    }


def build_gemini_request_body(payload: dict, prompt: str) -> dict:
    aspect_ratio = payload_text(payload, "aspect_ratio") or "1:1"
    image_size = payload_text(payload, "resolution").upper() or "1K"
    if aspect_ratio not in GEMINI_ASPECT_RATIOS:
        choices = ", ".join(sorted(GEMINI_ASPECT_RATIOS))
        raise ValueError(f"Gemini aspect_ratio={aspect_ratio!r} 不支持，可选值: {choices}")
    if image_size not in GEMINI_IMAGE_SIZES:
        raise ValueError("Gemini resolution 必须是 1K、2K 或 4K")

    parts: list[dict] = [{"text": prompt}]
    if payload_text_lower(payload, "mode", "generate") == "edit":
        for _name, image_bytes, content_type in decode_edit_image_payloads(payload):
            parts.append(
                {
                    "inlineData": {
                        "mimeType": content_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            )

    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect_ratio,
                "imageSize": image_size,
            },
        },
    }


def parse_gemini_image_response(
    data: dict,
    client_request_id: str,
    endpoint: str,
) -> list[str]:
    candidates = data.get("candidates")
    images: list[str] = []
    total_media_bytes = 0
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                inline_data = part.get("inlineData") or part.get("inline_data")
                if not isinstance(inline_data, dict):
                    continue
                mime = str(inline_data.get("mimeType") or inline_data.get("mime_type") or "").strip().lower()
                if not mime.startswith("image/"):
                    continue
                encoded = str(inline_data.get("data") or "")
                image_bytes = decode_base64_limited(encoded, MAX_REMOTE_MEDIA_BYTES, "Gemini 上游图像")
                total_media_bytes += len(image_bytes)
                if total_media_bytes > MAX_GENERATED_MEDIA_BYTES:
                    raise ValueError(f"生成结果总大小超过 {MAX_GENERATED_MEDIA_BYTES} 字节限制")
                images.append(data_url_from_bytes(image_bytes, mime))
                if len(images) >= MAX_UPSTREAM_MEDIA_ITEMS:
                    return images

    if not images:
        raise RuntimeError(
            json.dumps(
                {
                    "message": "Gemini 返回中没有可用的图片，可能被安全策略拦截或模型不支持生图",
                    "client_request_id": client_request_id,
                    "endpoint": endpoint,
                    "raw": compact_gemini_raw_response(data),
                },
                ensure_ascii=False,
            )
        )
    return images


def request_gemini_image_api(
    base_url: str,
    api_key: str,
    timeout: int,
    model: str,
    *,
    json_body: dict,
    count: int,
    should_cancel: CancelCheck = None,
) -> dict:
    if not api_key:
        raise ValueError("API Key 不能为空")
    endpoint = gemini_generate_endpoint(base_url, model)
    client_request_id = str(uuid.uuid4())
    started_at = time.time()
    images: list[str] = []
    raw_results: list[dict] = []
    total_media_bytes = 0

    for index in range(count):
        raise_if_cancelled(should_cancel)
        request_id = f"{client_request_id}-{index + 1}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "x-goog-api-key": api_key,
            "X-Client-Request-Id": request_id,
            "Content-Type": "application/json",
        }
        try:
            response = safe_http_request(
                "POST",
                endpoint,
                headers=headers,
                json=json_body,
                timeout=timeout,
                stream=True,
            )
        except RequestException as exc:
            raise RuntimeError(
                json.dumps(
                    {
                        "message": "Gemini 图片请求失败",
                        "last_endpoint": endpoint,
                        "client_request_id": client_request_id,
                        "diagnostics": [exception_diagnostic(endpoint, exc)],
                    },
                    ensure_ascii=False,
                )
            ) from exc

        if not response.ok or "text/html" in response.headers.get("Content-Type", "").lower():
            diagnostic = response_diagnostic(endpoint, response)
            message = preview_response_body(response) or "Gemini 图片请求失败"
            response.close()
            raise RuntimeError(
                json.dumps(
                    {
                        "message": message,
                        "last_endpoint": endpoint,
                        "client_request_id": client_request_id,
                        "diagnostics": [diagnostic],
                    },
                    ensure_ascii=False,
                )
            )

        try:
            data = response_json_limited(response)
        finally:
            response.close()
        call_images = parse_gemini_image_response(data, request_id, endpoint)
        image = call_images[0]
        total_media_bytes += data_url_payload_size(image)
        if total_media_bytes > MAX_GENERATED_MEDIA_BYTES:
            raise ValueError(f"生成结果总大小超过 {MAX_GENERATED_MEDIA_BYTES} 字节限制")
        images.append(image)
        raw_results.append(compact_gemini_raw_response(data))

    duration_ms = int((time.time() - started_at) * 1000)
    return {
        "ok": True,
        "media_type": "image",
        "endpoint": endpoint,
        "client_request_id": client_request_id,
        "duration_ms": duration_ms,
        "images": images,
        "media": [{"type": "image", "url": image} for image in images],
        "raw": raw_results[0] if len(raw_results) == 1 else raw_results,
    }


def request_image_stream(
    base_url: str,
    api_key: str,
    timeout: int,
    route: str,
    *,
    json_body: dict | None = None,
    form_data: dict | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
) -> tuple[requests.Response, str, str]:
    endpoints = candidate_endpoints(base_url, route)
    if not endpoints:
        raise ValueError("Base URL 不能为空")
    if not api_key:
        raise ValueError("API Key 不能为空")

    last_response: requests.Response | None = None
    last_endpoint = ""
    diagnostics: list[str] = []
    client_request_id = str(uuid.uuid4())

    for endpoint in endpoints:
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "X-Client-Request-Id": client_request_id,
                "Accept": "text/event-stream",
            }
            request_kwargs = {
                "headers": headers,
                "timeout": timeout,
                "stream": True,
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"
                request_kwargs["json"] = json_body
            else:
                request_kwargs["data"] = form_data or {}
                request_kwargs["files"] = files or []

            current = safe_http_request("POST", endpoint, **request_kwargs)
        except RequestException as exc:
            diagnostics.append(exception_diagnostic(endpoint, exc))
            last_endpoint = endpoint
            continue

        content_type = current.headers.get("Content-Type", "")
        if current.ok and "text/html" not in content_type.lower():
            return current, endpoint, client_request_id

        diagnostics.append(response_diagnostic(endpoint, current))
        last_response = current
        last_endpoint = endpoint
        current.close()
        if likely_gateway_or_upstream_error(current) or likely_non_retryable_client_error(current):
            break

    error_message = "请求失败，未找到可用的 OpenAI 图片接口。"
    if last_response is not None:
        error_message = preview_response_body(last_response) or error_message
    raise RuntimeError(
        json.dumps(
            {
                "message": error_message,
                "last_endpoint": last_endpoint,
                "client_request_id": client_request_id,
                "diagnostics": diagnostics,
            },
            ensure_ascii=False,
        )
    )


def extract_video_urls(data: dict) -> list[str]:
    videos: list[str] = []

    video = data.get("video")
    if isinstance(video, dict) and video.get("url"):
        videos.append(str(video["url"]))

    for key in ("videos", "data"):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("url"):
                videos.append(str(item["url"]))
                if len(videos) >= MAX_UPSTREAM_MEDIA_ITEMS:
                    break

    if data.get("url"):
        videos.append(str(data["url"]))

    return list(dict.fromkeys(videos))[:MAX_UPSTREAM_MEDIA_ITEMS]


def validate_video_result_urls(videos: list[str]) -> list[str]:
    return [validate_outbound_url(url, label="视频结果 URL") for url in dict.fromkeys(videos)]


def request_grok_video_api(
    base_url: str,
    api_key: str,
    timeout: int,
    *,
    json_body: dict,
    count: int = 1,
    poll_interval: int = 5,
    should_cancel: CancelCheck = None,
) -> dict:
    endpoints = candidate_endpoints(base_url, "videos/generations")
    if not endpoints:
        raise ValueError("Base URL 不能为空")
    if not api_key:
        raise ValueError("API Key 不能为空")

    started_at = time.time()
    videos: list[str] = []
    raw_results: list[dict] = []
    last_endpoint = ""
    client_request_id = str(uuid.uuid4())
    poll_delay = min(30, max(1, int(poll_interval or 5)))

    for _ in range(count):
        videos_before_task = len(videos)
        last_response: requests.Response | None = None
        diagnostics: list[str] = []
        request_id = ""
        task_completed = False

        for endpoint in endpoints:
            raise_if_cancelled(should_cancel)
            try:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "X-Client-Request-Id": client_request_id,
                    "Content-Type": "application/json",
                }
                current = safe_http_request(
                    "POST",
                    endpoint,
                    headers=headers,
                    json=json_body,
                    timeout=timeout,
                    stream=True,
                )
            except RequestException as exc:
                diagnostics.append(exception_diagnostic(endpoint, exc))
                last_endpoint = endpoint
                continue

            content_type = current.headers.get("Content-Type", "")
            if current.ok and "text/html" not in content_type.lower():
                last_endpoint = endpoint
                try:
                    data = response_json_limited(current)
                except ValueError:
                    diagnostics.append(response_diagnostic(endpoint, current))
                    last_response = current
                    continue

                request_id = str(data.get("request_id") or data.get("id") or "").strip()
                direct_videos = extract_video_urls(data)
                if direct_videos:
                    videos.extend(direct_videos)
                    raw_results.append(compact_raw_response(data))
                    task_completed = True
                    break
                if not request_id:
                    diagnostics.append(f"- {endpoint} -> 返回里没有 request_id")
                    last_response = current
                    continue

                poll_endpoint = video_poll_endpoint_from_generation(endpoint, request_id)
                deadline = time.time() + timeout
                while True:
                    if time.time() >= deadline:
                        raise RuntimeError(
                            json.dumps(
                                {
                                    "message": "视频任务超时",
                                    "last_endpoint": poll_endpoint,
                                    "client_request_id": client_request_id,
                                    "upstream_request_id": request_id,
                                    "diagnostics": diagnostics,
                                },
                                ensure_ascii=False,
                            )
                        )
                    cancellable_sleep(poll_delay, should_cancel)
                    try:
                        polled = safe_http_request(
                            "GET",
                            poll_endpoint,
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "X-Client-Request-Id": client_request_id,
                            },
                            timeout=min(30, max(5, timeout)),
                            stream=True,
                        )
                    except RequestException as exc:
                        diagnostics.append(exception_diagnostic(poll_endpoint, exc))
                        continue

                    if not polled.ok:
                        raise RuntimeError(
                            json.dumps(
                                {
                                    "message": preview_response_body(polled) or "视频任务轮询失败",
                                    "last_endpoint": poll_endpoint,
                                    "client_request_id": client_request_id,
                                    "upstream_request_id": request_id,
                                    "diagnostics": [response_diagnostic(poll_endpoint, polled)],
                                },
                                ensure_ascii=False,
                            )
                        )

                    poll_data = response_json_limited(polled)
                    status = str(poll_data.get("status", "")).strip().lower()
                    poll_videos = extract_video_urls(poll_data)
                    if status == "done" and not poll_videos:
                        raise RuntimeError(
                            json.dumps(
                                {
                                    "message": "视频任务完成但没有返回 video.url",
                                    "last_endpoint": poll_endpoint,
                                    "client_request_id": client_request_id,
                                    "upstream_request_id": request_id,
                                },
                                ensure_ascii=False,
                            )
                        )
                    if poll_videos:
                        poll_data.setdefault("request_id", request_id)
                        poll_data.setdefault("poll_endpoint", poll_endpoint)
                        videos.extend(poll_videos)
                        raw_results.append(compact_raw_response(poll_data))
                        task_completed = True
                        break
                    if status in {"failed", "error", "expired", "cancelled", "canceled"}:
                        raise RuntimeError(
                            json.dumps(
                                {
                                    "message": f"视频任务失败: {status or 'unknown'}",
                                    "last_endpoint": poll_endpoint,
                                    "client_request_id": client_request_id,
                                    "upstream_request_id": request_id,
                                    "raw": poll_data,
                                },
                                ensure_ascii=False,
                            )
                        )
                break

            diagnostics.append(response_diagnostic(endpoint, current))
            last_response = current
            last_endpoint = endpoint
            if likely_gateway_or_upstream_error(current) or likely_non_retryable_client_error(current):
                break

        if not task_completed:
            error_message = "请求失败，未找到可用的 Grok 视频接口。"
            if last_response is not None:
                error_message = preview_response_body(last_response) or error_message
            raise RuntimeError(
                json.dumps(
                    {
                        "message": error_message,
                        "last_endpoint": last_endpoint,
                        "client_request_id": client_request_id,
                        "upstream_request_id": request_id,
                        "diagnostics": diagnostics,
                    },
                    ensure_ascii=False,
                )
            )

        if len(videos) == videos_before_task:
            raise RuntimeError(
                json.dumps(
                    {
                        "message": "视频任务完成但没有返回 video.url",
                        "last_endpoint": last_endpoint,
                        "client_request_id": client_request_id,
                        "upstream_request_id": request_id,
                    },
                    ensure_ascii=False,
                )
            )

    if not videos:
        raise RuntimeError(
            json.dumps(
                {
                    "message": "视频任务完成但没有返回 video.url",
                    "last_endpoint": last_endpoint,
                    "client_request_id": client_request_id,
                    "raw": raw_results,
                },
                ensure_ascii=False,
            )
        )

    videos = validate_video_result_urls(videos)
    duration_ms = int((time.time() - started_at) * 1000)
    return {
        "ok": True,
        "media_type": "video",
        "endpoint": last_endpoint,
        "client_request_id": client_request_id,
        "duration_ms": duration_ms,
        "videos": videos,
        "images": [],
        "media": [{"type": "video", "url": url} for url in videos],
        "raw": raw_results[-1] if len(raw_results) == 1 else raw_results,
    }


def payload_text(payload: dict, key: str, default: str = "") -> str:
    value = payload.get(key, default)
    if value is None:
        return ""
    return str(value).strip()


def payload_text_lower(payload: dict, key: str, default: str = "") -> str:
    return payload_text(payload, key, default).lower()


def apply_openai_image_options(request_body: dict, payload: dict, model: str) -> None:
    quality = payload_text_lower(payload, "quality")
    output_format = payload_text_lower(payload, "output_format")
    background = payload_text_lower(payload, "background")
    moderation = payload_text_lower(payload, "moderation")
    output_compression = payload.get("output_compression")

    if quality:
        request_body["quality"] = quality
    if output_format:
        request_body["output_format"] = output_format
    if background:
        if is_gpt_image_2(model) and background == "transparent":
            raise ValueError("gpt-image-2 当前不支持 transparent 背景，请改为 auto 或 opaque")
        request_body["background"] = background
    if moderation:
        request_body["moderation"] = moderation
    if output_compression not in {None, ""} and output_format in {"jpeg", "webp"}:
        try:
            compression = int(output_compression)
        except (TypeError, ValueError) as exc:
            raise ValueError("output_compression 必须是 0 到 100 的整数") from exc
        if not 0 <= compression <= 100:
            raise ValueError("output_compression 必须是 0 到 100 的整数")
        request_body["output_compression"] = compression


def validate_grok_options(aspect_ratio: str, resolution: str, response_format: str) -> None:
    if aspect_ratio and aspect_ratio not in GROK_SUPPORTED_RATIOS:
        choices = ", ".join(sorted(GROK_SUPPORTED_RATIOS))
        raise ValueError(f"aspect_ratio={aspect_ratio!r} 不支持，可选值: {choices}")
    if resolution not in GROK_RESOLUTION_VALUES:
        raise ValueError("resolution 必须是 1k 或 2k")
    if response_format not in GROK_RESPONSE_FORMAT_VALUES:
        raise ValueError("response_format 必须是 url 或 b64_json")


def build_grok_generation_body(payload: dict, model: str, prompt: str, size: str, image_count: int) -> dict:
    aspect_ratio = payload_text_lower(payload, "aspect_ratio") or grok_aspect_ratio_from_size(size)
    resolution = payload_text_lower(payload, "resolution") or grok_resolution_from_size(size)
    response_format = payload_text_lower(payload, "response_format")
    validate_grok_options(aspect_ratio, resolution, response_format)

    request_body: dict = {
        "model": model,
        "prompt": prompt,
        "n": image_count,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    if response_format:
        request_body["response_format"] = response_format
    return request_body


def build_grok_image_refs(payload: dict) -> list[dict]:
    images = get_edit_image_payloads(payload)
    if len(images) > 3:
        raise ValueError("grok-imagine-image 图生图最多支持 3 张参考图")

    refs: list[dict] = []
    for index, image in enumerate(images, start=1):
        image_url = str(image.get("data_url") or image.get("dataUrl") or image.get("url") or "").strip()
        if not image_url:
            raise ValueError(f"第 {index} 张参考图缺少 data_url")
        refs.append({"type": "image_url", "url": image_url})
    return refs


def build_grok_edit_body(payload: dict, model: str, prompt: str, size: str, image_count: int) -> dict:
    refs = build_grok_image_refs(payload)
    explicit_aspect_ratio = bool(payload_text(payload, "aspect_ratio"))
    explicit_resolution = bool(payload_text(payload, "resolution"))
    aspect_ratio = payload_text_lower(payload, "aspect_ratio") or grok_aspect_ratio_from_size(size)
    resolution = payload_text_lower(payload, "resolution") or grok_resolution_from_size(size)
    response_format = payload_text_lower(payload, "response_format")
    validate_grok_options(aspect_ratio, resolution, response_format)

    request_body: dict = {
        "model": model,
        "prompt": prompt,
        "n": image_count,
    }
    if len(refs) == 1:
        request_body["image"] = refs[0]
    else:
        request_body["images"] = refs
    if len(refs) > 1 or explicit_aspect_ratio:
        request_body["aspect_ratio"] = aspect_ratio
    if len(refs) > 1 or explicit_resolution:
        request_body["resolution"] = resolution
    if response_format:
        request_body["response_format"] = response_format
    return request_body


def validate_grok_video_options(aspect_ratio: str, resolution: str, duration: int) -> None:
    if aspect_ratio and aspect_ratio not in GROK_VIDEO_SUPPORTED_RATIOS:
        choices = ", ".join(sorted(GROK_VIDEO_SUPPORTED_RATIOS))
        raise ValueError(f"aspect_ratio={aspect_ratio!r} 不支持，可选值: {choices}")
    if resolution not in GROK_VIDEO_RESOLUTION_VALUES:
        raise ValueError("视频 resolution 必须是 480p、720p 或 1080p")
    if duration and not 1 <= duration <= 15:
        raise ValueError("duration 必须在 1 到 15 秒之间")


def payload_int(payload: dict, key: str, default: int = 0) -> int:
    value = payload.get(key, default)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数") from exc


def bounded_payload_int(payload: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = payload_int(payload, key, default)
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return value


def build_grok_video_body(payload: dict, model: str, prompt: str, size: str) -> dict:
    aspect_ratio = payload_text_lower(payload, "aspect_ratio") or grok_video_aspect_ratio_from_size(size)
    resolution = payload_text_lower(payload, "video_resolution") or payload_text_lower(payload, "resolution") or grok_video_resolution_from_size(size)
    duration = payload_int(payload, "duration", 0)
    validate_grok_video_options(aspect_ratio, resolution, duration)

    request_body: dict = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    if duration:
        request_body["duration"] = duration

    refs: list[dict] = []
    if str(payload.get("mode", "generate")).strip().lower() == "edit":
        images = get_edit_image_payloads(payload)
        if len(images) > MAX_GROK_VIDEO_REFERENCE_IMAGES:
            raise ValueError(f"grok-imagine-video 最多支持 {MAX_GROK_VIDEO_REFERENCE_IMAGES} 张参考图")
        for index, image in enumerate(images, start=1):
            image_url = str(image.get("data_url") or image.get("dataUrl") or image.get("url") or "").strip()
            if not image_url:
                raise ValueError(f"第 {index} 张参考图缺少 data_url")
            refs.append({"url": image_url})

    if len(refs) == 1:
        request_body["image"] = refs[0]
    elif len(refs) > 1:
        if duration and duration > 10:
            raise ValueError("多参考图视频的 duration 最大为 10 秒")
        request_body["reference_images"] = refs
    return request_body


def is_video_payload(payload: dict, model: str | None = None) -> bool:
    mode = str(payload.get("mode", "generate")).strip().lower() or "generate"
    value = str(model if model is not None else payload.get("model", "")).strip()
    return mode == "video" or is_grok_video_model(value)


def generate_videos(payload: dict, *, should_cancel: CancelCheck = None) -> dict:
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    raw_model = str(payload.get("model", "")).strip()
    protocol = resolve_image_protocol(payload_text(payload, "protocol", "auto"), raw_model, base_url)
    model = raw_model or "grok-imagine-video"
    size = str(payload.get("size", "1920x1072")).strip() or "1920x1072"
    timeout = bounded_payload_int(payload, "timeout", DEFAULT_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    video_count = bounded_payload_int(payload, "n", 1, 1, MAX_VIDEO_COUNT)
    poll_interval = bounded_payload_int(payload, "poll_interval", 5, 1, 30)

    if protocol != "grok":
        raise ValueError("grok-imagine-video 只能使用 grok 协议")
    if not prompt:
        raise ValueError("提示词不能为空")
    request_body = build_grok_video_body(payload, model, prompt, size)
    return request_grok_video_api(
        base_url,
        api_key,
        timeout,
        json_body=request_body,
        count=video_count,
        poll_interval=poll_interval,
        should_cancel=should_cancel,
    )


def generate_images(payload: dict, *, should_cancel: CancelCheck = None) -> dict:
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    raw_model = str(payload.get("model", "")).strip()
    protocol = resolve_image_protocol(payload_text(payload, "protocol", "auto"), raw_model, base_url)
    model = raw_model or ("grok-imagine-image" if protocol == "grok" else "" if protocol == "gemini" else DEFAULT_IMAGE_MODEL)
    size = str(payload.get("size", "1024x1024")).strip() or "1024x1024"
    timeout = bounded_payload_int(payload, "timeout", DEFAULT_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    image_count = bounded_payload_int(payload, "n", 1, 1, MAX_IMAGE_COUNT)

    if not prompt:
        raise ValueError("提示词不能为空")
    if protocol == "gemini":
        request_body = build_gemini_request_body(payload, prompt)
        return request_gemini_image_api(
            base_url,
            api_key,
            timeout,
            model,
            json_body=request_body,
            count=image_count,
            should_cancel=should_cancel,
        )
    if protocol == "grok":
        request_body = build_grok_generation_body(payload, model, prompt, size, image_count)
    else:
        request_body = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": image_count,
        }
        apply_openai_image_options(request_body, payload, model)

    return request_image_api(
        base_url,
        api_key,
        timeout,
        "images/generations",
        json_body=request_body,
        should_cancel=should_cancel,
    )


def decode_data_url_image(data_url: str, fallback_content_type: str = "image/png") -> tuple[bytes, str]:
    value = (data_url or "").strip()
    if not value:
        raise ValueError("参考图不能为空")

    content_type = fallback_content_type or "image/png"
    encoded = value
    if value.startswith("data:"):
        header, _, encoded = value.partition(",")
        if not encoded:
            raise ValueError("参考图数据格式不正确")
        if ";base64" not in header:
            raise ValueError("参考图必须是 base64 data URL")
        detected = header[5:].split(";", 1)[0].strip()
        if detected:
            content_type = detected

    if not content_type.lower().startswith("image/"):
        raise ValueError("参考图 Content-Type 必须是 image/*")
    return decode_base64_limited(encoded, MAX_REFERENCE_IMAGE_BYTES, "参考图"), content_type


def normalize_edit_model(model: str) -> str:
    return model


def edit_image_file_field(model: str, image_count: int) -> str:
    if is_gpt_image_2(model) or image_count > 1:
        return OPENAI_IMAGE_EDIT_ARRAY_FIELD
    return OPENAI_IMAGE_EDIT_FILE_FIELD


def get_edit_image_payloads(payload: dict) -> list[dict]:
    raw_images = payload.get("edit_images")
    if isinstance(raw_images, list):
        images = raw_images
    elif isinstance(raw_images, dict):
        images = [raw_images]
    elif isinstance(payload.get("edit_image"), dict):
        images = [payload["edit_image"]]
    else:
        images = []

    if not images:
        raise ValueError("图生图模式下请先上传参考图")
    if len(images) > MAX_EDIT_REFERENCE_IMAGES:
        raise ValueError(f"图生图最多支持 {MAX_EDIT_REFERENCE_IMAGES} 张参考图")

    normalized: list[dict] = []
    for index, image in enumerate(images, start=1):
        if not isinstance(image, dict):
            raise ValueError(f"第 {index} 张参考图格式不正确")
        normalized.append(image)
    return normalized


def decode_edit_image_payloads(payload: dict) -> list[tuple[str, bytes, str]]:
    decoded_images: list[tuple[str, bytes, str]] = []
    total_bytes = 0
    for index, edit_image in enumerate(get_edit_image_payloads(payload), start=1):
        image_name = str(edit_image.get("name", "")).strip() or f"reference-image-{index}.png"
        content_type = (
            str(edit_image.get("content_type") or edit_image.get("contentType") or "").strip()
            or "image/png"
        )
        image_data_url = str(edit_image.get("data_url") or edit_image.get("dataUrl") or "").strip()
        image_bytes, content_type = decode_data_url_image(image_data_url, content_type)
        total_bytes += len(image_bytes)
        if total_bytes > MAX_REFERENCE_IMAGES_BYTES:
            raise ValueError(f"参考图总大小超过 {MAX_REFERENCE_IMAGES_BYTES} 字节限制")
        decoded_images.append((image_name, image_bytes, content_type))
    return decoded_images


def build_edit_image_files(
    payload: dict,
    model: str,
) -> list[tuple[str, tuple[str, bytes, str]]]:
    decoded_images = decode_edit_image_payloads(payload)
    file_field = edit_image_file_field(model, len(decoded_images))
    return [
        (file_field, (image_name, image_bytes, content_type))
        for image_name, image_bytes, content_type in decoded_images
    ]


def edit_images(payload: dict, *, should_cancel: CancelCheck = None) -> dict:
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    raw_model = str(payload.get("model", "")).strip()
    protocol = resolve_image_protocol(payload_text(payload, "protocol", "auto"), raw_model, base_url)
    model = normalize_edit_model(
        raw_model or ("grok-imagine-image" if protocol == "grok" else "" if protocol == "gemini" else DEFAULT_IMAGE_MODEL)
    )
    size = str(payload.get("size", "1024x1024")).strip() or "1024x1024"
    timeout = bounded_payload_int(payload, "timeout", DEFAULT_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    image_count = bounded_payload_int(payload, "n", 1, 1, MAX_IMAGE_COUNT)

    if not prompt:
        raise ValueError("图生图模式下提示词不能为空")
    if protocol == "gemini":
        request_body = build_gemini_request_body(payload, prompt)
        return request_gemini_image_api(
            base_url,
            api_key,
            timeout,
            model,
            json_body=request_body,
            count=image_count,
            should_cancel=should_cancel,
        )
    if protocol == "grok":
        request_body = build_grok_edit_body(payload, model, prompt, size, image_count)
        return request_image_api(
            base_url,
            api_key,
            timeout,
            "images/edits",
            json_body=request_body,
            should_cancel=should_cancel,
        )

    request_data = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": str(image_count),
    }
    apply_openai_image_options(request_data, payload, model)
    request_data = {key: str(value) for key, value in request_data.items()}
    files = build_edit_image_files(payload, model)
    return request_image_api(
        base_url,
        api_key,
        timeout,
        "images/edits",
        form_data=request_data,
        files=files,
        should_cancel=should_cancel,
    )


def build_stream_request(payload: dict) -> tuple[str, str, int, str, dict | None, dict | None, list[tuple[str, tuple[str, bytes, str]]] | None]:
    mode = str(payload.get("mode", "generate")).strip().lower() or "generate"
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    raw_model = str(payload.get("model", "")).strip()
    if is_video_payload(payload, raw_model):
        raise ValueError("grok-imagine-video 是异步视频任务，请使用普通生成或异步生成接口")
    protocol = resolve_image_protocol(payload_text(payload, "protocol", "auto"), raw_model, base_url)
    model = raw_model or ("grok-imagine-image" if protocol == "grok" else "" if protocol == "gemini" else DEFAULT_IMAGE_MODEL)
    size = str(payload.get("size", "1024x1024")).strip() or "1024x1024"
    timeout = bounded_payload_int(payload, "timeout", DEFAULT_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    image_count = bounded_payload_int(payload, "n", 1, 1, MAX_IMAGE_COUNT)
    partial_images = bounded_payload_int(payload, "partial_images", DEFAULT_STREAM_PARTIAL_IMAGES, 1, MAX_PARTIAL_IMAGES)

    if not prompt:
        raise ValueError("图生图模式下提示词不能为空" if mode == "edit" else "提示词不能为空")
    if protocol == "gemini":
        raise ValueError("Gemini 生图使用异步任务接口，不支持 SSE 流式接口")
    if mode == "generate":
        if protocol == "grok":
            request_body = build_grok_generation_body(payload, model, prompt, size, image_count)
            return base_url, api_key, timeout, "images/generations", request_body, None, None

        request_body = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": image_count,
            "stream": True,
            "partial_images": partial_images,
        }
        apply_openai_image_options(request_body, payload, model)
        return base_url, api_key, timeout, "images/generations", request_body, None, None

    if mode != "edit":
        raise ValueError("不支持的模式")

    normalized_model = normalize_edit_model(model)
    if protocol == "grok":
        request_body = build_grok_edit_body(payload, normalized_model, prompt, size, image_count)
        return base_url, api_key, timeout, "images/edits", request_body, None, None

    request_data = {
        "model": normalized_model,
        "prompt": prompt,
        "size": size,
        "n": str(image_count),
        "stream": "true",
        "partial_images": str(partial_images),
    }
    apply_openai_image_options(request_data, payload, normalized_model)
    request_data = {key: str(value) for key, value in request_data.items()}
    files = build_edit_image_files(payload, normalized_model)
    return base_url, api_key, timeout, "images/edits", None, request_data, files


def process_request_payload(
    payload: dict,
    *,
    should_cancel: CancelCheck = None,
    already_validated: bool = False,
) -> dict:
    # 校验会把每张参考图完整 base64 解码一遍以统计大小（上限 30MB）。
    # 异步任务在入队时已经校验过，重复校验等于白解一次。
    if not already_validated:
        validate_request_payload(payload)
    mode = str(payload.get("mode", "generate")).strip().lower() or "generate"
    raw_model = str(payload.get("model", "")).strip()
    if is_video_payload(payload, raw_model):
        return generate_videos(payload, should_cancel=should_cancel)
    if mode == "edit":
        return edit_images(payload, should_cancel=should_cancel)
    if mode != "generate":
        raise ValueError("不支持的模式")
    return generate_images(payload, should_cancel=should_cancel)


def validate_request_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("请求体顶层必须是 JSON 对象")
    mode = str(payload.get("mode", "generate")).strip().lower() or "generate"
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    raw_model = str(payload.get("model", "")).strip()
    if not base_url:
        raise ValueError("Base URL 不能为空")
    validate_outbound_url(base_url, label="Base URL")
    if not api_key:
        raise ValueError("API Key 不能为空")
    if len(api_key) > MAX_API_KEY_CHARS:
        raise ValueError("API Key 过长")
    if not prompt:
        raise ValueError("提示词不能为空" if mode == "generate" else "图生图模式下提示词不能为空")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"提示词不能超过 {MAX_PROMPT_CHARS} 个字符")
    if len(raw_model) > MAX_MODEL_CHARS:
        raise ValueError(f"模型名不能超过 {MAX_MODEL_CHARS} 个字符")
    if len(str(payload.get("size", ""))) > 64:
        raise ValueError("size 过长")
    is_video = mode == "video" or is_grok_video_model(raw_model)
    bounded_payload_int(payload, "n", 1, 1, MAX_VIDEO_COUNT if is_video else MAX_IMAGE_COUNT)
    bounded_payload_int(payload, "timeout", DEFAULT_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    protocol = resolve_image_protocol(payload_text(payload, "protocol", "auto"), raw_model, base_url)
    if protocol == "gemini" and not raw_model:
        raise ValueError("Gemini 模型名称不能为空")
    if protocol == "gemini":
        aspect_ratio = payload_text(payload, "aspect_ratio") or "1:1"
        image_size = payload_text(payload, "resolution").upper() or "1K"
        if aspect_ratio not in GEMINI_ASPECT_RATIOS:
            raise ValueError("Gemini aspect_ratio 不支持")
        if image_size not in GEMINI_IMAGE_SIZES:
            raise ValueError("Gemini resolution 必须是 1K、2K 或 4K")
    if mode == "edit":
        images = get_edit_image_payloads(payload)
        total_bytes = 0
        for index, image in enumerate(images, start=1):
            value = str(image.get("data_url") or image.get("dataUrl") or image.get("url") or "").strip()
            if not value:
                raise ValueError(f"第 {index} 张参考图缺少 data_url")
            if value.startswith("data:"):
                image_bytes, _ = decode_data_url_image(
                    value,
                    str(image.get("content_type") or image.get("contentType") or "image/png"),
                )
                total_bytes += len(image_bytes)
                if total_bytes > MAX_REFERENCE_IMAGES_BYTES:
                    raise ValueError(f"参考图总大小超过 {MAX_REFERENCE_IMAGES_BYTES} 字节限制")
            elif len(value) > MAX_BASE_URL_CHARS or not value.startswith(("http://", "https://")):
                raise ValueError(f"第 {index} 张参考图必须是图片 data URL 或 http(s) URL")
    if mode not in {"generate", "edit", "video"}:
        raise ValueError("不支持的模式")
    if is_video:
        bounded_payload_int(payload, "poll_interval", 5, 1, 30)
        payload_int(payload, "duration", 0)
    if "partial_images" in payload:
        bounded_payload_int(payload, "partial_images", DEFAULT_STREAM_PARTIAL_IMAGES, 1, MAX_PARTIAL_IMAGES)


def _asset_directory(job_id: str) -> Path:
    return ASSET_ROOT / job_id


def _remove_job_assets(job_id: str) -> None:
    shutil.rmtree(_asset_directory(job_id), ignore_errors=True)


def _data_url_parts(value: str) -> tuple[str, str] | None:
    if not value.startswith("data:"):
        return None
    header, separator, encoded = value.partition(",")
    if not separator or ";base64" not in header:
        raise ValueError("生成结果包含不合法的 data URL")
    mime = header[5:].split(";", 1)[0].strip().lower() or "application/octet-stream"
    return mime, encoded


def materialize_job_result(job_id: str, result: dict) -> dict:
    stored = dict(result)
    images = list(result.get("images") or [])
    materialized_images: list[str] = []
    asset_dir: Path | None = None

    for index, image in enumerate(images, start=1):
        value = str(image)
        parts = _data_url_parts(value)
        if not parts:
            materialized_images.append(value)
            continue
        mime, encoded = parts
        if not mime.startswith("image/"):
            raise ValueError("生成结果 data URL 不是图片")
        image_bytes = decode_base64_limited(encoded, MAX_REMOTE_MEDIA_BYTES, "生成结果")
        if asset_dir is None:
            asset_dir = _asset_directory(job_id)
            asset_dir.mkdir(parents=True, exist_ok=True)
        suffix = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".png")
        filename = f"image-{index}{suffix}"
        (asset_dir / filename).write_bytes(image_bytes)
        materialized_images.append(f"/api/assets/{job_id}/{filename}")

    stored["images"] = materialized_images
    videos = [str(value) for value in result.get("videos") or []]
    stored["media"] = [
        *({"type": "image", "url": value} for value in materialized_images),
        *({"type": "video", "url": value} for value in videos),
    ]
    return stored


def cleanup_jobs(now: float | None = None) -> None:
    current = time.time() if now is None else now
    expired_ids: list[str] = []
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.get("status") not in TERMINAL_JOB_STATUSES and current >= float(job.get("deadline_at") or float("inf")):
                job["status"] = "error"
                job["updated_at"] = current
                job["error"] = {"message": "任务超过最大运行时间", "code": "job_deadline_exceeded"}

        expired_ids = [
            job_id
            for job_id, job in JOBS.items()
            if job.get("status") in TERMINAL_JOB_STATUSES
            and current - float(job.get("updated_at") or job.get("created_at") or current) > JOB_TTL_SECONDS
        ]
        for job_id in expired_ids:
            JOBS.pop(job_id, None)

    for job_id in expired_ids:
        _remove_job_assets(job_id)


def create_async_job(payload: dict) -> str:
    cleanup_jobs()
    job_id = str(uuid.uuid4())
    now = time.time()
    requested_timeout = bounded_payload_int(
        payload,
        "timeout",
        DEFAULT_TIMEOUT_SECONDS,
        MIN_TIMEOUT_SECONDS,
        MAX_TIMEOUT_SECONDS,
    )
    runtime_limit = min(MAX_JOB_RUNTIME_SECONDS, requested_timeout + 30)

    with JOBS_LOCK:
        active_jobs = sum(job.get("status") not in TERMINAL_JOB_STATUSES for job in JOBS.values())
        unfinished_futures = sum(not future.done() for future in JOB_FUTURES.values())
        capacity = MAX_CONCURRENT_JOBS + MAX_PENDING_JOBS
        if len(JOBS) >= MAX_JOBS or active_jobs >= capacity or unfinished_futures >= capacity:
            raise JobQueueFullError("任务队列已满，请稍后重试")
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "deadline_at": now + runtime_limit,
        }

    def should_cancel() -> bool:
        """任务被显式取消、已被清理，或超过最大运行时间时返回 True。"""
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                return True
            if job.get("cancel_requested") or job.get("status") in TERMINAL_JOB_STATUSES:
                return True
            return time.time() >= float(job.get("deadline_at") or float("inf"))

    def worker() -> None:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None or job.get("status") != "queued":
                return
            job["status"] = "running"
            job["updated_at"] = time.time()

        try:
            result = materialize_job_result(
                job_id,
                # do_POST 在入队前已经完成校验，见 /api/generate-async。
                process_request_payload(
                    payload, should_cancel=should_cancel, already_validated=True
                ),
            )
        except JobCancelledError:
            # 状态已由 cancel_async_job / cleanup_jobs 写入，这里只需释放资产。
            _remove_job_assets(job_id)
            return
        except ValueError as exc:
            error_payload = {"message": str(exc)}
        except RuntimeError as exc:
            try:
                error_payload = json.loads(str(exc))
            except json.JSONDecodeError:
                error_payload = {"message": str(exc)}
        except Exception as exc:  # pragma: no cover
            error_payload = {"message": f"服务内部错误: {type(exc).__name__}: {exc}"}
        else:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job is None or job.get("status") != "running":
                    _remove_job_assets(job_id)
                    return
                if time.time() >= float(job["deadline_at"]):
                    job["status"] = "error"
                    job["updated_at"] = time.time()
                    job["error"] = {"message": "任务超过最大运行时间", "code": "job_deadline_exceeded"}
                    _remove_job_assets(job_id)
                    return
                job["status"] = "success"
                job["updated_at"] = time.time()
                job["result"] = result
            return

        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None or job.get("status") not in {"queued", "running"}:
                return
            job["status"] = "error"
            job["updated_at"] = time.time()
            job["error"] = error_payload

    try:
        future = JOB_EXECUTOR.submit(worker)
    except RuntimeError:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        raise JobQueueFullError("任务执行器不可用")
    with JOBS_LOCK:
        JOB_FUTURES[job_id] = future

    def forget_future(completed: Future) -> None:
        with JOBS_LOCK:
            if JOB_FUTURES.get(job_id) is completed:
                JOB_FUTURES.pop(job_id, None)

    future.add_done_callback(forget_future)
    return job_id


def get_async_job(job_id: str) -> dict | None:
    cleanup_jobs()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def cancel_async_job(job_id: str) -> dict | None:
    cleanup_jobs()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        if job.get("status") in TERMINAL_JOB_STATUSES:
            return dict(job)
        job["status"] = "cancelled"
        job["cancel_requested"] = True
        job["updated_at"] = time.time()
        job["error"] = {"message": "任务已取消", "code": "job_cancelled"}
        future = JOB_FUTURES.get(job_id)
        snapshot = dict(job)
    # 尚未启动的任务可以直接撤单；已启动的任务由 should_cancel 在下一个分片处退出。
    started = future is None or not future.cancel()
    if not started:
        # 确定没有工作线程在写这个目录，可以安全清理。
        _remove_job_assets(job_id)
    # 已启动的任务由 worker 自己在收到 JobCancelledError 后清理资产，
    # 避免 rmtree 与 write_bytes 竞争产生孤儿文件。
    return snapshot


def sse_write(handler: SimpleHTTPRequestHandler, event_name: str, payload: dict) -> None:
    body = (
        f"event: {event_name}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode("utf-8")
    handler.wfile.write(body)
    handler.wfile.flush()


def openai_stream_event_name(upstream_event: str) -> str:
    if upstream_event.endswith(".partial_image"):
        return "partial_image"
    if upstream_event.endswith(".completed"):
        return "completed"
    return upstream_event.rsplit(".", 1)[-1] if "." in upstream_event else upstream_event


def stream_images_to_client(handler: SimpleHTTPRequestHandler, payload: dict) -> None:
    validate_request_payload(payload)
    (
        base_url,
        api_key,
        timeout,
        route,
        json_body,
        form_data,
        files,
    ) = build_stream_request(payload)

    started_at = time.time()
    response, endpoint, client_request_id = request_image_stream(
        base_url,
        api_key,
        timeout,
        route,
        json_body=json_body,
        form_data=form_data,
        files=files,
    )

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    # SSE 没有 Content-Length，在 HTTP/1.1 下必须显式关闭连接来界定响应边界。
    # Connection: close 与 Cache-Control 都由 end_headers 统一发送，避免重复。
    handler.close_connection = True
    handler.end_headers()

    meta_sent = False
    completed_count = 0
    last_output_format = "png"

    try:
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/event-stream" not in content_type:
            data = response_json_limited(response)
            images = parse_image_api_response(data, timeout, client_request_id, endpoint)
            duration_ms = int((time.time() - started_at) * 1000)
            sse_write(
                handler,
                "meta",
                {
                    "client_request_id": client_request_id,
                    "endpoint": endpoint,
                    "duration_ms": duration_ms,
                    "status": "fallback-json",
                },
            )
            for index, image_url in enumerate(images, start=1):
                sse_write(
                    handler,
                    "completed",
                    {
                        "index": index,
                        "image": image_url,
                        "final": True,
                    },
                )
            sse_write(
                handler,
                "done",
                {
                    "client_request_id": client_request_id,
                    "endpoint": endpoint,
                    "duration_ms": duration_ms,
                    "count": len(images),
                },
            )
            return

        for upstream_event, data_text in iter_sse_messages(response):
            raw = data_text.strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                event_payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if not meta_sent:
                sse_write(
                    handler,
                    "meta",
                    {
                        "client_request_id": client_request_id,
                        "endpoint": endpoint,
                        "status": "streaming",
                    },
                )
                meta_sent = True

            event_key = upstream_event if upstream_event not in {"", "message"} else str(event_payload.get("type", ""))
            mapped_event = openai_stream_event_name(event_key)
            if mapped_event == "partial_image":
                output_format = str(event_payload.get("output_format", "")).strip() or last_output_format
                last_output_format = output_format or last_output_format
                image_url = stream_image_data_url(
                    str(event_payload.get("b64_json") or event_payload.get("url") or ""),
                    output_format,
                    timeout,
                )
                sse_write(
                    handler,
                    "partial_image",
                    {
                        "index": int(event_payload.get("partial_image_index") or 0),
                        "image": image_url,
                        "output_format": output_format or "png",
                    },
                )
                continue

            if mapped_event == "completed":
                output_format = str(event_payload.get("output_format", "")).strip() or last_output_format
                last_output_format = output_format or last_output_format
                image_url = stream_image_data_url(
                    str(event_payload.get("b64_json") or event_payload.get("url") or ""),
                    output_format,
                    timeout,
                )
                completed_count += 1
                sse_write(
                    handler,
                    "completed",
                    {
                        "index": completed_count,
                        "image": image_url,
                        "revised_prompt": str(event_payload.get("revised_prompt", "")).strip(),
                        "output_format": output_format or "png",
                        "final": True,
                    },
                )
                continue

            if mapped_event == "error":
                message = str(event_payload.get("message") or event_payload.get("error") or "流式生成失败")
                sse_write(handler, "error", {"message": message})
                return

        duration_ms = int((time.time() - started_at) * 1000)
        sse_write(
            handler,
            "done",
            {
                "client_request_id": client_request_id,
                "endpoint": endpoint,
                "duration_ms": duration_ms,
                "count": completed_count,
            },
        )
    except (BrokenPipeError, ConnectionResetError):
        raise
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            try:
                error_payload = json.loads(str(exc))
            except json.JSONDecodeError:
                error_payload = {"message": str(exc)}
        else:
            error_payload = {"message": str(exc) or type(exc).__name__}
        error_payload.setdefault("ok", False)
        sse_write(handler, "error", error_payload)
    finally:
        response.close()


class AppHandler(SimpleHTTPRequestHandler):
    # HTTP/1.1 keep-alive: 首屏的 html/css/js 与每张生成图不再各自新建 TCP 连接和线程。
    # 所有响应都显式带 Content-Length；唯一的例外是 SSE，见 do_POST 中的 Connection: close。
    protocol_version = "HTTP/1.1"
    # 防 slowloris：没有 timeout 时空连接会永久占住一个线程。
    timeout = 30

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[web] " + (fmt % args) + "\n")

    def end_headers(self) -> None:
        request_path = urlparse(self.path).path
        if request_path.startswith("/api/assets/"):
            # 资产按 job_id(UUID)/文件名 寻址，任务存续期内内容不变，可长期缓存。
            self.send_header("Cache-Control", f"private, max-age={ASSET_CACHE_SECONDS}, immutable")
        elif (
            request_path.startswith("/api/")
            or request_path in {"/", "/index.html"}
            or request_path.endswith((".html", ".css", ".js"))
        ):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        x_frame_options = getattr(self.server, "x_frame_options", None)
        if x_frame_options:
            self.send_header("X-Frame-Options", str(x_frame_options))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # 如果这条响应之后要关闭连接（SSE、未读完请求体的早退分支），明确告知客户端。
        # close_connection 由 BaseHTTPRequestHandler 在处理请求时赋值，用 getattr 兜底。
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        self.send_header(
            "Content-Security-Policy",
            build_content_security_policy(
                str(getattr(self.server, "frame_ancestors", "*"))
            ),
        )
        super().end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_api_access(self, *, mutation: bool = False) -> bool:
        server = self.server
        if bool(getattr(server, "require_auth", False)):
            expected = str(getattr(server, "auth_token", ""))
            authorization = self.headers.get("Authorization", "")
            supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
            supplied = supplied or self.headers.get("X-Image-Client-Token", "").strip()
            # compare_digest 拒绝含非 ASCII 字符的 str，先编码为字节再比较
            if not supplied or not hmac.compare_digest(
                supplied.encode("utf-8"), expected.encode("utf-8")
            ):
                self._send_json(401, {"ok": False, "message": "API 访问需要有效的服务令牌"})
                return False

        if mutation:
            origin = self.headers.get("Origin", "").strip()
            if origin:
                parsed_origin = urlparse(origin)
                host = self.headers.get("Host", "").lower()
                allowed_origins = {
                    item.strip().rstrip("/")
                    for item in os.environ.get("OPENAI_IMAGE_CLIENT_ALLOWED_ORIGINS", "").split(",")
                    if item.strip()
                }
                same_origin = parsed_origin.scheme in {"http", "https"} and parsed_origin.netloc.lower() == host
                if origin.rstrip("/") not in allowed_origins and not same_origin:
                    self._send_json(403, {"ok": False, "message": "请求 Origin 不受信任"})
                    return False
        return True

    def _send_asset(self, job_id: str, filename: str) -> None:
        try:
            normalized_job_id = str(uuid.UUID(job_id))
        except ValueError:
            self._send_json(404, {"ok": False, "message": "资源不存在"})
            return
        if normalized_job_id != job_id or not filename or Path(filename).name != filename:
            self._send_json(404, {"ok": False, "message": "资源不存在"})
            return
        job = get_async_job(job_id)
        if not job or job.get("status") != "success":
            self._send_json(404, {"ok": False, "message": "资源不存在或已过期"})
            return
        path = _asset_directory(job_id) / filename
        # 先看大小再读，否则这个上限什么也保护不了。
        try:
            stat_result = path.stat()
        except OSError:
            # 资产可能已被取消/过期清理删除，这里必须容错而不是抛出。
            self._send_json(404, {"ok": False, "message": "资源不存在或已过期"})
            return
        if not stat.S_ISREG(stat_result.st_mode):
            self._send_json(404, {"ok": False, "message": "资源不存在或已过期"})
            return
        if stat_result.st_size > MAX_REMOTE_MEDIA_BYTES:
            self._send_json(500, {"ok": False, "message": "资源超过大小限制"})
            return

        etag = f'"{job_id}-{filename}-{stat_result.st_size}"'
        if self.headers.get("If-None-Match", "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            handle = path.open("rb")
        except OSError:
            self._send_json(404, {"ok": False, "message": "资源不存在或已过期"})
            return
        with handle:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat_result.st_size))
            self.send_header("ETag", etag)
            self.end_headers()
            # 流式拷贝，避免把整张图再复制一份到内存。
            shutil.copyfileobj(handle, self.wfile, 64 * 1024)

    def do_GET(self) -> None:
        request_path = urlparse(self.path).path
        if request_path.startswith("/api/") and not self._check_api_access():
            return
        if request_path.startswith("/api/health"):
            self._send_json(200, {"ok": True, "service": "openai-image-client-web"})
            return
        if request_path.startswith("/api/jobs/"):
            job_id = request_path.rsplit("/", 1)[-1].strip()
            job = get_async_job(job_id)
            if not job:
                self._send_json(404, {"ok": False, "message": "任务不存在或已过期"})
                return
            payload = {
                "ok": True,
                "job_id": job["id"],
                "status": job["status"],
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
            }
            if job["status"] == "success":
                payload["result"] = job.get("result", {})
            elif job["status"] in {"error", "cancelled"}:
                payload["error"] = job.get("error", {"message": "任务执行失败"})
            if job.get("cancel_requested"):
                payload["cancel_requested"] = True
            self._send_json(200, payload)
            return
        if request_path.startswith("/api/assets/"):
            parts = request_path.split("/")
            if len(parts) != 5:
                self._send_json(404, {"ok": False, "message": "资源不存在"})
                return
            self._send_asset(parts[3], parts[4])
            return
        if request_path in {"/", "", "/image.html"}:
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        request_path = urlparse(self.path).path
        # 这些早退分支都在读取请求体之前应答；HTTP/1.1 复用连接时若不关闭连接，
        # 剩余的请求体会被当成下一个请求的起始行解析，导致连接错位。
        if request_path not in {"/api/generate", "/api/generate-async", "/api/generate-stream"}:
            self.close_connection = True
            self._send_json(404, {"ok": False, "message": "Not Found"})
            return
        if not self._check_api_access(mutation=True):
            self.close_connection = True
            return

        if self.headers.get("Transfer-Encoding", "").strip():
            self.close_connection = True
            self._send_json(411, {"ok": False, "message": "请求必须提供 Content-Length"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            self._send_json(400, {"ok": False, "message": "Content-Length 不合法"})
            return
        if content_length < 0:
            self.close_connection = True
            self._send_json(400, {"ok": False, "message": "Content-Length 不合法"})
            return
        if content_length > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            self._send_json(413, {"ok": False, "message": f"请求体不能超过 {MAX_REQUEST_BODY_BYTES} 字节"})
            return
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"ok": False, "message": "请求体不是合法 JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "message": "请求体顶层必须是 JSON 对象"})
            return

        if request_path == "/api/generate-stream":
            try:
                stream_images_to_client(self, payload)
            except BrokenPipeError:
                return
            except ValueError as exc:
                self._send_json(400, {"ok": False, "message": str(exc)})
            except RuntimeError as exc:
                try:
                    data = json.loads(str(exc))
                except json.JSONDecodeError:
                    data = {"message": str(exc)}
                self._send_json(502, {"ok": False, **data})
            except Exception as exc:  # pragma: no cover
                self._send_json(500, {"ok": False, "message": f"服务内部错误: {type(exc).__name__}: {exc}"})
            return

        if request_path == "/api/generate-async":
            try:
                validate_request_payload(payload)
            except ValueError as exc:
                self._send_json(400, {"ok": False, "message": str(exc)})
                return
            try:
                job_id = create_async_job(payload)
            except JobQueueFullError as exc:
                self._send_json(503, {"ok": False, "message": str(exc), "code": "job_queue_full"})
                return
            self._send_json(200, {"ok": True, "job_id": job_id, "status": "queued"})
            return

        try:
            result = process_request_payload(payload)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "message": str(exc)})
            return
        except RuntimeError as exc:
            try:
                data = json.loads(str(exc))
            except json.JSONDecodeError:
                data = {"message": str(exc)}
            self._send_json(502, {"ok": False, **data})
            return
        except Exception as exc:  # pragma: no cover
            self._send_json(500, {"ok": False, "message": f"服务内部错误: {type(exc).__name__}: {exc}"})
            return

        self._send_json(200, result)

    def do_DELETE(self) -> None:
        request_path = urlparse(self.path).path
        # DELETE 从不读取请求体；带体的请求必须关闭连接，否则会与下一个请求错位。
        if self.headers.get("Content-Length", "").strip() or self.headers.get(
            "Transfer-Encoding", ""
        ).strip():
            self.close_connection = True
        if not request_path.startswith("/api/jobs/"):
            self._send_json(404, {"ok": False, "message": "Not Found"})
            return
        if not self._check_api_access(mutation=True):
            return
        job_id = request_path.rsplit("/", 1)[-1].strip()
        job = cancel_async_job(job_id)
        if not job:
            self._send_json(404, {"ok": False, "message": "任务不存在或已过期"})
            return
        self._send_json(
            200,
            {
                "ok": True,
                "job_id": job["id"],
                "status": job["status"],
                "cancel_requested": bool(job.get("cancel_requested")),
            },
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenAI Image Client Web")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--strict-port", action="store_true", help="端口被占用时不自动尝试后续端口")
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument("--open-browser", dest="open_browser", action="store_true", help="启动后自动打开浏览器")
    browser_group.add_argument("--no-open-browser", dest="open_browser", action="store_false", help="启动后不自动打开浏览器")
    parser.set_defaults(open_browser=None)
    return parser


def is_loopback_bind(host: str) -> bool:
    normalized = str(host or "").strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def create_server(host: str, port: int, strict_port: bool) -> ThreadingHTTPServer:
    require_auth = not is_loopback_bind(host)
    auth_token = os.environ.get("OPENAI_IMAGE_CLIENT_AUTH_TOKEN", "").strip()
    frame_ancestors, x_frame_options = resolve_embed_policy(
        os.environ.get("OPENAI_IMAGE_CLIENT_EMBED_ORIGINS")
    )
    if require_auth and not auth_token:
        raise ValueError(
            "监听非回环地址时必须设置 OPENAI_IMAGE_CLIENT_AUTH_TOKEN；"
            "建议继续使用默认的 127.0.0.1"
        )
    candidates = [port]
    if not strict_port and port > 0:
        candidates.extend(range(port + 1, port + 100))

    last_error: OSError | None = None
    for candidate in candidates:
        try:
            server = ThreadingHTTPServer((host, candidate), AppHandler)
            server.require_auth = require_auth
            server.auth_token = auth_token
            server.frame_ancestors = frame_ancestors
            server.x_frame_options = x_frame_options
            return server
        except OSError as exc:
            last_error = exc
            if strict_port or port == 0:
                break
    if last_error is not None:
        raise last_error
    raise OSError("无法启动本地 Web 服务")


def browser_host(host: str) -> str:
    if host in {"", "0.0.0.0", "::"}:
        return DEFAULT_HOST
    return host


def should_open_browser(value: bool | None) -> bool:
    if value is not None:
        return value
    return bool(getattr(sys, "frozen", False))


def main() -> int:
    args = build_parser().parse_args()
    server = create_server(args.host, args.port, args.strict_port)
    bound_port = int(server.server_address[1])
    url = f"http://{browser_host(args.host)}:{bound_port}"
    print(f"OpenAI Image Client Web running at {url}")
    if should_open_browser(args.open_browser):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
