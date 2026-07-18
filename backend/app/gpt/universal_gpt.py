from app.gpt.base import GPT
from app.gpt.prompt_builder import generate_base_prompt
from app.models.gpt_model import GPTSource
import os
import hashlib
import json
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.gpt.prompt import BASE_PROMPT, AI_SUM, SCREENSHOT, LINK, MERGE_PROMPT
from app.gpt.utils import fix_markdown
from app.gpt.request_chunker import ChunkPayload, RequestChunker
from app.models.transcriber_model import TranscriptSegment
from datetime import timedelta
from typing import List


class UniversalGPT(GPT):
    def __init__(self, client, model: str, temperature: float = 0.7):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.screenshot = False
        self.link = False
        self.max_request_bytes = int(os.getenv("OPENAI_MAX_REQUEST_BYTES", str(45 * 1024 * 1024)))
        # Partial summaries are already condensed. Give the final merge a larger budget so
        # two long summaries can actually be combined instead of cycling as singleton groups.
        self.max_merge_request_bytes = int(
            os.getenv("OPENAI_MAX_MERGE_REQUEST_BYTES", str(max(self.max_request_bytes, 64_000)))
        )
        self.checkpoint_dir = Path(os.getenv("NOTE_OUTPUT_DIR", "note_results"))
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        # 初始化时缓存重试配置，避免每次请求重复读取环境变量
        self._max_retry_attempts = max(1, int(os.getenv("OPENAI_RETRY_ATTEMPTS", "3")))
        self._retry_base_backoff = float(os.getenv("OPENAI_RETRY_BACKOFF_SECONDS", "1.5"))
        self._retry_jitter_ratio = min(1.0, max(0.0, float(os.getenv("OPENAI_RETRY_JITTER_RATIO", "0.25"))))
        self._min_request_bytes = max(1024, int(os.getenv("OPENAI_MIN_REQUEST_BYTES", "6000")))
        self._chunk_shrink_factor = min(0.9, max(0.1, float(os.getenv("OPENAI_CHUNK_SHRINK_FACTOR", "0.5"))))
        self._stream_responses = os.getenv("OPENAI_STREAM_RESPONSES", "1").lower() not in {"0", "false", "no"}

    def _format_time(self, seconds: float) -> str:
        return str(timedelta(seconds=int(seconds)))[2:]

    def _build_segment_text(self, segments: List[TranscriptSegment]) -> str:
        return "\n".join(
            f"{self._format_time(seg.start)} - {seg.text.strip()}"
            for seg in segments
        )

    def ensure_segments_type(self, segments) -> List[TranscriptSegment]:
        return [TranscriptSegment(**seg) if isinstance(seg, dict) else seg for seg in segments]

    def create_messages(self, segments: List[TranscriptSegment], **kwargs):

        content_text = generate_base_prompt(
            title=kwargs.get('title'),
            segment_text=self._build_segment_text(segments),
            tags=kwargs.get('tags'),
            _format=kwargs.get('_format'),
            style=kwargs.get('style'),
            extras=kwargs.get('extras'),
        )

        video_img_urls = kwargs.get('video_img_urls', [])

        content: list[dict] | str
        if video_img_urls:
            # 有截图时走 OpenAI 多模态 content 数组（text + image_url）
            content = [{"type": "text", "text": content_text}]
            for url in video_img_urls:
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": url,
                        "detail": "auto"
                    }
                })
        else:
            # 纯文本场景退回 string content：DeepSeek deepseek-chat 等非多模态模型
            # 不识别 [{"type":"text",...}] 数组形态，会返回 invalid_request_error
            # （issue #282）。OpenAI 规范本身也允许 content 为 string。
            content = content_text

        messages = [{
            "role": "user",
            "content": content
        }]

        return messages

    def list_models(self):
        return self.client.models.list()

    def _estimate_messages_bytes(self, messages: list) -> int:
        import json
        return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))

    def _build_merge_messages(self, partials: list) -> list:
        merge_text = MERGE_PROMPT + "\n\n" + "\n\n---\n\n".join(partials)
        # 合并阶段没有图片，直接用 string content 兼容非多模态模型（issue #282）
        return [{
            "role": "user",
            "content": merge_text
        }]

    def _checkpoint_path(self, checkpoint_key: str) -> Path:
        safe_key = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in checkpoint_key)
        return self.checkpoint_dir / f"{safe_key}.gpt.checkpoint.json"

    def _build_source_signature(self, source: GPTSource) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_request_bytes": self.max_request_bytes,
            "title": source.title,
            "tags": source.tags,
            "format": source._format,
            "style": source.style,
            "extras": source.extras,
            "video_img_urls": source.video_img_urls or [],
            "segments": [
                {
                    "start": getattr(seg, "start", None),
                    "end": getattr(seg, "end", None),
                    "text": getattr(seg, "text", "")
                }
                for seg in source.segment
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_checkpoint(self, checkpoint_key: str, source_signature: str) -> dict | None:
        path = self._checkpoint_path(checkpoint_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("source_signature") != source_signature:
                path.unlink(missing_ok=True)
                return None
            return data
        except Exception:
            path.unlink(missing_ok=True)
            return None

    @staticmethod
    def _serialize_chunk(chunk: ChunkPayload) -> dict:
        segments = []
        for segment in chunk.segments:
            if isinstance(segment, dict):
                segments.append(dict(segment))
            else:
                segments.append({
                    "start": getattr(segment, "start", 0),
                    "end": getattr(segment, "end", 0),
                    "text": getattr(segment, "text", ""),
                })
        return {"segments": segments, "image_urls": list(chunk.image_urls)}

    @staticmethod
    def _deserialize_chunk(data: dict) -> ChunkPayload:
        segments = [TranscriptSegment(**segment) for segment in data.get("segments", [])]
        return ChunkPayload(segments=segments, image_urls=list(data.get("image_urls", [])))

    def _save_checkpoint(
        self,
        checkpoint_key: str,
        source_signature: str,
        partials: list,
        phase: str,
        *,
        pending_chunks: list[ChunkPayload] | None = None,
        merge_state: dict | None = None,
    ) -> None:
        path = self._checkpoint_path(checkpoint_key)
        data = {
            "version": 2,
            "source_signature": source_signature,
            "phase": phase,
            "partials": partials,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if pending_chunks is not None:
            data["pending_chunks"] = [self._serialize_chunk(chunk) for chunk in pending_chunks]
        if merge_state is not None:
            data["merge_state"] = merge_state
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _clear_checkpoint(self, checkpoint_key: str) -> None:
        self._checkpoint_path(checkpoint_key).unlink(missing_ok=True)

    @staticmethod
    def _is_insufficient_quota_error(exc: Exception) -> bool:
        raw = str(exc)
        return (
            "insufficient_user_quota" in raw
            or "预扣费额度失败" in raw
            or "insufficient quota" in raw.lower()
        )

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        raw = str(exc).lower()
        retryable_tokens = (
            "error code: 524",
            "bad_response_status_code",
            "timed out",
            "timeout",
            "rate limit",
            "error code: 429",
            "error code: 500",
            "error code: 502",
            "error code: 503",
            "error code: 504",
            "apiconnectionerror",
            "connection error",
            "service unavailable",
        )
        if any(token in raw for token in retryable_tokens):
            return True

        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        return status in {408, 409, 429, 500, 502, 503, 504, 524}

    @staticmethod
    def _is_temperature_unsupported_error(exc: Exception) -> bool:
        """OpenAI o1/o3/gpt-5 系列等新模型不接受自定义 temperature，
        只允许默认值 1，传 0.7 会报 `'temperature' does not support 0.7 ...`。"""
        raw = str(exc).lower()
        return "temperature" in raw and (
            "does not support" in raw
            or "unsupported_value" in raw
            or "only the default" in raw
        )

    def _create_request(self, messages: list, *, include_temperature: bool):
        kwargs = {
            "model": self.model,
            "messages": messages,
            "stream": self._stream_responses,
        }
        if include_temperature:
            kwargs["temperature"] = self.temperature

        response = self.client.chat.completions.create(**kwargs)
        if not self._stream_responses or hasattr(response, "choices"):
            return response

        parts = []
        for event in response:
            for choice in getattr(event, "choices", []) or []:
                delta = getattr(choice, "delta", None)
                content = getattr(delta, "content", None)
                if content:
                    parts.append(content)

        if not parts:
            raise RuntimeError("模型返回了空的流式响应")
        message = SimpleNamespace(content="".join(parts))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def _do_create(self, messages: list):
        """单次调用。如果模型拒绝自定义 temperature，就地去掉该参数再试一次
        （不消耗外层的重试次数预算），仍失败则把异常抛给外层重试逻辑。"""
        try:
            return self._create_request(messages, include_temperature=True)
        except Exception as exc:
            if self._is_temperature_unsupported_error(exc):
                print(f"[universal_gpt] 模型 {self.model} 不支持自定义 temperature，改用默认值重试")
                return self._create_request(messages, include_temperature=False)
            raise

    def _chat_completion_create(self, messages: list):
        last_exc = None
        for attempt in range(self._max_retry_attempts):
            try:
                return self._do_create(messages)
            except Exception as exc:
                last_exc = exc
                if attempt == self._max_retry_attempts - 1 or not self._is_retryable_error(exc):
                    raise
                jitter = random.uniform(1.0 - self._retry_jitter_ratio, 1.0 + self._retry_jitter_ratio)
                sleep_seconds = self._retry_base_backoff * (2 ** attempt) * jitter
                print(
                    f"[universal_gpt] 请求失败，{sleep_seconds:.1f}s 后进行第 {attempt + 2}/"
                    f"{self._max_retry_attempts} 次尝试: {exc}"
                )
                time.sleep(sleep_seconds)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("chat completion failed without exception")

    def _split_failed_chunk(self, chunk: ChunkPayload, source: GPTSource) -> list[ChunkPayload] | None:
        messages = self.create_messages(
            chunk.segments,
            title=source.title,
            tags=source.tags,
            video_img_urls=chunk.image_urls,
            _format=source._format,
            style=source.style,
            extras=source.extras,
        )
        current_bytes = self._estimate_messages_bytes(messages)
        if current_bytes <= self._min_request_bytes:
            return None

        target_bytes = max(self._min_request_bytes, int(current_bytes * self._chunk_shrink_factor))
        if target_bytes >= current_bytes:
            return None

        def message_builder(segments, image_urls, **kwargs):
            return self.create_messages(segments, video_img_urls=image_urls, **kwargs)

        chunker = RequestChunker(message_builder, target_bytes, self._estimate_messages_bytes)
        try:
            smaller_chunks = chunker.chunk(
                chunk.segments,
                chunk.image_urls,
                title=source.title,
                tags=source.tags,
                _format=source._format,
                style=source.style,
                extras=source.extras,
            )
        except ValueError:
            return None

        if len(smaller_chunks) <= 1:
            return None
        print(
            f"[universal_gpt] 请求持续失败，将 {current_bytes} 字节的分块缩小为 "
            f"{len(smaller_chunks)} 个分块（上限 {target_bytes} 字节）"
        )
        return smaller_chunks

    def _merge_partials(
        self,
        partials: list,
        checkpoint_key: str | None,
        source_signature: str | None,
        resume_state: dict | None = None,
    ) -> str:
        def build_messages(texts, *_args, **_kwargs):
            return self._build_merge_messages(texts)

        merge_chunker = RequestChunker(
            lambda *_args, **_kwargs: [],
            self.max_merge_request_bytes,
            self._estimate_messages_bytes
        )

        current_partials = list((resume_state or {}).get("current_partials", partials))
        completed_groups = list((resume_state or {}).get("completed_groups", []))
        next_group_index = int((resume_state or {}).get("next_group_index", 0))
        while len(current_partials) > 1:
            groups = merge_chunker.group_texts_by_budget(current_partials, build_messages)
            if next_group_index > len(groups):
                completed_groups = []
                next_group_index = 0
            new_partials = list(completed_groups)
            for group_idx in range(next_group_index, len(groups)):
                group = groups[group_idx]
                messages = build_messages(group)
                try:
                    response = self._chat_completion_create(messages)
                except Exception:
                    if checkpoint_key and source_signature:
                        merge_state = {
                            "current_partials": current_partials,
                            "completed_groups": new_partials,
                            "next_group_index": group_idx,
                        }
                        self._save_checkpoint(
                            checkpoint_key,
                            source_signature,
                            current_partials,
                            "merge",
                            merge_state=merge_state,
                        )
                    raise

                new_partials.append(response.choices[0].message.content.strip())

                if checkpoint_key and source_signature:
                    merge_state = {
                        "current_partials": current_partials,
                        "completed_groups": new_partials,
                        "next_group_index": group_idx + 1,
                    }
                    self._save_checkpoint(
                        checkpoint_key,
                        source_signature,
                        current_partials,
                        "merge",
                        merge_state=merge_state,
                    )

            current_partials = new_partials
            completed_groups = []
            next_group_index = 0
            if checkpoint_key and source_signature and len(current_partials) > 1:
                merge_state = {
                    "current_partials": current_partials,
                    "completed_groups": [],
                    "next_group_index": 0,
                }
                self._save_checkpoint(
                    checkpoint_key,
                    source_signature,
                    current_partials,
                    "merge",
                    merge_state=merge_state,
                )

        return current_partials[0]

    def summarize(self, source: GPTSource) -> str:
        self.screenshot = source.screenshot
        self.link = source.link
        source.segment = self.ensure_segments_type(source.segment)
        checkpoint_key = source.checkpoint_key
        source_signature = self._build_source_signature(source) if checkpoint_key else None
        checkpoint = self._load_checkpoint(checkpoint_key, source_signature) if checkpoint_key and source_signature else None

        if checkpoint and checkpoint.get("phase") == "merge":
            merge_partials = checkpoint.get("partials", [])
            merge_state = checkpoint.get("merge_state")
            merged = self._merge_partials(
                merge_partials,
                checkpoint_key,
                source_signature,
                resume_state=merge_state,
            )
            self._clear_checkpoint(checkpoint_key)
            return merged

        def message_builder(segments, image_urls, **kwargs):
            return self.create_messages(segments, video_img_urls=image_urls, **kwargs)

        chunker = RequestChunker(message_builder, self.max_request_bytes, self._estimate_messages_bytes)

        try:
            chunks = chunker.chunk(
                source.segment,
                source.video_img_urls or [],
                title=source.title,
                tags=source.tags,
                _format=source._format,
                style=source.style,
                extras=source.extras
            )
        except ValueError:
            chunks = chunker.chunk(
                source.segment,
                [],
                title=source.title,
                tags=source.tags,
                _format=source._format,
                style=source.style,
                extras=source.extras
            )

        partials = []
        pending_chunks = chunks
        if checkpoint and checkpoint.get("phase") == "summarize":
            if isinstance(checkpoint.get("partials"), list):
                partials = checkpoint["partials"]
            serialized_pending = checkpoint.get("pending_chunks")
            if isinstance(serialized_pending, list):
                pending_chunks = [self._deserialize_chunk(chunk) for chunk in serialized_pending]
            else:
                pending_chunks = chunks[len(partials):] if len(partials) <= len(chunks) else chunks
                if len(partials) > len(chunks):
                    partials = []

        if checkpoint_key and source_signature:
            self._save_checkpoint(
                checkpoint_key,
                source_signature,
                partials,
                "summarize",
                pending_chunks=pending_chunks,
            )

        while pending_chunks:
            chunk = pending_chunks[0]
            messages = self.create_messages(
                chunk.segments,
                title=source.title,
                tags=source.tags,
                video_img_urls=chunk.image_urls,
                _format=source._format,
                style=source.style,
                extras=source.extras
            )
            try:
                response = self._chat_completion_create(messages)
            except Exception as exc:
                smaller_chunks = self._split_failed_chunk(chunk, source) if self._is_retryable_error(exc) else None
                if smaller_chunks:
                    pending_chunks = smaller_chunks + pending_chunks[1:]
                    if checkpoint_key and source_signature:
                        self._save_checkpoint(
                            checkpoint_key,
                            source_signature,
                            partials,
                            "summarize",
                            pending_chunks=pending_chunks,
                        )
                    continue
                if checkpoint_key and source_signature:
                    self._save_checkpoint(
                        checkpoint_key,
                        source_signature,
                        partials,
                        "summarize",
                        pending_chunks=pending_chunks,
                    )
                raise

            partials.append(response.choices[0].message.content.strip())
            pending_chunks = pending_chunks[1:]
            if checkpoint_key and source_signature:
                self._save_checkpoint(
                    checkpoint_key,
                    source_signature,
                    partials,
                    "summarize",
                    pending_chunks=pending_chunks,
                )

        if len(partials) == 1:
            if checkpoint_key:
                self._clear_checkpoint(checkpoint_key)
            return partials[0]
        merged = self._merge_partials(partials, checkpoint_key, source_signature)
        if checkpoint_key:
            self._clear_checkpoint(checkpoint_key)
        return merged
