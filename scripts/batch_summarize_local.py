#!/usr/bin/env python3
"""Batch-summarize local audio files with the BiliNote backend.

Each audio file is submitted as a separate BiliNote task (platform=local),
polled until done, and the resulting Markdown note is saved into a target
Obsidian folder. Progress is tracked in a JSON state file so the batch can be
resumed after interruption. Use --concurrency to control how many files are
in flight at once; a failed file is recorded and does not stop its siblings.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import socket
import threading


BILINOTE_DIR = Path("/Users/youzh/本地文稿/productive_tools/active/BiliNote")
BILINOTE_BACKEND = os.environ.get("BILINOTE_BACKEND", "http://127.0.0.1:8483").rstrip("/")
AUDIO_EXTS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".wav", ".flac", ".ogg", ".opus",
    ".wma", ".aiff", ".alac", ".amr", ".mka",
}
INVALID_FILENAME_CHARS = set('\\/:*?"<>|#[]')

# macOS 系统代理会被 urllib 自动读取，导致本地后端请求被代理断开；这里直连本地。
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DEFAULT_NOTE_WRITE_LOCK = threading.Lock()


def log(msg, err=False):
    dest = sys.stderr if err else sys.stdout
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", file=dest, flush=True)


def default_model_config():
    """统一模型选择：默认模型配置文件（UI 设置页写入）> 数据库兜底。

    配置文件与后端同一份（backend/config/model_preference.json），由设置页写入；
    不再读取环境变量或仓库根目录的旧配置文件。CLI 参数在调用处覆盖本函数结果。
    """
    cfg_path = BILINOTE_DIR / "backend" / "config" / "model_preference.json"
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if data.get("provider_id") and data.get("model_name"):
                return {"provider_id": data["provider_id"], "model_name": data["model_name"]}
        except (OSError, ValueError) as exc:
            log(f"警告: 读取默认模型配置失败，使用数据库兜底: {exc}", err=True)

    db = BILINOTE_DIR / "backend" / "bili_note.db"
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            """
            select p.id, m.model_name
            from providers p
            join models m on m.provider_id = p.id
            where p.enabled = 1
            order by p.rowid, m.id
            limit 1
            """
        ).fetchone()
        if not row:
            raise RuntimeError("No enabled provider/model found in BiliNote database")
        return {"provider_id": row[0], "model_name": row[1]}
    finally:
        conn.close()


def request_json(method, url, payload=None, timeout=30, attempts=5):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            with NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.error) as exc:
            last_exc = exc
            time.sleep(2 * attempt)
    raise RuntimeError(f"请求失败（已重试 {attempts} 次）: {last_exc}") from last_exc


def submit_task(file_path, model_cfg, style, formats):
    payload = {
        "video_url": str(file_path),
        "platform": "local",
        "quality": "medium",
        "provider_id": model_cfg["provider_id"],
        "model_name": model_cfg["model_name"],
        "format": formats,
        "style": style,
    }
    data = request_json("POST", f"{BILINOTE_BACKEND}/api/generate_note", payload, timeout=60)
    if data.get("code") != 0:
        raise RuntimeError(data.get("msg") or json.dumps(data, ensure_ascii=False))
    task_id = (data.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"BiliNote did not return task_id: {data}")
    return task_id


def poll_task(task_id, timeout_seconds, poll_seconds):
    deadline = time.time() + timeout_seconds
    last = None
    while time.time() < deadline:
        data = request_json("GET", f"{BILINOTE_BACKEND}/api/task_status/{task_id}", timeout=30)
        last = data
        if data.get("code") != 0:
            raise RuntimeError(data.get("msg") or json.dumps(data, ensure_ascii=False))
        payload = data.get("data") or {}
        status = payload.get("status")
        if status == "SUCCESS":
            result = payload.get("result") or {}
            markdown = result.get("markdown")
            if not markdown:
                raise RuntimeError(f"BiliNote succeeded but returned no markdown for task {task_id}")
            return markdown
        if status == "FAILED":
            raise RuntimeError(payload.get("message") or f"BiliNote task failed: {task_id}")
        time.sleep(poll_seconds)
    raise TimeoutError(f"Timed out waiting for task {task_id}. Last response: {last}")


def safe_stem(name):
    stem = Path(name).stem
    cleaned = "".join(" " if ch in INVALID_FILENAME_CHARS else ch for ch in stem)
    cleaned = " ".join(cleaned.split()).strip().rstrip(".")
    return cleaned[:120] or "音频总结"


def frontmatter_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _note_destination(dest_dir, audio_path):
    """Return a destination without overwriting another source's note.

    Most notes retain the readable source filename. A stable suffix is only
    added when two different source paths sanitize to the same filename.
    """
    stem = safe_stem(audio_path.name)
    source_line = f'source: "{frontmatter_escape(str(audio_path))}"'
    candidate = dest_dir / f"{stem}.md"
    if not candidate.exists() or source_line in candidate.read_text(encoding="utf-8", errors="replace"):
        return candidate

    source_hash = hashlib.sha256(str(audio_path).encode("utf-8")).hexdigest()[:10]
    return dest_dir / f"{stem}--{source_hash}.md"


def write_note(dest_dir, audio_path, markdown, task_id, write_lock=None):
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(audio_path.name)
    now = dt.datetime.now().astimezone()
    body = [
        "---",
        f'title: "{frontmatter_escape(stem)}"',
        f'source: "{frontmatter_escape(str(audio_path))}"',
        "platform: local",
        f"bilinote_task_id: {task_id}",
        "status: done",
        f'created: "{now.isoformat(timespec="seconds")}"',
        "tags:",
        "  - 心理学",
        "  - 音频笔记",
        "  - 武志红心理学课",
        "---",
        "",
        markdown.strip(),
        "",
    ]
    with (write_lock or DEFAULT_NOTE_WRITE_LOCK):
        dest = _note_destination(dest_dir, audio_path)
        tmp = dest.with_name(f".{dest.name}.{task_id}.tmp")
        tmp.write_text("\n".join(body), encoding="utf-8")
        tmp.replace(dest)
    return dest


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def update_state(state_path, state, state_lock, filename, record):
    """Update one record and persist the whole state atomically."""
    with state_lock:
        state[filename] = record
        save_state(state_path, state)


def process_file(audio_path, record, model_cfg, args, state, state_path, state_lock, note_write_lock):
    """Submit, poll, and save one task without affecting sibling tasks."""
    task_id = record.get("task_id")
    try:
        if record.get("status") != "running" or not task_id:
            task_id = submit_task(audio_path, model_cfg, args.style, args.format)
            update_state(
                state_path,
                state,
                state_lock,
                audio_path.name,
                {"status": "running", "task_id": task_id},
            )
            log(f"  已提交任务 {task_id}: {audio_path.name}")

        markdown = poll_task(task_id, args.timeout_seconds, args.poll_seconds)
        dest = write_note(args.dest_dir_path, audio_path, markdown, task_id, note_write_lock)
        update_state(
            state_path,
            state,
            state_lock,
            audio_path.name,
            {"status": "success", "task_id": task_id, "dest": str(dest)},
        )
        log(f"  完成: {audio_path.name} → {dest.name}")
        return True
    except Exception as exc:
        update_state(
            state_path,
            state,
            state_lock,
            audio_path.name,
            {"status": "failed", "task_id": task_id or "", "error": str(exc)},
        )
        log(f"  失败: {audio_path.name}: {exc}", err=True)
        return False


def positive_int(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return parsed


def main():
    parser = argparse.ArgumentParser(
        description="Batch summarize local audio files with BiliNote",
        epilog=(
            "示例: python scripts/batch_summarize_local.py --concurrency 3\n"
            "实际同时执行数还受后端 TASK_MAX_WORKERS 限制；修改该配置后需重启后端。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source-dir", default="/Users/youzh/Downloads/音频")
    parser.add_argument(
        "--dest-dir",
        default="/Users/youzh/Library/Mobile Documents/iCloud~md~obsidian/Documents/imac_doc/心理学",
    )
    parser.add_argument("--style", default="detailed")
    parser.add_argument("--provider-id", default=None)
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--format", action="append", default=["toc", "link", "summary"])
    parser.add_argument("--timeout-seconds", type=int, default=5400)
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument(
        "--concurrency",
        type=positive_int,
        default=os.environ.get("BATCH_CONCURRENCY", "3"),
        help="同时提交并等待的任务数（默认: 3；也可设 BATCH_CONCURRENCY）",
    )
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N files (0 = all)")
    parser.add_argument("--start-from", default=None, help="Resume from this filename (inclusive)")
    parser.add_argument("--state-file", default=str(BILINOTE_DIR / "logs" / "batch_psychology_progress.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source_dir = Path(args.source_dir).expanduser()
    dest_dir = Path(args.dest_dir).expanduser()
    args.dest_dir_path = dest_dir
    state_path = Path(args.state_file).expanduser()
    state = load_state(state_path)
    model_cfg = default_model_config()
    if args.provider_id:
        model_cfg["provider_id"] = args.provider_id
    if args.model_name:
        model_cfg["model_name"] = args.model_name

    files = sorted(
        (p for p in source_dir.iterdir() if p.suffix.lower() in AUDIO_EXTS and p.is_file()),
        key=lambda p: p.name,
    )
    if not files:
        log(f"没有在 {source_dir} 找到音频文件")
        return 1

    # 完全相同的文件（md5 相同）只保留第一个，避免重复转写同一内容
    seen = {}
    deduped = []
    for p in files:
        if p.name in state and state[p.name].get("status") in ("success", "skipped"):
            continue
        md5 = file_md5(p)
        if md5 in seen:
            state[p.name] = {
                "status": "skipped",
                "error": f"与 {seen[md5]} 内容完全相同",
                "md5": md5,
            }
            log(f"跳过重复文件: {p.name}（与 {seen[md5]} 相同）")
            continue
        seen[md5] = p.name
        deduped.append(p)

    if args.start_from:
        deduped = [p for p in deduped if p.name >= args.start_from]

    if args.limit:
        deduped = deduped[: args.limit]

    if args.dry_run:
        for p in deduped:
            print(f"would process: {p.name}")
        print(f"total: {len(deduped)}")
        return 0

    log(
        f"共 {len(files)} 个音频文件，去重后待处理 {len(deduped)} 个；"
        f"并发数 {args.concurrency}"
    )
    log(f"模型: {model_cfg['provider_id']} / {model_cfg['model_name']}，风格: {args.style}")

    state_lock = threading.Lock()
    note_write_lock = threading.Lock()
    done = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=args.concurrency, thread_name_prefix="bilinote-batch") as executor:
        futures = {}
        for idx, audio_path in enumerate(deduped, 1):
            with state_lock:
                record = dict(state.get(audio_path.name, {}))
            log(f"[{idx}/{len(deduped)}] 排队处理: {audio_path.name}")
            future = executor.submit(
                process_file,
                audio_path,
                record,
                model_cfg,
                args,
                state,
                state_path,
                state_lock,
                note_write_lock,
            )
            futures[future] = audio_path

        for future in as_completed(futures):
            audio_path = futures[future]
            try:
                if future.result():
                    done += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                log(f"  失败: {audio_path.name}: {exc}", err=True)

    log(f"批次结束: 成功 {done}，失败 {failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
