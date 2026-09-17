import gzip
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.hotspots.client import APIError, TogetherClient
from collectors.hotspots.models import Candidate
from collectors.hotspots.runner import HotspotRunner, fair_select, load_config
from collectors.hotspots.sources import (PinnedHTTPSConnection, SafeFetcher, SourceError,
                                           fetch_candidates, public_addresses)
from collectors.hotspots.state import StateStore, dedup_key, normalize_url

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "hotspots"
NOW = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)


def candidate(url="https://news.example.com/a?utm_source=x&sig=keep", item_id="a-1"):
    return Candidate(1, "fixture", "Fixture News", url, "Fixture title",
                     "2026-09-14T03:00:00Z", item_id,
                     "2026-09-14T02:00:00Z", 1, "summary")


class FixtureFetcher:
    def __init__(self, payload):
        self.payload = payload

    def __call__(self, url, headers=None):
        return self.payload, {}, url, 200


class FakeClient:
    def __init__(self, fail_create=False):
        self.fail_create = fail_create
        self.create_calls = 0
        self.user_id = "one"

    def login(self):
        return {"user": {"id": "one"}}

    def system(self):
        return {"worker_recent_contact": True}

    def create(self, url, note):
        self.create_calls += 1
        if self.fail_create:
            raise OSError("response lost")
        return {"id": "a" * 32, "status": "queued"}

    def reconcile_marker(self, marker, normalized_url):
        return None

    def poll(self, poll_url):
        return {"status": "queued"}


class StatusClient(FakeClient):
    def __init__(self, create_status=None, poll_status="queued"):
        super().__init__(False)
        self.create_status = create_status
        self.poll_status = poll_status
        self.poll_calls = 0

    def create(self, url, note):
        self.create_calls += 1
        if isinstance(self.create_status, int):
            raise APIError(self.create_status, f"HTTP {self.create_status}")
        return {"id": (hex(self.create_calls)[2:] * 32)[:32].ljust(32, "a"), "status": "queued"}

    def poll(self, poll_url):
        self.poll_calls += 1
        return {"status": self.poll_status}


class HotspotCoreTests(unittest.TestCase):
    def test_normalize_keeps_unknown_query_order(self):
        url = "HTTPS://News.Example.com:443/A?z=2&utm_source=x&sig=a%2Bb&x=1#part"
        self.assertEqual(normalize_url(url),
                         "https://news.example.com/A?z=2&sig=a%2Bb&x=1")
        self.assertEqual(len(dedup_key(url)), 64)

    def test_rss_and_json_fixtures(self):
        rss = (FIXTURES / "rss.xml").read_bytes()
        source = {"id": "rss", "name": "RSS", "kind": "rss", "url": "https://feed.example.com/rss",
                  "enabled": True, "network": "direct", "allowed_article_hosts": ["news.example.com"]}
        items = fetch_candidates(source, fetcher=FixtureFetcher(rss), now=NOW,
                                 validator=lambda item, cfg: True)
        self.assertEqual([x.source_item_id for x in items], ["a-1", "a-2"])
        self.assertEqual(items[0].title_hint, "First story")

        payload = (FIXTURES / "ranking.json").read_bytes()
        source.update({"id": "json", "kind": "json", "url": "https://feed.example.com/hot",
                       "mapping": {"items_path": "data.items", "url_path": "url", "title_path": "title",
                                   "id_path": "id", "published_at_path": "published_at", "rank_path": "rank"}})
        items = fetch_candidates(source, fetcher=FixtureFetcher(payload), now=NOW,
                                 validator=lambda item, cfg: True)
        self.assertEqual([x.rank for x in items], [1, 2])
        self.assertEqual(items[0].source_item_id, "j1")

    def test_private_dns_is_rejected(self):
        with patch("collectors.hotspots.sources.socket.getaddrinfo",
                   return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(SourceError):
                public_addresses("source.example", 443)

    def test_source_address_fallback_ipv6_failure_then_ipv4_success(self):
        answers = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]
        attempts = []

        class Response:
            status = 200
            headers = {}
            def __init__(self):
                self.done = False
            def getheader(self, key, default=None):
                return default
            def getheaders(self):
                return []
            def read(self, _size):
                if self.done:
                    return b""
                self.done = True
                return b"ok"

        class Connection:
            def __init__(self, host, port, ip, timeout):
                attempts.append((host, port, ip, timeout))
                self.ip = ip
            def request(self, *_args, **_kwargs):
                if ":" in self.ip:
                    raise OSError("IPv6 route unavailable")
            def getresponse(self):
                return Response()
            def close(self):
                pass

        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=answers), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", Connection):
            raw, _, final_url, status = SafeFetcher(timeout=15)("https://feed.example.com/rss")
        self.assertEqual((raw, final_url, status), (b"ok", "https://feed.example.com/rss", 200))
        self.assertEqual([entry[2] for entry in attempts],
                         ["2606:4700:4700::1111", "93.184.216.34"])
        self.assertTrue(all(0 < entry[3] <= 15 for entry in attempts))

    def test_source_address_fallback_all_fail_and_private_mix_rejected(self):
        public = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]

        class FailingConnection:
            def __init__(self, host, port, ip, timeout):
                pass
            def request(self, *_args, **_kwargs):
                raise OSError("unreachable")
            def close(self):
                pass

        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=public), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", FailingConnection):
            with self.assertRaisesRegex(SourceError, "all source addresses failed"):
                SafeFetcher(timeout=15)("https://feed.example.com/rss")

        mixed = public + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=mixed), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection") as connection:
            with self.assertRaisesRegex(SourceError, "non-public"):
                SafeFetcher(timeout=15)("https://feed.example.com/rss")
            connection.assert_not_called()

    def test_source_redirect_revalidates_dns_before_second_hop(self):
        calls = []

        def resolver(host, port, **_kwargs):
            if host == "feed.example.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        class RedirectResponse:
            status = 302
            def getheader(self, key, default=None):
                return "https://private.example/secret" if key == "Location" else default

        class Connection:
            def __init__(self, host, port, ip, timeout):
                calls.append((host, ip))
            def request(self, *_args, **_kwargs):
                pass
            def getresponse(self):
                return RedirectResponse()
            def close(self):
                pass

        with patch("collectors.hotspots.sources.socket.getaddrinfo", side_effect=resolver), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", Connection):
            with self.assertRaisesRegex(SourceError, "non-public"):
                SafeFetcher(timeout=15)("https://feed.example.com/rss")
        self.assertEqual(calls, [("feed.example.com", "93.184.216.34")])

    def test_source_https_redirect_downgrade_is_rejected(self):
        class RedirectResponse:
            status = 302
            def getheader(self, key, default=None):
                return "http://public.example/final" if key == "Location" else default
        class Connection:
            def __init__(self, host, port, ip, timeout): self.sock = None
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return RedirectResponse()
            def close(self): pass
        answer=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=answer), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", Connection):
            with self.assertRaisesRegex(SourceError, "redirect downgrade"):
                SafeFetcher(timeout=15)("https://feed.example.com/rss")

    def test_source_address_fallback_respects_total_timeout_budget(self):
        answers = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]
        attempts = []

        clock = [100.0]
        class FailingConnection:
            def __init__(self, host, port, ip, timeout):
                attempts.append((ip, timeout))
                self.sock = None
            def request(self, *_args, **_kwargs):
                clock[0] = 116.0
                raise OSError("unreachable")
            def close(self):
                pass

        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=answers), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", FailingConnection), \
             patch("collectors.hotspots.sources.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(SourceError, "source fetch timeout"):
                SafeFetcher(timeout=15)("https://feed.example.com/rss")
        self.assertEqual(attempts, [("2606:4700:4700::1111", 15.0)])

    def test_pinned_https_connects_to_validated_ip_with_original_sni(self):
        connection = PinnedHTTPSConnection("feed.example.com", 443, "93.184.216.34", 7)
        class RawSocket:
            def __init__(self):
                self.timeouts = []
            def settimeout(self, value):
                self.timeouts.append(value)
            def close(self):
                pass
        raw_socket = RawSocket()
        with patch("collectors.hotspots.sources.socket.create_connection", return_value=raw_socket) as create, \
             patch.object(connection._context, "wrap_socket", return_value=object()) as wrap:
            connection.connect()
        create.assert_called_once_with(("93.184.216.34", 443), 7)
        wrap.assert_called_once_with(raw_socket, server_hostname="feed.example.com")
        self.assertTrue(raw_socket.timeouts and 0 < raw_socket.timeouts[-1] <= 7)

    def test_pinned_https_tls_uses_remaining_connect_budget(self):
        clock = [100.0]
        observed = []
        class RawSocket:
            def settimeout(self, value):
                observed.append(value)
            def close(self):
                pass
        raw = RawSocket()
        connection = PinnedHTTPSConnection("feed.example.com", 443, "93.184.216.34", 7)
        def connect(*_args, **_kwargs):
            clock[0] += 4.0
            return raw
        with patch("collectors.hotspots.sources.time.monotonic", side_effect=lambda: clock[0]), \
             patch("collectors.hotspots.sources.socket.create_connection", side_effect=connect), \
             patch.object(connection._context, "wrap_socket", return_value=object()):
            connection.connect()
        self.assertEqual(len(observed), 1)
        self.assertAlmostEqual(observed[0], 3.0, places=6)

    def test_source_header_phase_uses_remaining_budget(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        clock = [100.0]
        timeouts = []
        class Sock:
            def settimeout(self, value):
                timeouts.append(value)
            def shutdown(self, *_):
                pass
            def close(self):
                pass
        class Connection:
            def __init__(self, host, port, ip, timeout):
                self.sock = Sock()
            def request(self, *_args, **_kwargs):
                clock[0] += 4.0
            def getresponse(self):
                self.assert_header_timeout = timeouts[-1]
                clock[0] += 12.0
                raise socket.timeout("slow headers")
            def close(self):
                pass
        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=answers), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", Connection), \
             patch("collectors.hotspots.sources.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(SourceError, "source fetch timeout"):
                SafeFetcher(timeout=15)("https://feed.example.com/rss")
        self.assertTrue(any(abs(value - 11.0) < 1e-6 for value in timeouts))

    def test_source_watchdog_aborts_stalled_response_headers(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        aborted = threading.Event()
        class Sock:
            def settimeout(self, _value):
                pass
            def shutdown(self, *_):
                aborted.set()
            def close(self):
                aborted.set()
        class Connection:
            def __init__(self, host, port, ip, timeout):
                self.sock = Sock()
            def request(self, *_args, **_kwargs):
                pass
            def getresponse(self):
                if aborted.wait(1.0):
                    raise OSError("socket closed by deadline watchdog")
                raise AssertionError("watchdog did not abort stalled headers")
            def close(self):
                aborted.set()
        started = time.monotonic()
        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=answers), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", Connection):
            with self.assertRaisesRegex(SourceError, "source fetch timeout"):
                SafeFetcher(timeout=0.05)("https://feed.example.com/rss")
        elapsed = time.monotonic() - started
        self.assertTrue(aborted.is_set())
        self.assertLess(elapsed, 0.5)

    def test_source_watchdog_aborts_stalled_body_read(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        aborted = threading.Event()
        class Sock:
            def settimeout(self, _value): pass
            def shutdown(self, *_): aborted.set()
            def close(self): aborted.set()
        class Response:
            status = 200
            headers = {}
            def getheader(self, key, default=None): return default
            def getheaders(self): return []
            def read1(self, _size):
                if aborted.wait(1.0):
                    raise OSError("socket closed by deadline watchdog")
                raise AssertionError("watchdog did not abort stalled body")
        class Connection:
            def __init__(self, host, port, ip, timeout): self.sock = Sock()
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return Response()
            def close(self): aborted.set()
        started = time.monotonic()
        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=answers), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", Connection):
            with self.assertRaisesRegex(SourceError, "source fetch timeout"):
                SafeFetcher(timeout=0.05)("https://feed.example.com/rss")
        self.assertTrue(aborted.is_set())
        self.assertLess(time.monotonic() - started, 0.5)

    def test_source_slow_chunked_body_cannot_extend_deadline(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        clock = [100.0]
        timeouts = []
        class Sock:
            def settimeout(self, value):
                timeouts.append(value)
            def shutdown(self, *_):
                pass
            def close(self):
                pass
        class Response:
            status = 200
            headers = {}
            def getheader(self, key, default=None):
                return default
            def getheaders(self):
                return []
            def read1(self, _size):
                clock[0] += 4.0
                return b"x"
        class Connection:
            def __init__(self, host, port, ip, timeout):
                self.sock = Sock()
            def request(self, *_args, **_kwargs):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=answers), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", Connection), \
             patch("collectors.hotspots.sources.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(SourceError, "source fetch timeout"):
                SafeFetcher(timeout=15)("https://feed.example.com/rss")
        self.assertTrue(any(value <= 3.0 for value in timeouts))

    def test_gzip_expansion_is_bounded_before_full_decompression(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        payload = gzip.compress(b"A" * 4096)
        self.assertLess(len(payload), 1024)
        class Sock:
            def settimeout(self, _value):
                pass
            def shutdown(self, *_):
                pass
            def close(self):
                pass
        class Response:
            status = 200
            headers = {}
            def __init__(self):
                self.sent = False
            def getheader(self, key, default=None):
                return "gzip" if key == "Content-Encoding" else default
            def getheaders(self):
                return [("Content-Encoding", "gzip")]
            def read1(self, _size):
                if self.sent:
                    return b""
                self.sent = True
                return payload
        class Connection:
            def __init__(self, host, port, ip, timeout):
                self.sock = Sock()
            def request(self, *_args, **_kwargs):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        with patch("collectors.hotspots.sources.socket.getaddrinfo", return_value=answers), \
             patch("collectors.hotspots.sources.PinnedHTTPSConnection", Connection):
            with self.assertRaisesRegex(SourceError, "decompressed response too large"):
                SafeFetcher(timeout=15, max_bytes=1024)("https://feed.example.com/rss")

    def test_dry_run_does_not_write_state(self):
        cfg = {"schema_version": 1, "per_run_limit": 3, "daily_limit": 10,
               "max_inflight": 2, "freshness_hours": 48,
               "sources": [{"id": "fixture", "name": "Fixture News", "kind": "rss",
                            "url": "https://feed.example.com/rss", "enabled": True,
                            "priority": 50, "network": "direct",
                            "allowed_article_hosts": ["news.example.com"]}]}
        rss = (FIXTURES / "rss.xml").read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            state = StateStore(folder)
            runner = HotspotRunner(cfg, state, fetcher=FixtureFetcher(rss),
                                   validator=lambda item, source: True, now_fn=lambda: NOW)
            report = runner.run("dry-run")
            self.assertGreater(report["counts"]["discovered"], 0)
            with state.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM quota").fetchone()[0], 0)

    def test_unknown_submit_is_not_blindly_reposted(self):
        cfg = {"schema_version": 1, "per_run_limit": 1, "daily_limit": 10,
               "max_inflight": 2, "freshness_hours": 48,
               "sources": [{"id": "fixture", "name": "Fixture News", "kind": "rss",
                            "url": "https://feed.example.com/rss", "enabled": True,
                            "priority": 50, "network": "direct",
                            "allowed_article_hosts": ["news.example.com"]}]}
        rss = b'<rss version="2.0"><channel><item><guid>a-1</guid><title>Only story</title><link>https://news.example.com/a</link><pubDate>Sun, 14 Sep 2026 02:00:00 GMT</pubDate></item></channel></rss>'
        client = FakeClient(fail_create=True)
        with tempfile.TemporaryDirectory() as folder:
            state = StateStore(folder)
            runner = HotspotRunner(cfg, state, fetcher=FixtureFetcher(rss),
                                   validator=lambda item, source: True, client=client,
                                   now_fn=lambda: NOW)
            runner.run("submit")
            self.assertEqual(client.create_calls, 1)
            runner.run("submit")
            self.assertEqual(client.create_calls, 1)
            self.assertEqual(state.inflight()[0]["status"], "submit_unknown")

    def test_fair_select_prefers_rank_then_newer_unranked(self):
        a = candidate("https://news.example.com/a", "a")
        b = Candidate(1, "fixture", "Fixture News", "https://news.example.com/b", "B",
                      "2026-09-14T03:00:00Z", "b", "2026-09-14T02:30:00Z", None, None)
        c = Candidate(1, "fixture", "Fixture News", "https://news.example.com/c", "C",
                      "2026-09-14T03:00:00Z", "c", "2026-09-14T01:30:00Z", None, None)
        selected = fair_select([(c, 50), (b, 50), (a, 50)], 3)
        self.assertEqual([x.source_item_id for x in selected], ["a", "b", "c"])

    def test_api_client_sends_stable_user_agent(self):
        observed = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed["user_agent"] = self.headers.get("User-Agent")
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *_):
                pass

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            client = TogetherClient(f"http://127.0.0.1:{http.server_address[1]}", "one", "password")
            self.assertEqual(client._request("/api/system")[1], {"ok": True})
        finally:
            http.shutdown(); http.server_close()
        self.assertEqual(observed["user_agent"], "TogetherHotspots/1")

    def test_config_limits_use_defaults_and_explicit_hard_caps(self):
        def load(payload):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "hotspots.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                return load_config(path)

        defaults = load({"schema_version": 1, "sources": []})
        self.assertEqual((defaults["per_run_limit"], defaults["daily_limit"], defaults["max_inflight"]),
                         (3, 10, 2))
        maximums = load({"schema_version": 1, "sources": [], "per_run_limit": 20,
                         "daily_limit": 100, "max_inflight": 10})
        self.assertEqual((maximums["per_run_limit"], maximums["daily_limit"], maximums["max_inflight"]),
                         (20, 100, 10))
        for key, value in (("per_run_limit", 21), ("daily_limit", 101), ("max_inflight", 11),
                           ("per_run_limit", -1), ("daily_limit", -1), ("max_inflight", -1),
                           ("per_run_limit", "3"), ("daily_limit", 1.5), ("max_inflight", True)):
            with self.subTest(key=key, value=value):
                with self.assertRaises((TypeError, ValueError)):
                    load({"schema_version": 1, "sources": [], key: value})

    def test_api_redirects_are_not_followed_and_poll_path_is_strict(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "https://evil.example/api/system")
                self.end_headers()
            def log_message(self, *_):
                pass
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            client = TogetherClient(f"http://127.0.0.1:{http.server_address[1]}", "one", "password")
            with self.assertRaises(APIError) as caught:
                client._request("/api/system")
            self.assertEqual(caught.exception.status, 302)
        finally:
            http.shutdown(); http.server_close()

        client = TogetherClient("http://127.0.0.1:8765", "one", "password")
        with patch.object(client, "_authenticated", return_value={"status": "queued"}) as auth:
            good = "/api/link-preview/" + "a" * 32
            self.assertEqual(client.poll(good)["status"], "queued")
            auth.assert_called_once_with(good)
        for bad in (
            "/api/link-preview/abc",
            "/api/link-preview/" + "g" * 32,
            "/api/link-preview/" + "a" * 32 + "/extra",
            "/api/link-preview/" + "a" * 32 + "?x=1",
            "https://evil.example/api/link-preview/" + "a" * 32,
            "//evil.example/api/link-preview/" + "a" * 32,
        ):
            with self.assertRaises(ValueError):
                client.poll(bad)

    def test_source_item_id_is_unique_in_state(self):
        with tempfile.TemporaryDirectory() as folder:
            state = StateStore(folder)
            first = candidate("https://news.example.com/a", "stable")
            state.discover(first, dedup_key(first.url), normalize_url(first.url))
            other = candidate("https://news.example.com/changed", "stable")
            self.assertIsNotNone(state.existing_source_item("fixture", "stable"))
            state.discover(other, dedup_key(other.url), normalize_url(other.url))
            with state.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 1)

    def test_authenticated_request_relogs_once_only(self):
        client = TogetherClient("http://127.0.0.1:8765", "one", "password")
        with patch.object(client, "_request", side_effect=[
            APIError(401, "expired"),
            (200, {"user": {"id": "one"}}),
            (200, {"ok": True}),
        ]) as request:
            self.assertTrue(client.system()["ok"])
            self.assertEqual(request.call_count, 3)
        with patch.object(client, "_request", side_effect=[
            APIError(401, "expired"),
            (200, {"user": {"id": "one"}}),
            APIError(401, "still expired"),
        ]) as request:
            with self.assertRaises(APIError) as caught:
                client.system()
            self.assertEqual(caught.exception.status, 401)
            self.assertEqual(request.call_count, 3)

    def test_429_503_507_pause_new_submissions(self):
        rss = (FIXTURES / "rss.xml").read_bytes()
        cfg = {"schema_version": 1, "timezone": "Asia/Shanghai", "per_run_limit": 2,
               "daily_limit": 10, "max_inflight": 3, "freshness_hours": 48,
               "sources": [{"id": "fixture", "name": "Fixture News", "kind": "rss",
                            "url": "https://feed.example.com/rss", "enabled": True,
                            "priority": 50, "network": "direct",
                            "allowed_article_hosts": ["news.example.com"]}]}
        for status in (429, 503, 507):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                state = StateStore(folder)
                client = StatusClient(create_status=status)
                runner = HotspotRunner(cfg, state, fetcher=FixtureFetcher(rss),
                                       validator=lambda item, source: True, client=client,
                                       now_fn=lambda: NOW, poll_budget=0)
                runner.run("submit")
                self.assertEqual(client.create_calls, 1)
                with state.db() as db:
                    rows = list(db.execute("SELECT status,last_error FROM items ORDER BY first_seen"))
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(row["status"] == "deferred" for row in rows))
                self.assertIn(rows[1]["last_error"], ("round_paused", "max_inflight"))

    def test_poll_budget_exits_with_pending(self):
        rss = b'<rss version="2.0"><channel><item><guid>a-1</guid><title>Only story</title><link>https://news.example.com/a</link><pubDate>Sun, 14 Sep 2026 02:00:00 GMT</pubDate></item></channel></rss>'
        cfg = {"schema_version": 1, "timezone": "UTC", "per_run_limit": 1, "daily_limit": 10,
               "max_inflight": 2, "freshness_hours": 48,
               "sources": [{"id": "fixture", "name": "Fixture News", "kind": "rss",
                            "url": "https://feed.example.com/rss", "enabled": True,
                            "priority": 50, "network": "direct",
                            "allowed_article_hosts": ["news.example.com"]}]}
        ticks = iter([0.0, 0.1, 1.0, 2.1, 2.2, 2.3])
        with tempfile.TemporaryDirectory() as folder:
            state = StateStore(folder); client = StatusClient()
            runner = HotspotRunner(cfg, state, fetcher=FixtureFetcher(rss),
                                   validator=lambda item, source: True, client=client,
                                   now_fn=lambda: NOW, sleep_fn=lambda _: None,
                                   monotonic_fn=lambda: next(ticks, 3.0),
                                   poll_budget=2, poll_interval=1)
            report = runner.run("submit")
            self.assertEqual(report["counts"]["submitted"], 1)
            self.assertEqual(report["counts"]["pending"], 1)
            self.assertGreaterEqual(client.poll_calls, 1)
            self.assertEqual(state.inflight()[0]["status"], "queued")

    def test_etag_304_uses_conditional_headers(self):
        rss = (FIXTURES / "rss.xml").read_bytes()
        calls = []
        class ConditionalFetcher:
            def __call__(self, url, headers=None):
                calls.append(dict(headers or {}))
                if len(calls) == 1:
                    return rss, {"ETag": '"v1"', "Last-Modified": "Sun, 14 Sep 2026 02:00:00 GMT"}, url, 200
                return b"", {"ETag": '"v1"'}, url, 304
        cfg = {"schema_version": 1, "timezone": "UTC", "per_run_limit": 3, "daily_limit": 10,
               "max_inflight": 2, "freshness_hours": 48,
               "sources": [{"id": "fixture", "name": "Fixture News", "kind": "rss",
                            "url": "https://feed.example.com/rss", "enabled": True,
                            "priority": 50, "network": "direct",
                            "allowed_article_hosts": ["news.example.com"]}]}
        with tempfile.TemporaryDirectory() as folder:
            state = StateStore(folder)
            runner = HotspotRunner(cfg, state, fetcher=ConditionalFetcher(),
                                   validator=lambda item, source: True, now_fn=lambda: NOW,
                                   sleep_fn=lambda _: None)
            first, first_errors = runner.collect(); second, second_errors = runner.collect()
            self.assertEqual(len(first), 2); self.assertFalse(first_errors)
            self.assertEqual(second, []); self.assertFalse(second_errors)
            self.assertEqual(calls[1]["If-None-Match"], '"v1"')
            self.assertEqual(calls[1]["If-Modified-Since"], "Sun, 14 Sep 2026 02:00:00 GMT")

    def test_daily_quota_uses_configured_timezone_day(self):
        rss = b'<rss version="2.0"><channel><item><guid>a-1</guid><title>Only story</title><link>https://news.example.com/a</link><pubDate>Mon, 14 Sep 2026 16:00:00 GMT</pubDate></item></channel></rss>'
        now = datetime(2026, 9, 14, 16, 30, tzinfo=timezone.utc)
        cfg = {"schema_version": 1, "timezone": "Asia/Shanghai", "per_run_limit": 1, "daily_limit": 1,
               "max_inflight": 2, "freshness_hours": 48,
               "sources": [{"id": "fixture", "name": "Fixture News", "kind": "rss",
                            "url": "https://feed.example.com/rss", "enabled": True,
                            "priority": 50, "network": "direct",
                            "allowed_article_hosts": ["news.example.com"]}]}
        with tempfile.TemporaryDirectory() as folder:
            state = StateStore(folder); client = StatusClient()
            runner = HotspotRunner(cfg, state, fetcher=FixtureFetcher(rss),
                                   validator=lambda item, source: True, client=client,
                                   now_fn=lambda: now, poll_budget=0)
            runner.run("submit")
            with state.db() as db:
                days = [row["day"] for row in db.execute("SELECT day FROM quota")]
            self.assertEqual(days, ["2026-09-15"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
