import hashlib
import logging
import os
import subprocess
import tempfile
import threading
from abc import ABC
from typing import Optional, Union

import yt_dlp

from app.downloaders.base import Downloader, QUALITY_MAP
from app.enmus.note_enums import DownloadQuality
from app.models.audio_model import AudioDownloadResult
from app.services.cookie_manager import CookieConfigManager
from app.utils.path_helper import get_data_dir
from app.utils.url_parser import extract_video_id


logger = logging.getLogger(__name__)
_DOWNLOAD_LOCKS: dict[str, threading.Lock] = {}
_DOWNLOAD_LOCKS_GUARD = threading.Lock()


def _get_download_lock(key: str) -> threading.Lock:
    with _DOWNLOAD_LOCKS_GUARD:
        lock = _DOWNLOAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DOWNLOAD_LOCKS[key] = lock
        return lock


class XiaohongshuDownloader(Downloader, ABC):
    def __init__(self):
        super().__init__()
        self._cookie = CookieConfigManager().get("xiaohongshu")
        self._cookiefile = self._write_netscape_cookie_file()

    def _write_netscape_cookie_file(self) -> Optional[str]:
        if not self._cookie:
            logger.info("小红书 Cookie 未配置，将尝试免登录解析")
            return None

        lines = ["# Netscape HTTP Cookie File\n"]
        for pair in self._cookie.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            lines.append(f".xiaohongshu.com\tTRUE\t/\tFALSE\t0\t{key}\t{value}\n")

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        tmp.writelines(lines)
        tmp.close()
        return tmp.name

    @staticmethod
    def _lock_key(video_url: str) -> str:
        video_id = extract_video_id(video_url, "xiaohongshu")
        if video_id:
            return video_id
        return hashlib.sha256(video_url.encode("utf-8")).hexdigest()

    def _ydl_options(self, output_dir: str, *, audio: bool, skip_download: bool) -> dict:
        options = {
            "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
            "http_headers": {"Referer": "https://www.xiaohongshu.com/"},
            "noplaylist": True,
            "quiet": False,
        }
        if self._cookiefile:
            options["cookiefile"] = self._cookiefile
        if skip_download:
            options["skip_download"] = True
        elif audio:
            options["format"] = "bestaudio/best"
            options["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }]
        else:
            options["format"] = "best[ext=mp4]/best"
            options["merge_output_format"] = "mp4"
        return options

    def _extract_info(self, video_url: str, output_dir: str, *, audio: bool, download: bool) -> dict:
        options = self._ydl_options(output_dir, audio=audio, skip_download=not download)
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(video_url, download=download)

    @staticmethod
    def _raw_info(info: dict) -> dict:
        return {
            "tags": info.get("tags") or [],
            "description": info.get("description") or "",
            "uploader": info.get("uploader") or info.get("uploader_id") or "",
            "uploader_id": info.get("uploader_id") or "",
            "webpage_url": info.get("webpage_url") or "",
            "extractor": info.get("extractor") or "XiaoHongShu",
        }

    @staticmethod
    def _extract_audio(video_path: str, audio_path: str, quality: DownloadQuality) -> None:
        quality_value = quality.value if hasattr(quality, "value") else str(quality)
        bitrate = QUALITY_MAP.get(quality_value, "64")
        ffmpeg = os.getenv("FFMPEG_BIN_PATH", "ffmpeg")
        subprocess.run(
            [ffmpeg, "-y", "-i", video_path, "-vn", "-codec:a", "libmp3lame", "-b:a", f"{bitrate}k", audio_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
    ) -> AudioDownloadResult:
        output_dir = output_dir or get_data_dir() or self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        with _get_download_lock(self._lock_key(video_url)):
            expected_id = extract_video_id(video_url, "xiaohongshu")
            expected_audio = os.path.join(output_dir, f"{expected_id}.mp3") if expected_id else None
            expected_video = os.path.join(output_dir, f"{expected_id}.mp4") if expected_id else None

            if skip_download or (expected_audio and os.path.exists(expected_audio)):
                info = self._extract_info(video_url, output_dir, audio=True, download=False)
            elif expected_video and os.path.exists(expected_video):
                info = self._extract_info(video_url, output_dir, audio=True, download=False)
                self._extract_audio(expected_video, expected_audio, quality)
            else:
                info = self._extract_info(video_url, output_dir, audio=True, download=True)

            video_id = info.get("id") or expected_id
            if not video_id:
                raise ValueError("无法从小红书链接提取笔记 ID")

            audio_path = os.path.join(output_dir, f"{video_id}.mp3")
            video_path = os.path.join(output_dir, f"{video_id}.mp4")
            if not skip_download and not os.path.exists(audio_path):
                raise FileNotFoundError(f"音频文件未找到: {audio_path}")

            return AudioDownloadResult(
                file_path=audio_path,
                title=info.get("title") or info.get("description") or video_id,
                duration=float(info.get("duration") or 0),
                cover_url=info.get("thumbnail"),
                platform="xiaohongshu",
                video_id=video_id,
                raw_info=self._raw_info(info),
                video_path=video_path if os.path.exists(video_path) else None,
            )

    def download_video(self, video_url: str, output_dir: Union[str, None] = None) -> str:
        output_dir = output_dir or get_data_dir() or self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        with _get_download_lock(self._lock_key(video_url)):
            expected_id = extract_video_id(video_url, "xiaohongshu")
            expected_path = os.path.join(output_dir, f"{expected_id}.mp4") if expected_id else None
            if expected_path and os.path.exists(expected_path):
                return expected_path

            info = self._extract_info(video_url, output_dir, audio=False, download=True)
            video_id = info.get("id") or expected_id
            if not video_id:
                raise ValueError("无法从小红书链接提取笔记 ID")
            video_path = os.path.join(output_dir, f"{video_id}.mp4")
            if not os.path.exists(video_path):
                raise FileNotFoundError(f"视频文件未找到: {video_path}")
            return video_path
