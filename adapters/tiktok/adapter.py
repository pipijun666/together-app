"""TikTok 适配器（Playwright 核心）：经本机 HTTP 代理（SSH 隧道美国出口）
用真实浏览器打开作品页，由浏览器自身指纹/Cookie 完成 CDN 鉴权，捕获
video 响应体保存。不依赖 yt-dlp。

安全与健壮性：
- 所有请求走 PROXY_URL（缺失则快速失败）；
- 视频大小上限 MAX_VIDEO_BYTES，磁盘余量检查；
- 日志只记录脱敏 URL，不记录 Cookie / 签名参数。
"""
import json
import os
import re
import shutil
import time
from pathlib import Path

MAX_VIDEO_BYTES = 200 * 1024 * 1024
MIN_FREE_DISK = 1024 * 1024 * 1024
MOBILE_UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36')


class TiktokError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def redact_url(url):
    if not url:
        return url
    try:
        p = __import__('urllib.parse', fromlist=['urlsplit']).urlsplit(url)
        return __import__('urllib.parse', fromlist=['urlunsplit']).urlunsplit((p.scheme, p.netloc, p.path, '', ''))
    except ValueError:
        return str(url)[:300]


def check_disk_space(path):
    if shutil.disk_usage(str(path)).free < MIN_FREE_DISK:
        raise TiktokError('服务器存储空间不足，任务已取消', False)


def _proxy():
    return (os.environ.get('YTDLP_PROXY') or os.environ.get('PROXY_URL') or '').strip()


def _find_url(text):
    match = re.search(r'https?://[^\s\u4e00-\u9fff"\'<>]+', text or '')
    return match.group(0) if match else None


def resolve_video_url(share_text):
    url = _find_url(share_text) or (share_text or '').strip()
    url = url.strip().rstrip('，。；,.')
    if not url:
        raise TiktokError('没有找到可解析的链接')
    if re.search(r'tiktok\.com/@[^/]+/video/\d+', url) or 'tiktok.com/t/' in url:
        return url
    if 'vt.tiktok.com' in url or 'vm.tiktok.com' in url:
        return url
    raise TiktokError('暂无法识别的 TikTok 链接')


def _extract(html, key):
    m = re.search(r'"%s":"((?:[^"\\]|\\.)*)"' % key, html)
    if not m:
        return None
    try:
        return json.loads('"%s"' % m.group(1))
    except ValueError:
        return None


def _capture(page_url, dest, deadline=None, attempts=3):
    """Playwright 打开页面，等 video 可加载，捕获 200/206 视频响应体。"""
    proxy = _proxy()
    if not proxy:
        raise TiktokError('TikTok 需要海外代理出口，但未配置 PROXY_URL', True)
    from playwright.sync_api import sync_playwright  # lazy

    def remaining(default):
        if deadline is None:
            return default
        return max(5.0, min(deadline - time.monotonic(), default))

    last_error = None
    for attempt in range(attempts):
        if deadline is not None and time.monotonic() > deadline:
            break
        if attempt:
            time.sleep(2)
        captured = {}
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
                try:
                    context = browser.new_context(user_agent=MOBILE_UA,
                                                  viewport={'width': 412, 'height': 915},
                                                  locale='zh-CN',
                                                  proxy={'server': proxy})
                    page = context.new_page()

                    def on_response(resp):
                        url = resp.url
                        if ('video/tos' in url or 'tiktokcdn' in url or '.mp4' in url) and resp.status in (200, 206):
                            try:
                                body = resp.body()
                                if len(body) < MAX_VIDEO_BYTES:
                                    captured['video'] = body
                            except Exception:
                                pass

                    page.on('response', on_response)
                    try:
                        page.goto(page_url, timeout=int(remaining(50) * 1000), wait_until='domcontentloaded')
                    except Exception as exc:
                        last_error = TiktokError('打开 TikTok 页面失败（%s）' % type(exc).__name__, True)
                    # 等 video 元素可播放或捕获到响应
                    end = time.monotonic() + remaining(45)
                    while 'video' not in captured and time.monotonic() < end:
                        page.wait_for_timeout(1500)
                    html = page.content()
                    page.close()
                    if 'video' in captured:
                        detail = {
                            'id': _extract(html, 'id') or '',
                            'title': (_extract(html, 'desc') or 'TikTok 视频')[:300],
                            'author': (_extract(html, 'nickname') or '')[:120],
                            'cover': _extract(html, 'cover'),
                            'webpage_url': redact_url(page_url),
                        }
                        return detail, captured['video'], last_error
                    if last_error is None:
                        last_error = TiktokError('TikTok 视频未加载成功（可能被风控），系统会自动重试', True)
                finally:
                    browser.close()
        except Exception as exc:
            last_error = TiktokError('TikTok 浏览器会话失败（%s）' % type(exc).__name__, True)
    if last_error is None:
        last_error = TiktokError('TikTok 没有返回作品数据，请稍后重试', True)
    raise last_error


def download_social(share_url, directory, deadline=None, attempts=3):
    check_disk_space(directory)
    page_url = resolve_video_url(share_url)
    detail, video_body, _ = _capture(page_url, None, deadline, attempts)
    video_path = directory / 'source.mp4'
    video_path.write_bytes(video_body)
    thumb = None
    cover = detail.get('cover')
    if cover:
        try:
            from playwright.sync_api import sync_playwright
            proxy = _proxy()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
                try:
                    context = browser.new_context(user_agent=MOBILE_UA, proxy={'server': proxy} if proxy else None)
                    req = context.request.get(cover, timeout=30000)
                    if req.ok and int(req.headers.get('content-length', '0') or 0) < 8 * 1024 * 1024:
                        thumb = directory / 'cover.jpg'
                        thumb.write_bytes(req.body())
                finally:
                    browser.close()
        except Exception:
            thumb = None
    info = {
        'id': detail.get('id'),
        'title': detail.get('title'),
        'description': detail.get('title'),
        'uploader': detail.get('author'),
        'webpage_url': detail.get('webpage_url'),
        'original_url': share_url,
    }
    return info, video_path, thumb
