from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .assets import AssetStore, StorageQuota
from .jobs import JobManager
from .profiles import MCPSettings, load_settings
from .service import GenerationService, Processor


@dataclass
class MCPRuntime:
    settings: MCPSettings
    assets: AssetStore
    service: GenerationService
    jobs: JobManager

    @classmethod
    def create(
        cls,
        settings: MCPSettings | None = None,
        *,
        processor: Processor | None = None,
        project_root: Path | None = None,
        executor: ThreadPoolExecutor | None = None,
        capacity: threading.BoundedSemaphore | None = None,
        submission_guard: Callable[[], None] | None = None,
        storage_quota: StorageQuota | None = None,
    ) -> "MCPRuntime":
        resolved_settings = settings or load_settings(project_root=project_root)
        assets = AssetStore(
            resolved_settings.output_dir,
            resolved_settings.allowed_input_roots,
            max_reference_bytes=resolved_settings.max_reference_bytes,
            max_output_bytes=resolved_settings.max_output_bytes,
            max_store_bytes=resolved_settings.max_asset_store_bytes,
            shared_quota=storage_quota,
        )
        service = GenerationService(resolved_settings, assets, processor=processor)
        jobs = JobManager(
            max_workers=resolved_settings.max_workers,
            max_queue=resolved_settings.max_queue,
            max_jobs=resolved_settings.max_jobs,
            ttl_seconds=resolved_settings.job_ttl_seconds,
            error_sanitizer=service.sanitize_error,
            executor=executor,
            capacity=capacity,
            submission_guard=submission_guard,
        )
        return cls(
            settings=resolved_settings,
            assets=assets,
            service=service,
            jobs=jobs,
        )

    def capabilities(
        self,
        include_paths: bool = True,
        transport: str = "stdio",
    ) -> dict[str, Any]:
        try:
            import web_app

            grok_image_models = sorted(web_app.GROK_IMAGE_GENERATION_MODELS)
            grok_video_models = sorted(web_app.GROK_VIDEO_MODELS)
            grok_ratios = sorted(web_app.GROK_SUPPORTED_RATIOS)
            grok_video_ratios = sorted(web_app.GROK_VIDEO_SUPPORTED_RATIOS)
        except (ImportError, AttributeError):
            grok_image_models = []
            grok_video_models = []
            grok_ratios = []
            grok_video_ratios = []
        security = {
            "credential_arguments_allowed": False,
            "base_url_argument_allowed": False,
            "max_reference_bytes": self.settings.max_reference_bytes,
            "max_output_bytes": self.settings.max_output_bytes,
            "max_workers": self.settings.max_workers,
            "max_queue": self.settings.max_queue,
            "max_jobs": self.settings.max_jobs,
            "max_asset_store_bytes": self.settings.max_asset_store_bytes,
        }
        if include_paths:
            security["allowed_input_roots"] = [str(path) for path in self.settings.allowed_input_roots]
        return {
            "service": "openai-image-client-mcp",
            "transport": transport,
            "tools": {
                "generate_image": {
                    "count": {"minimum": 1, "maximum": 4},
                    "size": "WIDTHxHEIGHT",
                    "output_formats": ["png", "jpeg", "webp"],
                },
                "edit_image": {"reference_images": {"minimum": 1, "maximum": 16}},
                "generate_video": {
                    "count": {"minimum": 1, "maximum": 2},
                    "reference_images": {"minimum": 0, "maximum": 7},
                    "duration_seconds": {"minimum": 1, "maximum": 15},
                },
            },
            "models": {
                "openai_image_default": "gpt-image-2",
                "grok_images": grok_image_models,
                "grok_videos": grok_video_models,
            },
            "grok_aspect_ratios": grok_ratios,
            "grok_video_aspect_ratios": grok_video_ratios,
            "security": security,
            "media_delivery": {
                "scheme": "image-client://assets/{asset_id}",
                "remote_video_results": "text/uri-list pointer resource",
            },
        }

    def close(self, wait: bool = False) -> None:
        self.jobs.close(wait=wait)
