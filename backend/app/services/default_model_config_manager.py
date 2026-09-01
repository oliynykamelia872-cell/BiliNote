"""默认模型配置管理：由 UI 设置页写入，作为所有 LLM 入口的唯一兜底来源。

与 transcriber/proxy 配置一样存 JSON 文件（config/model_preference.json，相对
运行目录；Docker 把 ./backend 绑挂到 /app，因此开发环境与容器路径一致）。
文件不存在 / 损坏 / 字段为空 → 视为「未配置默认模型」。
"""
import json
from pathlib import Path
from typing import Any, Dict


class DefaultModelConfigManager:
    """管理默认供应商与模型，存储在 JSON 文件中，支持前端动态修改。"""

    def __init__(self, filepath: str = "config/model_preference.json"):
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        provider_id = str(data.get("provider_id") or "").strip()
        model_name = str(data.get("model_name") or "").strip()
        if not provider_id or not model_name:
            return {}
        return {"provider_id": provider_id, "model_name": model_name}

    def get(self) -> Dict[str, str]:
        """返回当前默认模型；未配置 / 损坏返回空字段。"""
        return self._read()

    def set(self, provider_id: str, model_name: str) -> Dict[str, str]:
        """写入默认模型并持久化。"""
        data = {
            "provider_id": str(provider_id).strip(),
            "model_name": str(model_name).strip(),
        }
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return self.get()
