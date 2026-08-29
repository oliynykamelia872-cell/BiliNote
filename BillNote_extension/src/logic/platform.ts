import type { Platform } from './types'

// 与 backend/app/validators/video_url_validator.py 保持一致
export function detectPlatform(url: string | undefined | null): Platform | null {
  if (!url)
    return null
  if (/bilibili\.com\/video\//.test(url))
    return 'bilibili'
  if (/(youtube\.com\/watch|youtu\.be\/)/.test(url))
    return 'youtube'
  if (/(xiaohongshu\.com|xhslink\.com)/.test(url))
    return 'xiaohongshu'
  if (/(weixin\.qq\.com\/sph\/|channels\.weixin\.qq\.com\/finder-preview\/)/.test(url))
    return 'wechat_channels'
  if (/podcasts\.apple\.com\/.+\/id\d+.*[?&]i=\d+/.test(url))
    return 'apple_podcasts'
  if (url.includes('douyin'))
    return 'douyin'
  if (url.includes('kuaishou'))
    return 'kuaishou'
  return null
}

export const PLATFORM_LABELS: Record<Platform, string> = {
  bilibili: '哔哩哔哩',
  youtube: 'YouTube',
  douyin: '抖音',
  kuaishou: '快手',
  xiaohongshu: '小红书',
  wechat_channels: '微信视频号',
  apple_podcasts: 'Apple Podcasts',
  local: '本地',
}
