"""Douyin adapter unit tests (offline).

Local simulations only (no network).  Real-platform verification is run on the
server and documented separately.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.douyin import adapter as dy  # noqa: E402


class TestDouyinAdapter(unittest.TestCase):
    def test_resolve_video_id_variants(self):
        self.assertEqual(dy.resolve_video_id('看看 https://www.douyin.com/video/7683443359242931465?foo=1 不错'), '7683443359242931465')
        self.assertEqual(dy.resolve_video_id('https://www.douyin.com/share/video/7683443359242931465'), '7683443359242931465')
        with self.assertRaises(dy.DouyinError):
            dy.resolve_video_id('https://example.com/other')

    def test_redact_url_strips_query(self):
        out = dy.redact_url('https://v3-web.douyinvod.com/v/a.mp4?sig=abc&token=xyz#f')
        self.assertEqual(out, 'https://v3-web.douyinvod.com/v/a.mp4')
        self.assertNotIn('sig', out)
        self.assertNotIn('token', out)

    def test_extract_json_escaped(self):
        html = '<script>"desc":"测试标题\\u0026更多" "nickname":"作者名"</script>'
        self.assertEqual(dy._extract(html, 'desc'), '测试标题&更多')
        self.assertEqual(dy._extract(html, 'nickname'), '作者名')
        self.assertIsNone(dy._extract('<html>none</html>', 'desc'))

    def test_missing_link_raises(self):
        with self.assertRaises(dy.DouyinError):
            dy.resolve_video_id('')

    def test_constants(self):
        self.assertEqual(dy.MAX_VIDEO_BYTES, 200 * 1024 * 1024)
        self.assertGreaterEqual(dy.MIN_FREE_DISK, 1024 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
