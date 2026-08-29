import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.obsidian import add_path, archive, list_paths


class TestObsidianArchive(unittest.TestCase):
    def test_archive_copies_assets_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            static = root / "static"
            vault.mkdir()
            (static / "document_assets" / "task").mkdir(parents=True)
            (static / "document_assets" / "task" / "image.png").write_bytes(b"image")
            config = root / "obsidian.json"
            with patch("app.services.obsidian.CONFIG_PATH", config), patch("app.services.obsidian.STATIC_ROOT", static):
                item = add_path("测试库", str(vault))
                payload = {"markdown": "# 标题\n\n![图](/static/document_assets/task/image.png)", "title": "标题", "path_ids": [item["id"]], "task_id": "task"}
                first = archive(**payload)
                second = archive(**payload)
                self.assertEqual(len(list(vault.glob("标题*.md"))), 2)
                saved = Path(first[0]["path"]).read_text(encoding="utf-8")
                self.assertIn("附件/标题/image.png", saved)
                self.assertEqual(len(list_paths()), 1)

    def test_relative_path_is_rejected(self):
        with self.assertRaises(ValueError):
            add_path("测试库", "relative/path")


if __name__ == "__main__":
    unittest.main()
