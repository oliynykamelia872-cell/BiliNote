import importlib.util
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path(__file__).with_name("batch_summarize_local.py")
spec = importlib.util.spec_from_file_location("batch_summarize_local", SCRIPT_PATH)
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


def test_parallel_tasks_are_isolated_and_state_is_safe(tmp_path, monkeypatch):
    files = [tmp_path / "one.mp3", tmp_path / "two.mp3", tmp_path / "three.mp3"]
    for path in files:
        path.write_bytes(path.name.encode())

    state_path = tmp_path / "state.json"
    state = {}
    state_lock = threading.Lock()
    note_lock = threading.Lock()
    active = 0
    peak = 0
    active_lock = threading.Lock()

    def submit(path, model_cfg, style, formats):
        return f"task-{path.stem}"

    def poll(task_id, timeout, interval):
        nonlocal active, peak
        with active_lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            if task_id == "task-two":
                raise RuntimeError("simulated failure")
            return f"# {task_id}"
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(batch, "submit_task", submit)
    monkeypatch.setattr(batch, "poll_task", poll)
    args = SimpleNamespace(
        style="detailed",
        format=["summary"],
        timeout_seconds=30,
        poll_seconds=0,
        dest_dir_path=tmp_path / "notes",
    )
    model_cfg = {"provider_id": "provider", "model_name": "model"}

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(
                batch.process_file,
                path,
                {},
                model_cfg,
                args,
                state,
                state_path,
                state_lock,
                note_lock,
            )
            for path in files
        ]
        results = [future.result() for future in futures]

    assert results == [True, False, True]
    assert peak >= 2
    assert state["one.mp3"]["status"] == "success"
    assert state["two.mp3"]["status"] == "failed"
    assert state["three.mp3"]["status"] == "success"
    assert len(list((tmp_path / "notes").glob("*.md"))) == 2
