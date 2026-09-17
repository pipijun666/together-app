import unittest
from unittest.mock import patch, MagicMock
import os
import json
import tempfile
import shutil
from pathlib import Path
from server import App


class TestWorkerTranslation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.app = App(
            directory=self.temp_dir,
            worker_token='x' * 32,
            hotspot_token='y' * 32,
            secure=False
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('urllib.request.build_opener')
    def test_call_worker_translate_success(self, mock_build_opener):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            'title': '测试标题',
            'body': '测试正文内容',
            'ok': True,
            'provider': 'gemini-worker'
        }).encode('utf-8')
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        res = self.app.call_worker_translate('Test Title', 'Test Body', 'https://workers.together.example.com/')
        self.assertEqual(res['title'], '测试标题')
        self.assertEqual(res['body'], '测试正文内容')
        self.assertTrue(res['ok'])
        self.assertEqual(res['provider'], 'gemini-worker')

    @patch('server.App.call_worker_translate')
    def test_translate_api_routes_to_worker(self, mock_worker):
        mock_worker.return_value = {
            'title': '你好',
            'body': '世界',
            'ok': True,
            'provider': 'gemini-worker'
        }
        with patch.dict(os.environ, {'GEMINI_WORKER_URL': 'https://workers.together.example.com/'}):
            with self.app.db() as db:
                res, _ = self.app.translate_content(db, {'title': 'Hello', 'body': 'World'})
            self.assertEqual(res['title'], '你好')
            self.assertEqual(res['body'], '世界')
            self.assertFalse(res['cached'])
            mock_worker.assert_called_once()

            # Second call should hit SQLite cache
            with self.app.db() as db:
                res2, _ = self.app.translate_content(db, {'title': 'Hello', 'body': 'World'})
            self.assertEqual(res2['title'], '你好')
            self.assertTrue(res2['cached'])


if __name__ == '__main__':
    unittest.main()
