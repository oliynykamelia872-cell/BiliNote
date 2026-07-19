import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Several legacy tests install lightweight fake ``app`` modules globally.
# Restore the real package before importing this integration-level downloader test.
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app.downloaders.xiaohongshu_downloader import XiaohongshuDownloader


SAMPLE_URL = (
    "https://www.xiaohongshu.com/explore/6a523217000000002100b841"
    "?xsec_token=sample&xsec_source=app_share"
)


def _sample_info():
    return {
        "id": "6a523217000000002100b841",
        "title": "如何在1小时内完全吃透一个行业",
        "duration": 465.746,
        "thumbnail": "https://sns-webpic-qc.xhscdn.com/cover.jpg",
        "tags": ["AI工具", "知识库"],
        "uploader_id": "6136b334000000000201a97c",
        "webpage_url": SAMPLE_URL,
        "extractor": "XiaoHongShu",
    }


class TestXiaohongshuDownloader(unittest.TestCase):
    def setUp(self):
        cookie_patcher = patch(
            "app.downloaders.xiaohongshu_downloader.CookieConfigManager.get",
            return_value=None,
        )
        self.addCleanup(cookie_patcher.stop)
        cookie_patcher.start()
        self.downloader = XiaohongshuDownloader()

    def test_metadata_only_does_not_download_media(self):
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            self.downloader,
            "_extract_info",
            return_value=_sample_info(),
        ) as extract_info:
            result = self.downloader.download(SAMPLE_URL, output_dir=tmp_dir, skip_download=True)

        extract_info.assert_called_once_with(SAMPLE_URL, tmp_dir, audio=True, download=False)
        self.assertEqual(result.platform, "xiaohongshu")
        self.assertEqual(result.video_id, "6a523217000000002100b841")
        self.assertEqual(result.raw_info["tags"], ["AI工具", "知识库"])

    def test_existing_video_is_reused_for_audio_extraction(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "6a523217000000002100b841.mp4"
            audio_path = Path(tmp_dir) / "6a523217000000002100b841.mp3"
            video_path.write_bytes(b"video")

            def create_audio(_video_path, target_path, _quality):
                Path(target_path).write_bytes(b"audio")

            with patch.object(self.downloader, "_extract_info", return_value=_sample_info()) as extract_info, patch.object(
                self.downloader,
                "_extract_audio",
                side_effect=create_audio,
            ) as extract_audio:
                result = self.downloader.download(SAMPLE_URL, output_dir=tmp_dir)

            extract_info.assert_called_once_with(SAMPLE_URL, tmp_dir, audio=True, download=False)
            extract_audio.assert_called_once()
            self.assertEqual(result.file_path, str(audio_path))
            self.assertEqual(result.video_path, str(video_path))

    def test_download_video_returns_downloaded_mp4(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "6a523217000000002100b841.mp4"

            def create_video(*_args, **_kwargs):
                video_path.write_bytes(b"video")
                return _sample_info()

            with patch.object(self.downloader, "_extract_info", side_effect=create_video) as extract_info:
                result = self.downloader.download_video(SAMPLE_URL, output_dir=tmp_dir)

            extract_info.assert_called_once_with(SAMPLE_URL, tmp_dir, audio=False, download=True)
            self.assertEqual(result, str(video_path))


if __name__ == "__main__":
    unittest.main()
