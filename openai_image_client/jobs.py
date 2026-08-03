from __future__ import annotations

import copy
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_for_futures
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


FINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class JobQueueFull(RuntimeError):
    """Raised when the bounded worker queue has no free slots."""


class JobCapacity(Protocol):
    def acquire(self, blocking: bool = True) -> bool: ...

    def release(self) -> None: ...


class CompositeJobCapacity:
    """Acquire multiple capacity gates as one all-or-nothing admission check."""

    def __init__(self, *capacities: JobCapacity) -> None:
        if not capacities:
            raise ValueError("at least one capacity gate is required")
        self._capacities = capacities

    def acquire(self, blocking: bool = True) -> bool:
        acquired: list[JobCapacity] = []
        try:
            for capacity in self._capacities:
                if not capacity.acquire(blocking=blocking):
                    for held in reversed(acquired):
                        held.release()
                    return False
                acquired.append(capacity)
        except BaseException:
            for held in reversed(acquired):
                held.release()
            raise
        return True

    def release(self) -> None:
        for capacity in reversed(self._capacities):
            capacity.release()


@dataclass
class JobRecord:
    job_id: str
    operation: str
    status: str
    created_at: float
    updated_at: float
    cancel_requested: bool = False
    result: dict[str, Any] | None = None
    error: str | None = None
    _future: Future[dict[str, Any]] | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "uri": f"image-client://jobs/{self.job_id}",
            "operation": self.operation,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cancel_requested": self.cancel_requested,
        }
        if self.status == "succeeded" and self.result is not None:
            payload["result"] = copy.deepcopy(self.result)
        if self.status == "failed" and self.error:
            payload["error"] = self.error
        return payload


class JobManager:
    def __init__(
        self,
        *,
        max_workers: int,
        max_queue: int,
        ttl_seconds: int,
        max_jobs: int = 200,
        error_sanitizer: Callable[[BaseException], str] | None = None,
        executor: ThreadPoolExecutor | None = None,
        capacity: JobCapacity | None = None,
        submission_guard: Callable[[], None] | None = None,
    ) -> None:
        if (executor is None) != (capacity is None):
            raise ValueError("executor and capacity must be provided together")
        self.max_workers = max_workers
        self.max_queue = max_queue
        self.max_jobs = max_jobs
        self.ttl_seconds = ttl_seconds
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="image-client-mcp",
        )
        self._capacity = capacity or threading.BoundedSemaphore(max_workers + max_queue)
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.RLock()
        self._error_sanitizer = error_sanitizer or self._default_error_sanitizer
        self._submission_guard = submission_guard
        self._closed = False

    @staticmethod
    def _default_error_sanitizer(exc: BaseException) -> str:
        value = f"{type(exc).__name__}: {exc}".replace("\x00", "")
        return value[:4000]

    def cleanup(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        with self._lock:
            expired = [
                job_id
                for job_id, job in self._jobs.items()
                if job.status in FINAL_STATUSES and current - job.updated_at > self.ttl_seconds
            ]
            for job_id in expired:
                self._jobs.pop(job_id, None)

    def submit(self, operation: str, work: Callable[[], dict[str, Any]]) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("job manager is closed")
        if self._submission_guard is not None:
            self._submission_guard()
        self.cleanup()
        if not self._capacity.acquire(blocking=False):
            raise JobQueueFull(
                f"job queue is full ({self.max_workers} workers, {self.max_queue} queued)"
            )
        job_id = uuid.uuid4().hex
        now = time.time()
        record = JobRecord(
            job_id=job_id,
            operation=operation,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            if self._closed:
                self._capacity.release()
                raise RuntimeError("job manager is closed")
            if len(self._jobs) >= self.max_jobs:
                terminal_jobs = sorted(
                    (job for job in self._jobs.values() if job.status in FINAL_STATUSES),
                    key=lambda item: item.updated_at,
                )
                for terminal in terminal_jobs:
                    self._jobs.pop(terminal.job_id, None)
                    if len(self._jobs) < self.max_jobs:
                        break
            if len(self._jobs) >= self.max_jobs:
                self._capacity.release()
                raise JobQueueFull(
                    f"job registry is full ({self.max_jobs} active jobs)"
                )
            self._jobs[job_id] = record

        def run() -> dict[str, Any]:
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None:
                    return {}
                if current.cancel_requested:
                    current.status = "cancelled"
                    current.updated_at = time.time()
                    return {}
                current.status = "running"
                current.updated_at = time.time()
            try:
                result = work()
            except BaseException as exc:
                with self._lock:
                    current = self._jobs.get(job_id)
                    if current is not None:
                        if current.cancel_requested:
                            current.status = "cancelled"
                        else:
                            current.status = "failed"
                            current.error = self._error_sanitizer(exc)
                        current.updated_at = time.time()
                return {}
            with self._lock:
                current = self._jobs.get(job_id)
                if current is not None:
                    if current.cancel_requested:
                        current.status = "cancelled"
                    else:
                        current.status = "succeeded"
                        current.result = copy.deepcopy(result)
                    current.updated_at = time.time()
            return result

        with self._lock:
            if self._closed:
                self._jobs.pop(job_id, None)
                self._capacity.release()
                raise RuntimeError("job manager is closed")
            try:
                future = self._executor.submit(run)
            except BaseException:
                self._capacity.release()
                self._jobs.pop(job_id, None)
                raise
            record._future = future
        future.add_done_callback(lambda _: self._capacity.release())
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        self.cleanup()
        with self._lock:
            record = self._jobs.get(str(job_id).strip().lower())
            return record.public_dict() if record is not None else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        normalized = str(job_id).strip().lower()
        with self._lock:
            record = self._jobs.get(normalized)
            if record is None:
                return None
            if record.status in FINAL_STATUSES:
                return record.public_dict()
            record.cancel_requested = True
            record.updated_at = time.time()
            future = record._future
            if future is not None and future.cancel():
                record.status = "cancelled"
                record.updated_at = time.time()
            return record.public_dict()

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        self.cleanup()
        with self._lock:
            records = sorted(
                self._jobs.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )
            return [record.public_dict() for record in records[:limit]]

    def close(self, wait: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            records = list(self._jobs.values())
            futures = [record._future for record in records if record._future is not None]
            for record in records:
                if record.status not in FINAL_STATUSES:
                    record.cancel_requested = True
                    record.updated_at = time.time()
                    if record._future is not None and record._future.cancel():
                        record.status = "cancelled"

        if self._owns_executor:
            self._executor.shutdown(wait=wait, cancel_futures=True)
        else:
            for future in futures:
                future.cancel()
            if wait and futures:
                wait_for_futures(futures)
