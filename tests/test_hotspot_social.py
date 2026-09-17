import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.hotspots.runner import HotspotRunner
from collectors.hotspots.sources import (SafeFetcher, SourceError, fetch_candidates, fetch_source,
                                         fetcher_for_source, source_proxy_endpoint)
from collectors.hotspots.state import StateStore

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


class FixtureFetcher:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, headers=None):
        self.calls.append((url, dict(headers or {})))
        payload = self.payload(url) if callable(self.payload) else self.payload
        return payload, {}, url, 200


TELEGRAM_HTML = b'''<html><body>
<div class="tgme_widget_message js-widget_message" data-post="example/101">
<div class="tgme_widget_message_text js-message_text">First <b>public</b> update</div>
<span class="tgme_widget_message_views">1.2K</span>
<a class="tgme_widget_message_date"><time datetime="2026-09-15T10:00:00+00:00">10:00</time></a></div>
<div class="tgme_widget_message js-widget_message" data-post="example/102">
<div class="tgme_widget_message_text js-message_text">Second<br/>breaking update</div>
<span class="tgme_widget_message_views">3.4K</span>
<a class="tgme_widget_message_date"><time datetime="2026-09-15T11:00:00Z">11:00</time></a></div>
</body></html>'''


class HotspotSocialTests(unittest.TestCase):
    def telegram_source(self, **extra):
        source = {
            "id": "tg-example", "name": "Telegram Example", "kind": "telegram_public_channel",
            "url": "https://t.me/s/example", "enabled": True, "network": "direct",
            "channel": "example", "allowed_article_hosts": ["t.me"], "region": "global",
            "coverage": "selected_public_channel",
        }
        source.update(extra)
        return source

    def test_telegram_public_channel_returns_ranked_post_permalinks(self):
        items = fetch_candidates(
            self.telegram_source(), fetcher=FixtureFetcher(TELEGRAM_HTML), now=NOW,
            validator=lambda item, source: True,
        )
        self.assertEqual([x.source_item_id for x in items], ["example/102", "example/101"])
        self.assertEqual(items[0].url, "https://t.me/example/102")
        self.assertEqual(items[0].rank, 1)
        self.assertEqual(items[0].metric_name, "views")
        self.assertEqual(items[0].metric_value, 3400)
        self.assertEqual(items[0].coverage, "selected_public_channel")
        self.assertEqual(items[0].published_at, "2026-09-15T11:00:00Z")
        self.assertIn("Second breaking update", items[0].title_hint)

    def test_telegram_channel_filter_rejects_other_channel_messages(self):
        html = TELEGRAM_HTML.replace(b"example/101", b"other/101")
        items = fetch_candidates(
            self.telegram_source(), fetcher=FixtureFetcher(html), now=NOW,
            validator=lambda item, source: True,
        )
        self.assertEqual([x.source_item_id for x in items], ["example/102"])


    def x_source(self, **extra):
        source = {
            "id": "x-world", "name": "X Worldwide Trends", "kind": "x_trends",
            "url": "https://api.x.com/2/trends/by/woeid/1", "enabled": True,
            "network": "direct", "allowed_article_hosts": ["x.com"], "region": "worldwide",
        }
        source.update(extra)
        return source

    def test_x_trends_require_bearer_token_without_leaking_config_secret(self):
        with patch.dict(os.environ, {"X_API_BEARER_TOKEN": ""}, clear=False):
            with self.assertRaisesRegex(SourceError, "bearer token is not configured"):
                fetch_candidates(self.x_source(), fetcher=FixtureFetcher(b"{}"), now=NOW,
                                 validator=lambda item, source: True)

    def test_x_trends_resolve_topic_to_public_post_permalink(self):
        def payload(url):
            if "/trends/by/woeid/1" in url:
                return json.dumps({"data": [{"trend_name": "AI", "tweet_count": 12000}]}).encode()
            if "/tweets/search/recent?" in url:
                return json.dumps({
                    "data": [
                        {"id": "123", "text": "AI public post", "author_id": "7",
                         "created_at": "2026-09-15T11:30:00Z",
                         "public_metrics": {"like_count": 20, "retweet_count": 5}},
                        {"id": "124", "text": "Lower engagement", "author_id": "8",
                         "created_at": "2026-09-15T11:20:00Z",
                         "public_metrics": {"like_count": 1}},
                    ],
                    "includes": {"users": [{"id": "7", "username": "example_user"}]},
                }).encode()
            raise AssertionError(url)
        fetcher = FixtureFetcher(payload)
        with patch.dict(os.environ, {"X_API_BEARER_TOKEN": "unit-test-token"}, clear=False):
            items = fetch_candidates(self.x_source(), fetcher=fetcher, now=NOW,
                                     validator=lambda item, source: True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://x.com/example_user/status/123")
        self.assertEqual((items[0].rank, items[0].metric_name, items[0].metric_value),
                         (1, "trend_tweet_count", 12000.0))
        self.assertTrue(all(call[1].get("Authorization") == "Bearer unit-test-token" for call in fetcher.calls))


    def test_tiktok_creative_center_stays_observation_only_without_post_permalink(self):
        source = {
            "id": "tiktok-us", "name": "TikTok Creative Center", "kind": "tiktok_creative_center",
            "url": "https://ads.tiktok.com/creative/creativeCenter/trends/video?period=7&region=US",
            "enabled": True, "network": "direct", "region": "US",
        }
        items, observations = fetch_source(source, fetcher=FixtureFetcher(b"<html>Creative Center Trends</html>"),
                                           now=NOW, validator=lambda item, cfg: True)
        self.assertEqual(items, [])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["status"], "topic_only")
        self.assertEqual(observations[0]["original_post_url_count"], 0)
        self.assertEqual(observations[0]["coverage"], "creative_center_trends")

    def test_tiktok_observed_permalink_is_not_auto_published(self):
        source = {
            "id": "tiktok-us", "name": "TikTok Creative Center", "kind": "tiktok_creative_center",
            "url": "https://ads.tiktok.com/creative/creativeCenter/trends/video?period=7&region=US",
            "enabled": True, "network": "direct",
        }
        body = b'<a href="https://www.tiktok.com/@creator/video/1234567890123456789">video</a>'
        items, observations = fetch_source(source, fetcher=FixtureFetcher(body), now=NOW,
                                           validator=lambda item, cfg: True)
        self.assertEqual(items, [])
        self.assertEqual(observations[0]["status"], "permalinks_observed_not_published")
        self.assertEqual(observations[0]["original_post_url_count"], 1)


    def test_safe_fetcher_drops_sensitive_headers_on_cross_origin_redirect(self):
        attempts = []

        def resolver(host, port, **_kwargs):
            ip = "93.184.216.34" if host == "api.x.com" else "93.184.216.35"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

        class Sock:
            def settimeout(self, _value): pass
            def shutdown(self, *_): pass
            def close(self): pass

        class RedirectResponse:
            status = 302
            def getheader(self, key, default=None):
                return "https://other.example/final" if key == "Location" else default

        class OkResponse:
            status = 200
            headers = {}
            def __init__(self): self.done = False
            def getheader(self, _key, default=None): return default
            def getheaders(self): return []
            def read(self, _size):
                if self.done: return b""
                self.done = True
                return b"ok"

        class Connection:
            def __init__(self, host, port, ip, timeout):
                self.host = host
                self.sock = Sock()
            def request(self, _method, _path, headers=None):
                attempts.append((self.host, dict(headers or {})))
            def getresponse(self):
                return RedirectResponse() if self.host == "api.x.com" else OkResponse()
            def close(self): pass

        with patch("collectors.hotspots.sources.socket.getaddrinfo", side_effect=resolver), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", Connection):
            raw, _, final_url, status = SafeFetcher(timeout=3)(
                "https://api.x.com/start",
                headers={"Authorization": "Bearer secret", "Cookie": "private=1", "X-Test": "keep"},
            )
        self.assertEqual((raw, final_url, status), (b"ok", "https://other.example/final", 200))
        self.assertEqual(attempts[0][1]["Authorization"], "Bearer secret")
        self.assertEqual(attempts[0][1]["Cookie"], "private=1")
        self.assertNotIn("Authorization", attempts[1][1])
        self.assertNotIn("Cookie", attempts[1][1])
        self.assertEqual(attempts[1][1]["X-Test"], "keep")


    def test_dry_run_reports_source_scope_last_success_and_metric(self):
        source = self.telegram_source(priority=50)
        config = {
            "schema_version": 1, "timezone": "UTC", "per_run_limit": 1,
            "daily_limit": 10, "max_inflight": 2, "freshness_hours": 48,
            "sources": [source],
        }
        with tempfile.TemporaryDirectory() as folder:
            runner = HotspotRunner(config, StateStore(folder), fetcher=FixtureFetcher(TELEGRAM_HTML),
                                   validator=lambda item, cfg: True, now_fn=lambda: NOW,
                                   sleep_fn=lambda _seconds: None)
            report = runner.run("dry-run")
        self.assertEqual(report["sources"][0]["coverage"], "selected_public_channel")
        self.assertEqual(report["sources"][0]["status"], "ok")
        self.assertTrue(report["sources"][0]["last_success_at"].endswith("Z"))
        selected = next(item for item in report["items"] if item.get("reason") == "dry_run")
        self.assertEqual(selected["coverage"], "selected_public_channel")
        self.assertEqual(selected["metric_name"], "views")
        self.assertEqual(selected["metric_value"], 3400.0)
    def test_source_proxy_requires_explicit_environment(self):
        source = self.telegram_source(network="source_proxy")
        with patch.dict(os.environ, {"HOTSPOT_SOURCE_PROXY": ""}, clear=False):
            with self.assertRaisesRegex(SourceError, "HOTSPOT_SOURCE_PROXY is required"):
                fetcher_for_source(source)

    def test_source_proxy_endpoint_rejects_public_remote_proxy(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 7897))]
        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=answer):
            with self.assertRaisesRegex(SourceError, "local/private"):
                source_proxy_endpoint("http://proxy.example:7897")

    def test_source_proxy_endpoint_rejects_credentials(self):
        with self.assertRaisesRegex(SourceError, "unauthenticated local http proxy"):
            source_proxy_endpoint("http://user:secret@127.0.0.1:7897")
    def test_source_proxy_uses_validated_target_ip(self):
        attempts = []
        def resolver(host, port, **_kwargs):
            ip = "127.0.0.1" if host == "localhost" else "93.184.216.34"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        class Response:
            status = 200
            headers = {}
            done = False
            def getheader(self, _key, default=None): return default
            def getheaders(self): return []
            def read(self, _size):
                if self.done: return b""
                self.done = True; return b"ok"
        class Connection:
            def __init__(self, host, port, ip, timeout, proxy_addresses, proxy_port):
                attempts.append((host, port, ip, tuple(proxy_addresses), proxy_port)); self.sock = None
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch("collectors.hotspots.sources.socket.getaddrinfo", side_effect=resolver), \
             patch("collectors.hotspots.sources.PinnedProxyHTTPSConnection", Connection):
            raw, _, _, status = SafeFetcher(proxy_url="http://localhost:7897")("https://feed.example.com/rss")
        self.assertEqual((raw, status), (b"ok", 200))
        self.assertEqual(attempts, [("feed.example.com", 443, "93.184.216.34", ("127.0.0.1",), 7897)])
    def test_system_transport_skips_personal_login_and_receives_candidate(self):
        class SystemFixture:
            requires_login = False
            def __init__(self): self.created = []
            def login(self): raise AssertionError("system transport must not use personal login")
            def system(self): return {"worker_recent_contact": True}
            def reconcile_marker(self, _marker, _url): return None
            def poll(self, _url): raise AssertionError("published fixture must not poll")
            def create_candidate(self, candidate, note):
                self.created.append((candidate, note))
                return {"id": "f" * 32, "status": "published"}
        source = self.telegram_source(priority=50)
        config = {"schema_version": 1, "timezone": "UTC", "per_run_limit": 1,
                  "daily_limit": 10, "max_inflight": 2, "freshness_hours": 48,
                  "sources": [source]}
        with tempfile.TemporaryDirectory() as folder:
            transport = SystemFixture()
            runner = HotspotRunner(config, StateStore(folder), transport=transport,
                                   fetcher=FixtureFetcher(TELEGRAM_HTML), validator=lambda item, cfg: True,
                                   now_fn=lambda: NOW, sleep_fn=lambda _seconds: None, poll_budget=0)
            report = runner.run("submit")
        self.assertEqual((report["counts"]["submitted"], report["counts"]["published"]), (1, 1))
        self.assertEqual(len(transport.created), 1)
        candidate, note = transport.created[0]
        self.assertEqual(candidate.coverage, "selected_public_channel")
        self.assertTrue(note.startswith("[hotspot:v1:"))


    def test_system_transport_uses_returned_poll_url_and_persists_it(self):
        class SystemQueued:
            requires_login = False
            def __init__(self): self.polls = []
            def login(self): raise AssertionError("system transport must not login")
            def system(self): return {"worker_recent_contact": True}
            def reconcile_marker(self, _marker, _url): return None
            def create_candidate(self, _candidate, _note):
                return {"id":"e"*32,"status":"queued","poll_url":"/internal/hotspots/task-1"}
            def poll(self, target): self.polls.append(target); return {"status":"published"}
        source=self.telegram_source(priority=50)
        config={"schema_version":1,"timezone":"UTC","per_run_limit":1,"daily_limit":10,
                "max_inflight":2,"freshness_hours":48,"sources":[source]}
        with tempfile.TemporaryDirectory() as folder:
            state=StateStore(folder); transport=SystemQueued()
            runner=HotspotRunner(config,state,transport=transport,fetcher=FixtureFetcher(TELEGRAM_HTML),
                validator=lambda item,cfg: True,now_fn=lambda: NOW,sleep_fn=lambda _s:None,poll_budget=1)
            report=runner.run("submit")
            with state.db() as db: key=db.execute("SELECT dedup_key FROM items WHERE share_id IS NOT NULL").fetchone()[0]
            row=state.existing(key)
        self.assertEqual(transport.polls,["/internal/hotspots/task-1"])
        self.assertEqual(report["counts"]["published"],1)
        self.assertEqual(row["poll_url"],"/internal/hotspots/task-1")


    def test_system_transport_never_guesses_or_follows_unsafe_poll_url(self):
        for supplied in (None, "https://evil.example/task"):
            class SystemQueued:
                requires_login=False
                def __init__(self): self.polls=[]
                def login(self): raise AssertionError("must not login")
                def system(self): return {"worker_recent_contact":True}
                def reconcile_marker(self,_marker,_url): return None
                def create_candidate(self,_candidate,_note):
                    result={"id":"d"*32,"status":"queued"}
                    if supplied is not None: result["poll_url"]=supplied
                    return result
                def poll(self,target): self.polls.append(target); raise AssertionError("unsafe poll")
            source=self.telegram_source(priority=50)
            config={"schema_version":1,"timezone":"UTC","per_run_limit":1,"daily_limit":10,
                    "max_inflight":2,"freshness_hours":48,"sources":[source]}
            with self.subTest(poll_url=supplied), tempfile.TemporaryDirectory() as folder:
                state=StateStore(folder); transport=SystemQueued()
                runner=HotspotRunner(config,state,transport=transport,fetcher=FixtureFetcher(TELEGRAM_HTML),
                    validator=lambda i,c:True,now_fn=lambda:NOW,sleep_fn=lambda _s:None,poll_budget=1)
                report=runner.run("submit")
                with state.db() as db: row=dict(db.execute("SELECT * FROM items").fetchone())
                self.assertEqual(transport.polls,[])
                self.assertEqual((report["counts"]["submitted"],report["counts"]["pending"]),(1,1))
                self.assertTrue(row["last_error"])
