import importlib.util
import pathlib
import threading
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "services" / "task_serial_executor.py"
spec = importlib.util.spec_from_file_location("task_serial_executor", MODULE_PATH)
if spec is None or spec.loader is None:
    raise ImportError("task_serial_executor module spec not found")
task_serial_executor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_serial_executor)
SerialTaskExecutor = task_serial_executor.SerialTaskExecutor


class TestTaskSerialExecutor(unittest.TestCase):
    def test_executor_respects_worker_limit(self):
        executor = SerialTaskExecutor(max_workers=1)
        state_lock = threading.Lock()
        state = {"active": 0, "peak_active": 0}

        def critical_work():
            with state_lock:
                state["active"] += 1
                state["peak_active"] = max(state["peak_active"], state["active"])
            time.sleep(0.05)
            with state_lock:
                state["active"] -= 1

        threads = [threading.Thread(target=lambda: executor.run(critical_work)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(state["peak_active"], 1)
        executor.shutdown()

    def test_reserve_rejects_duplicate_until_task_finishes(self):
        executor = SerialTaskExecutor(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def work():
            started.set()
            release.wait(timeout=1)
            return "done"

        self.assertTrue(executor.reserve("task-1"))
        thread = threading.Thread(target=lambda: executor.run_reserved("task-1", work))
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        self.assertFalse(executor.reserve("task-1"))
        self.assertTrue(executor.is_active("task-1"))

        release.set()
        thread.join(timeout=1)
        self.assertFalse(executor.is_active("task-1"))
        self.assertTrue(executor.reserve("task-1"))
        executor.shutdown()

    def test_run_reserved_releases_id_after_failure(self):
        executor = SerialTaskExecutor(max_workers=1)
        self.assertTrue(executor.reserve("task-1"))

        with self.assertRaisesRegex(RuntimeError, "boom"):
            executor.run_reserved("task-1", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        self.assertFalse(executor.is_active("task-1"))
        executor.shutdown()


if __name__ == "__main__":
    unittest.main()
