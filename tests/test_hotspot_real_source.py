import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.hotspots.client import TogetherClient
from collectors.hotspots.runner import HotspotRunner
from collectors.hotspots.state import StateStore
from server import App, handler_for
from worker import Worker

RUN_REAL = os.environ.get("HOTSPOT_REAL_SOURCE") == "1"
KEY = "real-source-e2e-" + "a" * 64
PASSWORD = "isolated-test-password-123456"


@unittest.skipUnless(RUN_REAL, "set HOTSPOT_REAL_SOURCE=1 for public network validation")
class HotspotRealSourceTests(unittest.TestCase):
    def test_npr_source_to_existing_worker_and_second_user(self):
        source = {
            "id": "npr-news", "name": "NPR News", "kind": "rss",
            "url": "https://feeds.npr.org/1001/rss.xml", "enabled": True,
            "priority": 50, "network": "direct", "allowed_article_hosts": ["npr.org"],
        }
        config = {
            "schema_version": 1, "timezone": "UTC", "per_run_limit": 1,
            "daily_limit": 10, "max_inflight": 2, "freshness_hours": 48,
            "sources": [source],
        }
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
                collector = TogetherClient(base, "one", PASSWORD)
                runner = HotspotRunner(config, StateStore(state_dir), client=collector, poll_budget=0)
                report = runner.run("submit")
                self.assertEqual(report["counts"]["submitted"], 1)
                submitted = [x for x in report["items"] if x.get("share_id")]
                self.assertEqual(len(submitted), 1)
                share_id = submitted[0]["share_id"]
                self.assertTrue(worker.once())

                viewer = TogetherClient(base, "two", PASSWORD)
                viewer.login()
                share = next(x for x in viewer.shares() if x["id"] == share_id)
                self.assertEqual(share["status"], "published")
                self.assertTrue(share["url"].startswith("https://www.npr.org/"))
                self.assertTrue(share["title"])
                self.assertTrue(share["blocks"])
                self.assertTrue(share["note"].startswith("[hotspot:v1:"))
            finally:
                http.shutdown()
                http.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
