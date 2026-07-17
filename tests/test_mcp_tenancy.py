from __future__ import annotations

import base64
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from openai_image_client.assets import AssetError
from openai_image_client.http_auth import HTTPAuthConfig, TenantGrant
from openai_image_client.jobs import JobQueueFull
from openai_image_client.profiles import ConfigurationError, MCPSettings, Profile
from openai_image_client.tenancy import TenantRateLimitExceeded, TenantRuntimeRegistry


def png_bytes(color: tuple[int, int, int] = (30, 60, 90)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 3), color).save(output, format="PNG")
    return output.getvalue()


def grant(
    tenant_id: str,
    profile: str,
    *,
    requests_per_minute: int = 60,
    operations_per_minute: int = 300,
    expires_at: int | None = None,
) -> TenantGrant:
    return TenantGrant(
        tenant_id=tenant_id,
        allowed_profiles=(profile,),
        default_profile=profile,
        scopes=("image:generate",),
        requests_per_minute=requests_per_minute,
        expires_at=expires_at,
        operations_per_minute=operations_per_minute,
    )


def settings(tmp_path: Path) -> MCPSettings:
    return MCPSettings(
        profiles={
            "openai": Profile(
                name="openai",
                base_url="https://openai.example/v1",
                api_key="openai-secret",
                protocol="openai",
            ),
            "grok": Profile(
                name="grok",
                base_url="https://grok.example/v1",
                api_key="grok-secret",
                protocol="grok",
            ),
        },
        default_profile="openai",
        output_dir=tmp_path / "assets",
        allowed_input_roots=(tmp_path,),
        max_workers=2,
        max_queue=4,
        max_jobs=20,
    )


def auth_config(*grants: TenantGrant) -> HTTPAuthConfig:
    return HTTPAuthConfig(
        grants_by_tenant={item.tenant_id: item for item in grants},
        grants_by_token_digest={},
    )


def wait_for_job(runtime, job_id: str) -> dict:
    deadline = time.time() + 3
    while time.time() < deadline:
        job = runtime.jobs.get(job_id)
        if job is not None and job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_registry_filters_profiles_paths_and_isolates_jobs_and_assets(tmp_path: Path) -> None:
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(grant("tenant-a", "openai"), grant("tenant-b", "grok")),
        global_workers=2,
        global_queue=4,
    )
    tenant_a = registry.get_runtime("tenant-a")
    tenant_b = registry.get_runtime("tenant-b")

    assert registry.get("tenant-a") is tenant_a
    assert set(tenant_a.settings.profiles) == {"openai"}
    assert set(tenant_b.settings.profiles) == {"grok"}
    assert tenant_a.settings.default_profile == "openai"
    assert tenant_b.settings.default_profile == "grok"
    assert tenant_a.settings.output_dir == tmp_path / "assets" / "tenants" / "tenant-a"
    assert tenant_b.settings.output_dir == tmp_path / "assets" / "tenants" / "tenant-b"
    assert tenant_a.settings.allowed_input_roots == (
        tmp_path / "assets" / "tenants" / "tenant-a" / "inputs",
    )
    assert "allowed_input_roots" in tenant_a.capabilities(include_paths=True)["security"]
    assert "allowed_input_roots" not in tenant_a.capabilities(include_paths=False)["security"]

    source = tenant_a.settings.allowed_input_roots[0] / "reference.png"
    source.write_bytes(png_bytes())
    asset = tenant_a.assets.import_reference(str(source))
    with pytest.raises(AssetError, match="does not exist"):
        tenant_b.assets.get(asset.asset_id)

    job_id = tenant_a.jobs.submit("test", lambda: {"ok": True})
    assert wait_for_job(tenant_a, job_id)["status"] == "succeeded"
    assert tenant_b.jobs.get(job_id) is None
    registry.close(wait=True)


def test_single_slot_global_capacity_is_shared_across_tenants(tmp_path: Path) -> None:
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(grant("tenant-a", "openai"), grant("tenant-b", "grok")),
        global_workers=1,
        global_queue=0,
    )
    tenant_a = registry.get("tenant-a")
    tenant_b = registry.get("tenant-b")
    gate = threading.Event()
    started = threading.Event()

    def blocking_job() -> dict:
        started.set()
        gate.wait(2)
        return {"ok": True}

    tenant_a.jobs.submit("blocking", blocking_job)
    assert started.wait(1)
    with pytest.raises(JobQueueFull, match="queue is full"):
        tenant_b.jobs.submit("rejected", lambda: {"ok": True})
    gate.set()
    registry.close(wait=True)


def test_queue_less_multi_tenant_capacity_reserves_one_worker(tmp_path: Path) -> None:
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(
            grant("tenant-a", "openai"),
            grant("tenant-b", "grok"),
            grant("tenant-c", "openai"),
        ),
        global_workers=2,
        global_queue=0,
    )
    tenant_a = registry.get("tenant-a")
    tenant_b = registry.get("tenant-b")
    tenant_c = registry.get("tenant-c")
    gate = threading.Event()

    def blocking_job() -> dict:
        gate.wait(2)
        return {"ok": True}

    tenant_a_job = tenant_a.jobs.submit("tenant-a", blocking_job)
    assert registry.tenant_job_capacity == 1
    with pytest.raises(JobQueueFull, match="queue is full"):
        tenant_a.jobs.submit("tenant-a-overflow", blocking_job)

    tenant_b_job = tenant_b.jobs.submit("tenant-b", blocking_job)
    with pytest.raises(JobQueueFull, match="queue is full"):
        tenant_c.jobs.submit("global-overflow", blocking_job)

    gate.set()
    assert wait_for_job(tenant_a, tenant_a_job)["status"] == "succeeded"
    assert wait_for_job(tenant_b, tenant_b_job)["status"] == "succeeded"
    retry = tenant_c.jobs.submit("after-global-release", lambda: {"ok": True})
    assert wait_for_job(tenant_c, retry)["status"] == "succeeded"
    registry.close(wait=True)


def test_tenant_capacity_reserves_shared_queue_for_other_tenants(
    tmp_path: Path,
) -> None:
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(grant("tenant-a", "openai"), grant("tenant-b", "grok")),
        global_workers=2,
        global_queue=3,
    )
    tenant_a = registry.get("tenant-a")
    tenant_b = registry.get("tenant-b")
    gate = threading.Event()

    def blocking_job() -> dict:
        gate.wait(2)
        return {"ok": True}

    tenant_a_jobs = [
        tenant_a.jobs.submit(f"tenant-a-{index}", blocking_job)
        for index in range(2)
    ]
    assert registry.tenant_job_capacity == 2
    with pytest.raises(JobQueueFull, match="queue is full"):
        tenant_a.jobs.submit("tenant-a-overflow", blocking_job)

    tenant_b_jobs = [
        tenant_b.jobs.submit(f"tenant-b-{index}", blocking_job)
        for index in range(2)
    ]
    with pytest.raises(JobQueueFull, match="queue is full"):
        tenant_b.jobs.submit("tenant-b-overflow", blocking_job)

    gate.set()
    for job_id in tenant_a_jobs:
        assert wait_for_job(tenant_a, job_id)["status"] == "succeeded"
    for job_id in tenant_b_jobs:
        assert wait_for_job(tenant_b, job_id)["status"] == "succeeded"

    replacement = tenant_a.jobs.submit("tenant-a-replacement", lambda: {"ok": True})
    assert wait_for_job(tenant_a, replacement)["status"] == "succeeded"
    registry.close(wait=True)


def test_single_tenant_can_use_full_worker_and_queue_capacity(tmp_path: Path) -> None:
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(grant("tenant-a", "openai")),
        global_workers=1,
        global_queue=2,
    )
    tenant = registry.get("tenant-a")
    gate = threading.Event()

    def blocking_job() -> dict:
        gate.wait(2)
        return {"ok": True}

    job_ids = [
        tenant.jobs.submit(f"job-{index}", blocking_job)
        for index in range(3)
    ]
    assert registry.tenant_job_capacity == 3
    with pytest.raises(JobQueueFull, match="queue is full"):
        tenant.jobs.submit("overflow", blocking_job)

    gate.set()
    for job_id in job_ids:
        assert wait_for_job(tenant, job_id)["status"] == "succeeded"
    registry.close(wait=True)


def test_global_asset_quota_is_shared_across_tenants(tmp_path: Path) -> None:
    data = png_bytes()
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(grant("tenant-a", "openai"), grant("tenant-b", "grok")),
        global_asset_store_bytes=len(data),
    )
    encoded = base64.b64encode(data).decode("ascii")
    tenant_a = registry.get("tenant-a")
    tenant_b = registry.get("tenant-b")

    tenant_a.assets.upload_reference(encoded)
    with pytest.raises(AssetError, match="shared asset store quota exceeded"):
        tenant_b.assets.upload_reference(encoded)
    assert registry.global_storage_quota.used_bytes == len(data)
    registry.close(wait=True)


def test_existing_tenant_assets_initialize_and_enforce_global_quota(tmp_path: Path) -> None:
    base_settings = settings(tmp_path)
    existing = base_settings.output_dir / "tenants" / "tenant-a" / "nested"
    existing.mkdir(parents=True)
    (existing / "asset.bin").write_bytes(b"12345")
    (existing / "asset.json").write_text("metadata", encoding="utf-8")

    registry = TenantRuntimeRegistry(
        base_settings,
        auth_config(grant("tenant-a", "openai")),
        global_asset_store_bytes=5,
    )
    assert registry.global_storage_quota.used_bytes == 5
    registry.close()

    with pytest.raises(ConfigurationError, match="existing tenant assets exceed"):
        TenantRuntimeRegistry(
            base_settings,
            auth_config(grant("tenant-a", "openai")),
            global_asset_store_bytes=4,
        )


def test_tenant_storage_startup_errors_do_not_disclose_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_settings = settings(tmp_path)

    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "mkdir",
            lambda self, *args, **kwargs: (_ for _ in ()).throw(
                OSError(f"cannot create {self}")
            ),
        )
        with pytest.raises(ConfigurationError) as error:
            TenantRuntimeRegistry(base_settings, auth_config(grant("tenant-a", "openai")))
    assert str(tmp_path) not in str(error.value)

    tenant_store = base_settings.output_dir / "tenants"
    tenant_store.mkdir(parents=True)
    stored_asset = tenant_store / "asset.bin"
    stored_asset.write_bytes(b"content")
    original_stat = Path.stat

    def fail_asset_stat(path: Path, *args, **kwargs):
        if path == stored_asset:
            raise OSError(f"cannot stat {path}")
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", fail_asset_stat)
        with pytest.raises(ConfigurationError) as error:
            TenantRuntimeRegistry(base_settings, auth_config(grant("tenant-a", "openai")))
    assert str(tmp_path) not in str(error.value)

    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "rglob",
            lambda self, *args, **kwargs: (_ for _ in ()).throw(
                OSError(f"cannot scan {self}")
            ),
        )
        with pytest.raises(ConfigurationError) as error:
            TenantRuntimeRegistry(base_settings, auth_config(grant("tenant-a", "openai")))
    assert str(tmp_path) not in str(error.value)


def test_rate_limits_are_per_tenant_and_reset_after_window(tmp_path: Path) -> None:
    now = [100.0]
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(
            grant("tenant-a", "openai", requests_per_minute=2),
            grant("tenant-b", "grok", requests_per_minute=1),
        ),
        global_workers=2,
        global_queue=10,
        clock=lambda: now[0],
    )
    tenant_a = registry.get("tenant-a")
    tenant_b = registry.get("tenant-b")
    first = tenant_a.jobs.submit("one", lambda: {"ok": 1})
    second = tenant_a.jobs.submit("two", lambda: {"ok": 2})
    wait_for_job(tenant_a, first)
    wait_for_job(tenant_a, second)
    with pytest.raises(TenantRateLimitExceeded, match="rate limit exceeded"):
        tenant_a.jobs.submit("three", lambda: {"ok": 3})

    other = tenant_b.jobs.submit("independent", lambda: {"ok": True})
    assert wait_for_job(tenant_b, other)["status"] == "succeeded"
    now[0] += 61
    reset = tenant_a.jobs.submit("after-reset", lambda: {"ok": True})
    assert wait_for_job(tenant_a, reset)["status"] == "succeeded"
    registry.close(wait=True)


def test_operation_rate_limit_is_independent_and_resets_after_window(tmp_path: Path) -> None:
    now = [100.0]
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(
            grant(
                "tenant-a",
                "openai",
                requests_per_minute=1,
                operations_per_minute=2,
            )
        ),
        clock=lambda: now[0],
    )

    registry.check_operation("tenant-a")
    registry.check_operation("tenant-a")
    with pytest.raises(TenantRateLimitExceeded, match="operation rate limit exceeded"):
        registry.check_operation("tenant-a")

    runtime = registry.get("tenant-a")
    job_id = runtime.jobs.submit("generation", lambda: {"ok": True})
    assert wait_for_job(runtime, job_id)["status"] == "succeeded"
    with pytest.raises(TenantRateLimitExceeded, match="generation rate limit exceeded"):
        runtime.jobs.submit("generation-overflow", lambda: {"ok": True})

    now[0] += 61
    registry.check_operation("tenant-a")
    registry.close(wait=True)


def test_operation_rate_limit_is_thread_safe(tmp_path: Path) -> None:
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(grant("tenant-a", "openai", operations_per_minute=5)),
    )
    barrier = threading.Barrier(20)

    def check() -> bool:
        barrier.wait()
        try:
            registry.check_operation("tenant-a")
        except TenantRateLimitExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _: check(), range(20)))

    assert results.count(True) == 5
    registry.close()


def test_closing_one_tenant_does_not_shutdown_shared_executor(tmp_path: Path) -> None:
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(grant("tenant-a", "openai"), grant("tenant-b", "grok")),
        global_workers=1,
        global_queue=2,
    )
    tenant_a = registry.get("tenant-a")
    tenant_b = registry.get("tenant-b")
    tenant_a.close(wait=True)
    job_id = tenant_b.jobs.submit("still-running", lambda: {"ok": True})
    assert wait_for_job(tenant_b, job_id)["status"] == "succeeded"
    registry.close(wait=True)
    registry.close(wait=True)
    with pytest.raises(RuntimeError, match="registry is closed"):
        registry.get("tenant-b")
    with pytest.raises(RuntimeError, match="registry is closed"):
        registry.check_operation("tenant-b")


def test_expired_grant_is_rejected_before_runtime_or_job_creation(tmp_path: Path) -> None:
    registry = TenantRuntimeRegistry(
        settings(tmp_path),
        auth_config(grant("tenant-a", "openai", expires_at=999)),
        wall_clock=lambda: 1000,
    )
    with pytest.raises(TenantRateLimitExceeded, match="grant has expired"):
        registry.get("tenant-a")
    with pytest.raises(TenantRateLimitExceeded, match="grant has expired"):
        registry.check_operation("tenant-a")
    registry.close()
