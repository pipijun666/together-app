"""Telegram 轻量适配器离线单元测试（mock 网络与磁盘）。"""
import io
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.telegram.adapter import (
    TelegramError, MAX_BYTES, resolve_post, fetch_detail, _download, download_social,
)

EMBED_HTML = '''
<html><head>
<meta property="og:title" content="帖子标题">
</head><body>
<div class="tgme_widget_message_text">第一行正文<br>第二行正文</div>
<time datetime="2026-08-16T19:21:06+00:00"></time>
<video src="https://cdn4.telesco.pe/file/abc.mp4?token=SIG"></video>
</body></html>
'''
SINGLE_HTML = '''
<html><head>
<meta property="og:title" content="帖子标题">
<meta property="og:image" content="https://cdn4.telesco.pe/file/photo.jpg?token=IMG">
<meta property="og:description" content="描述文字">
</head><body>
<div class="tgme_channel_info_header_title">频道名</div>
</body></html>
'''


class FakeResponse:
    def __init__(self, data, chunks=True):
        self.data = data
        self.pos = 0
        self.chunks = chunks

    def read(self, n=-1):
        if not self.chunks:
            # 无限流：每次返回 1MB，模拟永不结束的下载
            return b'\x00' * (1 << 20)
        if self.pos >= len(self.data):
            return b''
        end = len(self.data) if n < 0 else min(self.pos + n, len(self.data))
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestResolvePost(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(resolve_post('https://t.me/channel/123?embed=1'), ('channel', '123'))
        self.assertEqual(resolve_post('https://t.me/s/channel/123'), ('channel', '123'))
        self.assertEqual(resolve_post('http://t.me/channel/123#x'), ('channel', '123'))

    def test_invalid(self):
        for bad in ('https://t.me/channel', 'https://example.com/1/2', 'not a url', 'https://t.me/ab/12/x'):
            with self.assertRaises(TelegramError):
                resolve_post(bad)


class TestFetchDetail(unittest.TestCase):
    def test_parses_embed_and_single(self):
        def fake_fetch(url, timeout=25):
            if 'embed' in url:
                return FakeResponse(EMBED_HTML.encode('utf-8'))
            return FakeResponse(SINGLE_HTML.encode('utf-8'))
        with patch('adapters.telegram.adapter._fetch', side_effect=fake_fetch):
            detail = fetch_detail('channel', '123', time.monotonic() + 300)
        self.assertEqual(detail['video_url'], 'https://cdn4.telesco.pe/file/abc.mp4?token=SIG')
        self.assertEqual(detail['photo_url'], 'https://cdn4.telesco.pe/file/photo.jpg?token=IMG')
        self.assertIn('第一行正文', detail['text'])
        # 现状：_strip_tags 把 <br> 压成空格，标题取整段正文（无换行分割）
        self.assertEqual(detail['title'], '第一行正文 第二行正文')
        self.assertEqual(detail['channel'], '频道名')
        from datetime import datetime
        expected = datetime.fromisoformat('2026-08-16T19:21:06+00:00').timestamp()
        self.assertAlmostEqual(detail['timestamp'], expected, delta=1)


class TestDownload(unittest.TestCase):
    def test_enforces_200mb_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'source.mp4'
            opener = MagicMock()
            opener.open.return_value = FakeResponse(b'', chunks=False)
            with patch('adapters.telegram.adapter._opener', return_value=opener):
                with self.assertRaises(TelegramError) as ctx:
                    _download('https://cdn4.telesco.pe/file/big.mp4', dest, time.monotonic() + 300)
            self.assertIn('200 MB', str(ctx.exception))
            self.assertFalse(dest.exists())

    def test_disk_full_classified_non_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'source.mp4'
            opener = MagicMock()
            opener.open.return_value = FakeResponse(b'data' * 100)
            with patch('adapters.telegram.adapter._opener', return_value=opener), \
                 patch('builtins.open', side_effect=OSError(28, 'No space left on device')):
                with self.assertRaises(TelegramError) as ctx:
                    _download('https://cdn4.telesco.pe/file/x.mp4', dest, time.monotonic() + 300)
            self.assertFalse(ctx.exception.retryable)
            self.assertIn('存储空间不足', str(ctx.exception))

    def test_deadline_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'source.mp4'
            opener = MagicMock()
            opener.open.return_value = FakeResponse(b'', chunks=False)
            with patch('adapters.telegram.adapter._opener', return_value=opener):
                with self.assertRaises(TelegramError) as ctx:
                    _download('https://cdn4.telesco.pe/file/x.mp4', dest, time.monotonic() - 5)
            self.assertTrue(ctx.exception.retryable)
            self.assertIn('时间限制', str(ctx.exception))

    def test_success_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'source.mp4'
            opener = MagicMock()
            opener.open.return_value = FakeResponse(b'video-bytes')
            with patch('adapters.telegram.adapter._opener', return_value=opener):
                result = _download('https://cdn4.telesco.pe/file/x.mp4', dest, time.monotonic() + 300)
            self.assertEqual(result, dest)
            self.assertEqual(dest.read_bytes(), b'video-bytes')

    def test_network_error_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'source.mp4'
            opener = MagicMock()
            opener.open.side_effect = urllib.error.URLError('timeout')
            with patch('adapters.telegram.adapter._opener', return_value=opener):
                with self.assertRaises(TelegramError) as ctx:
                    _download('https://cdn4.telesco.pe/file/x.mp4', dest, time.monotonic() + 300)
            self.assertTrue(ctx.exception.retryable)


class TestDownloadSocial(unittest.TestCase):
    def test_no_content_raises(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch('adapters.telegram.adapter.resolve_post', return_value=('ch', '1')), \
             patch('adapters.telegram.adapter.fetch_detail', return_value={'title': 'x'}):
            with self.assertRaises(TelegramError):
                download_social('https://t.me/ch/1', Path(tmp), time.monotonic() + 300)

    def test_video_and_photo_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch('adapters.telegram.adapter.resolve_post', return_value=('ch', '1')), \
                 patch('adapters.telegram.adapter.fetch_detail', return_value={
                     'video_url': 'https://cdn/v.mp4', 'photo_url': 'https://cdn/p.jpg',
                     'text': '正文', 'title': '标题', 'channel': 'ch', 'timestamp': None,
                     'description': '描述',
                 }), \
                 patch('adapters.telegram.adapter._download', side_effect=lambda u, d, dl: (d.write_bytes(b'x') or d)):
                info, video, thumb = download_social('https://t.me/ch/1', directory, time.monotonic() + 300)
            self.assertEqual(info['id'], '1')
            self.assertIsNotNone(video)
            self.assertIsNotNone(thumb)


if __name__ == '__main__':
    unittest.main()
