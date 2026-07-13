"""Ordered background work with a durable review-job hand-off."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Callable


@dataclass(frozen=True)
class MemoryJob:
    job_key: str
    subject_id: str
    workspace_id: str
    job_type: str
    fn: Callable[[], object]


class MemoryWorker:
    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="meta-memory")
        self._tails: dict[tuple[str, str], Future] = {}
        self._all: list[Future] = []
        self._lock = Lock()

    def submit(self, job: MemoryJob) -> str:
        key = (job.subject_id, job.workspace_id)
        with self._lock:
            previous = self._tails.get(key)
            def run() -> object:
                if previous is not None:
                    previous.result()
                return job.fn()
            future = self._pool.submit(run)
            self._tails[key] = future
            self._all.append(future)
        return job.job_key

    def flush(self, timeout: float | None = None) -> bool:
        with self._lock:
            pending = list(self._all)
        try:
            for future in pending:
                future.result(timeout=timeout)
        except Exception:
            return False
        return True

    def shutdown(self, timeout: float = 5.0) -> None:
        self.flush(timeout)
        self._pool.shutdown(wait=False, cancel_futures=False)
