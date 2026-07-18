import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path


def _install_stubs():
    app_mod = types.ModuleType("app")
    gpt_pkg = types.ModuleType("app.gpt")
    models_pkg = types.ModuleType("app.models")

    base_mod = types.ModuleType("app.gpt.base")

    class _GPT:
        pass

    base_mod.GPT = _GPT

    prompt_builder_mod = types.ModuleType("app.gpt.prompt_builder")

    def _generate_base_prompt(**_kwargs):
        return "prompt"

    prompt_builder_mod.generate_base_prompt = _generate_base_prompt

    prompt_mod = types.ModuleType("app.gpt.prompt")
    prompt_mod.BASE_PROMPT = ""
    prompt_mod.AI_SUM = ""
    prompt_mod.SCREENSHOT = ""
    prompt_mod.LINK = ""
    prompt_mod.MERGE_PROMPT = "merge"

    utils_mod = types.ModuleType("app.gpt.utils")

    def _fix_markdown(text):
        return text

    utils_mod.fix_markdown = _fix_markdown

    request_chunker_mod = types.ModuleType("app.gpt.request_chunker")

    @dataclass
    class _ChunkPayload:
        segments: list
        image_urls: list

    class _RequestChunker:
        def __init__(self, *_args, **_kwargs):
            pass

        def group_texts_by_budget(self, texts, _builder, **_kwargs):
            return [texts]

    request_chunker_mod.ChunkPayload = _ChunkPayload
    request_chunker_mod.RequestChunker = _RequestChunker

    gpt_model_mod = types.ModuleType("app.models.gpt_model")

    class _GPTSource:
        pass

    gpt_model_mod.GPTSource = _GPTSource

    transcriber_model_mod = types.ModuleType("app.models.transcriber_model")

    class _TranscriptSegment:
        def __init__(self, **kwargs):
            self.start = kwargs.get("start", 0)
            self.end = kwargs.get("end", 0)
            self.text = kwargs.get("text", "")

    transcriber_model_mod.TranscriptSegment = _TranscriptSegment

    sys.modules.setdefault("app", app_mod)
    sys.modules.setdefault("app.gpt", gpt_pkg)
    sys.modules.setdefault("app.models", models_pkg)
    sys.modules["app.gpt.base"] = base_mod
    sys.modules["app.gpt.prompt_builder"] = prompt_builder_mod
    sys.modules["app.gpt.prompt"] = prompt_mod
    sys.modules["app.gpt.utils"] = utils_mod
    sys.modules["app.gpt.request_chunker"] = request_chunker_mod
    sys.modules["app.models.gpt_model"] = gpt_model_mod
    sys.modules["app.models.transcriber_model"] = transcriber_model_mod


def _load_universal_gpt_class():
    _install_stubs()
    root = pathlib.Path(__file__).resolve().parents[1]
    module_path = root / "app" / "gpt" / "universal_gpt.py"
    spec = importlib.util.spec_from_file_location("universal_gpt", module_path)
    if spec is None or spec.loader is None:
        raise ImportError("universal_gpt module spec not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.UniversalGPT


UniversalGPT = _load_universal_gpt_class()


class _FailingCompletions:
    def create(self, **_kwargs):
        raise Exception("Error code: 524 - bad_response_status_code")


class _DummyChat:
    def __init__(self):
        self.completions = _FailingCompletions()


class _DummyModels:
    @staticmethod
    def list():
        return []


class _DummyClient:
    def __init__(self):
        self.chat = _DummyChat()
        self.models = _DummyModels()


class TestUniversalGPTCheckpoint(unittest.TestCase):
    def test_streaming_response_is_assembled(self):
        calls = []

        class _StreamingCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return iter([
                    types.SimpleNamespace(choices=[types.SimpleNamespace(
                        delta=types.SimpleNamespace(content="hello ")
                    )]),
                    types.SimpleNamespace(choices=[types.SimpleNamespace(
                        delta=types.SimpleNamespace(content="world")
                    )]),
                ])

        client = _DummyClient()
        client.chat.completions = _StreamingCompletions()
        gpt = UniversalGPT(client, model="mock-model")

        response = gpt._do_create([{"role": "user", "content": "prompt"}])

        self.assertTrue(calls[0]["stream"])
        self.assertEqual(response.choices[0].message.content, "hello world")

    def test_merge_524_error_persists_checkpoint(self):
        original_attempts = os.environ.get("OPENAI_RETRY_ATTEMPTS")
        os.environ["OPENAI_RETRY_ATTEMPTS"] = "1"
        gpt = UniversalGPT(_DummyClient(), model="mock-model")
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                gpt.checkpoint_dir = Path(tmp_dir)

                with self.assertRaises(Exception):
                    gpt._merge_partials(["part-a", "part-b"], "task-1", "sig-1")

                checkpoint_path = gpt._checkpoint_path("task-1")
                self.assertTrue(checkpoint_path.exists())
                payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["phase"], "merge")
                self.assertEqual(payload["partials"], ["part-a", "part-b"])
                self.assertEqual(payload["version"], 2)
                self.assertEqual(payload["merge_state"]["next_group_index"], 0)
        finally:
            if original_attempts is None:
                os.environ.pop("OPENAI_RETRY_ATTEMPTS", None)
            else:
                os.environ["OPENAI_RETRY_ATTEMPTS"] = original_attempts

    def test_merge_resumes_after_completed_group(self):
        class _TwoGroupsChunker:
            def __init__(self, *_args, **_kwargs):
                pass

            def group_texts_by_budget(self, texts, _builder, **_kwargs):
                if len(texts) == 3:
                    return [[texts[0], texts[1]], [texts[2]]]
                return [texts]

        calls = []
        gpt = UniversalGPT(_DummyClient(), model="mock-model")
        gpt._chat_completion_create = lambda messages: self._response(calls, messages)

        method_globals = UniversalGPT._merge_partials.__globals__
        original_chunker = method_globals["RequestChunker"]
        method_globals["RequestChunker"] = _TwoGroupsChunker
        try:
            resume_state = {
                "current_partials": ["part-a", "part-b", "part-c"],
                "completed_groups": ["merged-ab"],
                "next_group_index": 1,
            }
            result = gpt._merge_partials(
                ["part-a", "part-b", "part-c"],
                None,
                None,
                resume_state=resume_state,
            )
        finally:
            method_globals["RequestChunker"] = original_chunker

        self.assertEqual(result, "merged-final")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("part-a", calls[0])
        self.assertNotIn("part-b", calls[0])

    def test_merge_uses_larger_dedicated_request_budget(self):
        observed = {}

        class _RecordingChunker:
            def __init__(self, _builder, max_bytes, _estimator):
                observed["max_bytes"] = max_bytes

            def group_texts_by_budget(self, texts, _builder, **_kwargs):
                return [texts]

        gpt = UniversalGPT(_DummyClient(), model="mock-model")
        gpt.max_request_bytes = 18_000
        gpt.max_merge_request_bytes = 64_000
        gpt._chat_completion_create = lambda messages: self._response([], messages)

        method_globals = UniversalGPT._merge_partials.__globals__
        original_chunker = method_globals["RequestChunker"]
        method_globals["RequestChunker"] = _RecordingChunker
        try:
            gpt._merge_partials(["part-a", "part-b"], None, None)
        finally:
            method_globals["RequestChunker"] = original_chunker

        self.assertEqual(observed["max_bytes"], 64_000)

    def test_summarize_resumes_merge_phase_without_reprocessing_chunks(self):
        source = types.SimpleNamespace(
            screenshot=False,
            link=False,
            segment=[],
            checkpoint_key="task-merge",
            video_img_urls=[],
            title="title",
            tags=[],
            _format=[],
            style=None,
            extras=None,
        )
        gpt = UniversalGPT(_DummyClient(), model="mock-model")

        with tempfile.TemporaryDirectory() as tmp_dir:
            gpt.checkpoint_dir = Path(tmp_dir)
            signature = gpt._build_source_signature(source)
            merge_state = {
                "current_partials": ["part-a", "part-b"],
                "completed_groups": [],
                "next_group_index": 0,
            }
            gpt._save_checkpoint(
                source.checkpoint_key,
                signature,
                ["part-a", "part-b"],
                "merge",
                merge_state=merge_state,
            )

            calls = []

            def resume_merge(partials, checkpoint_key, source_signature, resume_state=None):
                calls.append((partials, checkpoint_key, source_signature, resume_state))
                return "merged-result"

            gpt._merge_partials = resume_merge
            result = gpt.summarize(source)

            self.assertEqual(result, "merged-result")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], ["part-a", "part-b"])
            self.assertEqual(calls[0][3], merge_state)
            self.assertFalse(gpt._checkpoint_path(source.checkpoint_key).exists())

    def test_retryable_large_chunk_is_split_after_retries_are_exhausted(self):
        chunk_payload = UniversalGPT._split_failed_chunk.__globals__["ChunkPayload"]

        class _SplitChunker:
            def __init__(self, _builder, max_bytes, _estimator):
                self.max_bytes = max_bytes

            def chunk(self, segments, image_urls, **_kwargs):
                midpoint = len(segments) // 2
                return [
                    chunk_payload(segments[:midpoint], image_urls[:1]),
                    chunk_payload(segments[midpoint:], image_urls[1:]),
                ]

        source = types.SimpleNamespace(
            title="title",
            tags=[],
            _format=[],
            style=None,
            extras=None,
        )
        chunk = chunk_payload(
            segments=[
                types.SimpleNamespace(start=0, end=1, text="a"),
                types.SimpleNamespace(start=1, end=2, text="b"),
            ],
            image_urls=[],
        )
        gpt = UniversalGPT(_DummyClient(), model="mock-model")
        gpt._min_request_bytes = 10
        gpt.create_messages = lambda *_args, **_kwargs: [{"role": "user", "content": "x" * 100}]

        method_globals = UniversalGPT._split_failed_chunk.__globals__
        original_chunker = method_globals["RequestChunker"]
        method_globals["RequestChunker"] = _SplitChunker
        try:
            result = gpt._split_failed_chunk(chunk, source)
        finally:
            method_globals["RequestChunker"] = original_chunker

        self.assertEqual(len(result), 2)
        self.assertEqual([segment.text for segment in result[0].segments], ["a"])
        self.assertEqual([segment.text for segment in result[1].segments], ["b"])

    @staticmethod
    def _response(calls, messages):
        content = messages[0]["content"]
        calls.append(content)
        result = "merged-c" if "part-c" in content else "merged-final"
        message = types.SimpleNamespace(content=result)
        choice = types.SimpleNamespace(message=message)
        return types.SimpleNamespace(choices=[choice])


if __name__ == "__main__":
    unittest.main()
