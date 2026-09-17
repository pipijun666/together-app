import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from server import App, handler_for
from collectors.hotspots.models import Candidate
from collectors.hotspots.runner import HotspotRunner, build_from_env
from collectors.hotspots.state import StateStore
from worker import Worker
from collectors.hotspots.transport import SystemHotspotTransport

WORKER_TOKEN = "worker-" + "w" * 64
HOTSPOT_TOKEN = "hotspot-" + "h" * 64
PASSWORD = "system-core-test-password"
MARKER = "[hotspot:v1:" + "a" * 64 + "]"


def request(port, method, path, body=None, token=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if body is not None:
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    try: result = json.loads(raw) if raw else None
    except (UnicodeDecodeError,json.JSONDecodeError): result = raw
    set_cookie = response.getheader("Set-Cookie")
    conn.close()
    return response.status, result, set_cookie


def login(port, user):
    status, body, set_cookie = request(
        port, "POST", "/api/login", {"user": user, "password": PASSWORD}
    )
    assert status == 200, body
    cookie = SimpleCookie(set_cookie)
    return "session=" + cookie["session"].value


class SystemCoreTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.app = App(self.data.name, WORKER_TOKEN, hotspot_token=HOTSPOT_TOKEN)
        self.app.add_user("one", "One", PASSWORD)
        self.app.add_user("two", "Two", PASSWORD)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.app))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.http.server_address[1]
        self.one = login(self.port, "one")
        self.two = login(self.port, "two")
    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.data.cleanup()

    def create_system(self, status="queued"):
        payload = {
            "text": "https://example.com/system-story",
            "note": MARKER + " 系统热点",
            "source": {"source_id": "fixture", "source_name": "Fixture", "coverage": "test"},
        }
        code, body, _ = request(
            self.port, "POST", "/internal/hotspots", payload, token=HOTSPOT_TOKEN
        )
        self.assertEqual(code, 200, body)
        if status != "queued":
            with self.app.db() as db:
                db.execute(
                    "UPDATE shares SET status=?,title='System title',blocks='[{\"type\":\"paragraph\",\"text\":\"body\"}]' WHERE id=?",
                    (status, body["id"]),
                )
        return body["id"], body

    def test_system_create_poll_reconcile_and_source_kind_filters(self):
        sid, created = self.create_system("published")
        self.assertEqual(created["poll_url"], "/internal/hotspots/" + sid)
        code, polled, _ = request(self.port, "GET", created["poll_url"], token=HOTSPOT_TOKEN)
        self.assertEqual((code, polled["status"]), (200, "published"))

        code, found, _ = request(
            self.port, "POST", "/internal/hotspots/reconcile",
            {"marker": MARKER, "url": "https://example.com/system-story"}, token=HOTSPOT_TOKEN,
        )
        self.assertEqual(code, 200)
        self.assertEqual(found["match"]["id"], sid)

        code, system_list, _ = request(
            self.port, "GET", "/api/shares?source_kind=system", cookie=self.one
        )
        self.assertEqual(code, 200)
        self.assertEqual([item["id"] for item in system_list["shares"]], [sid])
        self.assertEqual(system_list["shares"][0]["author"], "system")
        self.assertEqual(system_list["shares"][0]["author_name"], "系统推送")
        self.assertEqual(system_list["shares"][0]["source_kind"], "system")

        code, manual_list, _ = request(
            self.port, "GET", "/api/shares?source_kind=manual", cookie=self.one
        )
        self.assertEqual(code, 200)
        self.assertEqual(manual_list["shares"], [])

    def test_public_create_cannot_forge_system_identity(self):
        for path, payload in (
            ("/api/shares", {"manual": True, "body": "x", "source_kind": "system"}),
            ("/api/link-preview", {"text": "https://example.com/x", "author": "system"}),
        ):
            code, body, _ = request(self.port, "POST", path, payload, cookie=self.one)
            self.assertEqual(code, 400, body)

    def test_internal_hotspot_auth_is_separate(self):
        payload = {"text": "https://example.com/x", "note": MARKER}
        code, _, _ = request(self.port, "POST", "/internal/hotspots", payload, token=WORKER_TOKEN)
        self.assertEqual(code, 401)
        code, _, _ = request(self.port, "POST", "/internal/hotspots", payload)
        self.assertEqual(code, 401)

        class FakeHandler:
            client_address = ("10.0.0.8", 1234)
            headers = {"Authorization": "Bearer " + HOTSPOT_TOKEN}
        with self.assertRaises(Exception) as caught:
            self.app.hotspots(FakeHandler(), "POST", "/internal/hotspots", payload)
        self.assertEqual(getattr(caught.exception, "status", None), 403)

    def test_system_permissions_and_failed_visibility(self):
        published, _ = self.create_system("published")
        failed, _ = self.create_system("failed")
        code, body, _ = request(self.port, "GET", "/api/shares?source_kind=system", cookie=self.two)
        self.assertEqual(code, 200)
        self.assertEqual([x["id"] for x in body["shares"]], [published])
        code, _, _ = request(
            self.port, "POST", f"/api/shares/{published}/comment", {"body": "ok"}, cookie=self.two
        )
        self.assertEqual(code, 200)
        code, _, _ = request(
            self.port, "POST", f"/api/shares/{published}/edit", {"body": "no"}, cookie=self.two
        )
        self.assertIn(code, (403, 404))
        code, _, _ = request(self.port, "GET", f"/api/shares/{failed}", cookie=self.one)
        self.assertEqual(code, 404)
        code, _, _ = request(self.port, "DELETE", f"/api/shares/{published}", {}, cookie=self.one)
        self.assertEqual(code, 200)
    def test_media_visibility_respects_manual_private_and_system_published_rules(self):
        aid_manual = "b" * 64
        aid_system_failed = "c" * 64
        aid_system_ok = "d" * 64
        manual_id = "3" * 32
        failed_id = "4" * 32
        published_id = "5" * 32
        with self.app.db() as db:
            db.execute("INSERT INTO shares(id,author,url,title,note,status,created,source_kind) VALUES(?,?,?,?,?,'review',1,'manual')", (manual_id,'one','', 'Manual',''))
            db.execute("INSERT INTO shares(id,author,url,title,note,status,created,source_kind) VALUES(?,?,?,?,?,'failed',1,'system')", (failed_id,'system','https://example.com/f','Failed',''))
            db.execute("INSERT INTO shares(id,author,url,title,note,status,created,source_kind) VALUES(?,?,?,?,?,'published',1,'system')", (published_id,'system','https://example.com/p','Published',''))
            for aid,sid in ((aid_manual,manual_id),(aid_system_failed,failed_id),(aid_system_ok,published_id)):
                db.execute("INSERT INTO assets(id,share_id,mime,size,kind,object_key,sha256) VALUES(?,?,?,4,'image','','')",(aid,sid,'image/jpeg'))
                (self.app.media/aid).write_bytes(b'test')
        self.assertEqual(request(self.port,'GET','/media/'+aid_manual,cookie=self.two)[0],404)
        self.assertEqual(request(self.port,'GET','/media/'+aid_manual,cookie=self.one)[0],200)
        self.assertEqual(request(self.port,'GET','/media/'+aid_system_failed,cookie=self.one)[0],404)
        self.assertEqual(request(self.port,'GET','/media/'+aid_system_ok,cookie=self.one)[0],200)
        self.assertEqual(request(self.port,'GET','/media/'+aid_system_ok,cookie=self.two)[0],200)

    def test_worker_complete_preserves_collector_metadata(self):
        sid, _ = self.create_system("queued")
        lease = "lease-token"
        with self.app.db() as db:
            db.execute(
                "UPDATE shares SET status='fetching',lease=?,lease_until=? WHERE id=?",
                (lease, 9999999999, sid),
            )
        payload = {
            "id": sid, "lease": lease, "title": "Parsed title",
            "blocks": [{"type": "paragraph", "text": "Parsed body"}],
            "warnings": [],
            "metadata": {"author": "Original Author", "platform": "web", "canonical_url": "https://example.com/system-story"},
        }
        code, body, _ = request(
            self.port, "POST", "/internal/jobs/complete", payload, token=WORKER_TOKEN
        )
        self.assertEqual(code, 200, body)
        with self.app.db() as db:
            row = db.execute("SELECT metadata FROM shares WHERE id=?", (sid,)).fetchone()
        metadata = json.loads(row["metadata"])
        self.assertEqual(metadata["author"], "Original Author")
        self.assertEqual(metadata["collector"]["source_id"], "fixture")

    def test_build_from_env_never_silently_falls_back_to_personal_account(self):
        config = Path(self.data.name) / "hotspots.json"
        config.write_text('{"schema_version":1,"sources":[]}', encoding="utf-8")
        legacy = {"HOTSPOT_API_BASE": f"http://127.0.0.1:{self.port}", "HOTSPOT_USER_ID": "one", "HOTSPOT_USER_PASSWORD": PASSWORD}
        with patch.dict(os.environ, legacy, clear=True):
            self.assertIsNone(build_from_env(config, Path(self.data.name)/"state-a").client)
        system = {"HOTSPOT_SYSTEM_API_BASE": f"http://127.0.0.1:{self.port}", "HOTSPOT_SERVICE_TOKEN": HOTSPOT_TOKEN}
        with patch.dict(os.environ, system, clear=True):
            runner = build_from_env(config, Path(self.data.name)/"state-b")
        self.assertIsInstance(runner.client, SystemHotspotTransport)

    def test_system_http_transport_uses_only_internal_contract(self):
        transport = SystemHotspotTransport(f"http://127.0.0.1:{self.port}", HOTSPOT_TOKEN)
        self.assertIn("worker_recent_contact", transport.system())
        candidate = Candidate(1,"fixture","Fixture","https://example.com/transport","Transport story","2026-09-16T03:00:00Z","post-1",None,1,None,coverage="test")
        created = transport.create_candidate(candidate, MARKER + " transport")
        self.assertTrue(created["poll_url"].startswith("/internal/hotspots/"))
        self.assertEqual(transport.poll(created["poll_url"])["status"], "queued")
        match = transport.reconcile_marker(MARKER, candidate.url)
        self.assertEqual(match["id"], created["id"])
        with self.assertRaises(ValueError):
            SystemHotspotTransport("https://payload.example", HOTSPOT_TOKEN)

    def test_system_identity_runner_transport_worker_published_visible_in_system_only(self):
        state_dir = Path(self.data.name) / "system-e2e-state"
        feed = b"""<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>
<item><guid>system-e2e-1</guid><title>System identity E2E story</title>
<link>https://news.example.com/system-e2e</link><pubDate>Wed, 16 Sep 2026 03:30:00 GMT</pubDate></item>
</channel></rss>"""

        class FixtureFetcher:
            def __call__(self, url, headers=None):
                return feed, {}, url, 200

        config = {
            "schema_version": 1, "timezone": "UTC", "per_run_limit": 1,
            "daily_limit": 10, "max_inflight": 2, "freshness_hours": 48,
            "sources": [{
                "id": "system-e2e", "name": "System E2E", "kind": "rss",
                "url": "https://feed.example.com/rss", "enabled": True,
                "priority": 50, "network": "direct",
                "allowed_article_hosts": ["news.example.com"],
            }],
        }
        worker = Worker(f"http://127.0.0.1:{self.port}", WORKER_TOKEN)
        worker.api("/internal/heartbeat", {})
        transport = SystemHotspotTransport(
            f"http://127.0.0.1:{self.port}", HOTSPOT_TOKEN
        )
        runner = HotspotRunner(
            config, StateStore(state_dir), fetcher=FixtureFetcher(),
            validator=lambda item, source: True, transport=transport, poll_budget=0,
            now_fn=lambda: __import__('datetime').datetime(
                2026, 9, 16, 4, 0, tzinfo=__import__('datetime').timezone.utc
            ),
        )
        report = runner.run("submit")
        self.assertEqual(report["counts"]["submitted"], 1)
        self.assertEqual(report["counts"]["pending"], 1)
        sid = report["items"][0]["share_id"]

        html = b"<html><title>System parsed headline</title><article><h1>System parsed headline</h1><p>" + (
            b"System identity end to end body. " * 12
        ) + b"</p></article></html>"
        with patch(
            "worker.fetch_public",
            return_value=(html, "text/html; charset=utf-8", "https://news.example.com/system-e2e"),
        ):
            self.assertTrue(worker.once())

        for cookie in (self.one, self.two):
            code, body, _ = request(
                self.port, "GET", "/api/shares?source_kind=system", cookie=cookie
            )
            self.assertEqual(code, 200)
            self.assertEqual([item["id"] for item in body["shares"]], [sid])
            share = body["shares"][0]
            self.assertEqual(share["status"], "published")
            self.assertEqual(share["source_kind"], "system")
            self.assertEqual(share["author"], "system")
            self.assertEqual(share["author_name"], "\u7cfb\u7edf\u63a8\u9001")
            code, manual, _ = request(
                self.port, "GET", "/api/shares?source_kind=manual", cookie=cookie
            )
            self.assertEqual(code, 200)
            self.assertEqual(manual["shares"], [])

    def test_history_migration_requires_complete_evidence_and_is_idempotent(self):
        manual_id = "1" * 32
        ambiguous_id = "2" * 32
        with self.app.db() as db:
            for sid, url, note in (
                (manual_id, "https://example.com/old", MARKER + " old"),
                (ambiguous_id, "https://example.com/mismatch", MARKER + " mismatch"),
            ):
                db.execute(
                    "INSERT INTO shares(id,author,url,title,note,status,created) VALUES(?,?,?,?,?,'published',?)",
                    (sid, "one", url, "Legacy", note, 1.0),
                )
        state = Path(self.data.name) / "collector-state.sqlite3"
        db = sqlite3.connect(state)
        db.execute("CREATE TABLE items(share_id TEXT,note_marker TEXT,candidate_json TEXT)")
        db.execute(
            "INSERT INTO items VALUES(?,?,?)",
            (manual_id, MARKER, json.dumps({"url": "https://example.com/old", "source_id": "legacy", "source_item_id": "a"})),
        )
        db.execute(
            "INSERT INTO items VALUES(?,?,?)",
            (ambiguous_id, MARKER, json.dumps({"url": "https://example.com/not-the-share", "source_id": "legacy", "source_item_id": "b"})),
        )
        db.commit(); db.close()

        plan = self.app.migrate_hotspot_history(state, apply=False)
        self.assertEqual(plan["eligible"], [manual_id])
        self.assertIn(ambiguous_id, plan["skipped"])
        applied = self.app.migrate_hotspot_history(state, apply=True)
        self.assertEqual(applied["migrated"], [manual_id])
        again = self.app.migrate_hotspot_history(state, apply=True)
        self.assertEqual(again["migrated"], [])
        with self.app.db() as db:
            row = db.execute("SELECT author,source_kind FROM shares WHERE id=?", (manual_id,)).fetchone()
            audit = db.execute(
                "SELECT previous_author FROM system_migration_audit WHERE share_id=?", (manual_id,)
            ).fetchone()
        self.assertEqual((row["author"], row["source_kind"]), ("system", "system"))
        self.assertEqual(audit["previous_author"], "one")


if __name__ == "__main__":
    unittest.main(verbosity=2)
