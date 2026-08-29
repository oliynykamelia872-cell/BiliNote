import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class ObsidianError(ValueError):
    pass


CONFIG_PATH = Path(os.getenv("OBSIDIAN_CONFIG", "config/obsidian.json"))
STATIC_ROOT = Path(os.getenv("STATIC", "static"))


def _read() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"paths": []}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ObsidianError("Obsidian 路径配置损坏") from exc
    return data if isinstance(data, dict) and isinstance(data.get("paths", []), list) else {"paths": []}


def _write(data: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CONFIG_PATH)


def validate_directory(path: str, require_writable: bool = True) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ObsidianError("知识库路径必须是绝对路径")
    if not candidate.exists() or not candidate.is_dir():
        raise ObsidianError("知识库路径不存在或不是目录")
    if require_writable and not os.access(candidate, os.W_OK):
        raise ObsidianError("知识库目录不可写")
    return candidate.resolve()


def list_paths() -> list[dict[str, Any]]:
    return _read()["paths"]


def add_path(name: str, path: str, enabled: bool = True) -> dict[str, Any]:
    name = _safe_path_name(name)
    directory = validate_directory(path)
    data = _read()
    if any(item.get("path") == str(directory) for item in data["paths"]):
        raise ObsidianError("该知识库路径已存在")
    item = {"id": str(uuid.uuid4()), "name": name, "path": str(directory), "enabled": bool(enabled)}
    data["paths"].append(item)
    _write(data)
    return item


def update_path(path_id: str, name: str, path: str, enabled: bool = True) -> dict[str, Any]:
    name = _safe_path_name(name)
    directory = validate_directory(path)
    data = _read()
    for item in data["paths"]:
        if item.get("id") == path_id:
            item.update({"name": name.strip(), "path": str(directory), "enabled": bool(enabled)})
            _write(data)
            return item
    raise ObsidianError("知识库路径不存在")


def _safe_path_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
        raise ObsidianError("路径名称不能为空且不能超过 80 个字符")
    return name


def delete_path(path_id: str) -> None:
    data = _read()
    original = len(data["paths"])
    data["paths"] = [item for item in data["paths"] if item.get("id") != path_id]
    if len(data["paths"]) == original:
        raise ObsidianError("知识库路径不存在")
    _write(data)


def _safe_part(value: str, fallback: str, limit: int = 100) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", value or "")
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:limit] or fallback)


def _safe_subfolder(value: str) -> str:
    if not value:
        return ""
    raw = value.replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ObsidianError("子文件夹不能包含 ..")
    return "/".join(_safe_part(part, "未命名", 80) for part in parts)


def _unique_file(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ObsidianError("同名文件过多，无法创建新版本")


def _asset_source(url: str) -> Path | None:
    if not url.startswith("/static/"):
        return None
    root = STATIC_ROOT.resolve()
    relative = (root / url.removeprefix("/static/")).resolve()
    return relative if relative == root or root in relative.parents else None


def archive(*, markdown: str, title: str, path_ids: list[str], subfolder: str = "", tags: list[str] | None = None,
            source: str = "", task_id: str = "", model: str = "", revision_id: str = "") -> list[dict[str, Any]]:
    if not markdown.strip():
        raise ObsidianError("Markdown 内容不能为空")
    if not path_ids:
        raise ObsidianError("至少选择一个知识库路径")
    subfolder = _safe_subfolder(subfolder)
    selected = [item for item in list_paths() if item.get("id") in set(path_ids) and item.get("enabled", True)]
    if len(selected) != len(set(path_ids)):
        raise ObsidianError("存在无效或未启用的知识库路径")
    safe_title = _safe_part(title, "未命名笔记")
    safe_tags = [_safe_part(tag, "AI", 40) for tag in (tags or []) if tag.strip()][:30]
    frontmatter = ["---", f'title: "{safe_title.replace(chr(34), chr(39))}"',
                   f'source: "{source.replace(chr(34), chr(39))}"', f'created: "{datetime.now().astimezone().isoformat(timespec="seconds")}"',
                   f'task_id: "{task_id}"', f'model: "{model}"', f'revision_id: "{revision_id}"',
                   "tags:"] + [f"  - {tag}" for tag in safe_tags] + ["---", ""]
    results = []
    for item in selected:
        try:
            vault = validate_directory(item["path"])
            target_dir = vault / subfolder if subfolder else vault
            target_dir.mkdir(parents=True, exist_ok=True)
            if vault not in target_dir.resolve().parents and target_dir.resolve() != vault:
                raise ObsidianError("目标子文件夹不能逃逸知识库路径")
            note_path = _unique_file(target_dir / f"{safe_title}.md")
            attachment_dir = target_dir / "附件" / safe_title
            if attachment_dir.exists() and (vault not in attachment_dir.resolve().parents):
                raise ObsidianError("附件目录不能逃逸知识库路径")
            rewritten = markdown
            warnings = []
            for match in re.finditer(r"(!?\[[^\]]*\])\((/static/[^)]+)\)", markdown):
                source_path = _asset_source(match.group(2))
                if not source_path or not source_path.is_file():
                    warnings.append(f"附件不存在：{match.group(2)}")
                    continue
                attachment_dir.mkdir(parents=True, exist_ok=True)
                destination = attachment_dir / _safe_part(source_path.name, "附件.bin", 120)
                if destination.exists():
                    destination = _unique_file(destination)
                shutil.copy2(source_path, destination)
                relative = Path(os.path.relpath(destination, note_path.parent)).as_posix()
                rewritten = rewritten.replace(match.group(2), relative)
            note_path.write_text("\n".join(frontmatter) + rewritten.strip() + "\n", encoding="utf-8")
            results.append({"path": str(note_path), "attachments": str(attachment_dir) if attachment_dir.exists() else None, "warnings": warnings})
        except (OSError, ObsidianError) as exc:
            results.append({"path": item.get("path", ""), "error": str(exc)})
    if not any("path" in result and "error" not in result for result in results):
        raise ObsidianError("所有知识库路径均转存失败")
    return results
