import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.hotspots.client import TogetherClient
from collectors.hotspots.runner import HotspotRunner
from collectors.hotspots.state import StateStore
from server import App, handler_for
from worker import Worker

KEY = "e2e-" + "x" * 64
PASSWORD = "test-password-123456"

class FixtureFetcher:
    def __init__(self, payload):
        self.payload = payload
    def __call__(self, url, headers=None):
        return self.payload, {}, url, 200

class HotspotE2ETests(unittest.TestCase):
    def test_candidate_to_worker_to_published_visible_to_other_user(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as state_dir:
            app = App(data_dir, KEY)
            app.add_user("one", "One", PASSWORD)
            app.add_user("two", "Two", PASSWORD)
            http = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(app))
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{http.server_address[1]}"
            try:
                worker = Worker(base, KEY)
                worker.api("/internal/" + "heartbeat", {})
                feed = b'''<?xml version="1.0"?><rss version="2.0"><channel>
<item><guid>e2e-1</guid><title>Hotspot E2E story</title>
<link>https://news.example.com/e2e</link><pubDate>Mon, 14 Sep 2026 02:00:00 GMT</pubDate></item>
</channel></rss>'''
                config = {
                    "schema_version": 1, "timezone": "UTC", "per_run_limit": 1,
                    "daily_limit": 10, "max_inflight": 2, "freshness_hours": 48,
                    "sources": [{
                        "id": "fixture-e2e", "name": "Fixture E2E", "kind": "rss",
                        "url": "https://feed.example.com/rss", "enabled": True,
                        "priority": 50, "network": "direct",
                        "allowed_article_hosts": ["news.example.com"],
                    }],
                }
                collector = TogetherClient(base, "one", PASSWORD)
                runner = HotspotRunner(
                    config, StateStore(state_dir), fetcher=FixtureFetcher(feed),
                    validator=lambda item, source: True, client=collector, poll_budget=0,
                    now_fn=lambda: datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc),
                )
                report = runner.run("submit")
                self.assertEqual(report["counts"]["submitted"], 1)
                self.assertEqual(report["counts"]["pending"], 1)

                html = b"<html><title>Parsed E2E headline</title><article><h1>Parsed E2E headline</h1><p>" + (
                    b"Hotspot article body for isolated end to end verification. " * 8
                ) + b"</p></article></html>"
                with patch(
                    "worker.fetch_public",
                    return_value=(html, "text/html; charset=utf-8", "https://news.example.com/e2e"),
                ):
                    self.assertTrue(worker.once())

                viewer = TogetherClient(base, "two", PASSWORD)
                viewer.login()
                shares = viewer.shares()
                self.assertEqual(len(shares), 1)
                share = shares[0]
                self.assertEqual(share["status"], "published")
                self.assertEqual(share["title"], "Parsed E2E headline")
                self.assertEqual(share["url"], "https://news.example.com/e2e")
                self.assertTrue(share["note"].startswith("[hotspot:v1:"))
                self.assertTrue(any(block.get("type") == "paragraph" for block in share["blocks"]))
            finally:
                http.shutdown()
                http.server_close()

if __name__ == "__main__":
    unittest.main(verbosity=2)
