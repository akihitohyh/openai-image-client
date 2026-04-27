#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import uuid
from pathlib import Path

import requests
from requests import RequestException


ROOT = Path(__file__).resolve().parent
CONFIG_CANDIDATES = [
    ROOT / "config.local.json",
    ROOT / "config.json",
]
OUTPUT_DIR = ROOT / "outputs"


def load_config() -> dict:
    for path in CONFIG_CANDIDATES:
        if not path.exists():
            continue
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
    value = base_url.strip()
    if not value:
        return value
    if value.endswith("/v1/images/generations"):
        return value
    if value.endswith("/images/generations"):
        return value
    return value.rstrip("/")


def candidate_endpoints(base_url: str) -> list[str]:
    value = normalize_base_url(base_url)
    if not value:
        return []
    if value.endswith("/images/generations"):
        return [value]
    if value.endswith("/v1"):
        return [value + "/images/generations"]
    return [
        value + "/v1/images/generations",
        value + "/images/generations",
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
    parser.add_argument("--prompt", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--size", default="")
    parser.add_argument("--n", type=int, default=0)
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=int, default=120)
    return parser


def save_b64_image(image_b64: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(base64.b64decode(image_b64))


def save_url_image(image_url: str, output_path: Path, timeout: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(image_url, timeout=timeout)
    response.raise_for_status()
    output_path.write_bytes(response.content)


def main() -> int:
    args = build_parser().parse_args()
    config = load_config()

    base_url = prompt_if_missing(
        args.base_url or str(config.get("base_url", "")),
        "请输入 Base URL",
    )
    api_key = prompt_if_missing(
        args.api_key or str(config.get("api_key", "")),
        "请输入 API Key",
        secret=True,
    )
    prompt = prompt_if_missing(
        args.prompt or str(config.get("prompt", "")),
        "请输入生图提示词",
    )
    model = args.model or str(config.get("model", "gpt-image-1"))
    size = args.size or str(config.get("size", "1024x1024"))
    image_count = args.n or int(config.get("n", 1))

    endpoints = candidate_endpoints(base_url)
    if not endpoints:
        raise SystemExit("Base URL 不能为空")
    if not api_key:
        raise SystemExit("API Key 不能为空")
    if not prompt:
        raise SystemExit("提示词不能为空")
    if image_count < 1:
        raise SystemExit("n 必须大于等于 1")

    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": image_count,
    }

    last_response: requests.Response | None = None
    last_endpoint = ""
    diagnostics: list[str] = []
    response: requests.Response | None = None
    client_request_id = str(uuid.uuid4())

    for endpoint in endpoints:
        print(f"请求地址: {endpoint}")
        try:
            current = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Client-Request-Id": client_request_id,
                },
                json=payload,
                timeout=args.timeout,
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
        message = ["请求失败，未找到可用的 OpenAI 图片生成接口。"]
        if last_endpoint:
            message.append(f"最后请求地址: {last_endpoint}")
        if last_response is not None:
            message.append(f"最后状态码: HTTP {last_response.status_code}")
        message.append(f"客户端请求 ID: {client_request_id}")
        if diagnostics:
            message.append("尝试过的地址：")
            message.extend(diagnostics)
        message.append(
            "如果是 ConnectionError / RemoteDisconnected，通常表示目标地址、协议(http/https)、反向代理或上游服务本身有问题，而不是请求体 JSON 格式错误。"
        )
        if last_response is not None and likely_gateway_or_upstream_error(last_response):
            message.append(
                "接口路径和鉴权大概率已进入中转站，但中转站/上游模型通道返回了网关错误。"
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

    stamp = time.strftime("%Y%m%d-%H%M%S")
    output_root = Path(args.output).expanduser() if args.output else OUTPUT_DIR
    output_root.mkdir(parents=True, exist_ok=True)

    saved_files: list[Path] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            file_path = output_root / f"image-{stamp}-{index}.png"
            save_b64_image(str(item["b64_json"]), file_path)
            saved_files.append(file_path)
            continue
        if item.get("url"):
            url = str(item["url"])
            suffix = ".png"
            if "." in url.rsplit("/", 1)[-1]:
                suffix = "." + url.rsplit(".", 1)[-1].split("?", 1)[0]
            file_path = output_root / f"image-{stamp}-{index}{suffix}"
            save_url_image(url, file_path, timeout=args.timeout)
            saved_files.append(file_path)
            continue

    if not saved_files:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        raise SystemExit("返回里没有可保存的 b64_json 或 url")

    print("生成完成：")
    for path in saved_files:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
