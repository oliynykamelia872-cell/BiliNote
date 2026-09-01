from typing import Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app.services.chat_service import chat as chat_service
from app.exceptions.provider import ProviderError
from app.services.provider import ProviderService
from app.models.model_config import ModelConfig
from app.gpt.gpt_factory import GPTFactory
from app.services.model import ModelService
from app.services.vector_store import VectorStoreManager
from app.utils.logger import get_logger
from app.utils.response import ResponseWrapper as R

logger = get_logger(__name__)

router = APIRouter()

# 索引状态追踪: task_id -> "indexing" | "indexed" | "failed"
_index_status: dict[str, str] = {}


class IndexRequest(BaseModel):
    task_id: str


class ChatMessage(BaseModel):
    role: str
    content: str


class AskRequest(BaseModel):
    task_id: str
    question: str
    history: list[ChatMessage] = []
    # 模型参数可选：都不传时使用 UI 设置页配置的默认模型
    provider_id: Optional[str] = None
    model_name: Optional[str] = None


class ReviseRequest(BaseModel):
    task_id: str = ""
    instruction: str
    markdown: str
    selection: str = ""
    history: list[ChatMessage] = []
    provider_id: Optional[str] = None
    model_name: Optional[str] = None


def _do_index(task_id: str):
    """后台执行索引任务。"""
    try:
        _index_status[task_id] = "indexing"
        store = VectorStoreManager()
        store.index_task(task_id)
        _index_status[task_id] = "indexed"
        logger.info(f"索引完成: {task_id}")
    except Exception as e:
        _index_status[task_id] = "failed"
        logger.error(f"索引失败: {task_id}, {e}")


@router.post("/chat/index")
def index_task(data: IndexRequest, background_tasks: BackgroundTasks):
    """触发后台索引，立即返回。"""
    if _index_status.get(data.task_id) == "indexing":
        return R.success(msg="正在索引中")

    # 如果已经索引过，直接返回
    store = VectorStoreManager()
    if store.is_indexed(data.task_id):
        _index_status[data.task_id] = "indexed"
        return R.success(msg="已完成索引")

    _index_status[data.task_id] = "indexing"
    background_tasks.add_task(_do_index, data.task_id)
    return R.success(msg="开始索引")


@router.get("/chat/status")
def chat_status(task_id: str):
    """返回索引状态：idle / indexing / indexed / failed。"""
    try:
        # 优先检查内存状态
        status = _index_status.get(task_id)
        if status:
            return R.success(data={"status": status, "indexed": status == "indexed"})

        # 内存没有记录，检查持久化
        store = VectorStoreManager()
        indexed = store.is_indexed(task_id)
        if indexed:
            _index_status[task_id] = "indexed"
        return R.success(data={"status": "indexed" if indexed else "idle", "indexed": indexed})
    except Exception as e:
        logger.error(f"查询索引状态失败: {e}")
        return R.success(data={"status": "idle", "indexed": False})


@router.post("/chat/ask")
def ask_question(data: AskRequest):
    """基于笔记内容的 RAG 问答。"""
    try:
        provider_id, model_name = ModelService.resolve_model_pair(
            data.provider_id, data.model_name
        )
        history = [{"role": m.role, "content": m.content} for m in data.history]
        result = chat_service(
            task_id=data.task_id,
            question=data.question,
            history=history,
            provider_id=data.provider_id,
            model_name=data.model_name,
        )
        return R.success(data=result)
    except ProviderError as e:
        return R.error(msg=e.message)
    except ValueError as e:
        return R.error(msg=str(e))
    except Exception as e:
        logger.error(f"Chat 问答失败: {e}", exc_info=True)
        return R.error(msg=f"问答失败: {str(e)}")


@router.post("/chat/revise")
def revise_note(data: ReviseRequest):
    if not data.instruction.strip() or not data.markdown.strip():
        return R.error(msg="修订指令和 Markdown 内容不能为空", code=400)
    if len(data.markdown) > 200000:
        return R.error(msg="Markdown 内容过长，请先拆分文稿", code=400)
    try:
        provider_id, model_name = ModelService.resolve_model_pair(
            data.provider_id, data.model_name
        )
        provider = ProviderService.get_provider_by_id(provider_id)
        if not provider:
            raise ValueError(f"未找到模型供应商: {provider_id}")
        config = ModelConfig(
            api_key=provider["api_key"], base_url=provider["base_url"], model_name=model_name,
            provider=provider["type"], name=provider["name"],
        )
        gpt = GPTFactory.from_config(config)
        scope = "selection" if data.selection else "full"
        target = data.selection if data.selection else data.markdown
        if data.selection and data.selection not in data.markdown:
            return R.error(msg="选中文字已不在当前文稿中，请重新选择", code=400)
        messages = [{"role": "system", "content": "你是中文 Markdown 编辑。只输出修订后的正文，不要解释，不要包裹代码围栏。保持事实，不虚构来源；保留 Markdown 结构。"}]
        messages.extend({"role": item.role, "content": item.content} for item in data.history[-10:])
        messages.append({"role": "user", "content": f"用户指令：{data.instruction}\n修订范围：{scope}\n\n文本：\n{target}"})
        response = gpt.client.chat.completions.create(
            model=gpt.model, messages=messages, temperature=0.3, stream=False,
        )
        candidate = (response.choices[0].message.content or "").strip()
        if not candidate:
            raise ValueError("模型没有返回修订内容")
        full_candidate = data.markdown.replace(data.selection, candidate, 1) if data.selection else candidate
        return R.success(data={"candidate_markdown": full_candidate, "scope": scope, "notes": "已根据指令生成候选修订稿"})
    except ProviderError as exc:
        return R.error(msg=exc.message, code=400)
    except ValueError as exc:
        return R.error(msg=str(exc), code=400)
    except Exception as exc:
        logger.error(f"文稿修订失败: {exc}", exc_info=True)
        return R.error(msg=f"文稿修订失败: {exc}")
