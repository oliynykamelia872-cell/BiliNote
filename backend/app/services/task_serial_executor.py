import os
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable


class ConcurrentTaskExecutor:
    """使用线程池并发执行任务，替代原来的串行锁。"""

    def __init__(self, max_workers: int | None = None):
        self._max_workers = max_workers or int(os.getenv("TASK_MAX_WORKERS", "3"))
        self._pool = ThreadPoolExecutor(max_workers=self._max_workers)
        self._active_task_ids: set[str] = set()
        self._active_lock = threading.Lock()

    def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        future: Future = self._pool.submit(fn, *args, **kwargs)
        return future.result()

    def reserve(self, task_id: str) -> bool:
        """Atomically reserve a task ID. False means it is already queued/running."""
        with self._active_lock:
            if task_id in self._active_task_ids:
                return False
            self._active_task_ids.add(task_id)
            return True

    def is_active(self, task_id: str) -> bool:
        with self._active_lock:
            return task_id in self._active_task_ids

    def run_reserved(self, task_id: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a previously reserved task and always release its ID afterwards."""
        if not self.is_active(task_id):
            raise RuntimeError(f"task_id is not reserved: {task_id}")
        try:
            return self.run(fn, *args, **kwargs)
        finally:
            with self._active_lock:
                self._active_task_ids.discard(task_id)

    def shutdown(self, wait: bool = True):
        self._pool.shutdown(wait=wait)


# 保持向后兼容的导出名
SerialTaskExecutor = ConcurrentTaskExecutor
task_serial_executor = ConcurrentTaskExecutor()
