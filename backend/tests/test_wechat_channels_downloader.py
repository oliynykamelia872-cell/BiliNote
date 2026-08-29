import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app.downloaders.wechat_channels_downloader import (  # noqa: E402
    WechatChannelsDownloader,
    WechatChannelsUnavailableError,
)


SAMPLE_URL = "https://weixin.qq.com/sph/ArWtRAQJv7"


def _sample_info(video_url="https://finder.video.qq.com/video.mp4"):
    return {
        "id": "ArWtRAQJv7",
        "title": "微信视频号样例",
        "description": "微信视频号样例",
        "duration": 12.0,
        "thumbnail": "https://finder.video.qq.com/cover.jpg",
        "webpage_url": SAMPLE_URL,
        "video_url": video_url,
        "uploader": "作者",
    }


class TestWechatChannelsDownloader(unittest.TestCase):
    def setUp(self):
        self.downloader = WechatChannelsDownloader()

    def test_metadata_only_does_not_download_media(self):
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            self.downloader,
            "_fetch_info",
            return_value=_sample_info(),
        ) as fetch_info:
            result = self.downloader.download(SAMPLE_URL, output_dir=tmp_dir, skip_download=True)

        fetch_info.assert_called_once_with(SAMPLE_URL)
        self.assertEqual(result.platform, "wechat_channels")
        self.assertEqual(result.video_id, "ArWtRAQJv7")
        self.assertEqual(result.raw_info["extractor"], "WechatChannels")

    def test_download_video_returns_downloaded_mp4(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "ArWtRAQJv7.mp4"

            def create_video(_url, target_path):
                Path(target_path).write_bytes(b"video")

            with patch.object(self.downloader, "_fetch_info", return_value=_sample_info()), patch.object(
                self.downloader,
                "_download_file",
                side_effect=create_video,
            ) as download_file:
                result = self.downloader.download_video(SAMPLE_URL, output_dir=tmp_dir)

            download_file.assert_called_once()
            self.assertEqual(result, str(video_path))

    def test_missing_video_url_reports_clear_error(self):
        feed = {"description": "只有文案", "coverUrl": "https://finder.video.qq.com/cover.jpg"}
        with self.assertRaises(WechatChannelsUnavailableError):
            if not self.downloader._pick_video_url(feed):
                raise WechatChannelsUnavailableError(
                    "微信视频号未向网页预览接口开放视频流，只返回了封面/文案"
                )


if __name__ == "__main__":
    unittest.main()
