#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests import RequestException

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"


def normalize_base_url(base_url: str) -> str:
    value = (base_url or "").strip()
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


def data_url_from_bytes(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def data_url_from_remote(image_url: str, timeout: int) -> str:
    response = requests.get(image_url, timeout=timeout)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip() or None
    if not content_type:
        guessed, _ = mimetypes.guess_type(urlparse(image_url).path)
        content_type = guessed or "image/png"
    return data_url_from_bytes(response.content, content_type)


def generate_images(payload: dict) -> dict:
    base_url = str(payload.get("base_url", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    model = str(payload.get("model", "gpt-image-1")).strip() or "gpt-image-1"
    size = str(payload.get("size", "1024x1024")).strip() or "1024x1024"
    timeout = int(payload.get("timeout") or 120)
    image_count = int(payload.get("n") or 1)

    endpoints = candidate_endpoints(base_url)
    if not endpoints:
        raise ValueError("Base URL 不能为空")
    if not api_key:
        raise ValueError("API Key 不能为空")
    if not prompt:
        raise ValueError("提示词不能为空")
    if image_count < 1:
        raise ValueError("n 必须大于等于 1")

    request_body = {
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

    started_at = time.time()
    for endpoint in endpoints:
        try:
            current = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Client-Request-Id": client_request_id,
                },
                json=request_body,
                timeout=timeout,
            )
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
        if likely_gateway_or_upstream_error(current):
            break

    if response is None:
        error_message = "请求失败，未找到可用的 OpenAI 图片生成接口。"
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
        if item.get("b64_json"):
            images.append(f"data:image/png;base64,{item['b64_json']}")
            continue
        if item.get("url"):
            images.append(data_url_from_remote(str(item["url"]), timeout=timeout))

    if not images:
        raise RuntimeError(
            json.dumps(
                {
                    "message": "返回里没有可保存的 b64_json 或 url",
                    "client_request_id": client_request_id,
                    "endpoint": last_endpoint,
                    "raw": data,
                },
                ensure_ascii=False,
            )
        )

    duration_ms = int((time.time() - started_at) * 1000)
    return {
        "ok": True,
        "endpoint": last_endpoint,
        "client_request_id": client_request_id,
        "duration_ms": duration_ms,
        "images": images,
        "raw": data,
    }


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[web] " + (fmt % args) + "\n")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/api/health"):
            self._send_json(200, {"ok": True, "service": "openai-image-client-web"})
            return
        if self.path in {"/", ""}:
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/generate":
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

        try:
            result = generate_images(payload)
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"OpenAI Image Client Web running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
