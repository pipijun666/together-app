# -*- coding: utf-8 -*-
"""Douyin adapter (mobile share-page based).

TikTok-style fix: the desktop SPA detail API returns an empty body for
datacenter IPs, but the **mobile** share page (``bid=aweme_share``) served to a
mobile User-Agent embeds a working ``<video>`` whose src is the official
watermark-free ``m.douyin.com/aweme/v1/playwm/`` endpoint.  That endpoint
redirects to the CDN and accepts a plain ``urllib`` request (mobile UA +
Referer), so only *resolution* needs the browser:

1. resolve aweme_id (text / short link / full URL);
2. open ``https://www.douyin.com/video/{id}`` in Playwright with a mobile UA
   (direct connection; douyin is reachable domestically, no proxy needed);
3. read ``<video>.src`` (playwm URL) and capture the ``aweme/post`` JSON for
   title / author / cover / aweme_type;
4. stream-download the playwm URL with urllib (200 MB cap, disk check).

Logging only carries redacted URLs; no cookies / signed params are logged.
"""
import errno
import json
import os
import re
import shutil
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MAX_VIDEO_BYTES = 200 * 1024 * 1024
MIN_FREE_DISK = 1024 * 1024 * 1024
MOBILE_UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36')


def redact_url(url):
    """Strip query/fragment before logging (stream URLs carry signed params)."""
    if not url:
        return url
    try:
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, p.path, '', ''))
    except ValueError:
        return str(url)[:300]


class DouyinError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def check_disk_space(path):
    usage = shutil.disk_usage(str(path))
    if usage.free < MIN_FREE_DISK:
        raise DouyinError('服务器存储空间不足，任务已取消', False)


# ---------------------------------------------------------------- resolution

def _find_url(text):
    match = re.search(r'https?://[^\s\u4e00-\u9fff"\'<>]+', text or '')
    return match.group(0) if match else None


def resolve_video_id(share_text):
    """Extract the aweme id from share text / short link / full URL."""
    url = _find_url(share_text) or share_text or ''
    url = url.strip().rstrip('，。；,.')
    if not url:
        raise DouyinError('没有找到可解析的链接')
    match = re.search(r'/(?:video|note|share/video|share/note)/(\d{15,21})', url)
    if match:
        return match.group(1)
    if 'v.douyin.com' not in url:
        raise DouyinError('暂无法识别的抖音链接')
    req = urllib.request.Request(url, headers={
        'User-Agent': MOBILE_UA,
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            final = resp.geturl()
            body = resp.read(2048).decode('utf-8', 'replace')
    except (OSError, urllib.error.URLError) as exc:
        raise DouyinError('抖音短链跳转失败，请稍后重试', True) from exc
    match = re.search(r'/(?:video|note|share/video|share/note)/(\d{15,21})', final) \
        or re.search(r'item_ids[=/](\d{15,21})', final) \
        or re.search(r'video/(\d{15,21})', body)
    if not match:
        raise DouyinError('无法从链接中解析出作品编号')
    return match.group(1)


# ----------------------------------------------------------- mobile page info

def _extract(html, key):
    m = re.search(r'"%s":"((?:[^"\\]|\\.)*)"' % key, html)
    if not m:
        return None
    try:
        return json.loads('"%s"' % m.group(1))
    except ValueError:
        return None


def fetch_video_src(video_id, deadline=None, attempts=3):
    """Open the mobile share page, return (playwm_url, meta dict).

    meta keys: title / author / cover / aweme_type / id.
    """
    from playwright.sync_api import sync_playwright  # lazy

    DEBUG = os.environ.get('DY_ADAPTER_DEBUG') == '1'

    def remaining(default):
        if deadline is None:
            return default
        left = deadline - time.monotonic()
        return max(5.0, min(left, default))

    def one_attempt(attempt):
        captured = {}
        done = threading.Event()

        def on_response(resp):
            if DEBUG:
                print(f'[dy-adapter][{attempt}] resp: {resp.status} {redact_url(resp.url)}', flush=True)
            if 'aweme/post' in resp.url and resp.status == 200:
                try:
                    data = resp.json()
                except Exception:
                    return
                lst = (data or {}).get('aweme_list') or []
                for item in lst:
                    if str(item.get('aweme_id')) == video_id:
                        captured['meta'] = item
                        done.set()
                        return
                if lst:
                    captured['meta_list'] = lst

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                context = browser.new_context(
                    user_agent=MOBILE_UA,
                    viewport={'width': 412, 'height': 915},
                    locale='zh-CN',
                )
                page = context.new_page()
                page.on('response', on_response)
                try:
                    page.goto(f'https://www.douyin.com/video/{video_id}',
                              timeout=int(remaining(45) * 1000),
                              wait_until='domcontentloaded')
                except Exception as exc:
                    if DEBUG:
                        print(f'[dy-adapter][{attempt}] goto err: {type(exc).__name__} {str(exc)[:90]}', flush=True)
                end = time.monotonic() + remaining(40)
                src = ''
                while time.monotonic() < end:
                    try:
                        src = page.evaluate("""() => {
                            const v = document.querySelector('video');
                            return v ? (v.currentSrc || v.src || '') : '';
                        }""")
                    except Exception:
                        src = ''
                    if src:
                        break
                    page.wait_for_timeout(1500)
                html = page.content()
                if not src:
                    # the video element may be added by JS; read it from SSR HTML
                    m = re.search(r'(https?:\\?/\\?/m\.douyin\.com/aweme/v1/playwm/[^"\']+)', html)
                    if m:
                        src = m.group(1).replace('\\u002F', '/').replace('\\/', '/')
                page.close()
                meta = captured.get('meta')
                if meta is None:
                    meta = {
                        'aweme_id': video_id,
                        'desc': _extract(html, 'desc') or '抖音视频',
                        'nickname': _extract(html, 'nickname') or '',
                    }
                return src, meta, html
            finally:
                browser.close()

    last_error = None
    for attempt in range(attempts):
        if deadline is not None and time.monotonic() > deadline:
            break
        if attempt:
            time.sleep(3)
        try:
            src, meta, html = one_attempt(attempt)
        except Exception as exc:
            last_error = DouyinError('抖音页面打开失败（%s）' % type(exc).__name__, True)
            continue
        if src:
            return src, meta
        last_error = DouyinError('抖音没有返回作品数据，请稍后重试', True)
    if last_error is None:
        last_error = DouyinError('抖音没有返回作品数据，请稍后重试', True)
    raise last_error


# ----------------------------------------------------------------- download

def _download(url, dest, deadline=None, referer=None):
    """Stream a file to dest; enforce the 200 MB cap and the deadline."""
    headers = {'User-Agent': MOBILE_UA}
    if referer:
        headers['Referer'] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            with open(dest, 'wb') as out:
                while True:
                    if deadline and time.monotonic() > deadline:
                        out.close()
                        dest.unlink(missing_ok=True)
                        raise DouyinError('导入超过时间限制，请手动补充', True)
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    if out.tell() + len(chunk) > MAX_VIDEO_BYTES:
                        out.close()
                        dest.unlink(missing_ok=True)
                        raise DouyinError('视频超过 200 MB，已保留标题和封面')
                    out.write(chunk)
    except DouyinError:
        raise
    except (urllib.error.URLError, TimeoutError) as exc:
        dest.unlink(missing_ok=True)
        raise DouyinError('抖音媒体下载失败，请稍后重试', True) from exc
    except OSError as exc:
        dest.unlink(missing_ok=True)
        if exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EROFS):
            raise DouyinError('服务器存储空间不足或写入失败，任务已取消', False) from exc
        raise DouyinError('抖音媒体下载失败，请稍后重试', True) from exc
    return dest.stat().st_size


# ----------------------------------------------------------- public entry

def download_social(share_url, directory, deadline=None):
    """Mimic worker.download_social: return (info, video_path, thumb_path)."""
    directory = Path(directory)
    check_disk_space(directory)
    video_id = resolve_video_id(share_url)
    playwm, meta = fetch_video_src(video_id, deadline)

    aweme_type = meta.get('aweme_type')
    if aweme_type == 68:  # image post
        raise DouyinError('该作品是图文帖，当前版本暂仅支持视频分享')

    title = str(meta.get('desc') or '').strip() or '抖音视频'
    author = str(meta.get('nickname') or '').strip()
    create_time = meta.get('create_time')

    info = {
        'id': str(meta.get('aweme_id') or video_id),
        'title': title[:300],
        'description': title[:12000],
        'uploader': author[:300],
        'timestamp': create_time,
        'webpage_url': f'https://www.douyin.com/video/{video_id}',
        'original_url': share_url,
    }

    if not playwm:
        raise DouyinError('抖音没有返回可用的视频地址', True)
    video_path = directory / 'source.mp4'
    _download(playwm, video_path, deadline,
              referer=f'https://www.douyin.com/video/{video_id}')

    thumb_path = None
    covers = ((meta.get('video') or {}).get('cover') or {}).get('url_list') or []
    if covers:
        try:
            thumb_path = directory / 'source.jpg'
            _download(covers[0], thumb_path, deadline)
        except Exception:
            thumb_path = None
            if video_path.stat().st_size == 0:
                raise
    return info, video_path, thumb_path
