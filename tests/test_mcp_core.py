from __future__ import annotations

import base64
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from openai_image_client.assets import AssetError, AssetStore, StorageQuota
from openai_image_client.jobs import JobManager, JobQueueFull
from openai_image_client.profiles import MCPSettings, Profile, load_settings
from openai_image_client.runtime import MCPRuntime
from openai_image_client.service import GenerationService


def png_bytes(color: tuple[int, int, int] = (20, 40, 60)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 3), color).save(output, format="PNG")
    return output.getvalue()


def make_settings(tmp_path: Path, *, max_workers: int = 1, max_queue: int = 1) -> MCPSettings:
    return MCPSettings(
        profiles={
            "default": Profile(
                name="default",
                base_url="https://provider.example/v1",
                api_key="top-secret-key",
                protocol="openai",
                image_model="gpt-image-2",
                allowed_models=("gpt-image-2",),
                timeout=120,
            )
        },
        default_profile="default",
        output_dir=tmp_path / "assets",
        allowed_input_roots=(tmp_path.resolve(),),
        max_workers=max_workers,
        max_queue=max_queue,
    )


def make_asset_store(settings: MCPSettings) -> AssetStore:
    return AssetStore(
        settings.output_dir,
        settings.allowed_input_roots,
        max_reference_bytes=settings.max_reference_bytes,
        max_output_bytes=settings.max_output_bytes,
    )


def test_profiles_from_env_are_named_and_public_view_redacts_secrets(tmp_path: Path) -> None:
    env = {
        "OPENAI_IMAGE_MCP_PROFILES_JSON": json.dumps(
            {
                "profiles": {
                    "primary": {
                        "base_url": "https://provider.example/v1",
                        "api_key_env": "PROVIDER_KEY",
                        "protocol": "openai",
                    }
                }
            }
        ),
        "PROVIDER_KEY": "secret-value",
        "OPENAI_IMAGE_MCP_DEFAULT_PROFILE": "primary",
    }
    settings = load_settings(environ=env, project_root=tmp_path)
    assert settings.get_profile().api_key == "secret-value"
    public = json.dumps(settings.public_profiles())
    assert "primary" in public
    assert "secret-value" not in public
    assert "provider.example" not in public
    assert "secret-value" not in repr(settings.get_profile())
    assert "provider.example" not in repr(settings.get_profile())


def test_import_reference_enforces_allowed_root_and_materializes_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = make_asset_store(settings)
    source = tmp_path / "reference.png"
    source.write_bytes(png_bytes())

    asset = store.import_reference(str(source))
    assert asset.mime_type == "image/png"
    assert store.read(asset.asset_id) == source.read_bytes()
    assert store.as_data_url(asset.asset_id)["data_url"].startswith("data:image/png;base64,")

    outside = tmp_path.parent / "outside-reference.png"
    outside.write_bytes(png_bytes())
    try:
        with pytest.raises(AssetError, match="outside allowed roots"):
            store.import_reference(str(outside))
    finally:
        outside.unlink(missing_ok=True)


def test_asset_store_quota_fails_closed(tmp_path: Path) -> None:
    data = png_bytes()
    store = AssetStore(
        tmp_path / "quota-assets",
        (tmp_path.resolve(),),
        max_reference_bytes=1024 * 1024,
        max_output_bytes=1024 * 1024,
        max_store_bytes=len(data),
    )
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(data)
    second.write_bytes(data)
    store.import_reference(str(first))
    with pytest.raises(AssetError, match="quota exceeded"):
        store.import_reference(str(second))


def test_shared_storage_quota_reservation_is_atomic() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        StorageQuota(max_bytes=0)
    with pytest.raises(ValueError, match="non-negative integer"):
        StorageQuota(max_bytes=1, used_bytes=-1)
    with pytest.raises(AssetError, match="quota exceeded"):
        StorageQuota(max_bytes=1, used_bytes=2)

    quota = StorageQuota(max_bytes=1)
    barrier = threading.Barrier(8)

    def reserve() -> bool:
        barrier.wait()
        try:
            quota.reserve(1)
        except AssetError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: reserve(), range(8)))

    assert results.count(True) == 1
    assert quota.used_bytes == 1
    quota.release(1)
    assert quota.used_bytes == 0


def test_asset_write_failure_releases_shared_storage_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = png_bytes()
    quota = StorageQuota(max_bytes=len(data))
    store = AssetStore(
        tmp_path / "shared-quota-assets",
        (tmp_path.resolve(),),
        max_reference_bytes=1024 * 1024,
        max_output_bytes=1024 * 1024,
        shared_quota=quota,
    )
    encoded = base64.b64encode(data).decode("ascii")

    def fail_metadata_write(*args, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail_metadata_write)
        with pytest.raises(AssetError, match="asset storage is unavailable") as error:
            store.upload_reference(encoded)

    assert str(tmp_path) not in str(error.value)
    assert quota.used_bytes == 0
    assert not any(store.root.iterdir())
    store.upload_reference(encoded)
    assert quota.used_bytes == len(data)


def test_failed_asset_cleanup_retains_shared_storage_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = png_bytes()
    quota = StorageQuota(max_bytes=len(data))
    store = AssetStore(
        tmp_path / "failed-cleanup-assets",
        (tmp_path.resolve(),),
        max_reference_bytes=1024 * 1024,
        max_output_bytes=1024 * 1024,
        shared_quota=quota,
    )
    encoded = base64.b64encode(data).decode("ascii")
    original_unlink = Path.unlink

    def fail_metadata_write(*args, **kwargs):
        raise OSError("metadata write failed")

    def fail_asset_unlink(path: Path, *args, **kwargs):
        if path.suffix == ".png":
            raise OSError(f"cannot remove {path}")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail_metadata_write)
        patch.setattr(Path, "unlink", fail_asset_unlink)
        with pytest.raises(AssetError, match="asset storage is unavailable") as error:
            store.upload_reference(encoded)

    assert str(tmp_path) not in str(error.value)
    assert quota.used_bytes == len(data)
    assert store._stored_bytes == len(data)
    with pytest.raises(AssetError, match="quota exceeded"):
        store.upload_reference(encoded)


def test_input_read_errors_do_not_disclose_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    store = make_asset_store(settings)
    source = tmp_path / "private-reference.png"
    source.write_bytes(png_bytes())
    original_open = Path.open

    def fail_source_open(path: Path, *args, **kwargs):
        if path == source:
            raise OSError(f"cannot open {path}")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_source_open)
        with pytest.raises(AssetError, match="cannot read input file") as error:
            store.import_reference(str(source))

    assert str(tmp_path) not in str(error.value)


def test_service_injects_profile_credentials_but_returns_only_asset_metadata(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = make_asset_store(settings)
    observed: list[dict] = []
    encoded = base64.b64encode(png_bytes()).decode("ascii")

    def processor(payload: dict) -> dict:
        observed.append(payload)
        return {
            "ok": True,
            "media_type": "image",
            "media": [{"type": "image", "url": f"data:image/png;base64,{encoded}"}],
            "raw": {"api_key": payload["api_key"], "base_url": payload["base_url"]},
            "client_request_id": "request-1",
            "duration_ms": 10,
        }

    service = GenerationService(settings, store, processor=processor)
    result = service.generate_image(
        profile_name="default",
        prompt="A test image",
        model="gpt-image-2",
    )

    assert observed[0]["api_key"] == "top-secret-key"
    assert observed[0]["base_url"] == "https://provider.example/v1"
    serialized = json.dumps(result)
    assert "top-secret-key" not in serialized
    assert "provider.example" not in serialized
    assert result["assets"][0]["uri"].startswith("image-client://assets/")
    assert "data:image" not in serialized
    error = service.sanitize_error(
        RuntimeError("top-secret-key https://provider.example/v1 provider.example")
    )
    assert "top-secret-key" not in error
    assert "provider.example" not in error


def test_edit_resolves_reference_asset_to_internal_data_url(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = make_asset_store(settings)
    source = tmp_path / "reference.png"
    source.write_bytes(png_bytes())
    reference = store.import_reference(str(source))
    observed: dict = {}
    encoded = base64.b64encode(png_bytes((80, 70, 60))).decode("ascii")

    def processor(payload: dict) -> dict:
        observed.update(payload)
        return {
            "media_type": "image",
            "images": [f"data:image/png;base64,{encoded}"],
        }

    service = GenerationService(settings, store, processor=processor)
    result = service.edit_image(
        profile_name="default",
        prompt="Change the background",
        reference_asset_ids=[reference.asset_id],
        model="gpt-image-2",
    )
    assert observed["mode"] == "edit"
    assert observed["edit_images"][0]["data_url"].startswith("data:image/png;base64,")
    assert "data_url" not in json.dumps(result)


def test_job_manager_bounds_queue_and_redacts_errors() -> None:
    gate = threading.Event()
    jobs = JobManager(
        max_workers=1,
        max_queue=0,
        ttl_seconds=60,
        error_sanitizer=lambda exc: str(exc).replace("secret", "[redacted]"),
    )
    first = jobs.submit("blocked", lambda: (gate.wait(2), {"ok": True})[1])
    with pytest.raises(JobQueueFull):
        jobs.submit("overflow", lambda: {"ok": True})
    gate.set()
    deadline = time.time() + 3
    while time.time() < deadline and jobs.get(first)["status"] not in {"succeeded", "failed"}:
        time.sleep(0.02)
    assert jobs.get(first)["status"] == "succeeded"

    failed = jobs.submit("failed", lambda: (_ for _ in ()).throw(RuntimeError("secret failure")))
    deadline = time.time() + 3
    while time.time() < deadline and jobs.get(failed)["status"] not in {"succeeded", "failed"}:
        time.sleep(0.02)
    assert jobs.get(failed)["error"] == "[redacted] failure"
    jobs.close(wait=True)


def test_runtime_job_error_redacts_profile_credentials_and_endpoint(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    def failing_processor(payload: dict) -> dict:
        raise RuntimeError(
            f"failed at {payload['base_url']}/images with {payload['api_key']} "
            "from provider.example"
        )

    runtime = MCPRuntime.create(settings, processor=failing_processor)
    job_id = runtime.jobs.submit(
        "generate_image",
        lambda: runtime.service.generate_image(
            profile_name="default",
            prompt="Trigger a sanitized failure",
        ),
    )
    deadline = time.time() + 3
    while time.time() < deadline and runtime.jobs.get(job_id)["status"] not in {"failed", "succeeded"}:
        time.sleep(0.02)
    job = runtime.jobs.get(job_id)
    serialized = json.dumps(job)
    assert job["status"] == "failed"
    assert "top-secret-key" not in serialized
    assert "provider.example" not in serialized
    assert "https://provider.example/v1" not in serialized
    assert "[redacted]" in serialized
    runtime.close(wait=True)


def test_job_registry_evicts_oldest_terminal_and_rejects_when_all_active() -> None:
    jobs = JobManager(max_workers=1, max_queue=1, max_jobs=2, ttl_seconds=60)

    def wait_done(job_id: str) -> None:
        deadline = time.time() + 3
        while time.time() < deadline and jobs.get(job_id)["status"] not in {"succeeded", "failed"}:
            time.sleep(0.01)

    first = jobs.submit("first", lambda: {"order": 1})
    wait_done(first)
    second = jobs.submit("second", lambda: {"order": 2})
    wait_done(second)
    third = jobs.submit("third", lambda: {"order": 3})
    wait_done(third)
    assert jobs.get(first) is None
    assert jobs.get(second)["status"] == "succeeded"
    assert jobs.get(third)["status"] == "succeeded"
    jobs.close(wait=True)

    gate = threading.Event()
    active_jobs = JobManager(max_workers=1, max_queue=1, max_jobs=1, ttl_seconds=60)
    active_jobs.submit("active", lambda: (gate.wait(2), {"ok": True})[1])
    with pytest.raises(JobQueueFull, match="registry is full"):
        active_jobs.submit("rejected", lambda: {"ok": True})
    gate.set()
    active_jobs.close(wait=True)
