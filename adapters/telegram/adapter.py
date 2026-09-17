"""Telegram 轻量适配器：解析公开频道帖子 t.me/{channel}/{msg}。

yt-dlp 的 telegram:embed 提取器只支持有 embed 数据的视频媒体帖，
图片帖、文字帖、部分转发帖都会返回 nothing。本适配器直接抓取
t.me 的 embed 帧与 single 页，提取：

- 视频帖：<video src="https://cdnN.telesco.pe/file/xxx.mp4?token=..."> 直链
- 图片帖：og:image 封面/正文图
- 文字帖：tgme_widget_message_text 正文
- 时间：datetime 属性；标题：og:title；作者：频道显示名

网络：t.me 页面抓取必须可出网（本机可用 YTDLP_PROXY 环境变量走代理，
与 worker 的 yt-dlp 代理配置一致）；媒体（telesco.pe CDN）直连优先，
失败时回退代理。

返回与 yt-dlp 兼容的 (info, video_path, thumb_path)，供 worker 消费。
"""
import os
import errno
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
MAX_BYTES = 200 * 1024 * 1024
MIN_FREE_DISK = 1024 * 1024 * 1024
_PAT = re.compile(r'https?://t\.me/(?:s/)?([A-Za-z0-9_]{5,32})/(\d+)(?:[/?#].*)?$')


class TelegramError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def check_disk_space(path):
    usage = shutil.disk_usage(str(path))
    if usage.free < MIN_FREE_DISK:
        raise TelegramError('服务器存储空间不足，任务已取消', False)


def _proxy_map():
    # Telegram 默认走海外代理：优先 YTDLP_PROXY，回退 DB-003 统一的 PROXY_URL
    p = os.environ.get('YTDLP_PROXY') or os.environ.get('PROXY_URL')
    return {'http': p, 'https': p} if p else {}


def _opener(proxy_map):
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxy_map))


def _fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        return _opener(_proxy_map()).open(req, timeout=timeout)
    except Exception as exc:
        raise TelegramError('Telegram 页面暂时无法读取', True) from exc


def _strip_tags(html):
    text = re.sub(r'<br\s*/?>', '\n', html)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _download(url, dest, deadline):
    remaining = max(1, int(deadline - time.monotonic()))
    attempts = [({}, 'direct'), (_proxy_map(), 'proxy')]
    last = None
    for proxy_map, label in attempts:
        if not proxy_map:
            pass
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://t.me/'})
        try:
            with _opener(proxy_map).open(req, timeout=min(25, remaining)) as r:
                total = 0
                with open(dest, 'wb') as f:
                    while True:
                        if time.monotonic() > deadline:
                            f.close()
                            dest.unlink(missing_ok=True)
                            raise TelegramError('导入超过时间限制，请手动补充', True)
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        total += len(chunk)
                        if total > MAX_BYTES:
                            f.close()
                            dest.unlink(missing_ok=True)
                            raise TelegramError('媒体超过 200 MB')
                return dest
        except TelegramError:
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            dest.unlink(missing_ok=True)
            last = exc
            if label == 'direct' and _proxy_map():
                continue
            raise TelegramError('Telegram 媒体下载失败', True) from last
        except OSError as exc:
            dest.unlink(missing_ok=True)
            if exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EROFS):
                raise TelegramError('服务器存储空间不足或写入失败，任务已取消', False) from exc
            last = exc
            if label == 'direct' and _proxy_map():
                continue
            raise TelegramError('Telegram 媒体下载失败', True) from last
        except Exception as exc:
            last = exc
            if label == 'direct' and _proxy_map():
                continue  # direct failed; retry via proxy
            raise TelegramError('Telegram 媒体下载失败', True) from last
    raise TelegramError('Telegram 媒体下载失败', True) from last


def resolve_post(url):
    m = _PAT.match(url)
    if not m:
        raise TelegramError('不是有效的 Telegram 帖子链接（应为 t.me/频道/消息号）')
    return m.group(1), m.group(2)


def fetch_detail(channel, msg, deadline):
    embed_url = 'https://t.me/%s/%s?embed=1' % (channel, msg)
    single_url = 'https://t.me/%s/%s?single' % (channel, msg)
    embed = _fetch(embed_url, timeout=min(25, max(1, int(deadline - time.monotonic())))).read().decode('utf-8', 'replace')
    single = _fetch(single_url, timeout=min(25, max(1, int(deadline - time.monotonic())))).read().decode('utf-8', 'replace')
    detail = {}

    m = re.search(r'<video[^>]+src="([^"]+)"', embed)
    detail['video_url'] = m.group(1) if m else None

    m = re.search(r'<meta property="og:image" content="([^"]+)"', single)
    detail['photo_url'] = m.group(1) if m else None

    m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</(?:div|p)>', embed, re.S)
    detail['text'] = _strip_tags(m.group(1)) if m else ''

    m = re.search(r'datetime="([^"]+)"', embed)
    detail['timestamp'] = None
    if m:
        try:
            detail['timestamp'] = datetime.fromisoformat(m.group(1).replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass

    m = re.search(r'<meta property="og:title" content="([^"]+)"', single)
    og_title = m.group(1).strip() if m else ''
    first_line = detail['text'].splitlines()[0] if detail['text'] else ''
    detail['title'] = first_line or og_title or 'Telegram 分享'

    m = re.search(r'class="tgme_channel_info_header_title"[^>]*>(.*?)<', single, re.S)
    detail['channel'] = _strip_tags(m.group(1)) if m else channel

    m = re.search(r'<meta property="og:description" content="([^"]*)"', single)
    detail['description'] = m.group(1).strip() if m else ''
    return detail


def download_social(url, directory, deadline):
    channel, msg = resolve_post(url)
    check_disk_space(directory)
    detail = fetch_detail(channel, msg, deadline)
    video = None
    thumb = None
    if detail.get('video_url'):
        video = _download(detail['video_url'], directory / 'source.mp4', deadline)
    if detail.get('photo_url'):
        thumb = _download(detail['photo_url'], directory / 'source.jpg', deadline)
    if not video and not thumb and not detail.get('text'):
        raise TelegramError('该帖子没有可展示的图片、视频或文字')
    info = {
        'id': msg,
        'title': detail.get('title') or 'Telegram 分享',
        'description': detail.get('description') or detail.get('text', ''),
        'uploader': detail.get('channel') or channel,
        'timestamp': detail.get('timestamp'),
        'webpage_url': 'https://t.me/%s/%s' % (channel, msg),
        'thumbnail': detail.get('photo_url') or '',
    }
    return info, video, thumb
