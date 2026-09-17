"""link_input 链接提取离线单元测试（不改代码，纯回归验证）。

覆盖：完整分享文案中的链接提取、带查询参数的链接、平台短链判定、
尾部标点/括号清理、裸域名补全。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from link_input import extract_url, platform_for_url, content_id_for_url, source_reference


class TestExtractUrl(unittest.TestCase):
    def test_from_full_share_text(self):
        text = '5.69 z@g.BG VlC:/ :4pm 08/12 赛博神话 https://v.douyin.com/xI2lsOdYs90/ 复制此链接，打开Dou音搜索'
        self.assertEqual(extract_url(text), 'https://v.douyin.com/xI2lsOdYs90/')

    def test_keeps_query_parameters(self):
        self.assertEqual(
            extract_url('看这个 https://www.tiktok.com/@a/video/1?_r=1&foo=bar 怎么样'),
            'https://www.tiktok.com/@a/video/1?_r=1&foo=bar',
        )

    def test_strips_trailing_punctuation(self):
        self.assertEqual(extract_url('点这 https://t.me/ch/5。'), 'https://t.me/ch/5')
        self.assertEqual(extract_url('看 https://example.com/a，'), 'https://example.com/a')
        self.assertEqual(extract_url('https://example.com/a?b=1，'), 'https://example.com/a?b=1')

    def test_balanced_brackets(self):
        self.assertEqual(extract_url('[https://example.com/a] 内容'), 'https://example.com/a')
        self.assertEqual(extract_url('见（https://example.com/a）'), 'https://example.com/a')

    def test_bare_known_domain_prefixed(self):
        self.assertEqual(extract_url('tiktok.com/@a/video/1 分享'), 'https://tiktok.com/@a/video/1')
        self.assertEqual(extract_url('x.com/user/status/9'), 'https://x.com/user/status/9')
        self.assertEqual(extract_url('v.douyin.com/AbC/'), 'https://v.douyin.com/AbC/')

    def test_no_url_raises(self):
        for value in ('', '   ', '没有链接的文本'):
            with self.assertRaises(ValueError):
                extract_url(value)

    def test_html_entity_unescaped(self):
        self.assertEqual(extract_url('https://example.com/a&amp;b=1'), 'https://example.com/a&b=1')

    def test_oversized_rejected(self):
        with self.assertRaises(ValueError):
            extract_url('x' * 10001)


class TestPlatformForUrl(unittest.TestCase):
    def test_all_platforms(self):
        cases = {
            'https://v.douyin.com/x/': 'douyin',
            'https://www.iesdouyin.com/share/video/1': 'douyin',
            'https://www.tiktok.com/@a/video/1': 'tiktok',
            'https://x.com/u/status/1': 'x',
            'https://twitter.com/u/status/1': 'x',
            'https://t.me/ch/5': 'telegram',
            'https://telegram.me/ch/5': 'telegram',
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ': 'youtube',
            'https://youtu.be/dQw4w9WgXcQ': 'youtube',
            'https://m.youtube.com/watch?v=dQw4w9WgXcQ': 'youtube',
            'https://www.youtube.com/shorts/dQw4w9WgXcQ': 'youtube',
            'https://example.com/article': 'web',
        }
        for url, expected in cases.items():
            self.assertEqual(platform_for_url(url), expected, url)

    def test_evil_youtube_domain_not_matched(self):
        # 防误识别：伪 YouTube 域名必须归 web，不能当 youtube 处理
        self.assertEqual(platform_for_url('https://www.youtube.com.evil.com/watch?v=dQw4w9WgXcQ'), 'web')
        self.assertEqual(platform_for_url('https://youtube.com.evil.io/x'), 'web')
        self.assertEqual(platform_for_url('https://notyoutube.com/watch?v=dQw4w9WgXcQ'), 'web')

    def test_short_links(self):
        self.assertEqual(platform_for_url('https://v.douyin.com/AbC/'), 'douyin')
        self.assertEqual(platform_for_url('https://t.co/abc'), 'web')  # 通用短链归 web，由 fetch_public 跳转


class TestContentId(unittest.TestCase):
    def test_extracts_ids(self):
        self.assertEqual(content_id_for_url('https://www.douyin.com/video/123456789012345678'), '123456789012345678')
        self.assertEqual(content_id_for_url('https://www.tiktok.com/@a/video/1234567890'), '1234567890')
        self.assertEqual(content_id_for_url('https://x.com/u/status/9876543210'), '9876543210')
        self.assertEqual(content_id_for_url('https://t.me/ch/55'), '55')
        self.assertEqual(content_id_for_url('https://example.com/plain'), '')

    def test_douyin_modal_id_query(self):
        url = 'https://www.douyin.com/?modal_id=123456789012345678'
        self.assertEqual(content_id_for_url(url, 'douyin'), '123456789012345678')

    def test_youtube_ids(self):
        self.assertEqual(content_id_for_url('https://www.youtube.com/watch?v=dQw4w9WgXcQ'), 'dQw4w9WgXcQ')
        self.assertEqual(content_id_for_url('https://youtu.be/dQw4w9WgXcQ'), 'dQw4w9WgXcQ')
        self.assertEqual(content_id_for_url('https://www.youtube.com/shorts/dQw4w9WgXcQ'), 'dQw4w9WgXcQ')
        self.assertEqual(content_id_for_url('https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=1'), 'dQw4w9WgXcQ')

    def test_youtube_bad_ids(self):
        self.assertEqual(content_id_for_url('https://www.youtube.com/playlist?list=abc'), '')
        self.assertEqual(content_id_for_url('https://www.youtube.com/watch?v=bad'), '')


class TestSourceReference(unittest.TestCase):
    def test_full_pipeline(self):
        ref = source_reference('分享 https://t.me/somechannel/42?embed=1 内容')
        self.assertEqual(ref['url'], 'https://t.me/somechannel/42?embed=1')
        self.assertEqual(ref['platform'], 'telegram')
        self.assertEqual(ref['content_id'], '42')


if __name__ == '__main__':
    unittest.main()
