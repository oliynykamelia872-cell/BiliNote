import os
import subprocess
from abc import ABC
from typing import Optional

from app.downloaders.base import Downloader
from app.enmus.note_enums import DownloadQuality
from app.models.audio_model import AudioDownloadResult
import os
import subprocess

from app.utils.video_helper import save_cover_to_static

# 纯音频文件扩展名（不含视频画面流）。这类文件不需要转码成 mp3，
# 转写器（faster-whisper / mlx-whisper / 在线接口）可直接解码；也没有视频流可截封面。
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".wav", ".flac", ".ogg", ".opus",
    ".wma", ".aiff", ".alac", ".amr", ".mka",
}


def is_audio_file(path: str) -> bool:
    """根据扩展名判断是否为纯音频文件（不含视频流）。"""
    ext = os.path.splitext(str(path))[1].lower()
    return ext in AUDIO_EXTENSIONS


def get_media_duration(input_path: str) -> float:
    """用 ffprobe 读取媒体时长（秒），失败时返回 0，不影响主流程。"""
    try:
        command = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_path,
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return float(result.stdout.decode().strip())
    except Exception:
        return 0.0


class LocalDownloader(Downloader, ABC):
    def __init__(self):

        super().__init__()


    def extract_cover(self, input_path: str, output_dir: Optional[str] = None) -> str:
        """
        从本地视频文件中提取一张封面图（默认取第一帧）
        :param input_path: 输入视频路径
        :param output_dir: 输出目录，默认和视频同目录
        :return: 提取出的封面图片路径
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"输入文件不存在: {input_path}")

        if output_dir is None:
            output_dir = os.path.dirname(input_path)

        base_name = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join(output_dir, f"{base_name}_cover.jpg")

        try:
            command = [
                'ffmpeg',
                '-i', input_path,
                '-ss', '00:00:01',  # 跳到视频第1秒，防止黑屏
                '-vframes', '1',  # 只截取一帧
                '-q:v', '2',  # 输出质量高一点（qscale，2是很高）
                '-y',  # 覆盖
                output_path
            ]
            subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

            if not os.path.exists(output_path):
                raise RuntimeError(f"封面图片生成失败: {output_path}")

            return output_path
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"提取封面失败: {output_path}") from e

    def convert_to_mp3(self,input_path: str, output_path: str = None) -> str:
        """
        将本地视频文件转为 MP3 音频文件
        :param input_path: 输入文件路径（如 .mp4）
        :param output_path: 输出文件路径（可选，默认同目录同名 .mp3）
        :return: 生成的 mp3 文件路径
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"输入文件不存在: {input_path}")

        if output_path is None:
            base, _ = os.path.splitext(input_path)
            output_path = base + ".mp3"
        try:
        # 调用 ffmpeg 转换
            command = [
                'ffmpeg',
                '-i', input_path,
                '-vn',  # 不要视频流
                '-acodec', 'libmp3lame',  # 使用mp3编码
                '-y',  # 覆盖输出文件
                output_path
            ]

            subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

            if not os.path.exists(output_path):
                raise RuntimeError(f"mp3 文件生成失败: {output_path}")

            return output_path
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"mp3 文件生成失败: {output_path}") from e
    def download_video(self, video_url: str, output_dir: str = None) -> str:
        """
        处理本地文件路径，返回视频文件路径
        """
        if video_url.startswith('/uploads'):
            project_root = os.getcwd()
            video_url = os.path.join(project_root, video_url.lstrip('/'))
            video_url = os.path.normpath(video_url)

        if not os.path.exists(video_url):
            raise FileNotFoundError()
        if is_audio_file(video_url):
            raise ValueError("音频文件不含视频画面，无法进行截图或视频理解")
        return video_url

    def download(
            self,
            video_url: str,
            output_dir: str = None,
            quality: DownloadQuality = "fast",
            need_video: Optional[bool] = False
    ) -> AudioDownloadResult:
        """
        处理本地文件路径，返回音频元信息
        """
        if video_url.startswith('/uploads'):
            project_root = os.getcwd()
            video_url = os.path.join(project_root, video_url.lstrip('/'))
            video_url = os.path.normpath(video_url)

        if not os.path.exists(video_url):
            raise FileNotFoundError(f"本地文件不存在: {video_url}")

        file_name = os.path.basename(video_url)
        title, _ = os.path.splitext(file_name)

        # 纯音频文件（mp3 / m4a / wav 等）：直接使用原文件，不转码、不截封面
        if is_audio_file(video_url):
            print(f"检测到本地音频文件: {file_name}")
            return AudioDownloadResult(
                file_path=video_url,
                title=title,
                duration=get_media_duration(video_url),
                cover_url=None,
                platform="local",
                video_id=title,
                raw_info={
                    'path': video_url,
                    'is_audio': True,
                    'extension': os.path.splitext(video_url)[1].lower(),
                },
                video_path=None
            )

        file_path = self.convert_to_mp3(video_url)
        cover_path = self.extract_cover(video_url)
        cover_url = save_cover_to_static(cover_path)

        print('file——path', file_path)
        return AudioDownloadResult(
            file_path=file_path,
            title=title,
            duration=get_media_duration(video_url),
            cover_url=cover_url,  # 暂无封面
            platform="local",
            video_id=title,
            raw_info={
                'path':  file_path
            },
            video_path=None
        )
