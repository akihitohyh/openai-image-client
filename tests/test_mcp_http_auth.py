from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from openai_image_client import http_auth
from openai_image_client.http_auth import (
    HTTPAuthConfig,
    StaticTenantTokenVerifier,
    load_http_auth_config,
)
from openai_image_client.profiles import ConfigurationError


TOKEN_A = "a" * 32
TOKEN_B = "b" * 40


def tenant_config(**overrides):
    value = {
        "tenant_id": "tenant-a",
        "token_env": "TENANT_A_TOKEN",
        "allowed_profiles": ["default", "video"],
        "default_profile": "default",
        "scopes": ["mcp:use", "image:generate"],
        "requests_per_minute": 60,
        "operations_per_minute": 600,
        "expires_at": None,
    }
    value.update(overrides)
    return value


def write_config(tmp_path: Path, tenants, *, mode: int = 0o600, extra=None) -> Path:
    payload = {"tenants": tenants, **(extra or {})}
    path = tmp_path / "http-auth.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


def load(tmp_path: Path, tenants, environ=None) -> HTTPAuthConfig:
    path = write_config(tmp_path, tenants)
    return load_http_auth_config(path, environ or {})


def test_loads_environment_and_digest_credentials_without_public_secret_repr(tmp_path: Path):
    digest_b = hashlib.sha256(TOKEN_B.encode()).hexdigest()
    second = tenant_config(
        tenant_id="tenant-b",
        token_env=None,
        token_sha256=digest_b.upper(),
        allowed_profiles=["video"],
        default_profile="video",
        scopes=["mcp:use"],
        requests_per_minute=5,
    )
    second.pop("token_env")

    config = load(
        tmp_path,
        [tenant_config(), second],
        {"TENANT_A_TOKEN": TOKEN_A},
    )

    assert config.grants_by_tenant["tenant-a"].allowed_profiles == ("default", "video")
    assert config.grants_by_tenant["tenant-b"].requests_per_minute == 5
    assert hashlib.sha256(TOKEN_A.encode()).hexdigest() in config.grants_by_token_digest
    assert digest_b in config.grants_by_token_digest
    public_repr = repr(config)
    assert public_repr == "HTTPAuthConfig(tenants=2)"
    assert TOKEN_A not in public_repr
    assert digest_b not in public_repr
    with pytest.raises(TypeError):
        config.grants_by_tenant["new"] = config.grants_by_tenant["tenant-a"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable to Windows")
def test_config_file_permissions_must_be_exactly_0600(tmp_path: Path):
    path = write_config(tmp_path, [tenant_config()], mode=0o640)
    with pytest.raises(ConfigurationError, match="exactly 0600"):
        load_http_auth_config(path, {"TENANT_A_TOKEN": TOKEN_A})


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda item: item.pop("token_env"), "exactly one"),
        (lambda item: item.update(token_sha256="0" * 64), "exactly one"),
        (
            lambda item: (item.pop("token_env"), item.update(token_sha256="not-a-digest")),
            "SHA-256",
        ),
    ],
)
def test_requires_exactly_one_valid_token_mode(tmp_path: Path, mutate, message):
    item = tenant_config()
    mutate(item)
    with pytest.raises(ConfigurationError, match=message):
        load(tmp_path, [item], {"TENANT_A_TOKEN": TOKEN_A})


def test_environment_token_must_exist_and_have_minimum_entropy_length(tmp_path: Path):
    path = write_config(tmp_path, [tenant_config()])
    with pytest.raises(ConfigurationError, match="unset or empty") as missing:
        load_http_auth_config(path, {})
    assert TOKEN_A not in str(missing.value)

    short_token = "short-secret"
    with pytest.raises(ConfigurationError, match="at least 32") as short:
        load_http_auth_config(path, {"TENANT_A_TOKEN": short_token})
    assert short_token not in str(short.value)

    oversized_token = "x" * (http_auth.MAX_TOKEN_LENGTH + 1)
    with pytest.raises(ConfigurationError, match="at most 4096") as oversized:
        load_http_auth_config(path, {"TENANT_A_TOKEN": oversized_token})
    assert oversized_token not in str(oversized.value)


def test_duplicate_tenant_and_token_digest_are_rejected_without_secret_disclosure(tmp_path: Path):
    duplicate_tenant = tenant_config(token_env="SECOND_TOKEN")
    path = write_config(tmp_path, [tenant_config(), duplicate_tenant])
    with pytest.raises(ConfigurationError, match="duplicate tenant_id"):
        load_http_auth_config(path, {"TENANT_A_TOKEN": TOKEN_A, "SECOND_TOKEN": TOKEN_B})

    duplicate_credential = tenant_config(tenant_id="tenant-b", token_env="SECOND_TOKEN")
    path = write_config(tmp_path, [tenant_config(), duplicate_credential])
    with pytest.raises(ConfigurationError, match="duplicate token credential") as duplicate:
        load_http_auth_config(path, {"TENANT_A_TOKEN": TOKEN_A, "SECOND_TOKEN": TOKEN_A})
    message = str(duplicate.value)
    assert TOKEN_A not in message
    assert hashlib.sha256(TOKEN_A.encode()).hexdigest() not in message


@pytest.mark.parametrize("tenant_id", ["", "Tenant-A", "-tenant", "tenant-", "has space", "x" * 65])
def test_tenant_id_must_match_restricted_pattern(tmp_path: Path, tenant_id: str):
    with pytest.raises(ConfigurationError, match="tenant_id"):
        load(tmp_path, [tenant_config(tenant_id=tenant_id)], {"TENANT_A_TOKEN": TOKEN_A})


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"allowed_profiles": []}, "allowed_profiles"),
        ({"allowed_profiles": ["default", "default"]}, "duplicate"),
        ({"allowed_profiles": ["bad profile"]}, "invalid"),
        ({"default_profile": "missing"}, "present in allowed_profiles"),
        ({"scopes": []}, "scopes"),
        ({"scopes": ["mcp:use", "mcp:use"]}, "duplicate"),
    ],
)
def test_profile_default_and_scope_validation(tmp_path: Path, overrides, message):
    with pytest.raises(ConfigurationError, match=message):
        load(tmp_path, [tenant_config(**overrides)], {"TENANT_A_TOKEN": TOKEN_A})


@pytest.mark.parametrize("rpm", [0, 1001, True, "60"])
def test_requests_per_minute_bounds(tmp_path: Path, rpm):
    with pytest.raises(ConfigurationError, match="requests_per_minute"):
        load(
            tmp_path,
            [tenant_config(requests_per_minute=rpm)],
            {"TENANT_A_TOKEN": TOKEN_A},
        )


@pytest.mark.parametrize("operations", [0, 10_001, True, "600"])
def test_operations_per_minute_bounds(tmp_path: Path, operations):
    with pytest.raises(ConfigurationError, match="operations_per_minute"):
        load(
            tmp_path,
            [tenant_config(operations_per_minute=operations)],
            {"TENANT_A_TOKEN": TOKEN_A},
        )


def test_tenant_count_is_bounded(tmp_path: Path):
    tenants = []
    environ = {}
    for index in range(http_auth.MAX_TENANTS + 1):
        variable = f"TOKEN_{index}"
        tenants.append(tenant_config(tenant_id=f"t{index}", token_env=variable))
        environ[variable] = f"{index:032d}"
    with pytest.raises(ConfigurationError, match="more than 1000"):
        load(tmp_path, tenants, environ)


def test_unknown_fields_and_direct_tokens_are_rejected_without_echo(tmp_path: Path):
    raw_token = "raw-token-that-must-not-appear-1234567890"
    item = tenant_config(token=raw_token)
    with pytest.raises(ConfigurationError, match="unsupported fields") as error:
        load(tmp_path, [item], {"TENANT_A_TOKEN": TOKEN_A})
    assert raw_token not in str(error.value)

    path = write_config(tmp_path, [tenant_config()], extra={"debug": True})
    with pytest.raises(ConfigurationError, match="root contains unsupported"):
        load_http_auth_config(path, {"TENANT_A_TOKEN": TOKEN_A})


def test_static_verifier_returns_tenant_access_token_and_redacts_repr(tmp_path: Path, monkeypatch):
    future_expiry = 2_000_000_000
    config = load(
        tmp_path,
        [tenant_config(expires_at=future_expiry)],
        {"TENANT_A_TOKEN": TOKEN_A},
    )
    verifier = StaticTenantTokenVerifier(
        config,
        issuer="https://auth.example/",
        resource="https://images.example/mcp/",
    )
    monkeypatch.setattr(http_auth.time, "time", lambda: 1_900_000_000)

    access = asyncio.run(verifier.verify_token(TOKEN_A))
    assert access is not None
    assert access.token == TOKEN_A
    assert access.subject == "tenant-a"
    assert access.client_id == "tenant:tenant-a"
    assert access.scopes == ["mcp:use", "image:generate"]
    assert access.expires_at == future_expiry
    assert access.resource == "https://images.example/mcp"
    assert access.claims == {"iss": "https://auth.example", "tenant_id": "tenant-a"}
    assert TOKEN_A not in repr(access)
    assert TOKEN_A not in repr(verifier)
    assert hashlib.sha256(TOKEN_A.encode()).hexdigest() not in repr(verifier)


def test_static_verifier_rejects_unknown_short_and_expired_tokens(tmp_path: Path, monkeypatch):
    config = load(
        tmp_path,
        [tenant_config(expires_at=100)],
        {"TENANT_A_TOKEN": TOKEN_A},
    )
    verifier = StaticTenantTokenVerifier(
        config,
        issuer="https://auth.example",
        resource="https://images.example/mcp",
    )
    monkeypatch.setattr(http_auth.time, "time", lambda: 100)

    assert asyncio.run(verifier.verify_token(TOKEN_A)) is None
    assert asyncio.run(verifier.verify_token(TOKEN_B)) is None
    assert asyncio.run(verifier.verify_token("too-short")) is None
    assert asyncio.run(verifier.verify_token("x" * (http_auth.MAX_TOKEN_LENGTH + 1))) is None


@pytest.mark.parametrize("expires_at", [0, -1, True, "2000000000"])
def test_expiry_must_be_positive_integer_or_null(tmp_path: Path, expires_at):
    with pytest.raises(ConfigurationError, match="expires_at"):
        load(
            tmp_path,
            [tenant_config(expires_at=expires_at)],
            {"TENANT_A_TOKEN": TOKEN_A},
        )


@pytest.mark.parametrize(
    "issuer, resource",
    [
        ("not-a-url", "https://resource.example"),
        ("https://issuer.example", "file:///tmp/mcp"),
    ],
)
def test_verifier_requires_absolute_http_urls(tmp_path: Path, issuer: str, resource: str):
    config = load(tmp_path, [tenant_config()], {"TENANT_A_TOKEN": TOKEN_A})
    with pytest.raises(ValueError):
        StaticTenantTokenVerifier(config, issuer=issuer, resource=resource)
