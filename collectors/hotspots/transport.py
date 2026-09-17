from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol, runtime_checkable

from .client import APIError, NoRedirect
from .models import Candidate


@runtime_checkable
class CollectorTransport(Protocol):
    requires_login: bool
    def login(self): ...
    def system(self): ...
    def create_candidate(self, candidate: Candidate, note: str): ...
    def poll(self, poll_url: str): ...
    def reconcile_marker(self, marker: str, url: str): ...


class SystemHotspotTransport:
    requires_login = False

    def __init__(self, base, token, timeout=20):
        self.base = str(base or "").rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("HOTSPOT_SYSTEM_API_BASE must be loopback http(s)")
        if len(str(token or "")) < 32:
            raise ValueError("HOTSPOT_SERVICE_TOKEN must contain at least 32 characters")
        self.token = str(token)
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def login(self):
        return None

    def _request(self, path, data=None, method=None):
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path.startswith("/"):
            raise ValueError("invalid system hotspot API path")
        target = self.base + parsed.path
        payload = None if data is None else json.dumps(
            data, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "User-Agent": "TogetherHotspots/1",
            "Authorization": "Bearer " + self.token,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            target, data=payload, headers=headers,
            method=method or ("POST" if payload is not None else "GET"),
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            raise APIError(exc.code, body.get("error") or f"HTTP {exc.code}", body) from exc

    def system(self):
        return self._request("/internal/hotspots/status")

    def create_candidate(self, candidate: Candidate, note: str):
        source = {
            key: value for key, value in {
                "source_id": candidate.source_id,
                "source_name": candidate.source_name,
                "source_item_id": candidate.source_item_id,
                "region": candidate.region,
                "coverage": candidate.coverage,
                "metric_name": candidate.metric_name,
                "metric_value": candidate.metric_value,
                "metric_observed_at": candidate.metric_observed_at,
            }.items() if value is not None
        }
        return self._request("/internal/hotspots", {"text": candidate.url, "note": note, "source": source})
    def poll(self, poll_url: str):
        parsed = urllib.parse.urlsplit(poll_url)
        if (
            parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            or not re.fullmatch(r"/internal/hotspots/[0-9a-f]{32}", parsed.path)
        ):
            raise ValueError("invalid system hotspot poll_url")
        return self._request(parsed.path)

    def reconcile_marker(self, marker: str, url: str):
        response = self._request(
            "/internal/hotspots/reconcile", {"marker": marker, "url": url}
        )
        match = response.get("match")
        if match is not None and not isinstance(match, dict):
            raise ValueError("invalid hotspot reconcile response")
        return match
