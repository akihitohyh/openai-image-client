from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from .assets import Asset, AssetStore
from .profiles import MCPSettings, Profile


Processor = Callable[[dict[str, Any]], dict[str, Any]]
SIZE_PATTERN = re.compile(r"^(\d{2,5})x(\d{2,5})$")


class GenerationService:
    """Thin adapter around the existing web_app generation service."""

    def __init__(
        self,
        settings: MCPSettings,
        assets: AssetStore,
        processor: Processor | None = None,
    ) -> None:
        self.settings = settings
        self.assets = assets
        self._processor = processor

    @property
    def processor(self) -> Processor:
        if self._processor is None:
            from web_app import process_request_payload

            self._processor = process_request_payload
        return self._processor

    def sanitize_error(self, exc: BaseException) -> str:
        message = f"{type(exc).__name__}: {exc}".replace("\x00", "")
        for profile in self.settings.profiles.values():
            parsed = urlparse(profile.base_url)
            sensitive_values = [
                profile.api_key,
                profile.base_url,
                parsed.netloc,
                parsed.hostname or "",
            ]
            for value in sorted(set(sensitive_values), key=len, reverse=True):
                if value:
                    message = message.replace(value, "[redacted]")
        return message[:4000]

    @staticmethod
    def _validate_prompt(prompt: str) -> str:
        value = str(prompt).strip()
        if not value:
            raise ValueError("prompt must not be empty")
        if len(value) > 8000:
            raise ValueError("prompt must not exceed 8000 characters")
        return value

    @staticmethod
    def _validate_size(size: str, default: str) -> str:
        value = str(size).strip() or default
        match = SIZE_PATTERN.fullmatch(value.lower())
        if not match:
            raise ValueError("size must use WIDTHxHEIGHT syntax")
        width, height = int(match.group(1)), int(match.group(2))
        if not (16 <= width <= 8192 and 16 <= height <= 8192):
            raise ValueError("size dimensions must be between 16 and 8192")
        return f"{width}x{height}"

    @staticmethod
    def _validate_count(count: int, maximum: int) -> int:
        value = int(count)
        if not 1 <= value <= maximum:
            raise ValueError(f"count must be between 1 and {maximum}")
        return value

    @staticmethod
    def _validate_timeout(timeout: int | None, profile: Profile) -> int:
        value = profile.timeout if timeout is None else int(timeout)
        if not 5 <= value <= 600:
            raise ValueError("timeout must be between 5 and 600 seconds")
        return value

    @staticmethod
    def _copy_options(payload: dict[str, Any], options: Mapping[str, Any], allowed: set[str]) -> None:
        for key in allowed:
            value = options.get(key)
            if value not in {None, ""}:
                payload[key] = value

    def _base_payload(
        self,
        *,
        profile_name: str,
        prompt: str,
        model: str,
        size: str,
        count: int,
        timeout: int | None,
        video: bool,
    ) -> tuple[Profile, dict[str, Any]]:
        profile = self.settings.get_profile(profile_name)
        resolved_model = profile.resolve_model(model, video=video)
        payload = {
            "base_url": profile.base_url,
            "api_key": profile.api_key,
            "protocol": profile.protocol,
            "prompt": self._validate_prompt(prompt),
            "size": self._validate_size(size, "1920x1072" if video else "1024x1024"),
            "n": self._validate_count(count, 2 if video else 4),
            "timeout": self._validate_timeout(timeout, profile),
        }
        if resolved_model:
            payload["model"] = resolved_model
        return profile, payload

    def _materialize(self, raw: dict[str, Any], profile: Profile) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise RuntimeError("generation service returned a non-object result")
        media_items = raw.get("media")
        normalized: list[tuple[str, str]] = []
        if isinstance(media_items, list):
            for item in media_items:
                if not isinstance(item, dict):
                    continue
                media_type = str(item.get("type", "image")).strip().lower()
                value = str(item.get("url", "")).strip()
                if media_type in {"image", "video"} and value:
                    normalized.append((media_type, value))
        if not normalized:
            for key, media_type in (("images", "image"), ("videos", "video")):
                values = raw.get(key)
                if isinstance(values, list):
                    normalized.extend(
                        (media_type, str(value).strip())
                        for value in values
                        if str(value).strip()
                    )
        if not normalized:
            raise RuntimeError("generation completed without media output")

        assets = [
            self.assets.store_media_value(value, media_type=media_type, index=index)
            for index, (media_type, value) in enumerate(normalized, start=1)
        ]
        return {
            "ok": True,
            "profile": profile.name,
            "media_type": str(raw.get("media_type", assets[0].media_type)),
            "count": len(assets),
            "assets": [asset.public_dict() for asset in assets],
            "client_request_id": str(raw.get("client_request_id", "")) or None,
            "duration_ms": int(raw.get("duration_ms", 0) or 0),
        }

    def generate_image(
        self,
        *,
        profile_name: str,
        prompt: str,
        model: str = "",
        size: str = "1024x1024",
        count: int = 1,
        timeout: int | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile, payload = self._base_payload(
            profile_name=profile_name,
            prompt=prompt,
            model=model,
            size=size,
            count=count,
            timeout=timeout,
            video=False,
        )
        payload["mode"] = "generate"
        self._copy_options(
            payload,
            options or {},
            {
                "quality",
                "output_format",
                "output_compression",
                "background",
                "moderation",
                "aspect_ratio",
                "resolution",
                "response_format",
            },
        )
        return self._materialize(self.processor(payload), profile)

    def edit_image(
        self,
        *,
        profile_name: str,
        prompt: str,
        reference_asset_ids: Sequence[str],
        model: str = "",
        size: str = "1024x1024",
        count: int = 1,
        timeout: int | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        references = list(reference_asset_ids)
        if not 1 <= len(references) <= 16:
            raise ValueError("reference_asset_ids must contain between 1 and 16 images")
        profile, payload = self._base_payload(
            profile_name=profile_name,
            prompt=prompt,
            model=model,
            size=size,
            count=count,
            timeout=timeout,
            video=False,
        )
        payload.update(
            {
                "mode": "edit",
                "edit_images": [self.assets.as_data_url(asset_id) for asset_id in references],
            }
        )
        self._copy_options(
            payload,
            options or {},
            {
                "quality",
                "output_format",
                "output_compression",
                "background",
                "moderation",
                "aspect_ratio",
                "resolution",
                "response_format",
            },
        )
        return self._materialize(self.processor(payload), profile)

    def generate_video(
        self,
        *,
        profile_name: str,
        prompt: str,
        reference_asset_ids: Sequence[str] = (),
        model: str = "",
        size: str = "1920x1072",
        count: int = 1,
        timeout: int | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        references = list(reference_asset_ids)
        if len(references) > 7:
            raise ValueError("reference_asset_ids must not contain more than 7 images")
        profile, payload = self._base_payload(
            profile_name=profile_name,
            prompt=prompt,
            model=model,
            size=size,
            count=count,
            timeout=timeout,
            video=True,
        )
        payload["mode"] = "edit" if references else "video"
        if references:
            payload["edit_images"] = [self.assets.as_data_url(asset_id) for asset_id in references]
        self._copy_options(
            payload,
            options or {},
            {"aspect_ratio", "resolution", "video_resolution", "duration", "poll_interval"},
        )
        return self._materialize(self.processor(payload), profile)

    def import_reference(self, path: str) -> Asset:
        return self.assets.import_reference(path)
