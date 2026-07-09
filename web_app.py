#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import threading
import sys
import time
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests import RequestException

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def app_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


ROOT = app_root()
WEB_ROOT = ROOT / "web"
KNOWN_API_ENDPOINTS = (
    "/v1/images/generations",
    "/images/generations",
    "/v1/images/edits",
    "/images/edits",
    "/v1/images/variations",
    "/images/variations",
    "/v1/videos/generations",
    "/videos/generations",
    "/v1/videos/edits",
    "/videos/edits",
    "/v1/videos/extensions",
    "/videos/extensions",
)
OPENAI_IMAGE_EDIT_FILE_FIELD = "image"
OPENAI_IMAGE_EDIT_ARRAY_FIELD = "image[]"
GROK_IMAGE_GENERATION_MODELS = {
    "grok-imagine-edit",
    "grok-imagine-image",
    "grok-imagine-image-lite",
    "grok-imagine-image-pro",
    "grok-imagine-image-quality",
}
GROK_VIDEO_MODELS = {
    "grok-imagine-video",
    "grok-imagine-video-1.5",
}
PROTOCOL_VALUES = {"auto", "openai", "grok"}
GROK_RESOLUTION_VALUES = {"", "1k", "2k"}
GROK_RESPONSE_FORMAT_VALUES = {"", "url", "b64_json"}
GROK_VIDEO_RESOLUTION_VALUES = {"", "480p", "720p", "1080p"}
GROK_SUPPORTED_RATIOS = {
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "3:2",
    "2:3",
    "2:1",
    "1:2",
    "19.5:9",
    "9:19.5",
    "20:9",
    "9:20",
    "auto",
}
GROK_VIDEO_SUPPORTED_RATIOS = {
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "3:2",
    "2:3",
}
JOB_TTL_SECONDS = 60 * 60
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
DEFAULT_STREAM_PARTIAL_IMAGES = 2
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_TIMEOUT_SECONDS = 500
MAX_EDIT_REFERENCE_IMAGES = 16
MAX_GROK_VIDEO_REFERENCE_IMAGES = 7


def normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def join_url(base_url: str, suffix: str) -> str:
    return f"{base_url.rstrip('/')}/{suffix.lstrip('/')}"


def candidate_endpoints(base_url: str, route: str) -> list[str]:
    value = normalize_base_url(base_url)
    if not value:
        return []

    for endpoint in KNOWN_API_ENDPOINTS:
        if value.endswith(endpoint):
            prefix = value[: -len(endpoint)]
            if endpoint.startswith("/v1/"):
                return [join_url(prefix, f"v1/{route}")]
            return [join_url(prefix, route)]

    if value.endswith("/v1"):
        return [join_url(value, route)]
    return [
        join_url(value, f"v1/{route}"),
        join_url(value, route),
    ]


def is_gpt_image_2(model: str) -> bool:
    value = str(model or "").strip().lower()
    return value == "gpt-image-2" or value.startswith("gpt-image-2-")


def is_grok_image_model(model: str) -> bool:
    value = str(model or "").strip().lower()
    return value == "grok-imagine" or any(value == item or value.startswith(f"{item}-") for item in GROK_IMAGE_GENERATION_MODELS)


def is_grok_video_model(model: str) -> bool:
    value = str(model or "").strip().lower()
    return any(value == item or value.startswith(f"{item}-") for item in GROK_VIDEO_MODELS)


def resolve_image_protocol(protocol: str, model: str, base_url: str) -> str:
    value = str(protocol or "auto").strip().lower()
    if value not in PROTOCOL_VALUES:
        raise ValueError("protocol 必须是 auto、openai 或 grok")
    if value in {"openai", "grok"}:
        return value
    base = str(base_url or "").lower()
    if is_grok_image_model(model) or is_grok_video_model(model) or "api.x.ai" in base or "x.ai" in base:
        return "grok"
    return "openai"


def parse_size(size: str) -> tuple[int, int] | None:
    value = str(size or "").strip().lower()
    if "x" not in value:
        return None
    left, right = value.split("x", 1)
    try:
        width = int(left)
        height = int(right)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def closest_grok_ratio(width: int, height: int) -> str:
    exact_gcd = math.gcd(width, height)
    exact_ratio = f"{width // exact_gcd}:{height // exact_gcd}"
    if exact_ratio in GROK_SUPPORTED_RATIOS:
        return exact_ratio

    current = width / height
    best_ratio = "1:1"
    best_delta = float("inf")
    for candidate in GROK_SUPPORTED_RATIOS - {"auto"}:
        left, right = candidate.split(":", 1)
        candidate_value = float(left) / float(right)
        delta = abs(current - candidate_value)
        if delta < best_delta:
            best_ratio = candidate
            best_delta = delta
    return best_ratio


def grok_aspect_ratio_from_size(size: str) -> str:
    parsed = parse_size(size)
    if not parsed:
        return "1:1"
    width, height = parsed
    return closest_grok_ratio(width, height)


def grok_resolution_from_size(size: str) -> str:
    parsed = parse_size(size)
    if not parsed:
        return "1k"
    return "2k" if max(parsed) > 1536 else "1k"


def closest_grok_video_ratio(width: int, height: int) -> str:
    exact_gcd = math.gcd(width, height)
    exact_ratio = f"{width // exact_gcd}:{height // exact_gcd}"
    if exact_ratio in GROK_VIDEO_SUPPORTED_RATIOS:
        return exact_ratio

    current = width / height
    best_ratio = "16:9"
    best_delta = float("inf")
    for candidate in GROK_VIDEO_SUPPORTED_RATIOS:
        left, right = candidate.split(":", 1)
        candidate_value = float(left) / float(right)
        delta = abs(current - candidate_value)
        if delta < best_delta:
            best_ratio = candidate
            best_delta = delta
    return best_ratio


def grok_video_aspect_ratio_from_size(size: str) -> str:
    parsed = parse_size(size)
    if not parsed:
        return "16:9"
    width, height = parsed
    return closest_grok_video_ratio(width, height)


def grok_video_resolution_from_size(size: str) -> str:
    parsed = parse_size(size)
    if not parsed:
        return "480p"
    long_edge = max(parsed)
    if long_edge > 1536:
        return "1080p"
    if long_edge > 720:
        return "720p"
    return "480p"


def preview_response_body(response: requests.Response, limit: int = 300) -> str:
    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" in content_type:
        try:
            data = response.json()
        except ValueError:
            pass
        else:
            if isinstance(data, dict):
                message = data.get("message") or data.get("error")
                if isinstance(message, dict):
                    message = message.get("message")
                if message:
                    return str(message)[:limit]
                return json.dumps(data, ensure_ascii=False)[:limit]
    return response.text[:limit].replace("\n", " ").strip()


def json_error_type(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" not in content_type:
        return ""
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("type", ""))
    return str(data.get("type", ""))


def response_diagnostic(endpoint: str, response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "") or "unknown"
    request_id = response.headers.get("x-request-id") or response.headers.get("cf-ray")
    error_type = json_error_type(response)
    parts = [
        f"- {endpoint} -> HTTP {response.status_code}",
        f"Content-Type: {content_type}",
    ]
    if request_id:
        parts.append(f"Request-ID: {request_id}")
    if error_type:
        parts.append(f"Error-Type: {error_type}")
    body_preview = preview_response_body(response)
    if body_preview:
        parts.append(f"Body: {body_preview}")
    return ", ".join(parts)


def exception_diagnostic(endpoint: str, exc: BaseException) -> str:
    return f"- {endpoint} -> 请求异常: {type(exc).__name__}: {exc}"


def likely_gateway_or_upstream_error(response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    error_type = json_error_type(response)
    return (
        response.status_code >= 500
        or response.status_code in {502, 503, 504}
        or "upstream" in error_type
        or (response.status_code >= 500 and "text/html" in content_type)
    )


def likely_non_retryable_client_error(response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    return (
        400 <= response.status_code < 500
        and response.status_code not in {404, 405}
        and "text/html" not in content_type
    )


def data_url_from_bytes(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


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
    response = requests.get(image_url, timeout=timeout)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip() or None
    if not content_type:
        guessed, _ = mimetypes.guess_type(urlparse(image_url).path)
        content_type = guessed or "image/png"
    return data_url_from_bytes(response.content, content_type)


def stream_image_data_url(value: str, output_format: str, timeout: int) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("图像数据为空")
    if raw.startswith("data:"):
        return raw
    if raw.startswith(("http://", "https://")):
        try:
            return data_url_from_remote(raw, timeout=timeout)
        except RequestException:
            return raw
    mime = image_mime_from_format(output_format)
    return data_url_from_bytes(base64.b64decode(raw), mime)


def image_item_to_data_url(item: dict, timeout: int) -> str:
    if not isinstance(item, dict):
        raise ValueError("图像项格式不正确")
    if item.get("b64_json"):
        output_format = str(item.get("output_format", "")).strip()
        return data_url_from_bytes(base64.b64decode(str(item["b64_json"])), image_mime_from_format(output_format))
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

    for raw_line in response.iter_lines(decode_unicode=True):
        line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="replace")
        line = line.rstrip("\r")
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

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
                    "raw": data,
                },
                ensure_ascii=False,
            )
        )

    images: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            images.append(image_item_to_data_url(item, timeout=timeout))
        except ValueError:
            continue

    if not images:
        raise RuntimeError(
            json.dumps(
                {
                    "message": "返回里没有可保存的 b64_json、url 或 result",
                    "client_request_id": client_request_id,
                    "endpoint": last_endpoint,
                    "raw": data,
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

            current = requests.post(endpoint, **request_kwargs)
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

    data = response.json()
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
        "raw": data,
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

            current = requests.post(endpoint, **request_kwargs)
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


def video_poll_endpoint_from_generation(endpoint: str, request_id: str) -> str:
    value = endpoint.rstrip("/")
    if value.endswith("/generations"):
        return f"{value.rsplit('/', 1)[0]}/{request_id}"
    return join_url(value, f"videos/{request_id}")


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

    if data.get("url"):
        videos.append(str(data["url"]))

    return list(dict.fromkeys(videos))


def request_grok_video_api(
    base_url: str,
    api_key: str,
    timeout: int,
    *,
    json_body: dict,
    count: int = 1,
    poll_interval: int = 5,
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
        last_response: requests.Response | None = None
        diagnostics: list[str] = []
        request_id = ""
        task_completed = False

        for endpoint in endpoints:
            try:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "X-Client-Request-Id": client_request_id,
                    "Content-Type": "application/json",
                }
                current = requests.post(
                    endpoint,
                    headers=headers,
                    json=json_body,
                    timeout=timeout,
                )
            except RequestException as exc:
                diagnostics.append(exception_diagnostic(endpoint, exc))
                last_endpoint = endpoint
                continue

            content_type = current.headers.get("Content-Type", "")
            if current.ok and "text/html" not in content_type.lower():
                last_endpoint = endpoint
                try:
                    data = current.json()
                except ValueError:
                    diagnostics.append(response_diagnostic(endpoint, current))
                    last_response = current
                    continue

                request_id = str(data.get("request_id") or data.get("id") or "").strip()
                direct_videos = extract_video_urls(data)
                if not request_id and direct_videos:
                    videos.extend(direct_videos)
                    raw_results.append(data)
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
                    time.sleep(poll_delay)
                    try:
                        polled = requests.get(
                            poll_endpoint,
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "X-Client-Request-Id": client_request_id,
                            },
                            timeout=min(30, max(5, timeout)),
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

                    poll_data = polled.json()
                    status = str(poll_data.get("status", "")).strip().lower()
                    poll_videos = extract_video_urls(poll_data)
                    if status == "done" or poll_videos:
                        poll_data.setdefault("request_id", request_id)
                        poll_data.setdefault("poll_endpoint", poll_endpoint)
                        videos.extend(poll_videos)
                        raw_results.append(poll_data)
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

        if not videos:
            continue

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


def generate_videos(payload: dict) -> dict:
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    raw_model = str(payload.get("model", "")).strip()
    protocol = resolve_image_protocol(payload_text(payload, "protocol", "auto"), raw_model, base_url)
    model = raw_model or "grok-imagine-video"
    size = str(payload.get("size", "1920x1072")).strip() or "1920x1072"
    timeout = int(payload.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    video_count = int(payload.get("n") or 1)
    poll_interval = payload_int(payload, "poll_interval", 5)

    if protocol != "grok":
        raise ValueError("grok-imagine-video 只能使用 grok 协议")
    if not prompt:
        raise ValueError("提示词不能为空")
    if video_count < 1:
        raise ValueError("n 必须大于等于 1")

    request_body = build_grok_video_body(payload, model, prompt, size)
    return request_grok_video_api(
        base_url,
        api_key,
        timeout,
        json_body=request_body,
        count=video_count,
        poll_interval=poll_interval,
    )


def generate_images(payload: dict) -> dict:
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    raw_model = str(payload.get("model", "")).strip()
    protocol = resolve_image_protocol(payload_text(payload, "protocol", "auto"), raw_model, base_url)
    model = raw_model or ("grok-imagine-image" if protocol == "grok" else DEFAULT_IMAGE_MODEL)
    size = str(payload.get("size", "1024x1024")).strip() or "1024x1024"
    timeout = int(payload.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    image_count = int(payload.get("n") or 1)

    if not prompt:
        raise ValueError("提示词不能为空")
    if image_count < 1:
        raise ValueError("n 必须大于等于 1")

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

    try:
        return base64.b64decode(encoded), content_type
    except Exception as exc:  # pragma: no cover
        raise ValueError(f"参考图解码失败: {exc}") from exc


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
    for index, edit_image in enumerate(get_edit_image_payloads(payload), start=1):
        image_name = str(edit_image.get("name", "")).strip() or f"reference-image-{index}.png"
        content_type = (
            str(edit_image.get("content_type") or edit_image.get("contentType") or "").strip()
            or "image/png"
        )
        image_data_url = str(edit_image.get("data_url") or edit_image.get("dataUrl") or "").strip()
        image_bytes, content_type = decode_data_url_image(image_data_url, content_type)
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


def edit_images(payload: dict) -> dict:
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    raw_model = str(payload.get("model", "")).strip()
    protocol = resolve_image_protocol(payload_text(payload, "protocol", "auto"), raw_model, base_url)
    model = normalize_edit_model(raw_model or ("grok-imagine-image" if protocol == "grok" else DEFAULT_IMAGE_MODEL))
    size = str(payload.get("size", "1024x1024")).strip() or "1024x1024"
    timeout = int(payload.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    image_count = int(payload.get("n") or 1)

    if not prompt:
        raise ValueError("图生图模式下提示词不能为空")
    if image_count < 1:
        raise ValueError("n 必须大于等于 1")

    if protocol == "grok":
        request_body = build_grok_edit_body(payload, model, prompt, size, image_count)
        return request_image_api(
            base_url,
            api_key,
            timeout,
            "images/edits",
            json_body=request_body,
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
    model = raw_model or ("grok-imagine-image" if protocol == "grok" else DEFAULT_IMAGE_MODEL)
    size = str(payload.get("size", "1024x1024")).strip() or "1024x1024"
    timeout = int(payload.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    image_count = int(payload.get("n") or 1)
    partial_images = int(payload.get("partial_images") or DEFAULT_STREAM_PARTIAL_IMAGES)

    if not prompt:
        raise ValueError("图生图模式下提示词不能为空" if mode == "edit" else "提示词不能为空")
    if image_count < 1:
        raise ValueError("n 必须大于等于 1")
    if partial_images < 1:
        raise ValueError("partial_images 必须大于等于 1")

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


def process_request_payload(payload: dict) -> dict:
    mode = str(payload.get("mode", "generate")).strip().lower() or "generate"
    raw_model = str(payload.get("model", "")).strip()
    if is_video_payload(payload, raw_model):
        return generate_videos(payload)
    if mode == "edit":
        return edit_images(payload)
    if mode != "generate":
        raise ValueError("不支持的模式")
    return generate_images(payload)


def validate_request_payload(payload: dict) -> None:
    mode = str(payload.get("mode", "generate")).strip().lower() or "generate"
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    raw_model = str(payload.get("model", "")).strip()
    if not base_url:
        raise ValueError("Base URL 不能为空")
    if not api_key:
        raise ValueError("API Key 不能为空")
    if not prompt:
        raise ValueError("提示词不能为空" if mode == "generate" else "图生图模式下提示词不能为空")
    try:
        image_count = int(payload.get("n") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("n 必须大于等于 1") from exc
    if image_count < 1:
        raise ValueError("n 必须大于等于 1")
    if mode == "edit":
        get_edit_image_payloads(payload)
    if mode not in {"generate", "edit", "video"}:
        raise ValueError("不支持的模式")
    if mode == "video" or is_grok_video_model(raw_model):
        payload_int(payload, "poll_interval", 5)
        payload_int(payload, "duration", 0)


def cleanup_jobs(now: float | None = None) -> None:
    current = now or time.time()
    with JOBS_LOCK:
        expired_ids = [
            job_id
            for job_id, job in JOBS.items()
            if current - float(job.get("updated_at") or job.get("created_at") or current) > JOB_TTL_SECONDS
        ]
        for job_id in expired_ids:
            JOBS.pop(job_id, None)


def create_async_job(payload: dict) -> str:
    cleanup_jobs()
    job_id = str(uuid.uuid4())
    now = time.time()
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
        }

    def worker() -> None:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                return
            job["status"] = "running"
            job["updated_at"] = time.time()

        try:
            result = process_request_payload(payload)
        except ValueError as exc:
            error_payload = {"message": str(exc)}
            status = "error"
        except RuntimeError as exc:
            try:
                error_payload = json.loads(str(exc))
            except json.JSONDecodeError:
                error_payload = {"message": str(exc)}
            status = "error"
        except Exception as exc:  # pragma: no cover
            error_payload = {"message": f"服务内部错误: {type(exc).__name__}: {exc}"}
            status = "error"
        else:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job is None:
                    return
                job["status"] = "success"
                job["updated_at"] = time.time()
                job["result"] = result
            return

        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                return
            job["status"] = status
            job["updated_at"] = time.time()
            job["error"] = error_payload

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def get_async_job(job_id: str) -> dict | None:
    cleanup_jobs()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


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
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()

    meta_sent = False
    completed_count = 0
    final_images: list[str] = []
    last_output_format = "png"

    try:
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/event-stream" not in content_type:
            data = response.json()
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
                final_images.append(image_url)
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
                "count": len(final_images),
            },
        )
    finally:
        response.close()


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[web] " + (fmt % args) + "\n")

    def end_headers(self) -> None:
        request_path = urlparse(self.path).path
        if (
            request_path.startswith("/api/")
            or request_path in {"/", "/index.html"}
            or request_path.endswith((".html", ".css", ".js"))
        ):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        request_path = urlparse(self.path).path
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
            elif job["status"] == "error":
                payload["error"] = job.get("error", {"message": "任务执行失败"})
            self._send_json(200, payload)
            return
        if request_path in {"/", "", "/image.html"}:
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        if self.path not in {"/api/generate", "/api/generate-async", "/api/generate-stream"}:
            self._send_json(404, {"ok": False, "message": "Not Found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "message": "请求体不是合法 JSON"})
            return

        if self.path == "/api/generate-stream":
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

        if self.path == "/api/generate-async":
            try:
                validate_request_payload(payload)
            except ValueError as exc:
                self._send_json(400, {"ok": False, "message": str(exc)})
                return
            job_id = create_async_job(payload)
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


def create_server(host: str, port: int, strict_port: bool) -> ThreadingHTTPServer:
    candidates = [port]
    if not strict_port and port > 0:
        candidates.extend(range(port + 1, port + 100))

    last_error: OSError | None = None
    for candidate in candidates:
        try:
            return ThreadingHTTPServer((host, candidate), AppHandler)
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
