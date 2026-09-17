"""worker.py 运行链路加固的离线单元测试（不联网、不启动子进程）。"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import (
    Worker, ImportFailure, MAX_SOCIAL_BYTES, TMP_PREFIX,
    cleanup_stale_temp, check_disk_space, redact_url, run_tool,
    _on_terminate, _ACTIVE_PROCS, download_social,
)

TOKEN = 'x' * 48


class TestRedactUrl(unittest.TestCase):
    def test_strips_query_and_fragment(self):
        self.assertEqual(
            redact_url('https://x.com/user/status/123?_r=1&token=secret#frag'),
            'https://x.com/user/status/123',
        )
        self.assertEqual(redact_url('https://t.me/ch/5?single'), 'https://t.me/ch/5')
        self.assertEqual(redact_url(''), '')
        self.assertEqual(redact_url('https://v.douyin.com/abc/'), 'https://v.douyin.com/abc/')

    def test_keeps_path_only(self):
        self.assertEqual(redact_url('http://a.b/c/d?q=1'), 'http://a.b/c/d')


class TestCleanupStaleTemp(unittest.TestCase):
    def test_removes_leftover_dirs_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / (TMP_PREFIX + 'deadbeef')
            stale.mkdir()
            (stale / 'a.mp4').write_bytes(b'x' * 10)
            keep = root / 'other-dir'
            keep.mkdir()
            with patch('worker.tempfile.gettempdir', return_value=tmp):
                cleanup_stale_temp()
            self.assertFalse(stale.exists())
            self.assertTrue(keep.exists())


class TestCheckDiskSpace(unittest.TestCase):
    def _usage(self, free):
        return type('DU', (), {'free': free})()

    def test_low_disk_raises_non_retryable(self):
        with patch('worker.shutil.disk_usage', return_value=self._usage(512 * 1024 * 1024)):
            with self.assertRaises(ImportFailure) as ctx:
                check_disk_space('/tmp')
            self.assertFalse(ctx.exception.retryable)
            self.assertIn('存储空间不足', str(ctx.exception))

    def test_enough_disk_passes(self):
        with patch('worker.shutil.disk_usage', return_value=self._usage(8 * 1024 * 1024 * 1024)):
            check_disk_space('/tmp')  # 不抛


class TestRunTool(unittest.TestCase):
    def _popen(self, communicate_result=None, exc=None, pid=4242):
        proc = MagicMock()
        proc.pid = pid
        if exc is not None:
            proc.communicate.side_effect = exc
        else:
            proc.communicate.return_value = communicate_result or (None, b'')
        return proc

    def test_timeout_classified_retryable_and_killed(self):
        proc = self._popen(exc=subprocess.TimeoutExpired(cmd=['x'], timeout=1))
        with patch('worker.subprocess.Popen', return_value=proc), \
             patch('worker._kill_tree') as kill_tree:
            with self.assertRaises(ImportFailure) as ctx:
                run_tool(['echo'], '/tmp', time.monotonic() + 60, '超时提示')
            self.assertTrue(ctx.exception.retryable)
            kill_tree.assert_called_once_with(proc)

    def test_oserror_during_run_classified_disk(self):
        proc = self._popen(exc=OSError(28, 'No space left on device'))
        with patch('worker.subprocess.Popen', return_value=proc), \
             patch('worker._kill_tree'):
            with self.assertRaises(ImportFailure) as ctx:
                run_tool(['echo'], '/tmp', time.monotonic() + 60, '超时提示')
            self.assertFalse(ctx.exception.retryable)
            self.assertIn('存储空间不足', str(ctx.exception))

    def test_popen_spawn_failure(self):
        with patch('worker.subprocess.Popen', side_effect=OSError(2, 'No such file')):
            with self.assertRaises(ImportFailure) as ctx:
                run_tool(['missing-tool'], '/tmp', time.monotonic() + 60, '超时提示')
            self.assertFalse(ctx.exception.retryable)
            self.assertIn('工具无法启动', str(ctx.exception))

    def test_nonzero_disk_error_is_non_retryable(self):
        proc = self._popen(communicate_result=(None, b'No space left on device'))
        proc.returncode = 1
        with patch('worker.subprocess.Popen', return_value=proc):
            with self.assertRaises(ImportFailure) as ctx:
                run_tool(['echo'], '/tmp', time.monotonic() + 60, '超时提示')
        self.assertFalse(ctx.exception.retryable)

    def test_nonzero_returncode_returned(self):
        proc = self._popen(communicate_result=(None, b'err'))
        proc.returncode = 1
        with patch('worker.subprocess.Popen', return_value=proc):
            code = run_tool(['echo'], '/tmp', time.monotonic() + 60, '超时提示')
            self.assertEqual(code, 1)


class TestTerminateHandler(unittest.TestCase):
    def test_sigterm_kills_active_procs_and_exits(self):
        proc = MagicMock()
        proc.pid = 4242
        _ACTIVE_PROCS.append(proc)
        try:
            with patch('worker._kill_tree') as kill_tree:
                with self.assertRaises(SystemExit):
                    _on_terminate(15, None)
                kill_tree.assert_called_once_with(proc)
        finally:
            _ACTIVE_PROCS.clear()


class TestWorkerServerUrl(unittest.TestCase):
    def test_public_http_is_rejected_even_with_compatibility_flag(self):
        with patch.dict(os.environ, {'ALLOW_INSECURE_HTTP': '1'}):
            with self.assertRaises(ValueError):
                Worker('http://app.example.com', TOKEN)

    def test_wireguard_private_ip_requires_explicit_flag(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                Worker('http://10.77.0.1:8765', TOKEN)
        with patch.dict(os.environ, {'ALLOW_INSECURE_HTTP': '1'}):
            worker=Worker('http://10.77.0.1:8765', TOKEN)
            self.assertEqual(worker.server, 'http://10.77.0.1:8765')


class TestUploadVideo(unittest.TestCase):
    def setUp(self):
        self.worker = Worker('https://app.example.com', TOKEN)
        self.calls = []

        def fake_api(path, data):
            self.calls.append((path, data))
            return {'asset': 'asset-1'}
        self.worker.api = fake_api

    def test_enforces_200mb_before_upload(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'x')
            path = Path(f.name)
        try:
            with patch.object(Path, 'stat', return_value=MagicMock(st_size=MAX_SOCIAL_BYTES + 1)):
                with self.assertRaises(ImportFailure) as ctx:
                    self.worker.upload_video({'id': 1}, path, time.monotonic() + 60)
                self.assertFalse(ctx.exception.retryable)
                self.assertIn('200 MB', str(ctx.exception))
        finally:
            path.unlink(missing_ok=True)

    def test_deadline_expired_mid_upload(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'x' * (4 * 1024 * 1024))
            path = Path(f.name)
        try:
            with self.assertRaises(ImportFailure) as ctx:
                self.worker.upload_video({'id': 1}, path, time.monotonic() - 1)
            self.assertTrue(ctx.exception.retryable)
        finally:
            path.unlink(missing_ok=True)

    def test_chunks_uploaded_and_asset_returned(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'x' * (5 * 1024 * 1024))
            path = Path(f.name)
        try:
            asset = self.worker.upload_video({'id': 1}, path, time.monotonic() + 60)
            self.assertEqual(asset, 'asset-1')
            self.assertGreaterEqual(len(self.calls), 2)
            self.assertTrue(all(c[0] == '/internal/jobs/video-chunk' for c in self.calls))
            self.assertTrue(self.calls[-1][1]['final'])
        finally:
            path.unlink(missing_ok=True)


class TestProcessSocial(unittest.TestCase):
    def test_canonical_url_is_redacted_and_no_source_url_in_blocks(self):
        worker = Worker('https://app.example.com', TOKEN)
        calls = []

        def fake_api(path, data):
            calls.append((path, data))
            return {'asset': 'asset-1'}
        worker.api = fake_api

        with tempfile.TemporaryDirectory() as tmp:
            thumb = Path(tmp) / 'thumb.jpg'
            thumb.write_bytes(b'\xff\xd8\xff\xe0' + b'0' * 100)
            info = {
                'title': '视频标题',
                'description': '正文描述',
                'uploader': '作者',
                'timestamp': 1700000000,
                'webpage_url': 'https://x.com/u/status/9?_r=1&token=SECRET',
                'id': '9',
            }
            with patch('worker.download_social', return_value=(info, None, thumb)), \
                 patch('worker.time.monotonic', return_value=0), \
                 patch('worker.time.strftime', return_value='2023-11-14T22:13:20Z'):
                worker.process_social({'id': 7, 'url': 'https://x.com/u/status/9?_r=1&token=SECRET'}, time.monotonic() + 600)

        complete = [d for p, d in calls if p == '/internal/jobs/complete'][0]
        meta = complete['metadata']
        self.assertEqual(meta['canonical_url'], 'https://x.com/u/status/9')
        # canonical 不得携带查询参数；结果块不得出现源站媒体地址
        self.assertNotIn('_r=1', meta['canonical_url'])
        for _, data in calls:
            if 'metadata' in data:
                self.assertNotIn('telesco', json.dumps(data, ensure_ascii=False))
        self.assertEqual(complete['title'], '视频标题')

    def test_disk_full_aborts_before_download(self):
        worker = Worker('https://app.example.com', TOKEN)
        with patch('worker.download_social', side_effect=ImportFailure('服务器存储空间不足，任务已取消', False)):
            with self.assertRaises(ImportFailure) as ctx:
                worker.process_social({'id': 1, 'url': 'https://tiktok.com/@a/video/1'}, time.monotonic() + 600)
            self.assertFalse(ctx.exception.retryable)


class TestDownloadSocialDiskGuard(unittest.TestCase):
    def test_check_disk_runs_before_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('worker.check_disk_space', side_effect=ImportFailure('服务器存储空间不足，任务已取消', False)):
                with self.assertRaises(ImportFailure):
                    download_social('https://tiktok.com/@a/video/1', Path(tmp), time.monotonic() + 60)


class TestCleanTrailing(unittest.TestCase):
    def test_strips_fullwidth_punctuation(self):
        from worker import clean_trailing
        self.assertEqual(clean_trailing('https://example.com/a）'), 'https://example.com/a')
        self.assertEqual(clean_trailing('https://t.me/ch/5。'), 'https://t.me/ch/5')
        self.assertEqual(clean_trailing('https://x.com/a?b=1，'), 'https://x.com/a?b=1')
        self.assertEqual(clean_trailing('https://example.com/a'), 'https://example.com/a')


class TestYtdlpSuccessPath(unittest.TestCase):
    def test_parses_info_and_selects_largest_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            info = {'title': 'T', 'webpage_url': 'https://example.com/v?p=1', 'id': 'x'}
            (directory / 'source.info.json').write_text(json.dumps(info), encoding='utf-8')
            (directory / 'source.mp4').write_bytes(b'0' * 1024)
            (directory / 'thumb.webp').write_bytes(b'1' * 64)
            proc = MagicMock()
            proc.pid = 1
            proc.communicate.return_value = (None, b'')
            proc.returncode = 0
            with patch('worker.subprocess.Popen', return_value=proc), \
                 patch('worker.check_disk_space'):
                got_info, video, thumb = download_social('https://example.com/v', directory, time.monotonic() + 60)
            self.assertEqual(got_info['title'], 'T')
            self.assertEqual(video.name, 'source.mp4')
            self.assertEqual(thumb.suffix, '.webp')


if __name__ == '__main__':
    unittest.main()
