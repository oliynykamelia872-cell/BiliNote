from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.obsidian import ObsidianError, add_path, archive, delete_path, list_paths, update_path, validate_directory
from app.utils.response import ResponseWrapper as R

router = APIRouter()


class PathRequest(BaseModel):
    name: str
    path: str
    enabled: bool = True


class ValidateRequest(BaseModel):
    path: str


class ArchiveRequest(BaseModel):
    task_id: str = ""
    markdown: str
    title: str
    path_ids: list[str] = Field(min_length=1)
    subfolder: str = ""
    tags: list[str] = []
    source_url: str = ""
    model: str = ""
    revision_id: str = ""


def _error(exc: Exception):
    return R.error(str(exc), code=400)


@router.get("/obsidian/paths")
def get_paths():
    try:
        return R.success(data=list_paths())
    except ObsidianError as exc:
        return _error(exc)


@router.post("/obsidian/paths")
def create_path(data: PathRequest):
    try:
        return R.success(data=add_path(data.name, data.path, data.enabled))
    except ObsidianError as exc:
        return _error(exc)


@router.put("/obsidian/paths/{path_id}")
def edit_path(path_id: str, data: PathRequest):
    try:
        return R.success(data=update_path(path_id, data.name, data.path, data.enabled))
    except ObsidianError as exc:
        return _error(exc)


@router.delete("/obsidian/paths/{path_id}")
def remove_path(path_id: str):
    try:
        delete_path(path_id)
        return R.success(msg="知识库路径已删除")
    except ObsidianError as exc:
        return _error(exc)


@router.post("/obsidian/validate_path")
def check_path(data: ValidateRequest):
    try:
        directory = validate_directory(data.path)
        return R.success(data={"path": str(directory), "writable": True})
    except ObsidianError as exc:
        return _error(exc)


@router.post("/obsidian/archive")
def archive_note(data: ArchiveRequest):
    try:
        payload = data.model_dump()
        payload["source"] = payload.pop("source_url")
        return R.success(data={"results": archive(**payload)}, msg="文稿已转存")
    except ObsidianError as exc:
        return _error(exc)
