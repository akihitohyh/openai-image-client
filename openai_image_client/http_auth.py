from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken
from pydantic import Field

from .profiles import ConfigurationError


MAX_TENANTS = 1000
MIN_REQUESTS_PER_MINUTE = 1
MAX_REQUESTS_PER_MINUTE = 1000
MIN_OPERATIONS_PER_MINUTE = 1
MAX_OPERATIONS_PER_MINUTE = 10_000
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 4096
TENANT_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
TOKEN_DIGEST_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
SCOPE_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9:._/-]{0,126}[A-Za-z0-9])?$")

ROOT_KEYS = {"tenants"}
TENANT_KEYS = {
    "tenant_id",
    "token_env",
    "token_sha256",
    "allowed_profiles",
    "default_profile",
    "scopes",
    "requests_per_minute",
    "operations_per_minute",
    "expires_at",
}


@dataclass(frozen=True)
class TenantGrant:
    tenant_id: str
    allowed_profiles: tuple[str, ...]
    default_profile: str
    scopes: tuple[str, ...]
    requests_per_minute: int
    expires_at: int | None
    operations_per_minute: int = 300


@dataclass(frozen=True, repr=False)
class HTTPAuthConfig:
    grants_by_tenant: Mapping[str, TenantGrant]
    grants_by_token_digest: Mapping[str, TenantGrant]

    def __repr__(self) -> str:
        return f"HTTPAuthConfig(tenants={len(self.grants_by_tenant)})"


class _RedactedAccessToken(AccessToken):
    token: str = Field(repr=False)


def _configuration_error(message: str) -> ConfigurationError:
    return ConfigurationError(f"HTTP auth config: {message}")


def _load_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _configuration_error("file does not exist or cannot be resolved") from exc
    if not resolved.is_file():
        raise _configuration_error("path must refer to a regular file")

    if os.name != "nt":
        try:
            mode = stat.S_IMODE(resolved.stat().st_mode)
        except OSError as exc:
            raise _configuration_error("file permissions cannot be read") from exc
        if mode != 0o600:
            raise _configuration_error("file permissions must be exactly 0600")

    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _configuration_error("file cannot be read") from exc
    except json.JSONDecodeError as exc:
        raise _configuration_error(f"file is not valid JSON at line {exc.lineno}, column {exc.colno}") from exc
    if not isinstance(payload, dict):
        raise _configuration_error("root must be a JSON object")
    unknown_keys = set(payload) - ROOT_KEYS
    if unknown_keys:
        raise _configuration_error("root contains unsupported fields")
    return payload


def _required_string(raw: Mapping[str, Any], key: str, tenant_label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _configuration_error(f"{tenant_label}.{key} must be a non-empty string")
    return value.strip()


def _string_tuple(
    raw: Mapping[str, Any],
    key: str,
    tenant_label: str,
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise _configuration_error(f"{tenant_label}.{key} must be a non-empty array")
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise _configuration_error(f"{tenant_label}.{key} contains an invalid value")
        if item in parsed:
            raise _configuration_error(f"{tenant_label}.{key} contains a duplicate value")
        parsed.append(item)
    return tuple(parsed)


def _bounded_rpm(value: Any, tenant_label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _configuration_error(f"{tenant_label}.requests_per_minute must be an integer")
    if not MIN_REQUESTS_PER_MINUTE <= value <= MAX_REQUESTS_PER_MINUTE:
        raise _configuration_error(
            f"{tenant_label}.requests_per_minute must be between "
            f"{MIN_REQUESTS_PER_MINUTE} and {MAX_REQUESTS_PER_MINUTE}"
        )
    return value


def _bounded_operations(value: Any, tenant_label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _configuration_error(f"{tenant_label}.operations_per_minute must be an integer")
    if not MIN_OPERATIONS_PER_MINUTE <= value <= MAX_OPERATIONS_PER_MINUTE:
        raise _configuration_error(
            f"{tenant_label}.operations_per_minute must be between "
            f"{MIN_OPERATIONS_PER_MINUTE} and {MAX_OPERATIONS_PER_MINUTE}"
        )
    return value


def _expiry(value: Any, tenant_label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _configuration_error(f"{tenant_label}.expires_at must be a positive Unix timestamp or null")
    return value


def _token_digest(
    raw: Mapping[str, Any],
    tenant_label: str,
    environ: Mapping[str, str],
) -> str:
    has_token_env = "token_env" in raw
    has_token_digest = "token_sha256" in raw
    if has_token_env == has_token_digest:
        raise _configuration_error(
            f"{tenant_label} must contain exactly one of token_env or token_sha256"
        )

    if has_token_env:
        variable_name = _required_string(raw, "token_env", tenant_label)
        token = str(environ.get(variable_name, "")).strip()
        if not token:
            raise _configuration_error(
                f"{tenant_label}.token_env references an unset or empty environment variable"
            )
        if len(token) < MIN_TOKEN_LENGTH:
            raise _configuration_error(
                f"{tenant_label}.token_env must resolve to a token of at least {MIN_TOKEN_LENGTH} characters"
            )
        if len(token) > MAX_TOKEN_LENGTH:
            raise _configuration_error(
                f"{tenant_label}.token_env must resolve to a token of at most {MAX_TOKEN_LENGTH} characters"
            )
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    digest = _required_string(raw, "token_sha256", tenant_label)
    if not TOKEN_DIGEST_PATTERN.fullmatch(digest):
        raise _configuration_error(f"{tenant_label}.token_sha256 must be a SHA-256 hex digest")
    return digest.lower()


def load_http_auth_config(
    path: str | os.PathLike[str],
    environ: Mapping[str, str] | None,
) -> HTTPAuthConfig:
    payload = _load_json(path)
    raw_tenants = payload.get("tenants")
    if not isinstance(raw_tenants, list) or not raw_tenants:
        raise _configuration_error("tenants must be a non-empty array")
    if len(raw_tenants) > MAX_TENANTS:
        raise _configuration_error(f"tenants must not contain more than {MAX_TENANTS} entries")

    environment = os.environ if environ is None else environ
    grants_by_tenant: dict[str, TenantGrant] = {}
    grants_by_token_digest: dict[str, TenantGrant] = {}

    for index, raw_tenant in enumerate(raw_tenants):
        tenant_label = f"tenants[{index}]"
        if not isinstance(raw_tenant, dict):
            raise _configuration_error(f"{tenant_label} must be a JSON object")
        unknown_keys = set(raw_tenant) - TENANT_KEYS
        if unknown_keys:
            raise _configuration_error(f"{tenant_label} contains unsupported fields")

        tenant_id = _required_string(raw_tenant, "tenant_id", tenant_label)
        if not TENANT_ID_PATTERN.fullmatch(tenant_id):
            raise _configuration_error(
                f"{tenant_label}.tenant_id must match {TENANT_ID_PATTERN.pattern}"
            )
        if tenant_id in grants_by_tenant:
            raise _configuration_error(f"duplicate tenant_id {tenant_id!r}")

        allowed_profiles = _string_tuple(
            raw_tenant,
            "allowed_profiles",
            tenant_label,
            PROFILE_NAME_PATTERN,
        )
        default_profile = _required_string(raw_tenant, "default_profile", tenant_label)
        if not PROFILE_NAME_PATTERN.fullmatch(default_profile):
            raise _configuration_error(f"{tenant_label}.default_profile is invalid")
        if default_profile not in allowed_profiles:
            raise _configuration_error(
                f"{tenant_label}.default_profile must be present in allowed_profiles"
            )
        scopes = _string_tuple(raw_tenant, "scopes", tenant_label, SCOPE_PATTERN)
        requests_per_minute = _bounded_rpm(raw_tenant.get("requests_per_minute"), tenant_label)
        operations_per_minute = _bounded_operations(
            raw_tenant.get(
                "operations_per_minute",
                min(MAX_OPERATIONS_PER_MINUTE, max(60, requests_per_minute * 10)),
            ),
            tenant_label,
        )
        expires_at = _expiry(raw_tenant.get("expires_at"), tenant_label)
        digest = _token_digest(raw_tenant, tenant_label, environment)
        if digest in grants_by_token_digest:
            raise _configuration_error("duplicate token credential")

        grant = TenantGrant(
            tenant_id=tenant_id,
            allowed_profiles=allowed_profiles,
            default_profile=default_profile,
            scopes=scopes,
            requests_per_minute=requests_per_minute,
            expires_at=expires_at,
            operations_per_minute=operations_per_minute,
        )
        grants_by_tenant[tenant_id] = grant
        grants_by_token_digest[digest] = grant

    return HTTPAuthConfig(
        grants_by_tenant=MappingProxyType(grants_by_tenant),
        grants_by_token_digest=MappingProxyType(grants_by_token_digest),
    )


def _absolute_http_url(value: str, name: str) -> str:
    normalized = str(value).strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain credentials")
    return normalized


class StaticTenantTokenVerifier:
    def __init__(self, config: HTTPAuthConfig, issuer: str, resource: str) -> None:
        self._config = config
        self.issuer = _absolute_http_url(issuer, "issuer")
        self.resource = _absolute_http_url(resource, "resource")

    def __repr__(self) -> str:
        return (
            f"StaticTenantTokenVerifier(tenants={len(self._config.grants_by_tenant)}, "
            f"issuer={self.issuer!r}, resource={self.resource!r})"
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        if (
            not isinstance(token, str)
            or len(token) < MIN_TOKEN_LENGTH
            or len(token) > MAX_TOKEN_LENGTH
        ):
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        grant = self._config.grants_by_token_digest.get(digest)
        if grant is None:
            return None
        if grant.expires_at is not None and grant.expires_at <= int(time.time()):
            return None
        return _RedactedAccessToken(
            token=token,
            client_id=f"tenant:{grant.tenant_id}",
            scopes=list(grant.scopes),
            expires_at=grant.expires_at,
            resource=self.resource,
            subject=grant.tenant_id,
            claims={"iss": self.issuer, "tenant_id": grant.tenant_id},
        )
