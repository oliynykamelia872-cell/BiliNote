import os
from pathlib import Path
from typing import Optional, Union
from urllib.parse import parse_qs, urlparse

import requests

from app.downloaders.base import Downloader
from app.enmus.note_enums import DownloadQuality
from app.models.audio_model import AudioDownloadResult
from app.utils.path_helper import get_data_dir


class ApplePodcastsDownloader(Downloader):
    LOOKUP_URL = "https://itunes.apple.com/lookup"
    REQUEST_TIMEOUT = 30

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
    ) -> AudioDownloadResult:
        episode = self._get_episode(video_url)
        episode_id = str(episode["trackId"])
        audio_url = episode.get("episodeUrl") or episode.get("previewUrl")
        if not audio_url:
            raise ValueError("该 Apple Podcasts 单集未提供可下载的公开音频")

        output_dir = output_dir or get_data_dir() or self.cache_data
        if not output_dir:
            raise ValueError("未配置音频缓存目录")
        os.makedirs(output_dir, exist_ok=True)

        suffix = Path(urlparse(audio_url).path).suffix or ".m4a"
        audio_path = os.path.join(output_dir, f"{episode_id}{suffix}")
        if not skip_download and not os.path.exists(audio_path):
            self._download_audio(audio_url, audio_path)

        return AudioDownloadResult(
            file_path=audio_path,
            title=episode.get("trackName") or "Apple Podcasts",
            duration=float(episode.get("trackTimeMillis") or 0) / 1000,
            cover_url=episode.get("artworkUrl600") or episode.get("artworkUrl100"),
            platform="apple_podcasts",
            video_id=episode_id,
            raw_info=episode,
            video_path=None,
        )

    def _get_episode(self, video_url: str) -> dict:
        parsed = urlparse(video_url)
        query = parse_qs(parsed.query)
        episode_id = (query.get("i") or [None])[0]
        collection_id = self._extract_collection_id(parsed.path)
        if not episode_id or not collection_id:
            raise ValueError("Apple Podcasts 链接必须包含节目 ID 和单集 i 参数")

        country = (parsed.path.strip("/").split("/") or ["us"])[0]
        response = requests.get(
            self.LOOKUP_URL,
            params={"id": collection_id, "entity": "podcastEpisode", "country": country},
            timeout=self.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        for item in results:
            if str(item.get("trackId")) == str(episode_id):
                return item
        raise ValueError("未能从 Apple Podcasts 找到该单集，可能已下架或不可公开播放")

    @staticmethod
    def _extract_collection_id(path: str) -> Optional[str]:
        for component in reversed(path.split("/")):
            if component.startswith("id") and component[2:].isdigit():
                return component[2:]
        return None

    def _download_audio(self, audio_url: str, audio_path: str) -> None:
        with requests.get(audio_url, stream=True, timeout=self.REQUEST_TIMEOUT) as response:
            response.raise_for_status()
            with open(audio_path, "wb") as audio_file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        audio_file.write(chunk)
