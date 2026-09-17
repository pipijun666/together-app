from __future__ import annotations

import gzip
import http.client
import io
import ipaddress
import os
import json
import socket
import ssl
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .models import Candidate, resolve_timezone, utc_iso
from .social import fetch_x_trend_posts, parse_telegram_channel, tiktok_creative_observation

MAX_BODY = 2 * 1024 * 1024
MAX_REDIRECTS = 3
READ_CHUNK = 64 * 1024
SENSITIVE_SOURCE_HEADERS = {"authorization", "cookie", "proxy-authorization"}


class SourceError(RuntimeError):
    pass


def public_addresses(host, port):
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = []
    for result in results:
        address = ipaddress.ip_address(result[4][0])
        if not address.is_global:
            raise SourceError("source resolved to non-public address")
        addresses.append(str(address))
    if not addresses:
        raise SourceError("source DNS returned no addresses")
    # Preserve resolver order while deduplicating. Every address was validated
    # above before any connection is attempted, so a mixed public/private DNS
    # answer is rejected as a whole rather than silently falling back.
    return list(dict.fromkeys(addresses))


def source_proxy_endpoint(proxy_url):
    parsed = urllib.parse.urlsplit(str(proxy_url or "").strip())
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise SourceError("HOTSPOT_SOURCE_PROXY must be an unauthenticated local http proxy")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise SourceError("HOTSPOT_SOURCE_PROXY must not contain a path, query or fragment")
    port = parsed.port or 80
    addresses = []
    for result in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(result[4][0])
        local = address.is_loopback or (address.is_private and not address.is_unspecified and not address.is_multicast)
        if not local:
            raise SourceError("HOTSPOT_SOURCE_PROXY must resolve only to local/private addresses")
        addresses.append(str(address))
    if not addresses:
        raise SourceError("HOTSPOT_SOURCE_PROXY DNS returned no addresses")
    return port, list(dict.fromkeys(addresses))


def _proxy_authority(ip, port):
    host = f"[{ip}]" if ":" in ip else ip
    return f"{host}:{port}"


def _open_http_proxy_tunnel(proxy_ip, proxy_port, target_ip, target_port, timeout):
    raw = socket.create_connection((proxy_ip, proxy_port), timeout)
    raw.settimeout(timeout)
    authority = _proxy_authority(target_ip, target_port)
    request = (f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
               "Proxy-Connection: keep-alive\r\n\r\n").encode("ascii")
    raw.sendall(request)
    header = bytearray()
    while b"\r\n\r\n" not in header:
        chunk = raw.recv(1024)
        if not chunk:
            raw.close(); raise OSError("source proxy closed CONNECT response")
        header.extend(chunk)
        if len(header) > 16384:
            raw.close(); raise OSError("source proxy CONNECT response too large")
    first = bytes(header).split(b"\r\n", 1)[0].decode("latin1", "replace")
    parts = first.split()
    if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) != 200:
        raw.close(); raise OSError(f"source proxy CONNECT failed: {first[:120]}")
    return raw


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        deadline = time.monotonic() + self.timeout
        raw = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = raw
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raw.close()
            raise socket.timeout("TLS handshake budget exhausted")
        raw.settimeout(remaining)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class PinnedProxyHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout, proxy_addresses, proxy_port):
        super().__init__(host, port, timeout=timeout)
        self._ip, self._proxy_addresses, self._proxy_port = ip, proxy_addresses, proxy_port

    def connect(self):
        last = None
        for proxy_ip in self._proxy_addresses:
            try:
                self.sock = _open_http_proxy_tunnel(proxy_ip, self._proxy_port, self._ip, self.port, self.timeout)
                return
            except OSError as exc:
                last = exc
        raise last or OSError("source proxy connection failed")


class PinnedProxyHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, proxy_addresses, proxy_port):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip, self._proxy_addresses, self._proxy_port = ip, proxy_addresses, proxy_port

    def connect(self):
        deadline = time.monotonic() + self.timeout
        last = None
        for proxy_ip in self._proxy_addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("source proxy connection budget exhausted")
            try:
                raw = _open_http_proxy_tunnel(proxy_ip, self._proxy_port, self._ip, self.port, remaining)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raw.close(); raise socket.timeout("TLS handshake budget exhausted")
                raw.settimeout(remaining)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except (OSError, ssl.SSLError) as exc:
                last = exc
        raise last or OSError("source proxy connection failed")


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise SourceError("invalid source URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        raise SourceError("source port is not allowed")
    addresses = public_addresses(parsed.hostname, port)
    return parsed, port, addresses


class SafeFetcher:
    def __init__(self, timeout=15, max_bytes=MAX_BODY, proxy_url=None):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.proxy_url = proxy_url

    @staticmethod
    def _remaining(deadline, expired=None):
        if expired is not None and expired.is_set():
            raise SourceError("source fetch timeout")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError("source fetch timeout")
        return remaining

    @classmethod
    def _refresh_socket_timeout(cls, conn, deadline, expired=None):
        remaining = cls._remaining(deadline, expired)
        sock = getattr(conn, "sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        return remaining

    def _read_body(self, response, conn, deadline, expired):
        chunks = []
        total = 0
        reader = getattr(response, "read1", None) or response.read
        while True:
            self._refresh_socket_timeout(conn, deadline, expired)
            try:
                chunk = reader(min(READ_CHUNK, self.max_bytes + 1 - total))
            except socket.timeout as exc:
                raise SourceError("source fetch timeout") from exc
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                if expired.is_set() or deadline - time.monotonic() <= 0:
                    raise SourceError("source fetch timeout") from exc
                raise SourceError("source response read failed") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_bytes:
                raise SourceError("source response too large")
            chunks.append(chunk)
            self._remaining(deadline, expired)
        return b"".join(chunks)

    def _decompress_gzip(self, raw):
        output = bytearray()
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as stream:
                while True:
                    room = self.max_bytes + 1 - len(output)
                    chunk = stream.read(min(READ_CHUNK, room))
                    if not chunk:
                        return bytes(output)
                    output.extend(chunk)
                    if len(output) > self.max_bytes:
                        raise SourceError("decompressed response too large")
        except SourceError:
            raise
        except (OSError, EOFError) as exc:
            raise SourceError("invalid gzip response") from exc

    def __call__(self, url, headers=None):
        current = url
        deadline = time.monotonic() + self.timeout
        proxy = source_proxy_endpoint(self.proxy_url) if self.proxy_url else None
        self._remaining(deadline)
        active = {"conn": None}
        expired = threading.Event()

        def abort_active():
            expired.set()
            conn = active.get("conn")
            sock = getattr(conn, "sock", None) if conn is not None else None
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass
                try:
                    sock.close()
                except (OSError, AttributeError):
                    pass

        watchdog = threading.Timer(self.timeout, abort_active)
        watchdog.daemon = True
        watchdog.start()
        try:
            return self._fetch(url, headers, deadline, active, expired, proxy)
        finally:
            watchdog.cancel()

    def _fetch(self, url, headers, deadline, active, expired, proxy=None):
        current = url
        base_headers = dict(headers or {})
        original = urllib.parse.urlsplit(url)
        original_port = original.port or (443 if original.scheme == "https" else 80)
        credential_origin = (original.scheme.lower(), (original.hostname or "").lower(), original_port)
        for _ in range(MAX_REDIRECTS + 1):
            parsed, port, addresses = validate_url(current)
            self._remaining(deadline, expired)
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            if proxy:
                cls = PinnedProxyHTTPSConnection if parsed.scheme == "https" else PinnedProxyHTTPConnection
            else:
                cls = PinnedHTTPSConnection if parsed.scheme == "https" else PinnedHTTPConnection
            request_headers = {"User-Agent": "TogetherHotspots/1", "Accept-Encoding": "gzip", "Host": parsed.netloc}
            current_origin = (parsed.scheme.lower(), parsed.hostname.lower(), port)
            for key, value in base_headers.items():
                if key.lower() in SENSITIVE_SOURCE_HEADERS and current_origin != credential_origin:
                    continue
                request_headers[key] = value
            response = None
            conn = None
            last_error = None
            for address in addresses:
                remaining = self._remaining(deadline, expired)
                if proxy:
                    proxy_port, proxy_addresses = proxy
                    conn = cls(parsed.hostname, port, address, remaining, proxy_addresses, proxy_port)
                else:
                    conn = cls(parsed.hostname, port, address, remaining)
                active["conn"] = conn
                try:
                    conn.request("GET", path, headers=request_headers)
                    self._refresh_socket_timeout(conn, deadline, expired)
                    response = conn.getresponse()
                    self._remaining(deadline, expired)
                    break
                except socket.timeout as exc:
                    last_error = exc
                    conn.close()
                    conn = None
                    if expired.is_set() or deadline - time.monotonic() <= 0:
                        raise SourceError("source fetch timeout") from exc
                except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                    last_error = exc
                    conn.close()
                    conn = None
                    if expired.is_set():
                        raise SourceError("source fetch timeout") from exc
            if response is None or conn is None:
                if expired.is_set() or deadline - time.monotonic() <= 0:
                    raise SourceError("source fetch timeout") from last_error
                raise SourceError("all source addresses failed") from last_error
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                conn.close()
                if not location:
                    raise SourceError("redirect without Location")
                next_url = urllib.parse.urljoin(current, location)
                next_parsed = urllib.parse.urlsplit(next_url)
                if parsed.scheme == "https" and next_parsed.scheme != "https":
                    raise SourceError("HTTPS source redirect downgrade is not allowed")
                current = next_url
                continue
            if response.status == 304:
                conn.close()
                return b"", response.headers, current, 304
            if response.status != 200:
                conn.close()
                raise SourceError(f"source HTTP {response.status}")
            encoding = (response.getheader("Content-Encoding") or "").lower()
            result_headers = dict(response.getheaders())
            try:
                raw = self._read_body(response, conn, deadline, expired)
            finally:
                conn.close()
            if encoding == "gzip":
                raw = self._decompress_gzip(raw)
            return raw, result_headers, current, 200
        raise SourceError("too many redirects")


def parse_time(value, default_timezone="UTC"):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(str(value))
        except Exception:
            return None
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=resolve_timezone(default_timezone))
        except ValueError:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return utc_iso(parsed)


def child_text(node, names):
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def parse_rss(data, source, now):
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise SourceError("DTD/entities are forbidden")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SourceError("invalid XML feed") from exc
    result = []
    if root.tag.lower().endswith("rss") or root.find("channel") is not None:
        nodes = root.findall("./channel/item")
        for index, node in enumerate(nodes[:50], 1):
            link = child_text(node, ["link"])
            title = child_text(node, ["title"])
            if not link or not title:
                continue
            result.append(Candidate(
                1, source["id"], source["name"], link[:4096], title[:300], utc_iso(now),
                child_text(node, ["guid"]) or None,
                parse_time(child_text(node, ["pubDate", "date"]), source.get("timezone", "UTC")),
                index,
                (child_text(node, ["description"]) or "")[:1000] or None,
            ))
        return result

    namespace = "{http://www.w3.org/2005/Atom}"
    nodes = root.findall(f"./{namespace}entry") or root.findall("./entry")
    for index, node in enumerate(nodes[:50], 1):
        title = child_text(node, [f"{namespace}title", "title"])
        link = ""
        for element in list(node):
            if element.tag.endswith("link") and element.attrib.get("href"):
                if element.attrib.get("rel", "alternate") in ("alternate", ""):
                    link = element.attrib["href"]
                    break
        if not title or not link:
            continue
        result.append(Candidate(
            1, source["id"], source["name"], link[:4096], title[:300], utc_iso(now),
            child_text(node, [f"{namespace}id", "id"]) or None,
            parse_time(child_text(node, [f"{namespace}published", f"{namespace}updated", "published", "updated"]), source.get("timezone", "UTC")),
            index,
            (child_text(node, [f"{namespace}summary", "summary"]) or "")[:1000] or None,
        ))
    return result


def dotted_path(value, path):
    current = value
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def parse_json_source(data, source, now):
    try:
        payload = json.loads(data)
    except Exception as exc:
        raise SourceError("invalid JSON source") from exc
    mapping = source.get("mapping") or {}
    items = dotted_path(payload, mapping.get("items_path", "")) or []
    if not isinstance(items, list):
        raise SourceError("JSON items_path is not a list")
    result = []
    for index, item in enumerate(items[:50], 1):
        url = dotted_path(item, mapping.get("url_path", "url"))
        title = dotted_path(item, mapping.get("title_path", "title"))
        if not isinstance(url, str) or not isinstance(title, str) or not url or not title:
            continue
        raw_rank = dotted_path(item, mapping.get("rank_path", "")) if mapping.get("rank_path") else None
        try:
            rank = int(raw_rank) if raw_rank is not None else None
        except (TypeError, ValueError):
            rank = None
        raw_id = dotted_path(item, mapping.get("id_path", "")) if mapping.get("id_path") else None
        raw_time = dotted_path(item, mapping.get("published_at_path", "")) if mapping.get("published_at_path") else None
        result.append(Candidate(1, source["id"], source["name"], url[:4096], title[:300], utc_iso(now),
                                str(raw_id) if raw_id is not None else None,
                                parse_time(raw_time, source.get("timezone", "UTC")), rank, None))
    return result


def candidate_allowed(candidate, source):
    parsed, _, _ = validate_url(candidate.url)
    allowed = [str(host).lower().rstrip(".") for host in source.get("allowed_article_hosts", [])]
    if allowed:
        host = parsed.hostname.lower().rstrip(".")
        if not any(host == item or host.endswith("." + item) for item in allowed):
            raise SourceError("article host is outside source allowlist")
    return True


def fetcher_for_source(source_config):
    network = source_config.get("network", "direct")
    if network == "direct":
        return SafeFetcher()
    if network == "source_proxy":
        proxy_url = os.environ.get("HOTSPOT_SOURCE_PROXY", "").strip()
        if not proxy_url:
            raise SourceError("HOTSPOT_SOURCE_PROXY is required for source_proxy network")
        return SafeFetcher(proxy_url=proxy_url)
    raise SourceError(f"unsupported hotspot source network: {network}")


def fetch_source(source_config, *, fetcher=None, now=None, validator=None):
    if not source_config.get("enabled", False):
        return [], []
    if source_config.get("network", "direct") not in ("direct", "source_proxy"):
        raise SourceError("unsupported hotspot source network")
    now = now or datetime.now(timezone.utc)
    fetcher = fetcher or fetcher_for_source(source_config)
    kind = source_config.get("kind")
    observations = []

    if kind == "x_trends":
        try:
            candidates = fetch_x_trend_posts(source_config, fetcher, now)
        except (ValueError, json.JSONDecodeError) as exc:
            raise SourceError(str(exc)) from exc
    else:
        raw, _headers, _final_url, status = fetcher(source_config["url"])
        if status == 304:
            return [], []
        if kind in ("rss", "atom"):
            candidates = parse_rss(raw, source_config, now)
        elif kind == "json":
            candidates = parse_json_source(raw, source_config, now)
        elif kind == "telegram_public_channel":
            candidates = parse_telegram_channel(raw, source_config, now)
        elif kind == "tiktok_creative_center":
            candidates = []
            observations.append(tiktok_creative_observation(raw, source_config, now))
        else:
            raise SourceError(f"unsupported source kind: {kind}")

    check = validator or candidate_allowed
    safe = []
    for candidate in candidates:
        check(candidate, source_config)
        safe.append(candidate)
    return safe, observations


def fetch_candidates(source_config, *, fetcher=None, now=None, validator=None):
    return fetch_source(source_config, fetcher=fetcher, now=now, validator=validator)[0]
