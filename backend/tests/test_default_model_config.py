"""默认模型配置：manager 读写 + resolve_model_pair 严格校验 + /default_model 接口。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.enmus.exception import ProviderErrorEnum
from app.exceptions.provider import ProviderError
from app.services.default_model_config_manager import DefaultModelConfigManager
from app.services.model import ModelService
from app.routers import model as model_router

# 注意：套件里部分旧测试会在导入期删除 / 替换 sys.modules 里的 app.* 模块，
# 字符串形式的 patch 目标会按名字重新解析到新模块对象，与这里绑定的类分叉。
# 因此统一在导入期绑定模块对象，并只用 patch.object 打这个绑定对象。
import app.services.model as model_module


class TestDefaultModelConfigManager(unittest.TestCase):
    def _manager(self):
        tmp = tempfile.mkdtemp(prefix="bilinote-default-model-")
        return DefaultModelConfigManager(str(Path(tmp) / "model_preference.json"))

    def test_missing_file_returns_empty(self):
        self.assertEqual(self._manager().get(), {})

    def test_set_then_get_roundtrip(self):
        mgr = self._manager()
        mgr.set("deepseek", "deepseek-v4-flash")
        self.assertEqual(
            mgr.get(),
            {"provider_id": "deepseek", "model_name": "deepseek-v4-flash"},
        )

    def test_corrupt_file_returns_empty(self):
        mgr = self._manager()
        mgr.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(mgr.get(), {})

    def test_empty_or_partial_fields_returns_empty(self):
        mgr = self._manager()
        mgr.path.write_text(json.dumps({"provider_id": "", "model_name": ""}), encoding="utf-8")
        self.assertEqual(mgr.get(), {})
        mgr.path.write_text(json.dumps({"provider_id": "deepseek"}), encoding="utf-8")
        self.assertEqual(mgr.get(), {})


class TestResolveModelPair(unittest.TestCase):
    def setUp(self):
        self.provider = {
            "id": "deepseek",
            "name": "DeepSeek",
            "logo": "DeepSeek",
            "type": "built-in",
            "enabled": 1,
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-test",
            "created_at": None,
        }
        self.patch_provider = mock.patch.object(
            model_module.ProviderService,
            "get_provider_by_id",
            return_value=dict(self.provider),
        )
        self.patch_registered = mock.patch.object(
            model_module,
            "get_model_by_provider_and_name",
            return_value={"id": 1, "provider_id": "deepseek", "model_name": "deepseek-v4-flash"},
        )
        self.patch_default = mock.patch.object(
            model_module,
            "DefaultModelConfigManager",
        )
        self.mock_provider = self.patch_provider.start()
        self.mock_registered = self.patch_registered.start()
        self.mock_default_cls = self.patch_default.start()
        self.mock_default = self.mock_default_cls.return_value.get
        self.mock_default.return_value = {}

    def tearDown(self):
        self.patch_provider.stop()
        self.patch_registered.stop()
        self.patch_default.stop()

    def test_explicit_valid_pair(self):
        self.assertEqual(
            ModelService.resolve_model_pair("deepseek", "deepseek-v4-flash"),
            ("deepseek", "deepseek-v4-flash"),
        )

    def test_explicit_unregistered_model_raises(self):
        self.mock_registered.return_value = None
        with self.assertRaises(ProviderError) as ctx:
            ModelService.resolve_model_pair("deepseek", "no-such-model")
        self.assertIn("未在供应商", ctx.exception.message)
        self.assertIn("no-such-model", ctx.exception.message)

    def test_disabled_provider_raises(self):
        self.mock_provider.return_value = {**self.provider, "enabled": 0}
        with self.assertRaises(ProviderError) as ctx:
            ModelService.resolve_model_pair("openai", "gpt-4o")
        self.assertIn("未启用", ctx.exception.message)

    def test_partial_pair_raises(self):
        for pair in [("deepseek", None), (None, "deepseek-v4-flash")]:
            with self.assertRaises(ProviderError) as ctx:
                ModelService.resolve_model_pair(*pair)
            self.assertIn("必须同时提供", ctx.exception.message)

    def test_no_default_raises(self):
        with self.assertRaises(ProviderError) as ctx:
            ModelService.resolve_model_pair(None, None)
        self.assertIn("未配置默认模型", ctx.exception.message)

    def test_default_used_when_configured(self):
        self.mock_default.return_value = {"provider_id": "deepseek", "model_name": "deepseek-v4-flash"}
        self.assertEqual(
            ModelService.resolve_model_pair(None, None),
            ("deepseek", "deepseek-v4-flash"),
        )

    def test_invalid_default_treated_as_unconfigured(self):
        self.mock_default.return_value = {"provider_id": "deepseek", "model_name": "deepseek-v4-flash"}
        self.mock_registered.return_value = None
        with self.assertRaises(ProviderError) as ctx:
            ModelService.resolve_model_pair(None, None)
        self.assertIn("默认模型配置已失效", ctx.exception.message)


class TestDefaultModelEndpoints(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(model_router.router)
        self.client = TestClient(app)
        self.patch_get = mock.patch.object(ModelService, "get_default_model")
        self.patch_set = mock.patch.object(ModelService, "set_default_model")
        self.mock_get = self.patch_get.start()
        self.mock_set = self.patch_set.start()

    def tearDown(self):
        self.patch_get.stop()
        self.patch_set.stop()

    def test_get_returns_saved_default(self):
        self.mock_get.return_value = {"provider_id": "deepseek", "model_name": "deepseek-v4-flash"}
        res = self.client.get("/default_model")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"], {"provider_id": "deepseek", "model_name": "deepseek-v4-flash"})

    def test_post_valid_writes_default(self):
        self.mock_set.return_value = {"provider_id": "deepseek", "model_name": "deepseek-v4-flash"}
        res = self.client.post(
            "/default_model",
            json={"provider_id": "deepseek", "model_name": "deepseek-v4-flash"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["code"], 0)
        self.mock_set.assert_called_once_with("deepseek", "deepseek-v4-flash")

    def test_post_invalid_returns_error(self):
        self.mock_set.side_effect = ProviderError(
            code=ProviderErrorEnum.WRONG_PARAMETER,
            message="模型「no-such-model」未在供应商「DeepSeek」下登记，请先在模型供应商页添加",
        )
        res = self.client.post(
            "/default_model",
            json={"provider_id": "deepseek", "model_name": "no-such-model"},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertNotEqual(body["code"], 0)
        self.assertIn("未在供应商", body["msg"])
