from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


class ConfigurationError(RuntimeError):
    """Raised when MCP profile configuration is missing or invalid."""


ROOT_CONFIG_FIELDS = {
    "profiles",
    "default_profile",
    "output_dir",
    "allowed_input_roots",
    "max_workers",
    "max_queue",
    "max_jobs",
    "job_ttl_seconds",
    "max_reference_bytes",
    "max_output_bytes",
    "max_asset_store_bytes",
}
PROFILE_FIELDS = {
    "base_url",
    "api_key_env",
    "api_key",
    "protocol",
    "image_model",
    "video_model",
    "allowed_models",
    "timeout",
}


@dataclass(frozen=True, repr=False)
class Profile:
    name: str
    base_url: str
    api_key: str
    protocol: str = "auto"
    image_model: str = ""
    video_model: str = "grok-imagine-video"
    allowed_models: tuple[str, ...] = ()
    timeout: int = 500

    def __repr__(self) -> str:
        return (
            f"Profile(name={self.name!r}, protocol={self.protocol!r}, "
            f"configured={bool(self.base_url and self.api_key)!r})"
        )

    def resolve_model(self, requested: str, *, video: bool = False) -> str:
        if video:
            default_model = self.video_model
        else:
            default_model = self.image_model or (
                "grok-imagine-image"
                if self.protocol == "grok" or "x.ai" in self.base_url.lower()
                else "gpt-image-2"
            )
        model = requested.strip() or default_model
        if self.allowed_models and model and model not in self.allowed_models:
            raise ValueError(f"model {model!r} is not allowed by profile {self.name!r}")
        return model

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "protocol": self.protocol,
            "image_model": self.image_model or None,
            "video_model": self.video_model or None,
            "allowed_models": list(self.allowed_models),
            "timeout": self.timeout,
            "configured": bool(self.base_url and self.api_key),
        }


@dataclass(frozen=True)
class MCPSettings:
    profiles: Mapping[str, Profile]
    default_profile: str
    output_dir: Path
    allowed_input_roots: tuple[Path, ...]
    max_workers: int = 2
    max_queue: int = 20
    max_jobs: int = 200
    job_ttl_seconds: int = 24 * 60 * 60
    max_reference_bytes: int = 20 * 1024 * 1024
    max_output_bytes: int = 256 * 1024 * 1024
    max_asset_store_bytes: int = 2 * 1024 * 1024 * 1024

    def get_profile(self, name: str = "") -> Profile:
        selected = name.strip() or self.default_profile
        profile = self.profiles.get(selected)
        if profile is None:
            available = ", ".join(sorted(self.profiles)) or "none"
            raise ConfigurationError(
                f"MCP profile {selected!r} is not configured; available profiles: {available}"
            )
        return profile

    def public_profiles(self) -> dict[str, Any]:
        return {
            "default_profile": self.default_profile,
            "profiles": [self.profiles[name].public_dict() for name in sorted(self.profiles)],
        }


def _bounded_int(value: Any, default: int, minimum: int, maximum: int, name: str) -> int:
    if value in {None, ""}:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _resolve_path(value: str | os.PathLike[str], base_dir: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _contains_direct_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(key == "api_key" or _contains_direct_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_direct_secret(item) for item in value)
    return False


def _reject_unknown_fields(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ConfigurationError(f"{label} contains unknown fields: {', '.join(unknown)}")


def _load_config_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"MCP config file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"MCP config file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("MCP config root must be a JSON object")
    if os.name != "nt" and _contains_direct_secret(payload):
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ConfigurationError(
                f"MCP config contains an api_key and must have mode 0600: {path}"
            )
    return payload


def _parse_json_env(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"OPENAI_IMAGE_MCP_PROFILES_JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("OPENAI_IMAGE_MCP_PROFILES_JSON must be a JSON object")
    return payload


def _validate_base_url(value: str, profile_name: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(
            f"profile {profile_name!r} base_url must be an absolute http(s) URL"
        )
    if parsed.username or parsed.password:
        raise ConfigurationError(f"profile {profile_name!r} base_url must not contain credentials")
    return normalized


def _build_profile(name: str, raw: Any, environ: Mapping[str, str]) -> Profile:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"profile {name!r} must be a JSON object")
    _reject_unknown_fields(raw, PROFILE_FIELDS, f"profile {name!r}")
    base_url = _validate_base_url(str(raw.get("base_url", "")), name)
    api_key_env = str(raw.get("api_key_env", "")).strip()
    api_key = str(raw.get("api_key", "")).strip()
    if api_key_env:
        api_key = str(environ.get(api_key_env, "")).strip()
        if not api_key:
            raise ConfigurationError(
                f"profile {name!r} requires environment variable {api_key_env!r}"
            )
    if not api_key:
        raise ConfigurationError(f"profile {name!r} requires api_key or api_key_env")
    protocol = str(raw.get("protocol", "auto")).strip().lower() or "auto"
    if protocol not in {"auto", "openai", "grok"}:
        raise ConfigurationError(f"profile {name!r} protocol must be auto, openai, or grok")
    allowed_models_value = raw.get("allowed_models", [])
    if not isinstance(allowed_models_value, list):
        raise ConfigurationError(f"profile {name!r} allowed_models must be an array")
    allowed_models = tuple(str(item).strip() for item in allowed_models_value if str(item).strip())
    return Profile(
        name=name,
        base_url=base_url,
        api_key=api_key,
        protocol=protocol,
        image_model=str(raw.get("image_model", "")).strip(),
        video_model=str(raw.get("video_model", "grok-imagine-video")).strip()
        or "grok-imagine-video",
        allowed_models=allowed_models,
        timeout=_bounded_int(raw.get("timeout"), 500, 5, 600, f"profiles.{name}.timeout"),
    )


def load_settings(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> MCPSettings:
    env = dict(os.environ if environ is None else environ)
    root = (project_root or Path(__file__).resolve().parent.parent).resolve()
    raw_config: dict[str, Any] = {}
    config_base = root

    config_path_value = env.get("OPENAI_IMAGE_MCP_CONFIG", "").strip()
    if config_path_value:
        config_path = _resolve_path(config_path_value, root)
        raw_config = _load_config_file(config_path)
        _reject_unknown_fields(raw_config, ROOT_CONFIG_FIELDS, "MCP config")
        config_base = config_path.parent

    profiles_value = raw_config.get("profiles", {})
    if profiles_value and not isinstance(profiles_value, dict):
        raise ConfigurationError("profiles must be a JSON object")
    raw_profiles: dict[str, Any] = dict(profiles_value or {})

    profiles_json = env.get("OPENAI_IMAGE_MCP_PROFILES_JSON", "").strip()
    if profiles_json:
        env_payload = _parse_json_env(profiles_json)
        if "profiles" in env_payload:
            _reject_unknown_fields(env_payload, {"profiles"}, "profiles JSON")
            env_profiles = env_payload["profiles"]
        else:
            env_profiles = env_payload
        if not isinstance(env_profiles, dict):
            raise ConfigurationError("profiles JSON must contain an object")
        raw_profiles.update(env_profiles)

    if not raw_profiles and env.get("OPENAI_IMAGE_BASE_URL", "").strip():
        raw_profiles["default"] = {
            "base_url": env["OPENAI_IMAGE_BASE_URL"],
            "api_key": env.get("OPENAI_IMAGE_API_KEY", ""),
            "protocol": env.get("OPENAI_IMAGE_PROTOCOL", "auto"),
            "image_model": env.get("OPENAI_IMAGE_MODEL", ""),
            "video_model": env.get("OPENAI_IMAGE_VIDEO_MODEL", "grok-imagine-video"),
        }

    profiles = {
        str(name): _build_profile(str(name), value, env)
        for name, value in raw_profiles.items()
        if str(name).strip()
    }
    default_profile = (
        env.get("OPENAI_IMAGE_MCP_DEFAULT_PROFILE", "").strip()
        or str(raw_config.get("default_profile", "default")).strip()
        or "default"
    )

    output_value = env.get("OPENAI_IMAGE_MCP_OUTPUT_DIR", "").strip() or raw_config.get(
        "output_dir", "outputs/mcp"
    )
    output_dir = _resolve_path(str(output_value), config_base)

    roots_env = env.get("OPENAI_IMAGE_MCP_ALLOWED_INPUT_ROOTS", "").strip()
    if roots_env:
        raw_roots = [item for item in roots_env.split(os.pathsep) if item]
    else:
        raw_roots_value = raw_config.get("allowed_input_roots", [str(root)])
        if not isinstance(raw_roots_value, list):
            raise ConfigurationError("allowed_input_roots must be an array")
        raw_roots = [str(item) for item in raw_roots_value]
    allowed_roots = tuple(dict.fromkeys(_resolve_path(item, config_base) for item in raw_roots))
    if not allowed_roots:
        raise ConfigurationError("at least one allowed_input_root is required")

    return MCPSettings(
        profiles=profiles,
        default_profile=default_profile,
        output_dir=output_dir,
        allowed_input_roots=allowed_roots,
        max_workers=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_MAX_WORKERS", raw_config.get("max_workers")),
            2,
            1,
            8,
            "max_workers",
        ),
        max_queue=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_MAX_QUEUE", raw_config.get("max_queue")),
            20,
            0,
            100,
            "max_queue",
        ),
        max_jobs=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_MAX_JOBS", raw_config.get("max_jobs")),
            200,
            1,
            1000,
            "max_jobs",
        ),
        job_ttl_seconds=_bounded_int(
            env.get("OPENAI_IMAGE_MCP_JOB_TTL", raw_config.get("job_ttl_seconds")),
            24 * 60 * 60,
            60,
            7 * 24 * 60 * 60,
            "job_ttl_seconds",
        ),
        max_reference_bytes=_bounded_int(
            raw_config.get("max_reference_bytes"),
            20 * 1024 * 1024,
            1024,
            100 * 1024 * 1024,
            "max_reference_bytes",
        ),
        max_output_bytes=_bounded_int(
            raw_config.get("max_output_bytes"),
            256 * 1024 * 1024,
            1024,
            1024 * 1024 * 1024,
            "max_output_bytes",
        ),
        max_asset_store_bytes=_bounded_int(
            env.get(
                "OPENAI_IMAGE_MCP_MAX_ASSET_STORE_BYTES",
                raw_config.get("max_asset_store_bytes"),
            ),
            2 * 1024 * 1024 * 1024,
            1024,
            100 * 1024 * 1024 * 1024,
            "max_asset_store_bytes",
        ),
    )
