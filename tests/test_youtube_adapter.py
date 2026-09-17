"""YouTube 适配器离线单元测试（不需要真实网络）。

覆盖：URL 正反例（含防误识别/私网拒绝）、视频 ID 提取、播放列表拒绝、
代理变量隔离（只读 YOUTUBE_PROXY，不读全局 YTDLP_PROXY）、磁盘 2GiB
阈值、deadline 到期即拒、直播/首播/受限类型探针拒绝、无视频/超预算
失败语义。真实网络样本在 test_youtube_real.py。
"""
import json
import os
import sys
import tempfile
import time
import signal
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.youtube.adapter import (
    MAX_BYTES,
    MIN_FREE_DISK,
    WORKING_BUDGET,
    YouTubeError,
    check_disk_space,
    download_social,
    proxy_url,
    resolve_video,
    _classify_error,
    _js_runtime_args,
    _kill_process_tree,
    _probe_metadata,
    _reject_if_not_importable,
    _run_yt_dlp,
)


class TestJsRuntimeArgs(unittest.TestCase):
    @mock.patch("adapters.youtube.adapter.shutil.which")
    def test_prefers_deno(self, mock_which):
        mock_which.side_effect = lambda n: "/usr/bin/deno" if n == "deno" else None
        self.assertEqual(_js_runtime_args(), ["--js-runtimes", "deno:/usr/bin/deno"])

    @mock.patch("adapters.youtube.adapter.shutil.which")
    def test_falls_back_to_node(self, mock_which):
        mock_which.side_effect = lambda n: "/usr/bin/node" if n == "node" else None
        self.assertEqual(_js_runtime_args(), ["--js-runtimes", "node:/usr/bin/node"])

    @mock.patch("adapters.youtube.adapter.shutil.which")
    def test_nodejs_alias_ignored(self, mock_which):
        mock_which.side_effect = lambda n: "/usr/bin/nodejs" if n == "nodejs" else None
        self.assertEqual(_js_runtime_args(), [])

    @mock.patch("adapters.youtube.adapter.shutil.which", return_value=None)
    def test_no_runtime(self, mock_which):
        self.assertEqual(_js_runtime_args(), [])


class TestResolveVideo(unittest.TestCase):
    def test_watch_url(self):
        url, vid = resolve_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_watch_with_query_params(self):
        url, vid = resolve_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42&feature=share")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_short_url(self):
        url, vid = resolve_video("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_mobile_url(self):
        url, vid = resolve_video("https://m.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_shorts_url(self):
        url, vid = resolve_video("https://www.youtube.com/shorts/dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_watch_with_list_only_downloads_video(self):
        url, vid = resolve_video(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"
        )
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_playlist_rejected(self):
        with self.assertRaises(YouTubeError) as ctx:
            resolve_video("https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI")
        self.assertIn("播放列表", str(ctx.exception))
        self.assertFalse(ctx.exception.retryable)

    def test_evil_domain_not_matched(self):
        with self.assertRaises(YouTubeError):
            resolve_video("https://www.youtube.com.evil.com/watch?v=dQw4w9WgXcQ")

    def test_bad_id_length(self):
        with self.assertRaises(YouTubeError):
            resolve_video("https://www.youtube.com/watch?v=tooshort")

    def test_missing_video_id(self):
        with self.assertRaises(YouTubeError):
            resolve_video("https://www.youtube.com/@somechannel/videos")

    def test_non_youtube_host(self):
        with self.assertRaises(YouTubeError):
            resolve_video("https://example.com/watch?v=dQw4w9WgXcQ")

    def test_private_ip_rejected(self):
        # SSRF 边界：私网/回环伪装的"youtube"host 一律拒绝
        for bad in (
            "http://127.0.0.1/watch?v=dQw4w9WgXcQ",
            "http://10.0.0.5/watch?v=dQw4w9WgXcQ",
            "http://192.168.1.1/watch?v=dQw4w9WgXcQ",
            "http://localhost/watch?v=dQw4w9WgXcQ",
            "https://youtube.com.internal/watch?v=dQw4w9WgXcQ",
        ):
            with self.assertRaises(YouTubeError, msg=bad):
                resolve_video(bad)


class TestProxyIsolation(unittest.TestCase):
    def test_youtube_proxy_wins_over_legacy(self):
        with mock.patch.dict(os.environ, {"YOUTUBE_PROXY": "http://127.0.0.1:7890",
                                        "PROXY_URL": "http://127.0.0.1:8118",
                                        "YTDLP_PROXY": "http://1.2.3.4:9999"}, clear=False):
            # 只看本适配器读哪个：必须用 YouTube 专属出口，不读全局 YTDLP_PROXY
            self.assertEqual(proxy_url(), "http://127.0.0.1:7890")

    def test_falls_back_to_shared_proxy(self):
        with mock.patch.dict(os.environ, {"PROXY_URL": "http://127.0.0.1:8118"}, clear=False):
            os.environ.pop("YOUTUBE_PROXY", None)
            os.environ.pop("YTDLP_PROXY", None)
            self.assertEqual(proxy_url(), "http://127.0.0.1:8118")

    def test_ignores_global_ytdlp_proxy(self):
        # worker 全局 YTDLP_PROXY 不得被本适配器读取（避免污染其他平台出口）
        with mock.patch.dict(os.environ, {"YTDLP_PROXY": "http://1.2.3.4:9999"}, clear=False):
            os.environ.pop("YOUTUBE_PROXY", None)
            os.environ.pop("PROXY_URL", None)
            self.assertEqual(proxy_url(), "")


class TestClassifyError(unittest.TestCase):
    def test_bot_check_retryable(self):
        msg, retryable = _classify_error("ERROR: [youtube] Sign in to confirm you're not a bot.")
        self.assertTrue(retryable)

    def test_bot_check_curly_quote(self):
        msg, retryable = _classify_error("ERROR: [youtube] Sign in to confirm you\u2019re not a bot.")
        self.assertTrue(retryable)

    def test_age_confirm_retryable(self):
        msg, retryable = _classify_error("ERROR: [youtube] Sign in to confirm your age")
        self.assertTrue(retryable)

    def test_age_restricted_not_retryable(self):
        msg, retryable = _classify_error("ERROR: [youtube] This video is age-restricted and only available on YouTube.")
        self.assertFalse(retryable)

    def test_video_unavailable(self):
        msg, retryable = _classify_error("ERROR: [youtube] Video unavailable")
        self.assertFalse(retryable)

    def test_private_video(self):
        msg, retryable = _classify_error("ERROR: Private video. Sign in if you've been granted access.")
        self.assertFalse(retryable)

    def test_members_only(self):
        msg, retryable = _classify_error("This video is available to members only")
        self.assertFalse(retryable)

    def test_premiere_not_started(self):
        msg, retryable = _classify_error("Premieres in 3 hours")
        self.assertTrue(retryable)

    def test_http_error(self):
        msg, retryable = _classify_error("ERROR: HTTP Error 403: Forbidden")
        self.assertTrue(retryable)

    def test_generic(self):
        msg, retryable = _classify_error("ERROR: something weird happened")
        self.assertTrue(retryable)


class TestCheckDiskSpace(unittest.TestCase):
    @mock.patch("adapters.youtube.adapter.shutil.disk_usage")
    def test_insufficient_disk(self, mock_usage):
        # 阈值 2GiB：free 1GiB 应拒绝
        mock_usage.return_value = type("DU", (), {"free": 1 * 1024 * 1024 * 1024})()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(YouTubeError):
                check_disk_space(td)

    @mock.patch("adapters.youtube.adapter.shutil.disk_usage")
    def test_sufficient_disk(self, mock_usage):
        mock_usage.return_value = type("DU", (), {"free": 5 * 1024 * 1024 * 1024})()
        with tempfile.TemporaryDirectory() as td:
            check_disk_space(td)  # 不应抛错

    def test_threshold_alignment(self):
        # 对齐项目“根盘低于 2GiB 拒绝新媒体”
        self.assertEqual(MIN_FREE_DISK, 2 * 1024 * 1024 * 1024)


class TestRejectIfNotImportable(unittest.TestCase):
    def test_live_rejected(self):
        with self.assertRaises(YouTubeError) as ctx:
            _reject_if_not_importable({"is_live": True, "live_status": "is_live"})
        self.assertIn("直播", str(ctx.exception))
        self.assertTrue(ctx.exception.retryable)

    def test_upcoming_rejected(self):
        with self.assertRaises(YouTubeError) as ctx:
            _reject_if_not_importable({"live_status": "is_upcoming"})
        self.assertIn("首播", str(ctx.exception))

    def test_private_rejected(self):
        with self.assertRaises(YouTubeError) as ctx:
            _reject_if_not_importable({"availability": "private"})
        self.assertFalse(ctx.exception.retryable)

    def test_member_only_rejected(self):
        with self.assertRaises(YouTubeError):
            _reject_if_not_importable({"availability": "subscriber_only"})

    def test_age_restricted_rejected(self):
        with self.assertRaises(YouTubeError):
            _reject_if_not_importable({"availability": "age_restricted"})

    def test_past_live_recording_allowed(self):
        # 已结束的直播回放应可继续下载
        _reject_if_not_importable({"live_status": "was_live", "is_live": False})

    def test_normal_ok(self):
        _reject_if_not_importable({"availability": "public"})


class TestDownloadSocialOffline(unittest.TestCase):
    def setUp(self):
        # 所有 download_social 测试统一假装有代理（SSRF 检查在更后面）
        self._proxy_patcher = mock.patch(
            "adapters.youtube.adapter.proxy_url", return_value="http://127.0.0.1:7890"
        )
        self._proxy_patcher.start()
        self.addCleanup(self._proxy_patcher.stop)

    def _fake_ytdlp(self, calls):
        """返回一个可按调用顺序返回不同 (rc,out,err) 的 mock。"""
        return mock.MagicMock(side_effect=calls)

    def test_deadline_expired_before_probe(self):
        # deadline 已过：不应启动任何子进程
        with mock.patch("adapters.youtube.adapter._run_yt_dlp") as run:
            with mock.patch("adapters.youtube.adapter.time.monotonic", return_value=1000.0):
                with tempfile.TemporaryDirectory() as td:
                    with self.assertRaises(YouTubeError) as ctx:
                        download_social(
                            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                            Path(td), 999.0,  # deadline < now
                        )
                    self.assertIn("时间预算", str(ctx.exception))
                    run.assert_not_called()

    def test_ytdlp_failure_classified(self):
        with mock.patch("adapters.youtube.adapter._run_yt_dlp") as run:
            run.return_value = (1, "", "ERROR: [youtube] Video unavailable")
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(YouTubeError) as ctx:
                    download_social(
                        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        Path(td), time.monotonic() + 600,
                    )
                self.assertFalse(ctx.exception.retryable)

    def test_playlist_rejected_before_ytdlp(self):
        with mock.patch("adapters.youtube.adapter._run_yt_dlp") as run:
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(YouTubeError) as ctx:
                    download_social(
                        "https://www.youtube.com/playlist?list=abc",
                        Path(td), time.monotonic() + 600,
                    )
                self.assertIn("播放列表", str(ctx.exception))
                run.assert_not_called()

    def test_live_rejected_before_download(self):
        # 探针返回正在直播 → 抛错，不应走第二次下载命令
        probe = json.dumps({"is_live": True, "live_status": "is_live", "availability": "public"})
        with mock.patch("adapters.youtube.adapter._run_yt_dlp") as run:
            run.return_value = (0, probe, "")
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(YouTubeError) as ctx:
                    download_social(
                        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        Path(td), time.monotonic() + 600,
                    )
                self.assertIn("直播", str(ctx.exception))
                self.assertEqual(run.call_count, 1)  # 只有探针，没有下载

    def test_success_returns_info_video_thumb(self):
        probe = json.dumps({"id": "dQw4w9WgXcQ", "availability": "public",
                            "title": "Test", "uploader": "Tester"})
        with mock.patch("adapters.youtube.adapter._run_yt_dlp") as run:
            run.return_value = (0, probe, "")
            with tempfile.TemporaryDirectory() as td:
                # 写 yt-dlp 会产出的 info.json + 一个小 mp4 + 缩略图
                (Path(td) / "dQw4w9WgXcQ.info.json").write_text(
                    json.dumps({"id": "dQw4w9WgXcQ", "title": "Test",
                                "uploader": "Tester", "webpage_url": "https://youtu.be/dQw4w9WgXcQ"}),
                    encoding="utf-8")
                (Path(td) / "dQw4w9WgXcQ.mp4").write_bytes(b"\x00" * 1024)
                (Path(td) / "dQw4w9WgXcQ.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 512)
                info, video, thumb = download_social(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    Path(td), time.monotonic() + 600,
                )
                self.assertEqual(info["id"], "dQw4w9WgXcQ")
                self.assertEqual(video.suffix, ".mp4")
                self.assertIsNotNone(thumb)

    def test_no_video_file_raises(self):
        # 探针说 public、下载返回 0，但目录里没有 mp4 → 必须失败，
        # 不能返回"成功但无视频"的元数据
        probe = json.dumps({"id": "x", "availability": "public"})
        with mock.patch("adapters.youtube.adapter._run_yt_dlp") as run:
            run.return_value = (0, probe, "")
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "x.info.json").write_text(json.dumps({"id": "x", "title": "t"}), encoding="utf-8")
                with self.assertRaises(YouTubeError) as ctx:
                    download_social("https://youtu.be/dQw4w9WgXcQ", Path(td), time.monotonic() + 600)
                self.assertTrue(ctx.exception.retryable)

    def test_oversize_video_removed_and_fails(self):
        probe = json.dumps({"id": "big", "availability": "public"})
        with mock.patch("adapters.youtube.adapter._run_yt_dlp") as run:
            run.return_value = (0, probe, "")
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "big.info.json").write_text(json.dumps({"id": "big", "title": "t"}), encoding="utf-8")
                # 写一个超过 200MB 的假 mp4
                fake = Path(td) / "big.mp4"
                fake.write_bytes(b"\x00" * (MAX_BYTES + 1))
                with self.assertRaises(YouTubeError) as ctx:
                    download_social("https://youtu.be/dQw4w9WgXcQ", Path(td), time.monotonic() + 600)
                self.assertIn("200MB", str(ctx.exception))
                self.assertFalse(fake.exists())  # 超尺寸文件已清理

    def test_working_budget_constant(self):
        # 总量预算 = 2×MAX_BYTES（音视频分轨未合并的最坏情况）
        self.assertEqual(WORKING_BUDGET, 2 * MAX_BYTES)

    def test_no_proxy_refused(self):
        # SSRF：未配置代理时拒绝直连，不启动 yt-dlp
        with mock.patch("adapters.youtube.adapter.proxy_url", return_value=""):
            with mock.patch("adapters.youtube.adapter._run_yt_dlp") as run:
                with tempfile.TemporaryDirectory() as td:
                    with self.assertRaises(YouTubeError) as ctx:
                        download_social(
                            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                            Path(td), time.monotonic() + 600,
                        )
                    self.assertIn("代理", str(ctx.exception))
                    run.assert_not_called()

    def test_proxy_is_localhost_loopback(self):
        # proxy_url 返回值必须是回环地址（127.0.0.1），不接受公网/用户输入
        with mock.patch.dict(os.environ, {"YOUTUBE_PROXY": "http://127.0.0.1:7890"}, clear=False):
            self.assertEqual(proxy_url(), "http://127.0.0.1:7890")


class TestKillProcessTree(unittest.TestCase):
    """跨平台进程树清理：Windows 不调 os.killpg（会 AttributeError）。"""

    def test_unix_uses_killpg(self):
        fake = mock.MagicMock()
        fake.pid = 12345
        with mock.patch("sys.platform", "linux"), \
             mock.patch("signal.SIGKILL", 9, create=True), \
             mock.patch("os.killpg", create=True) as kp:
            _kill_process_tree(fake)
            kp.assert_called_once_with(12345, 9)

    def test_unix_killpg_fallback_to_kill(self):
        fake = mock.MagicMock()
        fake.pid = 12345
        with mock.patch("sys.platform", "linux"), \
             mock.patch("signal.SIGKILL", 9, create=True), \
             mock.patch("os.killpg", create=True, side_effect=ProcessLookupError):
            _kill_process_tree(fake)
            fake.kill.assert_called_once()

    def test_windows_uses_taskkill(self):
        fake = mock.MagicMock()
        fake.pid = 12345
        with mock.patch("sys.platform", "win32"), \
             mock.patch("subprocess.run") as run:
            _kill_process_tree(fake)
            run.assert_called_once()
            args = run.call_args[0][0]
            self.assertEqual(args[0], "taskkill")
            self.assertIn("/T", args)
            self.assertIn("/F", args)


class TestRunYtDlpWatch(unittest.TestCase):
    """_run_yt_dlp 运行中监控：超时/预算/磁盘不足/到期都杀进程树。"""

    def test_timeout_kills_process(self):
        # 真实跑一个 sleep 进程，timeout=2s，应在 ~2s 后抛超时错误
        import sys as _sys
        cmd = [_sys.executable, "-c", "import time; time.sleep(60)"]
        with tempfile.TemporaryDirectory() as td:
            deadline = time.monotonic() + 5
            with self.assertRaises(YouTubeError) as ctx:
                _run_yt_dlp(cmd, td, deadline, watch_dir=td)
            self.assertIn("时间限制", str(ctx.exception))
            self.assertTrue(ctx.exception.retryable)

    def test_deadline_expired_while_running(self):
        # deadline 很短：timeout=min(int(left),600) 与 deadline 几乎同时触发，
        # 核心是验证运行中超时/到期会杀进程树并抛 retryable 错误
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        with tempfile.TemporaryDirectory() as td:
            deadline = time.monotonic() + 1.5
            with self.assertRaises(YouTubeError) as ctx:
                _run_yt_dlp(cmd, td, deadline, watch_dir=td)
            self.assertIn("时间", str(ctx.exception))
            self.assertTrue(ctx.exception.retryable)

    def test_budget_exceeded_kills_process(self):
        # 目录里快速写入大文件，模拟下载中超过 WORKING_BUDGET
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        with tempfile.TemporaryDirectory() as td:
            # 预写一个超过预算的文件
            (Path(td) / "big.part").write_bytes(b"\x00" * (WORKING_BUDGET + 1024))
            deadline = time.monotonic() + 30
            with self.assertRaises(YouTubeError) as ctx:
                _run_yt_dlp(cmd, td, deadline, watch_dir=td)
            self.assertIn("预算", str(ctx.exception))

    def test_disk_low_kills_process(self):
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        fake_usage = mock.MagicMock()
        fake_usage.free = 100 * 1024 * 1024  # 100MB < MIN_FREE_DISK
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("shutil.disk_usage", return_value=fake_usage):
                deadline = time.monotonic() + 30
                with self.assertRaises(YouTubeError) as ctx:
                    _run_yt_dlp(cmd, td, deadline, watch_dir=td)
                self.assertIn("存储空间不足", str(ctx.exception))

    def test_natural_exit_returns_output(self):
        # 正常命令退出，返回 stdout
        cmd = [sys.executable, "-c", "print('HELLO_FROM_YTDLP')"]
        with tempfile.TemporaryDirectory() as td:
            deadline = time.monotonic() + 30
            rc, out, err = _run_yt_dlp(cmd, td, deadline, watch_dir=td)
            self.assertEqual(rc, 0)
            self.assertIn("HELLO_FROM_YTDLP", out)


if __name__ == "__main__":
    unittest.main()
