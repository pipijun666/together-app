from __future__ import annotations

import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .state import normalize_url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class APIError(RuntimeError):
    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class TogetherClient:
    def __init__(self, base, user_id, password, timeout=20):
        self.base = base.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("invalid HOTSPOT_API_BASE")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("non-loopback API must use HTTPS")
        self.user_id = user_id
        self.password = password
        self.timeout = timeout
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPCookieProcessor(jar))

    def _request(self, path, data=None, method=None):
        target = urllib.parse.urljoin(self.base + "/", path.lstrip("/"))
        expected = urllib.parse.urlsplit(self.base)
        actual = urllib.parse.urlsplit(target)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise ValueError("cross-origin API path rejected")
        payload = None if data is None else json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": "application/json", "User-Agent": "TogetherHotspots/1"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(target, data=payload, headers=headers,
                                         method=method or ("POST" if payload is not None else "GET"))
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            raise APIError(exc.code, body.get("error") or f"HTTP {exc.code}", body) from exc

    def login(self):
        return self._request("/api/login", {"user": self.user_id, "password": self.password})[1]

    def _authenticated(self, path, data=None, method=None):
        try:
            return self._request(path, data, method)[1]
        except APIError as exc:
            if exc.status != 401:
                raise
        self.login()
        return self._request(path, data, method)[1]

    def system(self):
        return self._authenticated("/api/system")

    def create(self, url, note):
        return self._authenticated("/api/link-preview", {"text": url, "note": note})

    def poll(self, poll_url):
        parsed = urllib.parse.urlsplit(poll_url)
        if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                or not re.fullmatch(r"/api/link-preview/[0-9a-f]{32}", parsed.path)):
            raise ValueError("invalid poll_url")
        return self._authenticated(parsed.path)

    def shares(self):
        return self._authenticated("/api/shares").get("shares", [])

    def reconcile_marker(self, marker, normalized_url):
        matches = []
        for share in self.shares():
            try:
                same_url = normalize_url(share.get("url", "")) == normalized_url
            except ValueError:
                same_url = False
            if (share.get("note", "").startswith(marker) and same_url
                    and share.get("author") == self.user_id):
                matches.append(share)
        if len(matches) == 1:
            return matches[0]
        return None
