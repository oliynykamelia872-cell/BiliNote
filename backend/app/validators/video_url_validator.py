from pydantic import AnyUrl, validator, BaseModel, field_validator, model_validator
import re
from urllib.parse import urlparse

from app.utils.url_parser import normalize_video_url

SUPPORTED_PLATFORMS = {
    "bilibili": r"(https?://)?(www\.)?bilibili\.com/video/[a-zA-Z0-9]+",
    "youtube": r"(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w\-]+",
    "douyin": "douyin",
    "kuaishou": "kuaishou",
    "xiaohongshu": "xiaohongshu",
    "wechat_channels": "wechat_channels",
    "apple_podcasts": "apple_podcasts",
}


def is_supported_video_url(url: str) -> bool:
    parsed = urlparse(url)

    # 检查是否为Bilibili的短链接
    if parsed.netloc == "b23.tv":
        return True

    for name, pattern in SUPPORTED_PLATFORMS.items():
        if name == "xiaohongshu":
            hostname = (parsed.hostname or "").lower()
            if (
                hostname == "xiaohongshu.com"
                or hostname.endswith(".xiaohongshu.com")
                or hostname in {"xhslink.cn", "xhslink.com"}
                or hostname.endswith((".xhslink.cn", ".xhslink.com"))
            ):
                return True
        elif name == "wechat_channels":
            hostname = (parsed.hostname or "").lower()
            if (
                hostname == "weixin.qq.com"
                or hostname.endswith(".weixin.qq.com")
                or hostname == "channels.weixin.qq.com"
                or hostname.endswith(".channels.weixin.qq.com")
            ) and ("/sph/" in parsed.path or "/finder-preview/" in parsed.path):
                return True
        elif name == "apple_podcasts":
            hostname = (parsed.hostname or "").lower()
            if (
                hostname == "podcasts.apple.com"
                or hostname.endswith(".podcasts.apple.com")
            ) and re.search(r"/id\d+", parsed.path) and parsed.query:
                return "i" in parsed.query
        elif pattern in ["douyin", "kuaishou"]:
            if pattern in url:
                return True
        else:
            if re.match(pattern, url):
                return True
    return False


class VideoRequest(BaseModel):
    url: AnyUrl
    platform: str

    @model_validator(mode="before")
    @classmethod
    def normalize_url(cls, data):
        if isinstance(data, dict) and data.get("platform") == "bilibili" and data.get("url"):
            data["url"] = normalize_video_url(str(data["url"]))
        return data

    @field_validator("url")
    def validate_video_url(cls, v):
        if not is_supported_video_url(str(v)):
            raise ValueError("暂不支持该视频平台或链接格式无效")
        return v
