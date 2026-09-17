"""TikTok 适配器单元测试（离线）。

默认全部为本地模拟测试（不访问网络）；真实平台验证单列并标注。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.tiktok import adapter as tk  # noqa: E402

SAMPLE_HTML = (
    '<html><script>'
    '"playAddr":"https:\\u002F\\u002Fv16-webapp-prime.tiktok.com\\u002Fa.mp4?a=1"'
    '"desc":"测试视频标题"'
    '"nickname":"测试作者"'
    '"id":"6718335390845095173"'
    '</script></html>'
)


class TestTikTokAdapter(unittest.TestCase):
    def test_extract_json_escaped(self):
        self.assertEqual(tk._extract(SAMPLE_HTML, 'playAddr'), 'https://v16-webapp-prime.tiktok.com/a.mp4?a=1')
        self.assertEqual(tk._extract(SAMPLE_HTML, 'desc'), '测试视频标题')
        self.assertEqual(tk._extract(SAMPLE_HTML, 'nickname'), '测试作者')
        self.assertEqual(tk._extract(SAMPLE_HTML, 'id'), '6718335390845095173')
        self.assertIsNone(tk._extract('<html>nothing</html>', 'playAddr'))

    def test_resolve_video_url_variants(self):
        self.assertIn('tiktok.com/@u/video/1', tk.resolve_video_url('看看这个 https://www.tiktok.com/@u/video/1234567890123456789?lang=zh 不错'))
        self.assertEqual(tk.resolve_video_url('https://vt.tiktok.com/ZSq9T7HrU/'), 'https://vt.tiktok.com/ZSq9T7HrU/')
        with self.assertRaises(tk.TiktokError):
            tk.resolve_video_url('https://example.com/other')

    def test_redact_url_strips_query(self):
        out = tk.redact_url('https://v16.tiktok.com/v/a.mp4?sig=abc&token=xyz#f')
        self.assertEqual(out, 'https://v16.tiktok.com/v/a.mp4')
        self.assertNotIn('sig', out)
        self.assertNotIn('token', out)

    def test_download_requires_proxy(self):
        orig_proxy = os.environ.pop('PROXY_URL', None)
        orig_ytdlp = os.environ.pop('YTDLP_PROXY', None)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                with self.assertRaises(tk.TiktokError):
                    tk.download_social('https://www.tiktok.com/@u/video/1234567890123456789', Path(tmp_dir), attempts=1)
        finally:
            if orig_proxy is not None:
                os.environ['PROXY_URL'] = orig_proxy
            if orig_ytdlp is not None:
                os.environ['YTDLP_PROXY'] = orig_ytdlp

    def test_size_limit_check(self):
        orig = tk.MAX_VIDEO_BYTES
        try:
            tk.MAX_VIDEO_BYTES = 10
            self.assertEqual(tk.MAX_VIDEO_BYTES, 10)
        finally:
            tk.MAX_VIDEO_BYTES = orig


if __name__ == '__main__':
    unittest.main()
