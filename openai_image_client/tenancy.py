from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .assets import AssetError, StorageQuota
from .http_auth import HTTPAuthConfig, TenantGrant
from .jobs import CompositeJobCapacity, JobQueueFull
from .profiles import ConfigurationError, MCPSettings
from .runtime import MCPRuntime
from .service import Processor


class TenantRateLimitExceeded(JobQueueFull):
    """Raised when a tenant exceeds a request allowance."""


class FixedWindowRateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        label: str = "generation",
    ) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")
        self.requests_per_minute = requests_per_minute
        self.label = label
        self._clock = clock
        self._lock = threading.Lock()
        self._window_started_at = clock()
        self._request_count = 0

    def check(self) -> None:
        with self._lock:
            now = self._clock()
            elapsed = now - self._window_started_at
            if elapsed < 0 or elapsed >= 60:
                self._window_started_at = now
                self._request_count = 0
                elapsed = 0
            if self._request_count >= self.requests_per_minute:
                retry_after = max(1, int(60 - elapsed))
                raise TenantRateLimitExceeded(
                    f"tenant {self.label} rate limit exceeded; "
                    f"retry after approximately {retry_after} seconds"
                )
            self._request_count += 1


class TenantRuntimeRegistry:
    def __init__(
        self,
        base_settings: MCPSettings,
        auth_config: HTTPAuthConfig,
        global_workers: int = 8,
        global_queue: int = 100,
        *,
        global_asset_store_bytes: int | None = None,
        processor: Processor | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if global_workers < 1:
            raise ValueError("global_workers must be positive")
        if global_queue < 0:
            raise ValueError("global_queue must not be negative")
        if global_asset_store_bytes is None:
            global_asset_store_bytes = max(
                10 * 1024 * 1024 * 1024,
                base_settings.max_asset_store_bytes,
            )
        if (
            isinstance(global_asset_store_bytes, bool)
            or not isinstance(global_asset_store_bytes, int)
            or global_asset_store_bytes < 1
        ):
            raise ValueError("global_asset_store_bytes must be a positive integer")
        self.base_settings = base_settings
        self.auth_config = auth_config
        self.global_workers = global_workers
        self.global_queue = global_queue
        self._processor = processor
        self._clock = clock
        self._wall_clock = wall_clock
        try:
            tenant_store_root = (base_settings.output_dir / "tenants").resolve()
            tenant_store_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            stored_bytes = sum(
                path.stat().st_size
                for path in tenant_store_root.rglob("*")
                if path.is_file() and path.suffix != ".json"
            )
        except OSError as exc:
            raise ConfigurationError("tenant asset storage cannot be initialized") from exc
        try:
            self.global_storage_quota = StorageQuota(
                global_asset_store_bytes,
                used_bytes=stored_bytes,
            )
        except AssetError as exc:
            raise ConfigurationError(
                "existing tenant assets exceed the global storage quota"
            ) from exc
        self._executor = ThreadPoolExecutor(
            max_workers=global_workers,
            thread_name_prefix="image-client-tenant",
        )
        global_capacity = global_workers + global_queue
        self._capacity = threading.BoundedSemaphore(global_capacity)
        configured_tenants = len(auth_config.grants_by_tenant)
        if configured_tenants == 1:
            tenant_capacity = global_capacity
        else:
            # A tenant may saturate workers, but not the shared queue or final slot.
            tenant_capacity = min(global_workers, max(1, global_capacity - 1))
        self.tenant_job_capacity = tenant_capacity
        self._tenant_capacities = {
            tenant_id: threading.BoundedSemaphore(tenant_capacity)
            for tenant_id in auth_config.grants_by_tenant
        }
        self._runtimes: dict[str, MCPRuntime] = {}
        self._generation_limiters: dict[str, FixedWindowRateLimiter] = {}
        self._operation_limiters: dict[str, FixedWindowRateLimiter] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _grant(self, tenant_id: str) -> TenantGrant:
        grant = self.auth_config.grants_by_tenant.get(str(tenant_id))
        if grant is None:
            raise ConfigurationError("tenant is not configured")
        return grant

    def _check_grant_active(self, grant: TenantGrant) -> None:
        if grant.expires_at is not None and grant.expires_at <= int(self._wall_clock()):
            raise TenantRateLimitExceeded("tenant grant has expired")

    def _tenant_settings(self, grant: TenantGrant) -> MCPSettings:
        allowed_names = tuple(dict.fromkeys(grant.allowed_profiles))
        profiles = {
            name: self.base_settings.profiles[name]
            for name in allowed_names
            if name in self.base_settings.profiles
        }
        missing = [name for name in allowed_names if name not in profiles]
        if missing:
            raise ConfigurationError(
                f"tenant {grant.tenant_id!r} references unknown profiles"
            )
        if not profiles:
            raise ConfigurationError(f"tenant {grant.tenant_id!r} has no usable profiles")
        if grant.default_profile not in profiles:
            raise ConfigurationError(
                f"tenant {grant.tenant_id!r} default profile is not allowed"
            )

        try:
            tenant_root = (
                self.base_settings.output_dir / "tenants" / grant.tenant_id
            ).resolve()
            expected_parent = (self.base_settings.output_dir / "tenants").resolve()
        except (OSError, RuntimeError) as exc:
            raise ConfigurationError("tenant storage directory cannot be prepared") from exc
        try:
            tenant_root.relative_to(expected_parent)
        except ValueError as exc:  # pragma: no cover - TenantGrant validates identifiers
            raise ConfigurationError("tenant output directory escapes its base") from exc
        input_root = tenant_root / "inputs"
        try:
            input_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ConfigurationError("tenant storage directory cannot be prepared") from exc
        return replace(
            self.base_settings,
            profiles=profiles,
            default_profile=grant.default_profile,
            output_dir=tenant_root,
            allowed_input_roots=(input_root,),
            max_workers=self.global_workers,
            max_queue=self.global_queue,
        )

    def _create_runtime(self, grant: TenantGrant) -> MCPRuntime:
        limiter = FixedWindowRateLimiter(
            grant.requests_per_minute,
            clock=self._clock,
            label="generation",
        )

        def submission_guard() -> None:
            self._check_grant_active(grant)
            limiter.check()

        try:
            runtime = MCPRuntime.create(
                self._tenant_settings(grant),
                processor=self._processor,
                executor=self._executor,
                capacity=CompositeJobCapacity(
                    self._tenant_capacities[grant.tenant_id],
                    self._capacity,
                ),
                submission_guard=submission_guard,
                storage_quota=self.global_storage_quota,
            )
        except (OSError, RuntimeError) as exc:
            raise ConfigurationError("tenant asset storage cannot be initialized") from exc
        self._generation_limiters[grant.tenant_id] = limiter
        return runtime

    def check_operation(self, tenant_id: str) -> None:
        grant = self._grant(tenant_id)
        self._check_grant_active(grant)
        with self._lock:
            if self._closed:
                raise RuntimeError("tenant runtime registry is closed")
            limiter = self._operation_limiters.get(grant.tenant_id)
            if limiter is None:
                limiter = FixedWindowRateLimiter(
                    grant.operations_per_minute,
                    clock=self._clock,
                    label="operation",
                )
                self._operation_limiters[grant.tenant_id] = limiter
        limiter.check()

    def get_runtime(self, tenant_id: str) -> MCPRuntime:
        grant = self._grant(tenant_id)
        self._check_grant_active(grant)
        with self._lock:
            if self._closed:
                raise RuntimeError("tenant runtime registry is closed")
            runtime = self._runtimes.get(grant.tenant_id)
            if runtime is None:
                runtime = self._create_runtime(grant)
                self._runtimes[grant.tenant_id] = runtime
            return runtime

    def get(self, tenant_id: str) -> MCPRuntime:
        return self.get_runtime(tenant_id)

    def close(self, wait: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
            self._generation_limiters.clear()
            self._operation_limiters.clear()
        for runtime in runtimes:
            runtime.close(wait=wait)
        self._executor.shutdown(wait=wait, cancel_futures=True)
