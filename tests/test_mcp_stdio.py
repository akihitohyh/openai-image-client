from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import AnyUrl
from PIL import Image


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (5, 4), (12, 34, 56)).save(output, format="PNG")
    return output.getvalue()


class MockUpstreamHandler(BaseHTTPRequestHandler):
    image_b64 = base64.b64encode(png_bytes()).decode("ascii")
    observed_authorizations: list[str] = []

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            self.rfile.read(content_length)
        self.observed_authorizations.append(self.headers.get("Authorization", ""))
        if self.path.endswith("/videos/generations"):
            payload = {"video": {"url": "https://1.1.1.1/generated-video.mp4"}}
        elif self.path.endswith(("/images/generations", "/images/edits")):
            payload = {"data": [{"b64_json": self.image_b64, "output_format": "png"}]}
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def mock_upstream() -> tuple[ThreadingHTTPServer, str]:
    MockUpstreamHandler.observed_authorizations = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.anyio
async def test_real_stdio_protocol_and_media_job_flow(
    tmp_path: Path,
    mock_upstream: tuple[ThreadingHTTPServer, str],
) -> None:
    _, base_url = mock_upstream
    reference = tmp_path / "reference.png"
    reference.write_bytes(png_bytes())
    output_dir = tmp_path / "mcp-assets"
    secret = "stdio-test-secret"
    profiles = {
        "profiles": {
            "default": {
                "base_url": base_url,
                "api_key_env": "TEST_UPSTREAM_KEY",
                "protocol": "openai",
                "image_model": "gpt-image-2",
                "allowed_models": ["gpt-image-2"],
                "timeout": 30,
            },
            "video": {
                "base_url": base_url,
                "api_key_env": "TEST_UPSTREAM_KEY",
                "protocol": "grok",
                "video_model": "grok-imagine-video",
                "allowed_models": ["grok-imagine-video"],
                "timeout": 30,
            },
        }
    }
    env = dict(os.environ)
    env.update(
        {
            "OPENAI_IMAGE_MCP_PROFILES_JSON": json.dumps(profiles),
            "OPENAI_IMAGE_MCP_DEFAULT_PROFILE": "default",
            "OPENAI_IMAGE_MCP_OUTPUT_DIR": str(output_dir),
            "OPENAI_IMAGE_MCP_ALLOWED_INPUT_ROOTS": str(tmp_path),
            "TEST_UPSTREAM_KEY": secret,
            "NO_PROXY": "127.0.0.1,localhost",
            "OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS": "127.0.0.1",
        }
    )
    project_root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(project_root / "mcp_server.py")],
        env=env,
        cwd=project_root,
    )

    observed_responses: list[str] = []
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "OpenAI Image Client"

            listed_tools = await session.list_tools()
            names = {tool.name for tool in listed_tools.tools}
            assert {
                "generate_image",
                "import_reference_image",
                "edit_image",
                "generate_video",
                "get_job",
                "cancel_job",
            } <= names
            for tool in listed_tools.tools:
                properties = set(tool.inputSchema.get("properties", {}))
                assert "api_key" not in properties
                assert "base_url" not in properties

            listed_resources = await session.list_resources()
            assert "image-client://profiles" in {str(item.uri) for item in listed_resources.resources}
            profiles_resource = await session.read_resource(AnyUrl("image-client://profiles"))
            profiles_text = profiles_resource.contents[0].text
            observed_responses.append(profiles_text)
            assert "default" in profiles_text and "video" in profiles_text
            capabilities_resource = await session.read_resource(
                AnyUrl("image-client://capabilities")
            )
            capabilities_payload = json.loads(capabilities_resource.contents[0].text)
            assert capabilities_payload["security"]["max_jobs"] == 200
            assert capabilities_payload["security"]["max_asset_store_bytes"] > 0

            imported = await session.call_tool(
                "import_reference_image",
                {"path": str(reference)},
            )
            assert not imported.isError
            imported_payload = imported.structuredContent
            assert imported_payload is not None
            reference_asset_id = imported_payload["asset"]["asset_id"]
            observed_responses.append(json.dumps(imported_payload))

            generated = await session.call_tool(
                "generate_image",
                {"prompt": "A protocol test image", "profile": "default"},
            )
            assert not generated.isError
            generated_payload = generated.structuredContent
            assert generated_payload is not None
            generated_asset = generated_payload["assets"][0]
            observed_responses.append(json.dumps(generated_payload))
            image_blocks = [item for item in generated.content if item.type == "image"]
            assert len(image_blocks) == 1
            assert image_blocks[0].mimeType == "image/png"
            generated_resource = await session.read_resource(AnyUrl(generated_asset["uri"]))
            assert generated_resource.contents[0].mimeType == "image/png"
            assert base64.b64decode(generated_resource.contents[0].blob).startswith(b"\x89PNG")

            edited = await session.call_tool(
                "edit_image",
                {
                    "prompt": "Keep the subject and change the background",
                    "profile": "default",
                    "reference_asset_ids": [reference_asset_id],
                },
            )
            assert not edited.isError
            assert edited.structuredContent["count"] == 1
            observed_responses.append(json.dumps(edited.structuredContent))

            started = await session.call_tool(
                "generate_video",
                {"prompt": "Slow camera push in", "profile": "video"},
            )
            assert not started.isError
            job_id = started.structuredContent["job_id"]
            observed_responses.append(json.dumps(started.structuredContent))

            completed_payload = None
            with anyio.fail_after(5):
                while completed_payload is None:
                    polled = await session.call_tool("get_job", {"job_id": job_id})
                    assert not polled.isError
                    if polled.structuredContent["status"] == "succeeded":
                        completed_payload = polled.structuredContent
                        break
                    if polled.structuredContent["status"] == "failed":
                        pytest.fail(str(polled.structuredContent.get("error")))
                    await anyio.sleep(0.05)
            observed_responses.append(json.dumps(completed_payload))
            video_asset = completed_payload["result"]["assets"][0]
            assert video_asset["storage"] == "url-pointer"

            job_resource = await session.read_resource(
                AnyUrl(f"image-client://jobs/{job_id}")
            )
            assert '"status": "succeeded"' in job_resource.contents[0].text
            video_resource = await session.read_resource(AnyUrl(video_asset["uri"]))
            video_pointer = base64.b64decode(video_resource.contents[0].blob).decode("utf-8")
            assert video_pointer == "https://1.1.1.1/generated-video.mp4\n"

            prompt = await session.get_prompt(
                "compose_image_prompt",
                {"subject": "a red bicycle", "style": "editorial"},
            )
            assert "red bicycle" in prompt.messages[0].content.text

    assert MockUpstreamHandler.observed_authorizations
    assert set(MockUpstreamHandler.observed_authorizations) == {f"Bearer {secret}"}
    serialized_responses = "\n".join(observed_responses)
    assert secret not in serialized_responses
    assert base_url not in serialized_responses
