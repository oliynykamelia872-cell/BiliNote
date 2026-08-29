#!/usr/bin/env python3
"""Archive a local Codex video course into a structured Obsidian knowledge base.

The script is deliberately resumable: state is recorded after every completed item,
and existing notes are never regenerated unless --force is supplied.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from openai import OpenAI


SOURCE_DIR = Path("/Users/youzh/视频资料/Codex AI 国内外AI大神教程合集（时长约21小时）")
VAULT = Path("/Users/youzh/Library/Mobile Documents/iCloud~md~obsidian/Documents/imac_doc")
ROOT = VAULT / "AI相关资料" / "Codex教程资料" / "2026 Codex 视频课程库"
BACKEND = "http://127.0.0.1:8483/api"
PROJECT = Path("/Users/youzh/本地文稿/productive_tools/active/BiliNote")
RESULT_DIR = PROJECT / "backend" / "note_results"
STATE_PATH = ROOT / "00 课程总览" / "处理进度.json"
HTTP = requests.Session()
HTTP.trust_env = False


def clean_title(value: str) -> str:
    value = re.sub(r"^【紫薯妹整理】\s*", "", value)
    value = Path(value).stem
    return re.sub(r"\s+", " ", value).strip()


def category(title: str) -> tuple[str, str]:
    text = title.lower()
    if any(word in text for word in ("skill", "goal", "工作流", "自动化", "插件")):
        return "04 工作流与扩展", "工作流、Skill、插件与自动化"
    if any(word in text for word in ("项目", "网页设计", "带货", "实战", "手机端")):
        return "03 项目实战", "项目开发、设计与跨端实战"
    if any(word in text for word in ("claude", "deepseek", "api", "缓存", "muse")):
        return "05 生态与对比", "模型、工具生态与维护"
    if any(word in text for word in ("app", "桌面", "核心用法", "功能", "命令行")):
        return "02 核心能力", "App、CLI 与核心能力"
    return "01 入门与认知", "基础认知、安装与上手"


def safe_filename(title: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]', " ", title)
    return re.sub(r"\s+", " ", value).strip()[:110]


def ensure_structure(videos: list[Path]) -> None:
    for folder in ("00 课程总览", "01 入门与认知", "02 核心能力", "03 项目实战", "04 工作流与扩展", "05 生态与对比", "06 发文素材池"):
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    rows = []
    for index, video in enumerate(videos, 1):
        title = clean_title(video.name)
        folder, focus = category(title)
        rows.append(f"| {index:02d} | [[{folder}/{index:02d} {safe_filename(title)}]] | {focus} | 待处理 |")
    index_lines = [
        "---", "title: \"2026 Codex 视频课程库\"", "tags: [Codex, 课程库, 视频文字稿]", "---", "",
        "# 2026 Codex 视频课程库", "", "这是一套按主题组织的课程资料库。每个条目包含原始文字稿、中文总结、行动清单与可用于自媒体的线索。", "",
        "## 学习路径", "", "1. [[01 入门与认知]]：先建立工具认知与基础操作。", "2. [[02 核心能力]]：掌握 App、CLI 和常见功能。", "3. [[03 项目实战]]：用真实项目巩固交付能力。", "4. [[04 工作流与扩展]]：把 Skill、Goal、插件串成系统。", "5. [[05 生态与对比]]：判断模型、工具与配置取舍。", "",
        "## 课程清单", "", "| 序号 | 教程 | 主题 | 状态 |", "| --- | --- | --- | --- |", *rows,
        "", "## 可复用资产", "", "- [[06 发文素材池/选题与观点池]]", "- [[06 发文素材池/待核验事实]]", "",
    ]
    index_text = "\n".join(index_lines)
    (ROOT / "00 课程总览" / "课程地图.md").write_text(index_text, encoding="utf-8")
    for name, body in {
        "选题与观点池.md": "# Codex 发文选题与观点池\n\n批处理完成后，这里会按视频沉淀可改写的观点、标题方向、目标读者与内容角度。\n",
        "待核验事实.md": "# 待核验事实\n\n视频中涉及版本、价格、政策、性能对比或“唯一/最强”等表述，发布前应回到官方资料验证。\n",
    }.items():
        target = ROOT / "06 发文素材池" / name
        if not target.exists():
            target.write_text(body, encoding="utf-8")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"completed": {}, "failed": {}, "updated_at": ""}


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def configured_client() -> tuple[OpenAI, str]:
    db = sqlite3.connect(PROJECT / "backend" / "bili_note.db")
    try:
        api_key, base_url = db.execute("select api_key, base_url from providers where id='deepseek'").fetchone()
    finally:
        db.close()
    return OpenAI(api_key=api_key, base_url=base_url), "deepseek-v4-flash"


def submit_transcription(video: Path) -> str:
    with video.open("rb") as handle:
        uploaded = HTTP.post(f"{BACKEND}/upload", files={"file": (video.name, handle, "video/mp4")}, timeout=180).json()
    url = uploaded["data"]["url"]
    payload = {
        "video_url": url, "platform": "local", "quality": "medium",
        "provider_id": "deepseek", "model_name": "deepseek-v4-flash",
        "format": ["toc"], "screenshot": False, "link": False, "style": "tutorial",
    }
    response = HTTP.post(f"{BACKEND}/generate_note", json=payload, timeout=60).json()
    if response.get("code") != 0:
        raise RuntimeError(response.get("msg", "task creation failed"))
    return response["data"]["task_id"]


def await_transcript(task_id: str, timeout_seconds: int) -> dict:
    deadline = time.time() + timeout_seconds
    transcript_path = RESULT_DIR / f"{task_id}_transcript.json"
    while time.time() < deadline:
        if transcript_path.exists():
            return json.loads(transcript_path.read_text(encoding="utf-8"))
        response = HTTP.get(f"{BACKEND}/task_status/{task_id}", timeout=30).json()
        if response.get("code") != 0:
            raise RuntimeError(response.get("msg", "transcription failed"))
        time.sleep(8)
    raise TimeoutError(f"transcript timeout: {task_id}")


def summarize(client: OpenAI, model: str, title: str, transcript: str) -> str:
    prompt = f"""你是知识库编辑。根据以下视频文字稿，生成一份中文 Markdown 学习笔记。
要求：忠实于原文；不补造演示或事实；适合之后提炼小红书内容。
必须包含：
## 一句话摘要
## 核心内容（3-7条）
## 操作/学习清单
## 可转化的发文素材（给出3-6个：选题、切入角度、适合读者）
## 待核验事实（只列需要以官方资料确认的版本、价格、性能或结论；没有则写“无”）

视频标题：{title}
文字稿：
{transcript[:50000]}"""
    response = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], temperature=0.2, stream=False, timeout=180,
    )
    return response.choices[0].message.content.strip()


def write_note(index: int, video: Path, transcript_data: dict, summary: str, task_id: str) -> Path:
    title = clean_title(video.name)
    folder, _ = category(title)
    target = ROOT / folder / f"{index:02d} {safe_filename(title)}.md"
    segments = transcript_data.get("segments") or []
    transcript = "\n".join(f"- [{int(item.get('start', 0)) // 60:02d}:{int(item.get('start', 0)) % 60:02d}] {item.get('text', '').strip()}" for item in segments)
    body = "\n".join([
        "---", f'title: "{title.replace(chr(34), chr(39))}"', "type: video-transcript", "status: completed",
        "tags: [Codex, 视频文字稿, AI相关资料]", f"source_file: \"{video}\"", f"bilinote_task_id: {task_id}",
        f'created: "{datetime.now().astimezone().isoformat(timespec="seconds")}"', "---", "", f"# {title}", "", summary, "", "## 原始文字稿", "", transcript or transcript_data.get("full_text", ""), "",
    ])
    target.write_text(body, encoding="utf-8")
    return target


def append_material(title: str, summary: str, note: Path) -> None:
    target = ROOT / "06 发文素材池" / "选题与观点池.md"
    content = target.read_text(encoding="utf-8")
    marker = "## 可转化的发文素材"
    section = summary[summary.find(marker):] if marker in summary else summary
    section = section.split("## 待核验事实", 1)[0].strip()
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {title}\n\n来源：[[{note.relative_to(ROOT).with_suffix('')}]]\n\n{section}\n")


def main() -> None:
    global SOURCE_DIR, ROOT, STATE_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Process at most N unfinished videos")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=int, default=5400)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    SOURCE_DIR = args.source_dir.expanduser()
    ROOT = args.root.expanduser()
    STATE_PATH = ROOT / "00 课程总览" / "处理进度.json"
    videos = sorted(SOURCE_DIR.glob("*.mp4"))
    ensure_structure(videos)
    state = load_state()
    client, model = configured_client()
    processed = 0
    for index, video in enumerate(videos, 1):
        key = str(video)
        if key in state["completed"] and not args.force:
            continue
        if args.limit and processed >= args.limit:
            break
        title = clean_title(video.name)
        print(f"[{index}/{len(videos)}] {title}", flush=True)
        try:
            task_id = submit_transcription(video)
            transcript_data = await_transcript(task_id, args.timeout)
            summary = summarize(client, model, title, transcript_data.get("full_text", ""))
            note = write_note(index, video, transcript_data, summary, task_id)
            append_material(title, summary, note)
            state["completed"][key] = {"task_id": task_id, "note": str(note)}
            state["failed"].pop(key, None)
        except Exception as exc:
            state["failed"][key] = str(exc)
            print(f"FAILED: {exc}", flush=True)
        save_state(state)
        processed += 1
    print(json.dumps({"completed": len(state["completed"]), "failed": len(state["failed"]), "root": str(ROOT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
