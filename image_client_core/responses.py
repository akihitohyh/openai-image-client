"""Bounded reading of upstream responses, and diagnostics built from them.

Everything here reads the response body through :func:`read_limited_response`, which
caches the bytes on the response object. That matters for two reasons:

* an upstream can otherwise stream an unbounded body straight into memory — the CLI
  used to call ``response.json()`` / ``response.text`` with no ceiling at all;
* a failed request gets inspected several times (error type, body preview,
  retry classification) and would otherwise be re-parsed on each look.

It also fixes a decoding bug: ``response.text`` falls back to ISO-8859-1 for
``text/*`` without an explicit charset (RFC 2616), which turned Chinese upstream
error messages — the primary debugging surface of this tool — into mojibake.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re

import requests

_WHITESPACE_RE = re.compile(r"\s+")

MAX_UPSTREAM_JSON_BYTES = 32 * 1024 * 1024
# Diagnostics only ever need the head of an error body.
DIAGNOSTIC_BODY_BYTES = 64 * 1024


def read_limited_response(response: requests.Response, limit: int) -> bytes:
    cached = getattr(response, "_image_client_body", None)
    if isinstance(cached, bytes):
        if len(cached) > limit:
            raise ValueError(f"上游响应超过 {limit} 字节限制")
        return cached

    raw_length = response.headers.get("Content-Length", "").strip()
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError:
            content_length = 0
        if content_length > limit:
            response.close()
            raise ValueError(f"上游响应超过 {limit} 字节限制")

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            response.close()
            raise ValueError(f"上游响应超过 {limit} 字节限制")
        chunks.append(chunk)
    body = b"".join(chunks)
    setattr(response, "_image_client_body", body)
    return body


def response_json_limited(
    response: requests.Response, limit: int = MAX_UPSTREAM_JSON_BYTES
) -> dict:
    body = read_limited_response(response, limit)
    try:
        data = json.loads(body.decode(getattr(response, "encoding", None) or "utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("上游返回的 JSON 不合法") from exc
    if not isinstance(data, dict):
        raise ValueError("上游返回的 JSON 顶层必须是对象")
    return data


def decode_base64_limited(encoded: str, limit: int, label: str) -> bytes:
    """Strict, size-bounded base64 decode.

    ``validate=True`` is essential: without it Python silently discards every
    character outside the base64 alphabet, so an HTML error page or a truncated
    payload decodes to garbage that then gets written out as a valid-looking image.
    """
    # Drop all whitespace first: strict validation rejects the newlines that
    # MIME-wrapped base64 legitimately contains, while still rejecting the
    # characters an HTML error page is made of ('<', '>', '"', ...).
    value = _WHITESPACE_RE.sub("", str(encoded or ""))
    max_encoded_length = 4 * math.ceil(limit / 3) + 4
    if len(value) > max_encoded_length:
        raise ValueError(f"{label} 超过 {limit} 字节限制")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} 不是合法的 Base64 数据") from exc
    if len(decoded) > limit:
        raise ValueError(f"{label} 超过 {limit} 字节限制")
    return decoded


def preview_response_body(response: requests.Response, limit: int = 300) -> str:
    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" in content_type:
        try:
            data = response_json_limited(response, DIAGNOSTIC_BODY_BYTES)
        except (ValueError, TypeError):
            pass
        else:
            if isinstance(data, dict):
                message = data.get("message") or data.get("error")
                if isinstance(message, dict):
                    message = message.get("message")
                if message:
                    return str(message)[:limit]
                return json.dumps(data, ensure_ascii=False)[:limit]
    try:
        body = read_limited_response(response, DIAGNOSTIC_BODY_BYTES)
    except ValueError:
        return "上游错误响应过大"
    decoded = body.decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
    return decoded[:limit].replace("\n", " ").strip()


def json_error_type(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" not in content_type:
        return ""
    try:
        data = response_json_limited(response, DIAGNOSTIC_BODY_BYTES)
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
    """Worth retrying against the next candidate endpoint.

    The previous version also tested ``status_code in {502, 503, 504}`` and
    ``status_code >= 500 and "text/html" in content_type``; both are fully subsumed
    by ``status_code >= 500``.
    """
    return response.status_code >= 500 or "upstream" in json_error_type(response)


def likely_non_retryable_client_error(response: requests.Response) -> bool:
    """A refusal the next endpoint would repeat, so stop probing.

    Without this the CLI re-POSTed the whole multipart body (up to 50 MB per
    reference image) to the second candidate endpoint after any 400 — wasted
    bandwidth, and a double-billing risk when the first request actually reached
    the provider.
    """
    content_type = response.headers.get("Content-Type", "").lower()
    return (
        400 <= response.status_code < 500
        and response.status_code not in {404, 405}
        and "text/html" not in content_type
    )
