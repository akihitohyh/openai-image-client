#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from requests import RequestException


ROOT = Path(__file__).resolve().parent
CONFIG_CANDIDATES = [
    ROOT / "config.local.json",
    ROOT / "config.json",
]
OUTPUT_DIR = ROOT / "outputs"
MAX_INPUT_IMAGE_BYTES = 50 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

IMAGE_QUALITY_VALUES = {"auto", "low", "medium", "high"}
OUTPUT_FORMAT_VALUES = {"png", "jpeg", "webp"}
BACKGROUND_VALUES = {"auto", "opaque", "transparent"}
GPT_IMAGE_2_BACKGROUND_VALUES = {"auto", "opaque"}
MODERATION_VALUES = {"auto", "low"}
PROTOCOL_VALUES = {"auto", "openai", "grok"}
MODE_VALUES = {"generate", "edit", "video"}
GROK_RESOLUTION_VALUES = {"", "1k", "2k"}
GROK_RESPONSE_FORMAT_VALUES = {"", "url", "b64_json"}
GROK_VIDEO_RESOLUTION_VALUES = {"", "480p", "720p", "1080p"}
GROK_IMAGE_MODELS = (
    "grok-imagine-edit",
    "grok-imagine-image",
    "grok-imagine-image-lite",
    "grok-imagine-image-pro",
    "grok-imagine-image-quality",
)
GROK_VIDEO_MODELS = (
    "grok-imagine-video",
    "grok-imagine-video-1.5",
)
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


def load_config() -> dict[str, Any]:
    for path in CONFIG_CANDIDATES:
        if not path.exists():
            continue
        if os.name == "posix" and path.stat().st_mode & 0o077:
            print(
                f"警告：{path.name} 可能包含 API Key，但当前权限不是 0600；请执行 chmod 600 {path}",
                file=sys.stderr,
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path.name} 不是合法 JSON: {exc}") from exc
    return {}


def prompt_if_missing(value: str, label: str, secret: bool = False) -> str:
    if value:
        return value
    prompt = f"{label}: "
    if secret:
        import getpass

        return getpass.getpass(prompt).strip()
    return input(prompt).strip()


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenAI 兼容生图脚本")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--protocol", choices=sorted(PROTOCOL_VALUES), default="", help="auto/openai/grok，默认读取配置或 auto")
    parser.add_argument("--mode", choices=sorted(MODE_VALUES), default="", help="generate 文生图；edit 图生图/图生视频；video 文生视频")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--size", default="")
    parser.add_argument("--n", type=int, default=0)
    parser.add_argument("--image", action="append", default=[], help="图生图参考图路径，可重复传入")
    parser.add_argument("--quality", default="", help="gpt-image-2: auto/low/medium/high")
    parser.add_argument("--output-format", default="", help="gpt-image-2: png/jpeg/webp")
    parser.add_argument("--output-compression", type=int, default=None, help="仅 jpeg/webp 有效，0-100")
    parser.add_argument("--background", default="", help="gpt-image-2: auto/opaque；不支持 transparent")
    parser.add_argument("--moderation", default="", help="auto/low")
    parser.add_argument("--aspect-ratio", default="", help="grok-imagine-image: 1:1/16:9/9:16 等，默认由 size 推导")
    parser.add_argument("--resolution", default="", help="grok-imagine-image: 1k/2k；grok-imagine-video: 480p/720p/1080p，默认由 size 推导")
    parser.add_argument("--response-format", default="", help="grok-imagine-image: url 或 b64_json")
    parser.add_argument("--duration", type=int, default=0, help="grok-imagine-video: 视频秒数，通常 1-15")
    parser.add_argument("--poll-interval", type=int, default=0, help="grok-imagine-video: 轮询间隔秒数，默认 5")
    parser.add_argument("--use-proxy", action="store_true", help="允许 requests 使用 HTTP(S)_PROXY 等系统代理环境变量")
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=int, default=0, help="请求超时秒数，默认读取配置 timeout 或 120")
    return parser


def config_text(config: dict[str, Any], key: str, default: str = "") -> str:
    value = config.get(key, default)
    if value is None:
        return ""
    return str(value).strip()


def config_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    raise SystemExit(f"配置项 {key} 必须是布尔值")


def config_int(config: dict[str, Any], key: str) -> int | None:
    value = config.get(key)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"配置项 {key} 必须是整数") from exc


def validate_choice(name: str, value: str, allowed: set[str]) -> None:
    if value and value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise SystemExit(f"{name}={value!r} 不支持，可选值: {choices}")


def is_gpt_image_2(model: str) -> bool:
    return model == "gpt-image-2" or model.startswith("gpt-image-2-")


def is_grok_image_model(model: str) -> bool:
    value = model.strip().lower()
    return value == "grok-imagine" or any(value == item or value.startswith(f"{item}-") for item in GROK_IMAGE_MODELS)


def is_grok_video_model(model: str) -> bool:
    value = model.strip().lower()
    return any(value == item or value.startswith(f"{item}-") for item in GROK_VIDEO_MODELS)


def resolve_protocol(protocol: str, model: str, base_url: str) -> str:
    value = (protocol or "auto").strip().lower()
    validate_choice("protocol", value, PROTOCOL_VALUES)
    if value in {"openai", "grok"}:
        return value
    base = base_url.lower()
    if is_grok_image_model(model) or is_grok_video_model(model) or "api.x.ai" in base or "x.ai" in base:
        return "grok"
    return "openai"


def parse_size(size: str) -> tuple[int, int] | None:
    value = (size or "").strip().lower()
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


def normalized_image_suffix(output_format: str) -> str:
    if output_format == "jpeg":
        return ".jpg"
    if output_format in {"png", "webp"}:
        return f".{output_format}"
    return ".png"


def suffix_from_url(url: str, default_suffix: str) -> str:
    tail = url.rsplit("/", 1)[-1]
    if "." not in tail:
        return default_suffix
    suffix = "." + tail.rsplit(".", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    if 2 <= len(suffix) <= 8:
        return suffix
    return default_suffix


def save_b64_image(image_b64: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if image_b64.startswith("data:") and "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    max_encoded_size = ((MAX_DOWNLOAD_BYTES + 2) // 3) * 4
    if len(image_b64) > max_encoded_size:
        raise ValueError(f"媒体文件超过 {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB 限制")
    decoded = base64.b64decode(image_b64)
    if len(decoded) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"媒体文件超过 {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB 限制")
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.part")
    try:
        temporary_path.write_bytes(decoded)
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def new_session(use_proxy: bool) -> requests.Session:
    session = requests.Session()
    # 默认禁用环境代理，避免 HTTP_PROXY/HTTPS_PROXY 导致 ProxyError/RemoteDisconnected。
    session.trust_env = use_proxy
    return session


def save_url_image(session: requests.Session, image_url: str, output_path: Path, timeout: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.part")
    try:
        with session.get(image_url, timeout=timeout, stream=True) as response:
            response.raise_for_status()
            declared_size = int(response.headers.get("Content-Length") or 0)
            if declared_size > MAX_DOWNLOAD_BYTES:
                raise RequestException(f"媒体文件超过 {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB 限制")
            downloaded = 0
            with temporary_path.open("wb") as output_file:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_BYTES:
                        raise RequestException(f"媒体文件超过 {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB 限制")
                    output_file.write(chunk)
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def save_url_pointer(image_url: str, output_path: Path) -> None:
    pointer_path = output_path.with_suffix(output_path.suffix + ".url.txt")
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(image_url + "\n", encoding="utf-8")


def image_mime_from_path(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "image/png"


def image_file_to_data_url(path: Path) -> str:
    image_bytes = path.read_bytes()
    mime = image_mime_from_path(path)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def load_image_paths(raw_paths: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw_path in raw_paths:
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise SystemExit(f"参考图不存在: {path}")
        if not path.is_file():
            raise SystemExit(f"参考图不是文件: {path}")
        if path.stat().st_size > MAX_INPUT_IMAGE_BYTES:
            raise SystemExit(f"参考图超过 {MAX_INPUT_IMAGE_BYTES // (1024 * 1024)} MB 限制: {path}")
        paths.append(path)
    return paths


def openai_edit_file_field(model: str, image_count: int) -> str:
    if is_gpt_image_2(model) or image_count > 1:
        return "image[]"
    return "image"


def build_openai_generation_payload(
    *,
    model: str,
    prompt: str,
    size: str,
    image_count: int,
    quality: str,
    output_format: str,
    output_compression: int | None,
    background: str,
    moderation: str,
) -> dict[str, Any]:
    validate_choice("quality", quality, IMAGE_QUALITY_VALUES)
    validate_choice("output_format", output_format, OUTPUT_FORMAT_VALUES)
    validate_choice("background", background, BACKGROUND_VALUES)
    validate_choice("moderation", moderation, MODERATION_VALUES)

    if is_gpt_image_2(model) and background == "transparent":
        raise SystemExit("gpt-image-2 当前不支持 background='transparent'，请改为 auto 或 opaque")
    if output_compression is not None and not 0 <= output_compression <= 100:
        raise SystemExit("output_compression 必须在 0 到 100 之间")

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": image_count,
    }
    if quality:
        payload["quality"] = quality
    if output_format:
        payload["output_format"] = output_format
    if background:
        payload["background"] = background
    if moderation:
        payload["moderation"] = moderation
    if output_compression is not None:
        if output_format in {"jpeg", "webp"}:
            payload["output_compression"] = output_compression
        else:
            print("提示：output_compression 仅对 jpeg/webp 有效，当前 output_format 不是 jpeg/webp，已忽略。")
    return payload


def build_openai_edit_request(
    *,
    model: str,
    prompt: str,
    size: str,
    image_count: int,
    image_paths: list[Path],
    quality: str,
    output_format: str,
    output_compression: int | None,
    background: str,
    moderation: str,
) -> tuple[dict[str, str], list[tuple[str, tuple[str, bytes, str]]]]:
    if not image_paths:
        raise SystemExit("图生图模式必须通过 --image 传入至少一张参考图")

    payload = build_openai_generation_payload(
        model=model,
        prompt=prompt,
        size=size,
        image_count=image_count,
        quality=quality,
        output_format=output_format,
        output_compression=output_compression,
        background=background,
        moderation=moderation,
    )
    form_data = {key: str(value) for key, value in payload.items()}
    file_field = openai_edit_file_field(model, len(image_paths))
    files = [
        (
            file_field,
            (path.name, path.read_bytes(), image_mime_from_path(path)),
        )
        for path in image_paths
    ]
    return form_data, files


def validate_grok_options(aspect_ratio: str, resolution: str, response_format: str) -> None:
    if aspect_ratio and aspect_ratio not in GROK_SUPPORTED_RATIOS:
        choices = ", ".join(sorted(GROK_SUPPORTED_RATIOS))
        raise SystemExit(f"aspect_ratio={aspect_ratio!r} 不支持，可选值: {choices}")
    validate_choice("resolution", resolution, GROK_RESOLUTION_VALUES)
    validate_choice("response_format", response_format, GROK_RESPONSE_FORMAT_VALUES)


def build_grok_generation_payload(
    *,
    model: str,
    prompt: str,
    size: str,
    image_count: int,
    aspect_ratio: str,
    resolution: str,
    response_format: str,
) -> dict[str, Any]:
    aspect_ratio = aspect_ratio or grok_aspect_ratio_from_size(size)
    resolution = resolution or grok_resolution_from_size(size)
    validate_grok_options(aspect_ratio, resolution, response_format)

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": image_count,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    if response_format:
        payload["response_format"] = response_format
    return payload


def build_grok_edit_payload(
    *,
    model: str,
    prompt: str,
    size: str,
    image_count: int,
    image_paths: list[Path],
    aspect_ratio: str,
    resolution: str,
    response_format: str,
) -> dict[str, Any]:
    if not image_paths:
        raise SystemExit("图生图模式必须通过 --image 传入至少一张参考图")
    if len(image_paths) > 3:
        raise SystemExit("grok-imagine-image 图生图最多支持 3 张参考图")

    explicit_aspect_ratio = bool(aspect_ratio)
    explicit_resolution = bool(resolution)
    aspect_ratio = aspect_ratio or grok_aspect_ratio_from_size(size)
    resolution = resolution or grok_resolution_from_size(size)
    validate_grok_options(aspect_ratio, resolution, response_format)

    images = [
        {"type": "image_url", "url": image_file_to_data_url(path)}
        for path in image_paths
    ]
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": image_count,
    }
    if len(images) == 1:
        payload["image"] = images[0]
    else:
        payload["images"] = images
    if len(images) > 1 or explicit_aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio
    if len(images) > 1 or explicit_resolution:
        payload["resolution"] = resolution
    if response_format:
        payload["response_format"] = response_format
    return payload


def validate_grok_video_options(aspect_ratio: str, resolution: str, duration: int) -> None:
    if aspect_ratio and aspect_ratio not in GROK_VIDEO_SUPPORTED_RATIOS:
        choices = ", ".join(sorted(GROK_VIDEO_SUPPORTED_RATIOS))
        raise SystemExit(f"aspect_ratio={aspect_ratio!r} 不支持，可选值: {choices}")
    validate_choice("resolution", resolution, GROK_VIDEO_RESOLUTION_VALUES)
    if duration and not 1 <= duration <= 15:
        raise SystemExit("duration 必须在 1 到 15 秒之间")


def build_grok_video_payload(
    *,
    model: str,
    prompt: str,
    size: str,
    image_paths: list[Path],
    aspect_ratio: str,
    resolution: str,
    duration: int,
) -> dict[str, Any]:
    if len(image_paths) > 7:
        raise SystemExit("grok-imagine-video 图生视频/参考视频最多支持 7 张参考图")

    aspect_ratio = aspect_ratio or grok_video_aspect_ratio_from_size(size)
    resolution = resolution or grok_video_resolution_from_size(size)
    validate_grok_video_options(aspect_ratio, resolution, duration)

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    if duration:
        payload["duration"] = duration

    refs = [{"url": image_file_to_data_url(path)} for path in image_paths]
    if len(refs) == 1:
        payload["image"] = refs[0]
    elif len(refs) > 1:
        payload["reference_images"] = refs
        if duration and duration > 10:
            raise SystemExit("多参考图视频的 duration 最大为 10 秒")
    return payload


def video_poll_endpoint_from_generation(endpoint: str, request_id: str) -> str:
    value = endpoint.rstrip("/")
    if value.endswith("/generations"):
        return f"{value.rsplit('/', 1)[0]}/{request_id}"
    return join_url(value, f"videos/{request_id}")


def extract_video_urls(data: dict[str, Any]) -> list[str]:
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


def request_grok_video_generation(
    *,
    session: requests.Session,
    endpoints: list[str],
    api_key: str,
    payload: dict[str, Any],
    timeout: int,
    poll_interval: int,
) -> tuple[dict[str, Any], str, str]:
    last_response: requests.Response | None = None
    last_endpoint = ""
    diagnostics: list[str] = []
    client_request_id = str(uuid.uuid4())

    for endpoint in endpoints:
        print(f"请求地址: {endpoint}")
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "X-Client-Request-Id": client_request_id,
                "Content-Type": "application/json",
            }
            current = session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except RequestException as exc:
            diagnostics.append(exception_diagnostic(endpoint, exc))
            last_endpoint = endpoint
            continue

        content_type = current.headers.get("Content-Type", "")
        if current.ok and "text/html" not in content_type.lower():
            try:
                data = current.json()
            except ValueError as exc:
                diagnostics.append(f"- {endpoint} -> JSON 解析失败: {exc}")
                last_response = current
                last_endpoint = endpoint
                continue

            request_id = str(data.get("request_id") or data.get("id") or "").strip()
            if not request_id:
                if extract_video_urls(data):
                    return data, endpoint, client_request_id
                diagnostics.append(f"- {endpoint} -> 返回里没有 request_id: {json.dumps(data, ensure_ascii=False)[:300]}")
                last_response = current
                last_endpoint = endpoint
                continue

            poll_endpoint = video_poll_endpoint_from_generation(endpoint, request_id)
            deadline = time.time() + timeout
            poll_delay = min(30, max(1, poll_interval or 5))

            while True:
                if time.time() >= deadline:
                    raise SystemExit(
                        f"视频任务超时：request_id={request_id}，timeout={timeout} 秒，最后轮询地址: {poll_endpoint}"
                    )
                time.sleep(poll_delay)
                print(f"轮询视频任务: {request_id}")
                try:
                    polled = session.get(
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
                    raise SystemExit(
                        "视频任务轮询失败：\n"
                        + response_diagnostic(poll_endpoint, polled)
                    )
                poll_data = polled.json()
                status = str(poll_data.get("status", "")).strip().lower()
                if status == "done" or extract_video_urls(poll_data):
                    poll_data.setdefault("request_id", request_id)
                    poll_data.setdefault("poll_endpoint", poll_endpoint)
                    return poll_data, endpoint, client_request_id
                if status in {"failed", "error", "expired", "cancelled", "canceled"}:
                    raise SystemExit(
                        f"视频任务失败：request_id={request_id}, status={status}, "
                        f"raw={json.dumps(poll_data, ensure_ascii=False)[:1000]}"
                    )
                print(f"视频任务状态: {status or 'unknown'}")

        diagnostics.append(response_diagnostic(endpoint, current))
        last_response = current
        last_endpoint = endpoint
        if likely_gateway_or_upstream_error(current):
            break

    message = ["请求失败，未找到可用的 grok 视频接口。"]
    if last_endpoint:
        message.append(f"最后请求地址: {last_endpoint}")
    if last_response is not None:
        message.append(f"最后状态码: HTTP {last_response.status_code}")
    message.append(f"客户端请求 ID: {client_request_id}")
    if diagnostics:
        message.append("尝试过的地址：")
        message.extend(diagnostics)
    raise SystemExit("\n".join(message))


def main() -> int:
    args = build_parser().parse_args()
    config = load_config()

    base_url = prompt_if_missing(
        args.base_url or os.environ.get("OPENAI_IMAGE_BASE_URL", "") or config_text(config, "base_url"),
        "请输入 Base URL",
    )
    api_key = prompt_if_missing(
        args.api_key or os.environ.get("OPENAI_IMAGE_API_KEY", "") or config_text(config, "api_key"),
        "请输入 API Key",
        secret=True,
    )
    mode = (args.mode or config_text(config, "mode", "generate")).lower()
    validate_choice("mode", mode, MODE_VALUES)
    requested_protocol = args.protocol or config_text(config, "protocol", "auto")
    raw_model = args.model or config_text(config, "model")
    if mode == "video" and not raw_model:
        raw_model = "grok-imagine-video"
    protocol = resolve_protocol(requested_protocol, raw_model, base_url)
    model = raw_model or ("grok-imagine-video" if mode == "video" else ("grok-imagine-image" if protocol == "grok" else "gpt-image-2"))
    is_video = is_grok_video_model(model) or mode == "video"
    prompt = prompt_if_missing(
        args.prompt or config_text(config, "prompt"),
        "请输入生图提示词",
    )
    size = args.size or config_text(config, "size", "1024x1024")
    image_count = args.n or int(config.get("n", 1))
    timeout = args.timeout or int(config.get("timeout", 120))
    quality = (args.quality or config_text(config, "quality")).lower()
    output_format = (args.output_format or config_text(config, "output_format")).lower()
    output_compression = args.output_compression
    if output_compression is None:
        output_compression = config_int(config, "output_compression")
    background = (args.background or config_text(config, "background")).lower()
    moderation = (args.moderation or config_text(config, "moderation")).lower()
    aspect_ratio = (args.aspect_ratio or config_text(config, "aspect_ratio")).lower()
    resolution = (args.resolution or config_text(config, "resolution")).lower()
    response_format = (args.response_format or config_text(config, "response_format")).lower()
    duration = args.duration or config_int(config, "duration") or 0
    poll_interval = args.poll_interval or config_int(config, "poll_interval") or 5
    use_proxy = bool(args.use_proxy or config_bool(config, "use_proxy", False))
    session = new_session(use_proxy)
    image_path_values = list(args.image)
    if not image_path_values:
        configured_images = config.get("images", config.get("image", []))
        if isinstance(configured_images, str):
            image_path_values = [configured_images]
        elif isinstance(configured_images, list):
            image_path_values = [str(item) for item in configured_images]
    image_paths = load_image_paths(image_path_values) if (mode == "edit" or (is_video and image_path_values)) else []

    if is_video and protocol != "grok":
        raise SystemExit("grok-imagine-video 只能使用 grok 协议")

    route = "videos/generations" if is_video else ("images/edits" if mode == "edit" else "images/generations")
    endpoints = candidate_endpoints(base_url, route)
    if not endpoints:
        raise SystemExit("Base URL 不能为空")
    if not api_key:
        raise SystemExit("API Key 不能为空")
    if not prompt:
        raise SystemExit("提示词不能为空")
    if image_count < 1:
        raise SystemExit("n 必须大于等于 1")
    if image_count > 10:
        raise SystemExit("n 最大为 10")
    if not 5 <= timeout <= 3600:
        raise SystemExit("timeout 必须在 5 到 3600 秒之间")
    if not 1 <= poll_interval <= 60:
        raise SystemExit("poll_interval 必须在 1 到 60 秒之间")

    json_payload: dict[str, Any] | None = None
    form_data: dict[str, str] | None = None
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None

    if is_video:
        json_payload = build_grok_video_payload(
            model=model,
            prompt=prompt,
            size=size,
            image_paths=image_paths,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            duration=duration,
        )
    elif protocol == "openai":
        if mode == "generate":
            json_payload = build_openai_generation_payload(
                model=model,
                prompt=prompt,
                size=size,
                image_count=image_count,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
            )
        else:
            form_data, files = build_openai_edit_request(
                model=model,
                prompt=prompt,
                size=size,
                image_count=image_count,
                image_paths=image_paths,
                quality=quality,
                output_format=output_format,
                output_compression=output_compression,
                background=background,
                moderation=moderation,
            )
    else:
        if mode == "generate":
            json_payload = build_grok_generation_payload(
                model=model,
                prompt=prompt,
                size=size,
                image_count=image_count,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                response_format=response_format,
            )
        else:
            json_payload = build_grok_edit_payload(
                model=model,
                prompt=prompt,
                size=size,
                image_count=image_count,
                image_paths=image_paths,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                response_format=response_format,
            )

    if is_video:
        if json_payload is None:
            raise SystemExit("视频请求体构建失败")
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        output_root = Path(args.output).expanduser() if args.output else OUTPUT_DIR
        output_root.mkdir(parents=True, exist_ok=True)
        saved_files: list[Path] = []

        for index in range(1, image_count + 1):
            data, endpoint, client_request_id = request_grok_video_generation(
                session=session,
                endpoints=endpoints,
                api_key=api_key,
                payload=json_payload,
                timeout=timeout,
                poll_interval=poll_interval,
            )
            videos = extract_video_urls(data)
            if not videos:
                print(json.dumps(data, ensure_ascii=False, indent=2))
                raise SystemExit(
                    f"视频任务完成但没有返回 video.url，客户端请求 ID: {client_request_id}, 请求地址: {endpoint}"
                )
            for media_index, video_url in enumerate(videos, start=1):
                suffix = suffix_from_url(video_url, ".mp4")
                file_path = output_root / f"video-{stamp}-{index}-{media_index}{suffix}"
                try:
                    save_url_image(session, video_url, file_path, timeout=timeout)
                except RequestException as exc:
                    print(f"提示：视频 URL 下载失败，已保存原始 URL：{type(exc).__name__}: {exc}")
                    save_url_pointer(video_url, file_path)
                    file_path = file_path.with_suffix(file_path.suffix + ".url.txt")
                saved_files.append(file_path)

        if not saved_files:
            raise SystemExit("返回里没有可保存的视频 URL")

        print("生成完成：")
        for path in saved_files:
            print(path)
        return 0

    last_response: requests.Response | None = None
    last_endpoint = ""
    diagnostics: list[str] = []
    response: requests.Response | None = None
    client_request_id = str(uuid.uuid4())

    for endpoint in endpoints:
        print(f"请求地址: {endpoint}")
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "X-Client-Request-Id": client_request_id,
            }
            request_kwargs: dict[str, Any] = {
                "headers": headers,
                "timeout": timeout,
            }
            if json_payload is not None:
                headers["Content-Type"] = "application/json"
                request_kwargs["json"] = json_payload
            else:
                request_kwargs["data"] = form_data or {}
                request_kwargs["files"] = files or []
            current = session.post(
                endpoint,
                **request_kwargs,
            )
        except RequestException as exc:
            diagnostics.append(exception_diagnostic(endpoint, exc))
            last_endpoint = endpoint
            continue
        content_type = current.headers.get("Content-Type", "")
        if current.ok and "text/html" not in content_type.lower():
            response = current
            break
        diagnostics.append(response_diagnostic(endpoint, current))
        last_response = current
        last_endpoint = endpoint
        if likely_gateway_or_upstream_error(current):
            break

    if response is None:
        message = [f"请求失败，未找到可用的 {protocol} 图片接口。"]
        if last_endpoint:
            message.append(f"最后请求地址: {last_endpoint}")
        if last_response is not None:
            message.append(f"最后状态码: HTTP {last_response.status_code}")
        message.append(f"客户端请求 ID: {client_request_id}")
        if diagnostics:
            if any("ReadTimeout" in item for item in diagnostics):
                message.append(
                    f"检测到读取超时：当前 timeout={timeout} 秒。4K/高分辨率生图可能需要更久，"
                    "可在 config.local.json 增加 \"timeout\": 600，或命令行加 --timeout 600；"
                    "也可以先改用 gpt-image-2 + 1024x1024 验证通道。"
                )
            message.append("尝试过的地址：")
            message.extend(diagnostics)
        if any("ProxyError" in item for item in diagnostics):
            message.append(
                "检测到 ProxyError：脚本默认已禁用系统代理；如果仍出现该错误，请检查是否在代码外层或 Python/requests 配置中强制设置了代理。"
            )
        message.append(
            "如果是 ConnectionError / RemoteDisconnected，通常表示目标地址、协议(http/https)、反向代理或上游服务本身有问题，而不是请求体 JSON 格式错误。"
        )
        if last_response is not None and likely_gateway_or_upstream_error(last_response):
            message.append(
                "接口路径和鉴权大概率已进入中转站，但中转站/上游模型通道返回了网关错误。"
            )
            if last_response.status_code == 524:
                message.append(
                    "HTTP 524 通常是 Cloudflare 等反代等待源站超时：本地 timeout 调得再大也可能无效，"
                    "需要降低模型/尺寸、改用异步任务接口，或让中转站管理员调整架构/关闭经过 Cloudflare 的长连接代理。"
                )
            message.append(
                "请稍后重试，或把上面的 Request-ID / cf-ray 发给中转站管理员排查图片通道。"
            )
        else:
            message.append(
                "请检查 base_url 是否填写为真正的 API 根地址，并确认协议是否应为 https。"
            )
        raise SystemExit("\n".join(message))

    data = response.json()
    items = data.get("data")
    if not isinstance(items, list) or not items:
        raise SystemExit(f"接口返回异常，未找到 data: {json.dumps(data, ensure_ascii=False)[:1000]}")

    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    output_root = Path(args.output).expanduser() if args.output else OUTPUT_DIR
    output_root.mkdir(parents=True, exist_ok=True)

    saved_files: list[Path] = []
    default_suffix = normalized_image_suffix(output_format)
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            file_path = output_root / f"image-{stamp}-{index}{default_suffix}"
            save_b64_image(str(item["b64_json"]), file_path)
            saved_files.append(file_path)
            continue
        if item.get("url"):
            url = str(item["url"])
            suffix = default_suffix
            if "." in url.rsplit("/", 1)[-1]:
                suffix = "." + url.rsplit(".", 1)[-1].split("?", 1)[0]
            file_path = output_root / f"image-{stamp}-{index}{suffix}"
            try:
                save_url_image(session, url, file_path, timeout=timeout)
            except RequestException as exc:
                print(f"提示：图片 URL 下载失败，已保存原始 URL：{type(exc).__name__}: {exc}")
                save_url_pointer(url, file_path)
                file_path = file_path.with_suffix(file_path.suffix + ".url.txt")
            saved_files.append(file_path)
            continue
        if item.get("result"):
            result = str(item["result"]).strip()
            suffix = default_suffix
            file_path = output_root / f"image-{stamp}-{index}{suffix}"
            if result.startswith(("http://", "https://")):
                try:
                    save_url_image(session, result, file_path, timeout=timeout)
                except RequestException as exc:
                    print(f"提示：图片 URL 下载失败，已保存原始 URL：{type(exc).__name__}: {exc}")
                    save_url_pointer(result, file_path)
                    file_path = file_path.with_suffix(file_path.suffix + ".url.txt")
            else:
                save_b64_image(result, file_path)
            saved_files.append(file_path)
            continue

    if not saved_files:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        raise SystemExit("返回里没有可保存的 b64_json、url 或 result")

    print("生成完成：")
    for path in saved_files:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
