import base64
import http.cookiejar
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import dumps
from server import App, MEDIA_CACHE_CONTROL, handler_for
from worker import Worker, mp4_faststart, optimize_video_for_streaming
from PIL import Image

TOKEN = "media-worker-" + "x" * 32
PASSWORD = "test-password-123456"
MP4_HEAD = b"\x00\x00\x00\x14ftypmp42" + b"\x00" * 8


def box(kind, payload=b""):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


FAST_MP4 = MP4_HEAD + box(b"moov", b"index") + box(b"mdat", b"data")
SLOW_MP4 = MP4_HEAD + box(b"mdat", b"data") + box(b"moov", b"index")


class VideoOptimizationTests(unittest.TestCase):
    def test_faststart_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            fast = Path(folder) / "fast.mp4"
            slow = Path(folder) / "slow.mp4"
            fast.write_bytes(FAST_MP4)
            slow.write_bytes(SLOW_MP4)
            self.assertTrue(mp4_faststart(fast))
            self.assertFalse(mp4_faststart(slow))

    def test_compliant_faststart_video_is_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(FAST_MP4)
            profile = {"codec": "h264", "width": 1280, "height": 720,
                       "bitrate": 1800000, "duration": 10.0}
            with patch("worker.probe_video", return_value=profile), \
                 patch("worker.run_tool") as run:
                result = optimize_video_for_streaming(source, Path(folder), time.monotonic() + 30)
            self.assertEqual(result, source)
            run.assert_not_called()

    def test_low_bitrate_non_faststart_video_is_remuxed(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(SLOW_MP4 + b"x" * 200)
            profile = {"codec": "h264", "width": 1280, "height": 720,
                       "bitrate": 1800000, "duration": 10.0}
            commands = []

            def fake_run(command, cwd, deadline, message):
                commands.append(command)
                Path(command[-1]).write_bytes(FAST_MP4 + b"y" * 10)
                return 0
            with patch("worker.probe_video", return_value=profile), \
                 patch("worker.run_tool", side_effect=fake_run), \
                 patch("worker.check_disk_space"):
                result = optimize_video_for_streaming(source, Path(folder), time.monotonic() + 30)
            self.assertEqual(result.name, "playback-faststart.mp4")
            self.assertIn("copy", commands[0])
            self.assertIn("+faststart", commands[0])

    def test_high_profile_video_is_transcoded_and_capped(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(SLOW_MP4 + b"x" * 1000)
            profile = {"codec": "h264", "width": 1920, "height": 1080,
                       "bitrate": 6500000, "duration": 10.0}
            commands = []

            def fake_run(command, cwd, deadline, message):
                commands.append(command)
                Path(command[-1]).write_bytes(FAST_MP4 + b"z" * 50)
                return 0

            with patch("worker.probe_video", return_value=profile), \
                 patch("worker.run_tool", side_effect=fake_run), \
                 patch("worker.check_disk_space"):
                result = optimize_video_for_streaming(source, Path(folder), time.monotonic() + 30)
            self.assertEqual(result.name, "playback-720p.mp4")
            command = commands[0]
            self.assertIn("libx264", command)
            self.assertIn("veryfast", command)
            self.assertIn("+faststart", command)
            self.assertTrue(any("min(720,ih)" in arg for arg in command))
            self.assertIn("2200000", command)

    def test_probe_or_optimizer_failure_keeps_original(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(SLOW_MP4 + b"x" * 200)
            with patch("worker.probe_video", return_value=None):
                self.assertEqual(optimize_video_for_streaming(
                    source, Path(folder), time.monotonic() + 30), source)


class MediaDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = App(cls.tmp.name, TOKEN)
        cls.app.add_user("one", "One", PASSWORD)
        cls.app.add_user("two", "Two", PASSWORD)
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(cls.app))
        cls.base = "http://127.0.0.1:" + str(cls.http.server_address[1])
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.one = cls.client()
        cls.two = cls.client()
        cls.anon = cls.client()
        cls.call(cls.one, "/api/login", {"user": "one", "password": PASSWORD})
        cls.call(cls.two, "/api/login", {"user": "two", "password": PASSWORD})

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.tmp.cleanup()

    @staticmethod
    def client():
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    @classmethod
    def request(cls, client, path, data=None, headers=None, method=None):
        req_headers = {"Content-Type": "application/json"} if data is not None else {}
        req_headers.update(headers or {})
        return client.open(urllib.request.Request(
            cls.base + path,
            data=dumps(data) if data is not None else None,
            headers=req_headers,
            method=method or ("POST" if data is not None else "GET"),
        ), timeout=10)

    @classmethod
    def call(cls, client, path, data=None, headers=None, method=None):
        try:
            with cls.request(client, path, data, headers, method) as response:
                raw = response.read()
                body = json.loads(raw) if "json" in response.headers.get("Content-Type", "") else raw
                return response.status, body, response.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = raw
            return exc.code, body, exc.headers

    def published_image(self):
        code, created, _ = self.call(self.one, "/api/shares", {
            "manual": True, "title": "Cached image", "body": "Private cache test body."
        })
        self.assertEqual(code, 200)
        sid = created["id"]
        image = Image.new("RGB", (24, 24), "orange")
        out = io.BytesIO()
        image.save(out, "PNG")
        code, _, _ = self.call(self.one, "/api/shares/" + sid + "/image", {
            "data": base64.b64encode(out.getvalue()).decode()
        })
        self.assertEqual(code, 200)
        share = self.call(self.one, "/api/shares/" + sid)[1]["share"]
        src = next(block["src"] for block in share["blocks"] if block["type"] == "image")
        self.assertEqual(self.call(self.one, "/api/shares/" + sid + "/publish", {})[0], 200)
        return sid, src

    def test_authenticated_media_uses_private_cache_etag_and_range(self):
        _, src = self.published_image()
        self.assertEqual(self.call(self.anon, src)[0], 401)
        code, body, headers = self.call(self.two, src)
        self.assertEqual(code, 200)
        self.assertTrue(body)
        self.assertEqual(headers.get("Cache-Control"), MEDIA_CACHE_CONTROL)
        self.assertEqual(headers.get("Vary"), "Cookie")
        etag = headers.get("ETag")
        self.assertRegex(etag or "", r'^"[a-f0-9]{64}"$')

        code, body, headers = self.call(self.two, src, headers={"If-None-Match": etag})
        self.assertEqual(code, 304)
        self.assertEqual(body, {})
        self.assertEqual(headers.get("ETag"), etag)

        code, body, headers = self.call(self.two, src, headers={"Range": "bytes=0-9"})
        self.assertEqual(code, 206)
        self.assertEqual(len(body), 10)
        self.assertTrue(headers.get("Content-Range", "").startswith("bytes 0-9/"))
        self.assertEqual(headers.get("Cache-Control"), MEDIA_CACHE_CONTROL)

    def test_x_accel_redirect_is_authenticated_and_internal(self):
        _, src = self.published_image()
        previous = self.app.x_accel_media
        self.app.x_accel_media = True
        try:
            code, body, headers = self.call(self.two, src)
            self.assertEqual(code, 200)
            self.assertEqual(body, b"")
            self.assertRegex(headers.get("X-Accel-Redirect", ""),
                             r"^/__together_image/[a-f0-9]{64}$")
            self.assertEqual(headers.get("Cache-Control"), MEDIA_CACHE_CONTROL)
            self.assertEqual(self.call(self.anon, src)[0], 401)
            internal = headers["X-Accel-Redirect"]
            self.assertEqual(self.call(self.two, internal)[0], 404)
        finally:
            self.app.x_accel_media = previous

    def test_video_x_accel_uses_video_location(self):
        code, created, _ = self.call(self.one, "/api/link-preview", {
            "text": "https://public.example/video"
        })
        self.assertEqual(code, 200)
        worker = Worker(self.base, TOKEN)
        job = worker.api("/internal/jobs/claim", {})["job"]
        self.assertEqual(job["id"], created["id"])
        upload = "a" * 32
        chunk = base64.b64encode(FAST_MP4).decode()
        res = worker.api("/internal/jobs/video-chunk", {
            **job, "upload": upload, "offset": 0, "data": chunk, "final": True
        })
        asset = res["asset"]
        src = res["src"]
        worker.api("/internal/jobs/complete", {
            **job,
            "title": "Video share",
            "blocks": [{"type": "video", "asset": asset, "poster": ""}],
            "warnings": [],
            "metadata": {"content_type": "video"}
        })
        previous = self.app.x_accel_media
        self.app.x_accel_media = True
        try:
            code, body, headers = self.call(self.two, src)
            self.assertEqual(code, 200)
            self.assertEqual(body, b"")
            self.assertRegex(headers.get("X-Accel-Redirect", ""),
                             r"^/__together_video/[a-f0-9]{64}$")
        finally:
            self.app.x_accel_media = previous

