import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 套件里部分旧测试会在导入期删除 / 替换 ``sys.modules`` 里的 ``app.*`` 模块，
# 字符串形式的 patch 目标会按名字重新解析到新模块对象，与这里绑定的类分叉。
# 因此统一在导入期绑定模块对象，并只用 patch.object 打这个绑定对象。
import app.downloaders.local_downloader as local_downloader_module

LocalDownloader = local_downloader_module.LocalDownloader
is_audio_file = local_downloader_module.is_audio_file


class TestIsAudioFile(unittest.TestCase):
    def test_detects_common_audio_extensions(self):
        for name in ["a.mp3", "b.m4a", "c.m4b", "d.wav", "e.flac", "f.ogg", "g.aac", "h.opus", "i.wma"]:
            with self.subTest(name=name):
                self.assertTrue(is_audio_file(name))

    def test_rejects_video_extensions(self):
        for name in ["a.mp4", "b.mkv", "c.mov", "d.webm", "e.avi"]:
            with self.subTest(name=name):
                self.assertFalse(is_audio_file(name))

    def test_extension_case_insensitive(self):
        self.assertTrue(is_audio_file("voice.MP3"))


class TestLocalDownloaderAudio(unittest.TestCase):
    def test_audio_file_skips_conversion_and_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "episode.mp3"
            audio_path.write_bytes(b"not-a-real-mp3")

            downloader = LocalDownloader()
            with (
                patch.object(downloader, "convert_to_mp3") as convert,
                patch.object(downloader, "extract_cover") as extract,
                patch.object(local_downloader_module, "get_media_duration", return_value=0.0),
            ):
                result = downloader.download(str(audio_path))

            convert.assert_not_called()
            extract.assert_not_called()
            self.assertEqual(result.file_path, str(audio_path))
            self.assertIsNone(result.cover_url)
            self.assertEqual(result.platform, "local")
            self.assertEqual(result.title, "episode")
            self.assertTrue(result.raw_info.get("is_audio"))
            self.assertEqual(result.raw_info.get("extension"), ".mp3")

    def test_audio_file_duration_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "episode.mp3"
            audio_path.write_bytes(b"not-a-real-mp3")

            with patch.object(local_downloader_module, "get_media_duration", return_value=123.4):
                result = local_downloader_module.LocalDownloader().download(str(audio_path))

            self.assertEqual(result.duration, 123.4)

    def test_video_file_keeps_existing_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            video_path = tmp_dir / "clip.mp4"
            video_path.write_bytes(b"not-a-real-video")

            downloader = LocalDownloader()
            with (
                patch.object(downloader, "convert_to_mp3", return_value=str(tmp_dir / "clip.mp3")),
                patch.object(downloader, "extract_cover", return_value=str(tmp_dir / "clip.jpg")),
                patch.object(local_downloader_module, "save_cover_to_static", return_value="/static/cover/clip.jpg"),
                patch.object(local_downloader_module, "get_media_duration", return_value=60.0),
            ):
                result = downloader.download(str(video_path))

            self.assertEqual(result.file_path, str(tmp_dir / "clip.mp3"))
            self.assertEqual(result.cover_url, "/static/cover/clip.jpg")
            self.assertEqual(result.duration, 60.0)
            self.assertNotIn("is_audio", result.raw_info)

    def test_download_video_rejects_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "episode.mp3"
            audio_path.write_bytes(b"x")

            with self.assertRaisesRegex(ValueError, "音频"):
                LocalDownloader().download_video(str(audio_path))


if __name__ == "__main__":
    unittest.main()
