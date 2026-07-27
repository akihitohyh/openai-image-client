"""Base-URL normalization and endpoint discovery."""

from __future__ import annotations

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


def video_poll_endpoint_from_generation(endpoint: str, request_id: str) -> str:
    value = endpoint.rstrip("/")
    if value.endswith("/generations"):
        return f"{value.rsplit('/', 1)[0]}/{request_id}"
    return join_url(value, f"videos/{request_id}")
