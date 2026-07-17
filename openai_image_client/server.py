from __future__ import annotations

import base64
import json
import threading
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, Mapping

import anyio
from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.resources import FileResource
from pydantic import Field

from .assets import AssetError
from .jobs import JobQueueFull
from .profiles import ConfigurationError
from .runtime import MCPRuntime


Prompt = Annotated[str, Field(min_length=1, max_length=8000)]
ProfileName = Annotated[str, Field(max_length=100)]
ModelName = Annotated[str, Field(max_length=200)]
Size = Annotated[str, Field(pattern=r"^\d{2,5}x\d{2,5}$")]
Timeout = Annotated[int | None, Field(ge=5, le=600)]
AssetIds = Annotated[list[str], Field(min_length=1, max_length=16)]
VideoAssetIds = Annotated[list[str], Field(max_length=7)]
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REFERENCE_BASE64_CHARS = ((100 * 1024 * 1024 + 2) // 3) * 4

GENERATE_SCOPE = "image:generate"
READ_SCOPE = "image:read"
UPLOAD_SCOPE = "image:upload"
CANCEL_SCOPE = "image:cancel"

RuntimeResolver = Callable[[str], MCPRuntime]
BlockingRunner = Callable[
    [MCPRuntime | None, Callable[[], Any]],
    Awaitable[Any],
]


GENERATE_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
READ_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
IMPORT_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def _asset_items(payload: dict) -> list[dict]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    assets = result.get("assets", []) if isinstance(result, dict) else []
    if not assets and isinstance(payload.get("asset"), dict):
        assets = [payload["asset"]]
    return [item for item in assets if isinstance(item, dict)]


def _asset_links(payload: dict) -> list[types.ResourceLink]:
    links: list[types.ResourceLink] = []
    for item in _asset_items(payload):
        links.append(
            types.ResourceLink(
                type="resource_link",
                uri=str(item.get("uri", "")),
                name=str(item.get("name", "generated media")),
                mimeType=str(item.get("mime_type", "application/octet-stream")),
                size=int(item.get("size", 0) or 0),
                description=f"Generated {item.get('media_type', 'media')} asset",
            )
        )
    return links


def _call_result(
    payload: dict,
    runtime: MCPRuntime,
    register_asset: Callable[[str], None],
) -> types.CallToolResult:
    content: list[types.ContentBlock] = [
        types.TextContent(
            type="text",
            text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    ]
    content.extend(_asset_links(payload))
    for item in _asset_items(payload):
        asset_id = str(item.get("asset_id", ""))
        if not asset_id:
            continue
        register_asset(asset_id)
        if (
            str(item.get("media_type", "")) == "image"
            and str(item.get("mime_type", "")).startswith("image/")
            and str(item.get("storage", "")) == "file"
            and int(item.get("size", 0) or 0) <= MAX_INLINE_IMAGE_BYTES
        ):
            data = runtime.assets.read(asset_id)
            content.append(
                types.ImageContent(
                    type="image",
                    data=base64.b64encode(data).decode("ascii"),
                    mimeType=str(item["mime_type"]),
                )
            )
    return types.CallToolResult(content=content, structuredContent=payload)


async def _wait_for_job(
    runtime: MCPRuntime,
    job_id: str,
    ctx: Context,
    call_result: Callable[[dict], types.CallToolResult],
    run_blocking: BlockingRunner,
) -> types.CallToolResult:
    last_status = ""
    try:
        while True:
            job = runtime.jobs.get(job_id)
            if job is None:
                raise ToolError("job no longer exists")
            status = str(job["status"])
            if status != last_status:
                progress = {"queued": 0.05, "running": 0.25}.get(status, 1.0)
                await ctx.report_progress(progress, 1.0, f"job {status}")
                last_status = status
            if status == "succeeded":
                result = dict(job.get("result") or {})
                result["job_id"] = job_id
                result["job_uri"] = job["uri"]
                return await run_blocking(runtime, lambda: call_result(result))
            if status == "failed":
                raise ToolError(str(job.get("error") or "generation failed"))
            if status == "cancelled":
                raise ToolError("generation was cancelled")
            await anyio.sleep(0.2)
    except BaseException:
        runtime.jobs.cancel(job_id)
        raise


def build_server(
    runtime: MCPRuntime | None = None,
    *,
    runtime_resolver: RuntimeResolver | None = None,
    blocking_runner: BlockingRunner | None = None,
    remote: bool = False,
    max_resource_bytes: int | None = None,
    fastmcp_options: Mapping[str, Any] | None = None,
) -> FastMCP:
    if runtime is not None and runtime_resolver is not None:
        raise ValueError("runtime and runtime_resolver are mutually exclusive")
    fixed_runtime = runtime or (None if runtime_resolver else MCPRuntime.create())

    def resolve_runtime(required_scope: str) -> MCPRuntime:
        if runtime_resolver is not None:
            return runtime_resolver(required_scope)
        if fixed_runtime is None:  # pragma: no cover
            raise RuntimeError("MCP runtime is unavailable")
        return fixed_runtime

    def resolve_tool_runtime(required_scope: str) -> MCPRuntime:
        try:
            return resolve_runtime(required_scope)
        except PermissionError as exc:
            raise ToolError(str(exc)) from exc
        except OSError as exc:
            raise ToolError("tenant storage is unavailable") from exc
        except (ConfigurationError, JobQueueFull, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc

    async def run_blocking(
        active: MCPRuntime | None,
        operation: Callable[[], Any],
    ) -> Any:
        if blocking_runner is not None:
            return await blocking_runner(active, operation)
        return await anyio.to_thread.run_sync(operation)

    async def resolve_async(required_scope: str, *, tool: bool) -> MCPRuntime:
        resolver = resolve_tool_runtime if tool else resolve_runtime
        return await run_blocking(None, lambda: resolver(required_scope))

    mcp = FastMCP(
        "OpenAI Image Client",
        instructions=(
            "Generate and edit images through named server-side profiles. "
            "Never request API keys or base URLs from the user. Generated media is returned "
            "as image-client:// asset resources. Video generation is asynchronous."
        ),
        **dict(fastmcp_options or {}),
    )
    registered_asset_ids: set[str] = set()
    registered_asset_lock = threading.RLock()

    def register_asset(active: MCPRuntime, asset_id: str) -> None:
        if remote:
            return
        with registered_asset_lock:
            if asset_id in registered_asset_ids:
                return
            asset_value = active.assets.get(asset_id)
            mcp.add_resource(
                FileResource(
                    uri=asset_value.uri,
                    name=asset_value.name,
                    description=f"Stored {asset_value.media_type} asset",
                    mime_type=asset_value.mime_type,
                    path=asset_value.path,
                    is_binary=True,
                )
            )
            registered_asset_ids.add(asset_id)

    if fixed_runtime is not None and not remote:
        for existing_asset in fixed_runtime.assets.list():
            register_asset(fixed_runtime, existing_asset.asset_id)

    def call_result(payload: dict, active: MCPRuntime) -> types.CallToolResult:
        return _call_result(payload, active, lambda asset_id: register_asset(active, asset_id))

    @mcp.tool(
        title="Generate image",
        description="Generate images with a named profile and return asset resource links.",
        annotations=GENERATE_ANNOTATIONS,
        structured_output=False,
    )
    async def generate_image(
        prompt: Prompt,
        ctx: Context,
        profile: ProfileName = "",
        model: ModelName = "",
        size: Size = "1024x1024",
        count: Annotated[int, Field(ge=1, le=4)] = 1,
        timeout: Timeout = None,
        quality: Literal["", "auto", "low", "medium", "high"] = "",
        output_format: Literal["", "png", "jpeg", "webp"] = "",
        output_compression: Annotated[int | None, Field(ge=0, le=100)] = None,
        background: Literal["", "auto", "opaque", "transparent"] = "",
        moderation: Literal["", "auto", "low"] = "",
        aspect_ratio: str = "",
        resolution: Literal["", "1k", "2k"] = "",
        response_format: Literal["", "url", "b64_json"] = "",
    ) -> types.CallToolResult:
        """Submit one bounded image job and wait for its resource-backed result."""
        active = await resolve_async(GENERATE_SCOPE, tool=True)
        try:
            job_id = await run_blocking(
                active,
                lambda: active.jobs.submit(
                    "generate_image",
                    lambda: active.service.generate_image(
                        profile_name=profile,
                        prompt=prompt,
                        model=model,
                        size=size,
                        count=count,
                        timeout=timeout,
                        options={
                            "quality": quality,
                            "output_format": output_format,
                            "output_compression": output_compression,
                            "background": background,
                            "moderation": moderation,
                            "aspect_ratio": aspect_ratio,
                            "resolution": resolution,
                            "response_format": response_format,
                        },
                    ),
                ),
            )
        except (ConfigurationError, JobQueueFull, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return await _wait_for_job(
            active,
            job_id,
            ctx,
            lambda payload: call_result(payload, active),
            run_blocking,
        )

    if remote:

        @mcp.tool(
            title="Upload reference image",
            description="Upload a bounded Base64 PNG, JPEG, or WebP reference and return an asset ID.",
            annotations=IMPORT_ANNOTATIONS,
            structured_output=False,
        )
        async def upload_reference_image(
            data_base64: Annotated[
                str,
                Field(min_length=1, max_length=MAX_REFERENCE_BASE64_CHARS),
            ],
            filename: Annotated[str, Field(max_length=255)] = "reference-image",
        ) -> types.CallToolResult:
            """Store a validated reference upload inside the authenticated tenant."""
            active = await resolve_async(UPLOAD_SCOPE, tool=True)
            try:
                asset = await run_blocking(
                    active,
                    lambda: active.assets.upload_reference(data_base64, name=filename),
                )
            except AssetError as exc:
                raise ToolError(str(exc)) from exc
            except OSError as exc:
                raise ToolError("tenant storage is unavailable") from exc
            return await run_blocking(
                active,
                lambda: call_result({"ok": True, "asset": asset.public_dict()}, active),
            )

    else:

        @mcp.tool(
            title="Import reference image",
            description="Import an image from a configured allowed local directory and return an asset ID.",
            annotations=IMPORT_ANNOTATIONS,
            structured_output=False,
        )
        async def import_reference_image(
            path: Annotated[str, Field(min_length=1, max_length=4096)],
        ) -> types.CallToolResult:
            """Register a bounded local PNG, JPEG, or WebP image for later edits."""
            active = await resolve_async(UPLOAD_SCOPE, tool=True)
            try:
                asset = await run_blocking(
                    active,
                    lambda: active.service.import_reference(path),
                )
            except AssetError as exc:
                raise ToolError(str(exc)) from exc
            except OSError as exc:
                raise ToolError("asset storage is unavailable") from exc
            return await run_blocking(
                active,
                lambda: call_result({"ok": True, "asset": asset.public_dict()}, active),
            )

    @mcp.tool(
        title="Edit image",
        description="Edit imported image assets and return generated asset resource links.",
        annotations=GENERATE_ANNOTATIONS,
        structured_output=False,
    )
    async def edit_image(
        prompt: Prompt,
        reference_asset_ids: AssetIds,
        ctx: Context,
        profile: ProfileName = "",
        model: ModelName = "",
        size: Size = "1024x1024",
        count: Annotated[int, Field(ge=1, le=4)] = 1,
        timeout: Timeout = None,
        quality: Literal["", "auto", "low", "medium", "high"] = "",
        output_format: Literal["", "png", "jpeg", "webp"] = "",
        output_compression: Annotated[int | None, Field(ge=0, le=100)] = None,
        background: Literal["", "auto", "opaque", "transparent"] = "",
        moderation: Literal["", "auto", "low"] = "",
        aspect_ratio: str = "",
        resolution: Literal["", "1k", "2k"] = "",
        response_format: Literal["", "url", "b64_json"] = "",
    ) -> types.CallToolResult:
        """Submit one bounded edit job and wait for its resource-backed result."""
        active = await resolve_async(GENERATE_SCOPE, tool=True)
        try:
            job_id = await run_blocking(
                active,
                lambda: active.jobs.submit(
                    "edit_image",
                    lambda: active.service.edit_image(
                        profile_name=profile,
                        prompt=prompt,
                        reference_asset_ids=reference_asset_ids,
                        model=model,
                        size=size,
                        count=count,
                        timeout=timeout,
                        options={
                            "quality": quality,
                            "output_format": output_format,
                            "output_compression": output_compression,
                            "background": background,
                            "moderation": moderation,
                            "aspect_ratio": aspect_ratio,
                            "resolution": resolution,
                            "response_format": response_format,
                        },
                    ),
                ),
            )
        except (AssetError, ConfigurationError, JobQueueFull, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return await _wait_for_job(
            active,
            job_id,
            ctx,
            lambda payload: call_result(payload, active),
            run_blocking,
        )

    @mcp.tool(
        title="Start video generation",
        description="Start a long-running Grok video job and immediately return its job URI.",
        annotations=GENERATE_ANNOTATIONS,
        structured_output=False,
    )
    async def generate_video(
        prompt: Prompt,
        profile: ProfileName = "",
        reference_asset_ids: VideoAssetIds | None = None,
        model: ModelName = "",
        size: Size = "1920x1072",
        count: Annotated[int, Field(ge=1, le=2)] = 1,
        timeout: Timeout = None,
        aspect_ratio: str = "",
        resolution: Literal["", "480p", "720p", "1080p"] = "",
        duration: Annotated[int, Field(ge=0, le=15)] = 0,
        poll_interval: Annotated[int, Field(ge=1, le=30)] = 5,
    ) -> types.CallToolResult:
        """Queue video generation; poll with get_job or the returned job resource."""
        active = await resolve_async(GENERATE_SCOPE, tool=True)
        references = list(reference_asset_ids or [])
        try:
            job_id = await run_blocking(
                active,
                lambda: active.jobs.submit(
                    "generate_video",
                    lambda: active.service.generate_video(
                        profile_name=profile,
                        prompt=prompt,
                        reference_asset_ids=references,
                        model=model,
                        size=size,
                        count=count,
                        timeout=timeout,
                        options={
                            "aspect_ratio": aspect_ratio,
                            "resolution": resolution,
                            "duration": duration,
                            "poll_interval": poll_interval,
                        },
                    ),
                ),
            )
        except (AssetError, ConfigurationError, JobQueueFull, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        job = await run_blocking(active, lambda: active.jobs.get(job_id))
        if job is None:  # pragma: no cover
            raise ToolError("job disappeared after submission")
        return await run_blocking(active, lambda: call_result(job, active))

    @mcp.tool(
        title="Get generation job",
        description="Read a generation job and include resource links when it has completed.",
        annotations=READ_ANNOTATIONS,
        structured_output=False,
    )
    async def get_job(
        job_id: Annotated[str, Field(min_length=32, max_length=32)],
    ) -> types.CallToolResult:
        """Return the current state of an image or video job."""
        active = await resolve_async(READ_SCOPE, tool=True)
        job = await run_blocking(active, lambda: active.jobs.get(job_id))
        if job is None:
            raise ToolError("job does not exist or has expired")
        return await run_blocking(active, lambda: call_result(job, active))

    @mcp.tool(
        title="Cancel generation job",
        description="Request cancellation of a queued or running generation job.",
        annotations=types.ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=False,
    )
    async def cancel_job(
        job_id: Annotated[str, Field(min_length=32, max_length=32)],
    ) -> types.CallToolResult:
        """Cancel queued work; running requests are discarded after the provider returns."""
        active = await resolve_async(CANCEL_SCOPE, tool=True)
        job = await run_blocking(active, lambda: active.jobs.cancel(job_id))
        if job is None:
            raise ToolError("job does not exist or has expired")
        return await run_blocking(active, lambda: call_result(job, active))

    @mcp.resource(
        "image-client://capabilities",
        name="capabilities",
        description="Supported tools, provider options, and server safety limits.",
        mime_type="application/json",
    )
    async def capabilities() -> str:
        active = await resolve_async(READ_SCOPE, tool=False)

        def read_capabilities() -> str:
            payload = active.capabilities(
                include_paths=not remote,
                transport="streamable-http" if remote else "stdio",
            )
            if remote and max_resource_bytes is not None:
                payload["security"]["max_resource_response_bytes"] = max_resource_bytes
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)

        return await run_blocking(active, read_capabilities)

    @mcp.resource(
        "image-client://profiles",
        name="profiles",
        description="Configured profile names and non-secret capabilities.",
        mime_type="application/json",
    )
    async def profiles() -> str:
        active = await resolve_async(READ_SCOPE, tool=False)
        return await run_blocking(
            active,
            lambda: json.dumps(
                active.settings.public_profiles(),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    @mcp.resource(
        "image-client://jobs",
        name="jobs",
        description="Recent image and video generation jobs.",
        mime_type="application/json",
    )
    async def jobs() -> str:
        active = await resolve_async(READ_SCOPE, tool=False)

        def read_jobs() -> str:
            values = active.jobs.list()
            for value in values:
                for item in _asset_items(value):
                    register_asset(active, str(item.get("asset_id", "")))
            return json.dumps({"jobs": values}, ensure_ascii=False, sort_keys=True)

        return await run_blocking(active, read_jobs)

    @mcp.resource(
        "image-client://jobs/{job_id}",
        name="job",
        description="Current state and result metadata for a generation job.",
        mime_type="application/json",
    )
    async def job(job_id: str) -> str:
        active = await resolve_async(READ_SCOPE, tool=False)

        def read_job() -> str:
            value = active.jobs.get(job_id)
            if value is None:
                raise ValueError("job does not exist or has expired")
            for item in _asset_items(value):
                register_asset(active, str(item.get("asset_id", "")))
            return json.dumps(value, ensure_ascii=False, sort_keys=True)

        return await run_blocking(active, read_job)

    @mcp.resource(
        "image-client://assets",
        name="assets",
        description="Recent generated and imported media assets.",
        mime_type="application/json",
    )
    async def assets() -> str:
        active = await resolve_async(READ_SCOPE, tool=False)
        return await run_blocking(
            active,
            lambda: json.dumps(
                {"assets": [asset.public_dict() for asset in active.assets.list()]},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    @mcp.resource(
        "image-client://assets/{asset_id}",
        name="asset",
        description="Binary media content or a text/uri-list pointer for remote video output.",
        mime_type="application/octet-stream",
    )
    async def asset(asset_id: str) -> bytes:
        active = await resolve_async(READ_SCOPE, tool=False)

        def read_asset() -> bytes:
            asset_value = active.assets.get(asset_id)
            try:
                stored_size = asset_value.path.stat().st_size
            except OSError as exc:
                raise AssetError("asset storage is unavailable") from exc
            if (
                remote
                and max_resource_bytes is not None
                and max(asset_value.size, stored_size) > max_resource_bytes
            ):
                raise AssetError("asset exceeds the remote resource response limit")
            return active.assets.read(asset_id)

        return await run_blocking(active, read_asset)

    @mcp.prompt(
        name="compose_image_prompt",
        description="Compose a precise image-generation prompt before calling generate_image.",
    )
    def compose_image_prompt(
        subject: str,
        style: str = "",
        composition: str = "",
        lighting: str = "",
        constraints: str = "",
    ) -> str:
        return (
            "Create a concise image-generation prompt from this brief. State the subject first, "
            "then composition, style, lighting, and explicit constraints. Do not invent text or logos.\n"
            f"Subject: {subject}\nComposition: {composition}\nStyle: {style}\n"
            f"Lighting: {lighting}\nConstraints: {constraints}"
        )

    @mcp.prompt(
        name="compose_edit_prompt",
        description="Compose an image-edit instruction that separates changes from preserved details.",
    )
    def compose_edit_prompt(change: str, preserve: str = "", constraints: str = "") -> str:
        return (
            "Write a direct image-edit prompt. Explicitly separate requested changes from details "
            "that must remain unchanged.\n"
            f"Change: {change}\nPreserve: {preserve}\nConstraints: {constraints}"
        )

    return mcp
