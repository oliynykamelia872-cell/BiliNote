import hashlib
import logging
import os
import subprocess
import threading
from abc import ABC
from typing import Optional, Union

import requests

from app.downloaders.base import Downloader, QUALITY_MAP
from app.enmus.note_enums import DownloadQuality
from app.models.audio_model import AudioDownloadResult
from app.utils.path_helper import get_data_dir
from app.utils.url_parser import extract_video_id


logger = logging.getLogger(__name__)
_DOWNLOAD_LOCKS: dict[str, threading.Lock] = {}
_DOWNLOAD_LOCKS_GUARD = threading.Lock()


class WechatChannelsUnavailableError(RuntimeError):
    pass


def _get_download_lock(key: str) -> threading.Lock:
    with _DOWNLOAD_LOCKS_GUARD:
        lock = _DOWNLOAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DOWNLOAD_LOCKS[key] = lock
        return lock


class WechatChannelsDownloader(Downloader, ABC):
    API_URL = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    @staticmethod
    def _lock_key(video_url: str) -> str:
        video_id = extract_video_id(video_url, "wechat_channels")
        if video_id:
            return video_id
        return hashlib.sha256(video_url.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_url(url: str) -> str:
        video_id = extract_video_id(url, "wechat_channels")
        if video_id:
            return f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={video_id}"
        return url

    @staticmethod
    def _pick_video_url(feed: dict) -> Optional[str]:
        candidates = [
            feed.get("h265VideoInfo", {}).get("videoUrl"),
            feed.get("h264VideoInfo", {}).get("videoUrl"),
            feed.get("videoUrl"),
        ]
        for item in candidates:
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                return item
        return None

    def _fetch_info(self, video_url: str) -> dict:
        video_id = extract_video_id(video_url, "wechat_channels")
        if not video_id:
            raise ValueError("无法从微信短视频链接提取 shortUri")

        referer = self._normalize_url(video_url)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://channels.weixin.qq.com",
            "Referer": referer,
            "User-Agent": self.USER_AGENT,
        }
        payload = {"baseReq": {"generalToken": ""}, "shortUri": video_id}
        response = requests.post(self.API_URL, json=payload, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        if data.get("errCode") not in (0, None):
            raise WechatChannelsUnavailableError(data.get("errMsg") or "微信视频号接口返回错误")

        body = data.get("data") or {}
        feed = body.get("feedInfo") or {}
        err_msg = body.get("errMsg") or {}
        if err_msg.get("type") not in (None, 0):
            raise WechatChannelsUnavailableError(err_msg.get("title") or err_msg.get("content") or "此内容暂时无法播放")

        video_url = self._pick_video_url(feed)
        if not video_url:
            raise WechatChannelsUnavailableError(
                "微信视频号未向网页预览接口开放视频流，只返回了封面/文案；请换一个可公开视频链接，或在微信内转发可播放的视频号链接。"
            )

        author = body.get("authorInfo") or {}
        return {
            "id": video_id,
            "title": (feed.get("description") or video_id).strip().splitlines()[0],
            "description": feed.get("description") or "",
            "duration": float(feed.get("duration") or feed.get("videoDuration") or 0),
            "thumbnail": feed.get("coverUrl"),
            "webpage_url": referer,
            "video_url": video_url,
            "uploader": author.get("nickname") or "",
            "raw": data,
        }

    @staticmethod
    def _download_file(url: str, target_path: str) -> None:
        headers = {
            "User-Agent": WechatChannelsDownloader.USER_AGENT,
            "Referer": "https://channels.weixin.qq.com/",
        }
        with requests.get(url, headers=headers, stream=True, timeout=60) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "image/" in content_type:
                raise WechatChannelsUnavailableError("微信接口返回的是封面图片，不是视频流")
            with open(target_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

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

    @staticmethod
    def _raw_info(info: dict) -> dict:
        return {
            "description": info.get("description") or "",
            "uploader": info.get("uploader") or "",
            "webpage_url": info.get("webpage_url") or "",
            "extractor": "WechatChannels",
        }

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
            info = self._fetch_info(video_url)
            video_id = info["id"]
            video_path = os.path.join(output_dir, f"{video_id}.mp4")
            audio_path = os.path.join(output_dir, f"{video_id}.mp3")

            if not skip_download:
                if not os.path.exists(video_path):
                    self._download_file(info["video_url"], video_path)
                if not os.path.exists(audio_path):
                    self._extract_audio(video_path, audio_path, quality)

            return AudioDownloadResult(
                file_path=audio_path,
                title=info.get("title") or video_id,
                duration=float(info.get("duration") or 0),
                cover_url=info.get("thumbnail"),
                platform="wechat_channels",
                video_id=video_id,
                raw_info=self._raw_info(info),
                video_path=video_path if os.path.exists(video_path) else None,
            )

    def download_video(self, video_url: str, output_dir: Union[str, None] = None) -> str:
        output_dir = output_dir or get_data_dir() or self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        with _get_download_lock(self._lock_key(video_url)):
            info = self._fetch_info(video_url)
            video_path = os.path.join(output_dir, f"{info['id']}.mp4")
            if not os.path.exists(video_path):
                self._download_file(info["video_url"], video_path)
            if not os.path.exists(video_path):
                raise FileNotFoundError(f"视频文件未找到: {video_path}")
            return video_path
