# app/routers/note.py
import json
import os
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File
from pydantic import BaseModel, validator, field_validator, model_validator
from dataclasses import asdict

from app.db.video_task_dao import get_task_by_video
from app.enmus.exception import NoteErrorEnum
from app.enmus.note_enums import DownloadQuality
from app.exceptions.note import NoteError
from app.exceptions.provider import ProviderError
from app.services.note import NoteGenerator, logger, update_task_status
from app.services.document_converter import DocumentConverter, DocumentConversionError
from app.services.model import ModelService
from app.services.task_serial_executor import TaskCancelledError, task_serial_executor
from app.utils.response import ResponseWrapper as R
from app.utils.url_parser import extract_video_id, normalize_video_url
from app.validators.video_url_validator import is_supported_video_url
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
import httpx
from app.enmus.task_status_enums import TaskStatus

# from app.services.downloader import download_raw_audio
# from app.services.whisperer import transcribe_audio

router = APIRouter()


class RecordRequest(BaseModel):
    video_id: str
    platform: str


class VideoRequest(BaseModel):
    video_url: str
    platform: str
    quality: DownloadQuality
    screenshot: Optional[bool] = False
    link: Optional[bool] = False
    # 模型参数可选：都不传时使用 UI 设置页配置的默认模型（服务端解析）
    model_name: Optional[str] = None
    provider_id: Optional[str] = None
    task_id: Optional[str] = None
    format: Optional[list] = []
    style: str = None
    extras: Optional[str]=None
    video_understanding: Optional[bool] = False
    video_interval: Optional[int] = 0
    grid_size: Optional[list] = []
    # 客户端（如浏览器插件）已经在用户浏览器里抓到字幕，直接传给后端复用，
    # 跳过 download_subtitles 和音频转写。形如：
    #   {"language": "zh", "full_text": "...", "segments": [{"start","end","text"}, ...]}
    prefetched_transcript: Optional[dict] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_url(cls, data):
        # 稍后再看/收藏夹/带追踪参数的 B 站链接先规范化成标准 /video/BVxxx 形式，
        # 后续校验和 yt-dlp 下载拿到的都是干净链接
        if isinstance(data, dict) and data.get("platform") == "bilibili" and data.get("video_url"):
            data["video_url"] = normalize_video_url(str(data["video_url"]))
        return data

    @field_validator("video_url")
    def validate_supported_url(cls, v):
        url = str(v)
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https"):
            # 是网络链接，继续用原有平台校验
            if not is_supported_video_url(url):
                raise NoteError(code=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.code,
                                message=NoteErrorEnum.PLATFORM_NOT_SUPPORTED.message)

        return v


NOTE_OUTPUT_DIR = os.getenv("NOTE_OUTPUT_DIR", "note_results")
UPLOAD_DIR = "uploads"
DOCUMENT_UPLOAD_DIR = Path(UPLOAD_DIR) / "documents"
MAX_DOCUMENT_SIZE = 50 * 1024 * 1024


def save_note_to_file(task_id: str, note):
    os.makedirs(NOTE_OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json"), "w", encoding="utf-8") as f:
        json.dump(asdict(note), f, ensure_ascii=False, indent=2)


def _persist_prefetched_transcript(task_id: str, transcript: dict) -> None:
    """把客户端预取的字幕写到 NoteGenerator 期望的转写缓存文件里。

    NoteGenerator.generate 会优先读 <task_id>_transcript.json，命中即跳过 download_subtitles
    与音频转写流程。要求字段：language(可空)/full_text/segments[{start,end,text}]
    """
    segments = transcript.get("segments") or []
    cleaned_segments = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        cleaned_segments.append({
            "start": float(s.get("start", 0)),
            "end": float(s.get("end", 0)),
            "text": text,
        })
    if not cleaned_segments:
        raise ValueError("prefetched_transcript 没有可用的 segments")

    full_text = transcript.get("full_text") or " ".join(s["text"] for s in cleaned_segments)
    payload = {
        "language": transcript.get("language") or "zh",
        "full_text": full_text,
        "segments": cleaned_segments,
    }

    os.makedirs(NOTE_OUTPUT_DIR, exist_ok=True)
    target = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}_transcript.json")
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"已写入客户端预取字幕缓存: {target} ({len(cleaned_segments)} 段)")


def run_note_task(task_id: str, video_url: str, platform: str, quality: DownloadQuality,
                  link: bool = False, screenshot: bool = False, model_name: str = None, provider_id: str = None,
                  _format: list = None, style: str = None, extras: str = None, video_understanding: bool = False,
                  video_interval=0, grid_size=[]
                  ):

    if not model_name or not provider_id:
        update_task_status(task_id, TaskStatus.FAILED, message="请选择模型和提供者")
        return

    def _execute_note_task():
        return NoteGenerator().generate(
            video_url=video_url,
            platform=platform,
            quality=quality,
            task_id=task_id,
            model_name=model_name,
            provider_id=provider_id,
            link=link,
            _format=_format,
            style=style,
            extras=extras,
            screenshot=screenshot,
            video_understanding=video_understanding,
            video_interval=video_interval,
            grid_size=grid_size,
        )

    logger.info(f"任务进入执行队列 (task_id={task_id})")
    try:
        note = task_serial_executor.run_reserved(task_id, _execute_note_task)
    except TaskCancelledError:
        update_task_status(task_id, TaskStatus.CANCELLED, message="任务已停止")
        return
    except Exception as exc:
        logger.exception(f"后台任务执行异常 (task_id={task_id})")
        update_task_status(task_id, TaskStatus.FAILED, message=str(exc))
        return
    if task_serial_executor.is_cancelled(task_id):
        update_task_status(task_id, TaskStatus.CANCELLED, message="任务已停止")
        return
    logger.info(f"Note generated: {task_id}")
    if not note or not note.markdown:
        logger.warning(f"任务 {task_id} 执行失败，跳过保存")
        return
    save_note_to_file(task_id, note)

    # 自动建立向量索引（用于 AI 问答），失败不影响笔记生成
    try:
        from app.services.vector_store import VectorStoreManager
        VectorStoreManager().index_task(task_id)
    except Exception as e:
        logger.warning(f"向量索引失败（不影响笔记）: {e}")


@router.post('/delete_task')
def delete_task(data: RecordRequest):
    try:
        # TODO: 待持久化完成
        # NoteGenerator().delete_note(video_id=data.video_id, platform=data.platform)
        return R.success(msg='删除成功')
    except Exception as e:
        return R.error(msg=e)


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_location = os.path.join(UPLOAD_DIR, file.filename)

    with open(file_location, "wb+") as f:
        f.write(await file.read())

    # 假设你静态目录挂载了 /uploads
    return R.success({"url": f"/uploads/{file.filename}"})


def _save_document_result(task_id: str, filename: str, conversion) -> None:
    os.makedirs(NOTE_OUTPUT_DIR, exist_ok=True)
    result = {
        "markdown": conversion.markdown,
        "transcript": {"full_text": "", "language": "", "raw": None, "segments": []},
        "audio_meta": {
            "file_path": "", "title": filename, "duration": 0, "cover_url": None,
            "platform": "document", "video_id": task_id, "raw_info": {"document": True},
        },
        "conversion_meta": {
            "engine": "markitdown",
            "assets": conversion.assets,
            "warnings": conversion.warnings,
            "failed_files": conversion.failed_files,
        },
    }
    with open(os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json"), "w", encoding="utf-8") as result_file:
        json.dump(result, result_file, ensure_ascii=False, indent=2)


def run_document_task(task_id: str, source_path: str, filename: str, ocr_mode: str,
                      model_name: Optional[str], provider_id: Optional[str]) -> None:
    def convert():
        task_serial_executor.raise_if_cancelled(task_id)
        update_task_status(task_id, TaskStatus.PARSING, "正在解析文档")
        converter = DocumentConverter(task_id, Path(source_path), ocr_mode, model_name, provider_id)
        conversion = converter.convert()
        task_serial_executor.raise_if_cancelled(task_id)
        update_task_status(task_id, TaskStatus.SAVING, "正在保存 Markdown 和图片")
        _save_document_result(task_id, filename, conversion)

    try:
        task_serial_executor.run_reserved(task_id, convert)
        task_serial_executor.raise_if_cancelled(task_id)
        update_task_status(task_id, TaskStatus.SUCCESS, "文档转换完成")
    except TaskCancelledError:
        update_task_status(task_id, TaskStatus.CANCELLED, "任务已停止")
    except Exception as exc:
        logger.exception("文档转换失败 (task_id=%s)", task_id)
        update_task_status(task_id, TaskStatus.FAILED, str(exc))


@router.post("/convert_document")
async def convert_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    ocr_mode: str = "offline_first",
    model_name: Optional[str] = None,
    provider_id: Optional[str] = None,
):
    original_name = Path(file.filename or "").name
    extension = Path(original_name).suffix.lower()
    if not original_name or extension not in DocumentConverter.supported_extensions:
        return R.error("不支持的文件格式，请上传受支持的本地文档、数据、图片或 ZIP 文件", code=400)
    if ocr_mode not in {"offline_first", "offline_only", "visual_fallback", "off"}:
        return R.error("无效的 OCR 策略", code=400)
    # 入队前解析视觉模型：显式参数成对校验；visual_fallback 必须解析到可用模型
    # （显式或默认）；offline_first 未提供模型时保持离线行为，仅在显式给出时校验。
    resolved_provider_id: Optional[str] = None
    resolved_model_name: Optional[str] = None
    if ocr_mode == "visual_fallback" or model_name or provider_id:
        try:
            resolved_provider_id, resolved_model_name = ModelService.resolve_model_pair(
                model_name, provider_id
            )
        except ProviderError as e:
            return R.error(msg=e.message, code=400)

    task_id = str(uuid.uuid4())
    upload_dir = DOCUMENT_UPLOAD_DIR / task_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    source_path = upload_dir / f"source{extension}"
    total_size = 0
    try:
        with source_path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                total_size += len(chunk)
                if total_size > MAX_DOCUMENT_SIZE:
                    raise DocumentConversionError("文件不能超过 50MB")
                target.write(chunk)
    except DocumentConversionError as exc:
        source_path.unlink(missing_ok=True)
        return R.error(str(exc), code=400)
    finally:
        await file.close()
    if total_size == 0:
        source_path.unlink(missing_ok=True)
        return R.error("不能上传空文件", code=400)
    try:
        DocumentConverter.validate_source(source_path)
    except DocumentConversionError as exc:
        source_path.unlink(missing_ok=True)
        return R.error(str(exc), code=400)

    if not task_serial_executor.reserve(task_id):
        return R.error("任务创建失败，请重试", code=500)
    update_task_status(task_id, TaskStatus.PENDING, "文档已进入转换队列")
    background_tasks.add_task(
        run_document_task, task_id, str(source_path), original_name, ocr_mode,
        resolved_model_name, resolved_provider_id,
    )
    return R.success({"task_id": task_id})


@router.post("/cancel_task/{task_id}")
def cancel_task(task_id: str):
    """Request cancellation for a queued or running conversion task."""
    try:
        uuid.UUID(task_id)
    except ValueError:
        return R.error("无效的任务 ID", code=400)

    status_path = Path(NOTE_OUTPUT_DIR) / f"{task_id}.status.json"
    if not status_path.exists():
        return R.error("任务不存在", code=404)
    try:
        status = json.loads(status_path.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError):
        return R.error("无法读取任务状态", code=500)
    if status in {TaskStatus.SUCCESS.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
        return R.error("任务已结束，无法停止", code=409)
    if not task_serial_executor.request_cancel(task_id):
        return R.error("任务已结束，无法停止", code=409)

    update_task_status(task_id, TaskStatus.CANCELLED, "正在停止任务，当前步骤结束后生效")
    return R.success({"task_id": task_id, "status": TaskStatus.CANCELLED.value}, msg="已请求停止任务")


@router.post("/generate_note")
def generate_note(data: VideoRequest, background_tasks: BackgroundTasks):
    try:
        # 就绪门禁：本地转写引擎（fast-whisper / mlx-whisper）必须等模型下载完才能跑视频，
        # 否则任务会卡在首次下载（慢 / OOM / 截断），用户只看到一个静默失败的任务。
        # 客户端已抓好字幕（prefetched_transcript）则不需要转写，跳过检查。
        if not data.prefetched_transcript:
            from app.services.transcriber_config_manager import TranscriberConfigManager
            readiness = TranscriberConfigManager().is_model_ready()
            if not readiness["ready"]:
                logger.warning(f"拒绝 generate_note：{readiness['reason']}")
                return R.error(
                    msg=readiness["reason"],
                    code=300102,
                    data={
                        "reason": "transcriber_model_not_ready",
                        "transcriber_type": readiness["transcriber_type"],
                        "model_size": readiness["model_size"],
                        "downloading": readiness["downloading"],
                    },
                )

        # 入队前解析模型：显式参数（成对）优先，否则用默认模型；失败立即返回
        try:
            model_provider_id, model_name = ModelService.resolve_model_pair(
                data.provider_id, data.model_name
            )
        except ProviderError as e:
            return R.error(msg=e.message)

        video_id = extract_video_id(data.video_url, data.platform)
        # if not video_id:
        #     raise HTTPException(status_code=400, detail="无法提取视频 ID")
        # existing = get_task_by_video(video_id, data.platform)
        # if existing:
        #     return R.error(
        #         msg='笔记已生成，请勿重复发起',
        #
        #     )
        if data.task_id:
            # 如果传了task_id，说明是重试！
            task_id = data.task_id
            logger.info(f"重试模式，复用已有 task_id={task_id}")
        else:
            # 正常新建任务
            task_id = str(uuid.uuid4())

        # 同一个任务只能有一个执行实例。重复点击重试时复用正在运行的任务，
        # 避免多个线程同时覆盖状态文件和 GPT 断点。
        if not task_serial_executor.reserve(task_id):
            logger.info(f"任务已在队列或运行中，忽略重复提交 (task_id={task_id})")
            return R.success({"task_id": task_id, "already_running": True})

        # 统一先写入 PENDING，表示已进入队列等待串行执行
        update_task_status(task_id, TaskStatus.PENDING, message="任务已进入队列")

        # 客户端已经抓好字幕的话，写到转写缓存文件，NoteGenerator 的 cache-hit 逻辑会直接用上
        if data.prefetched_transcript:
            try:
                _persist_prefetched_transcript(task_id, data.prefetched_transcript)
            except Exception as e:
                logger.warning(f"写入预取字幕失败 (task_id={task_id}): {e}")

        background_tasks.add_task(run_note_task, task_id, data.video_url, data.platform, data.quality, data.link,
                                  data.screenshot, model_name, model_provider_id, data.format, data.style,
                                  data.extras, data.video_understanding, data.video_interval, data.grid_size)
        return R.success({"task_id": task_id})
    except ProviderError as e:
        return R.error(msg=e.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/task_status/{task_id}")
def get_task_status(task_id: str):
    status_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.status.json")
    result_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json")

    # 优先读状态文件
    if os.path.exists(status_path):
        with open(status_path, "r", encoding="utf-8") as f:
            status_content = json.load(f)

        status = status_content.get("status")
        message = status_content.get("message", "")

        if status == TaskStatus.SUCCESS.value:
            # 成功状态的话，继续读取最终笔记内容
            if os.path.exists(result_path):
                with open(result_path, "r", encoding="utf-8") as rf:
                    result_content = json.load(rf)
                return R.success({
                    "status": status,
                    "result": result_content,
                    "message": message,
                    "task_id": task_id
                })
            else:
                # 理论上不会出现，保险处理
                return R.success({
                    "status": TaskStatus.PENDING.value,
                    "message": "任务完成，但结果文件未找到",
                    "task_id": task_id
                })

        if status == TaskStatus.FAILED.value:
            return R.error(message or "任务失败", code=500)

        # 总结请求耗时较长时，从断点补充真实进度，避免界面看起来卡死。
        checkpoint_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}_markdown.gpt.checkpoint.json")
        terminal_statuses = {TaskStatus.SUCCESS.value, TaskStatus.FAILED.value}
        if status not in terminal_statuses and os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, "r", encoding="utf-8") as cf:
                    checkpoint = json.load(cf)
                phase = checkpoint.get("phase")
                if phase == "summarize":
                    completed = len(checkpoint.get("partials") or [])
                    pending = len(checkpoint.get("pending_chunks") or [])
                    message = f"正在总结视频内容：已完成 {completed}/{completed + pending} 个分段"
                    status = TaskStatus.SUMMARIZING.value
                elif phase == "merge":
                    merge_state = checkpoint.get("merge_state") or {}
                    completed = len(merge_state.get("completed_groups") or [])
                    current = len(merge_state.get("current_partials") or checkpoint.get("partials") or [])
                    message = f"正在合并 {current} 个分段摘要"
                    if completed:
                        message += f"（本轮已完成 {completed} 组）"
                    status = TaskStatus.SUMMARIZING.value
            except (OSError, ValueError, TypeError):
                logger.warning(f"读取 GPT 断点进度失败 (task_id={task_id})", exc_info=True)

        # 处理中状态
        return R.success({
            "status": status,
            "message": message,
            "task_id": task_id
        })

    # 没有状态文件，但有结果
    if os.path.exists(result_path):
        with open(result_path, "r", encoding="utf-8") as f:
            result_content = json.load(f)
        return R.success({
            "status": TaskStatus.SUCCESS.value,
            "result": result_content,
            "task_id": task_id
        })

    # 什么都没有，默认PENDING
    return R.success({
        "status": TaskStatus.PENDING.value,
        "message": "任务排队中",
        "task_id": task_id
    })


@router.get("/image_proxy")
async def image_proxy(request: Request, url: str):
    source_host = (urlparse(url).hostname or "").lower()
    referer = "https://www.xiaohongshu.com/" if (
        source_host.endswith("xhscdn.com") or source_host.endswith("xiaohongshu.com")
    ) else "https://www.bilibili.com/"
    headers = {
        "Referer": referer,
        "User-Agent": request.headers.get("User-Agent", ""),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)

            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="图片获取失败")

            content_type = resp.headers.get("Content-Type", "image/jpeg")
            return StreamingResponse(
                resp.aiter_bytes(),
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",  #  缓存一天
                    "Content-Type": content_type,
                }
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
