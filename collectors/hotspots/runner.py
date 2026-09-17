from __future__ import annotations

import json
import os
import random
import subprocess
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import APIError, TogetherClient
from .models import resolve_timezone, utc_iso
from .sources import SafeFetcher, fetch_source, fetcher_for_source
from .transport import SystemHotspotTransport
from .state import StateStore, dedup_key, normalize_url, RETRYABLE_LOCAL_STATES


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config.get("schema_version") != 1:
        raise ValueError("unsupported config schema_version")
    limits = (("per_run_limit", 3, 20), ("daily_limit", 10, 100), ("max_inflight", 2, 10))
    for key, default, maximum in limits:
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        if value < 0 or value > maximum:
            raise ValueError(f"{key} out of range")
        config[key] = value
    config["freshness_hours"] = min(max(int(config.get("freshness_hours", 48)), 1), 168)
    if not isinstance(config.get("sources", []), list):
        raise ValueError("sources must be a list")
    resolve_timezone(config.get("timezone", "UTC"))
    return config


def source_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def public_report_url(url):
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def is_fresh(candidate, now, hours):
    if not candidate.published_at:
        return True
    try:
        published = datetime.fromisoformat(candidate.published_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return published >= now - timedelta(hours=hours)


def fair_select(entries, limit):
    grouped = {}
    priorities = {}
    for candidate, priority in entries:
        grouped.setdefault(candidate.source_id, []).append(candidate)
        priorities[candidate.source_id] = max(priorities.get(candidate.source_id, priority), priority)
    def candidate_order(item):
        if item.rank is not None:
            return (0, item.rank, 0.0, item.url)
        if item.published_at:
            try:
                stamp = datetime.fromisoformat(item.published_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                stamp = 0.0
        else:
            stamp = 0.0
        return (1, 0, -stamp, item.url)
    for candidates in grouped.values():
        candidates.sort(key=candidate_order)
    order = sorted(grouped, key=lambda source_id: (-priorities[source_id], source_id))
    selected = []
    while order and len(selected) < limit:
        next_order = []
        for source_id in order:
            if grouped[source_id] and len(selected) < limit:
                selected.append(grouped[source_id].pop(0))
            if grouped[source_id]:
                next_order.append(source_id)
        order = next_order
    return selected


class HotspotRunner:
    def __init__(self, config, state, *, fetcher=None, validator=None, client=None, transport=None,
                 now_fn=None, sleep_fn=time.sleep, monotonic_fn=time.monotonic,
                 poll_budget=300, poll_interval=10):
        self.config = config
        self.state = state
        self.fetcher = fetcher
        self.validator = validator
        if client is not None and transport is not None:
            raise ValueError("provide client or transport, not both")
        self.client = transport or client
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep_fn
        self.monotonic = monotonic_fn
        self.poll_budget = poll_budget
        self.poll_interval = poll_interval

    def _transport_login(self):
        if getattr(self.client, "requires_login", True):
            self.client.login()

    def _transport_create(self, candidate, note):
        create_candidate = getattr(self.client, "create_candidate", None)
        if create_candidate is not None:
            return create_candidate(candidate, note)
        return self.client.create(candidate.url, note)

    def _poll_target(self, share_id, poll_url=None):
        if poll_url:
            parsed = urllib.parse.urlsplit(poll_url)
            if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//") or parsed.fragment:
                raise ValueError("poll_url must be a same-origin absolute path")
            return poll_url
        if getattr(self.client, "requires_login", True):
            return "/api/link-preview/" + share_id
        raise RuntimeError("system transport task has no poll_url")

    def collect(self):
        now = self.now_fn()
        entries = []
        errors = []
        self.source_observations = []
        enabled = [s for s in self.config.get("sources", []) if s.get("enabled", False)]
        for source_index, source in enumerate(enabled):
            source = dict(source)
            source.setdefault("timezone", self.config.get("timezone", "UTC"))
            try:
                base_fetcher = self.fetcher or fetcher_for_source(source)
            except Exception as exc:
                errors.append({"source_id": source.get("id", "?"), "error": str(exc)})
                if source_index + 1 < len(enabled):
                    self.sleep(1)
                continue
            cached = self.state.source_cache(source.get("id", ""))
            conditional = {}
            if cached.get("etag"):
                conditional["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                conditional["If-Modified-Since"] = cached["last_modified"]
            captured = {}

            def cached_fetcher(url, headers=None):
                merged = dict(headers or {})
                if url == source.get("url"):
                    merged = {**conditional, **merged}
                response = base_fetcher(url, headers=merged)
                if url == source.get("url"):
                    captured["headers"] = response[1]
                    captured["status"] = response[3]
                return response

            last_error = None
            candidates = None
            for attempt in range(2):
                try:
                    candidates, observations = fetch_source(source, fetcher=cached_fetcher, now=now,
                                                           validator=self.validator)
                    self.source_observations.extend(observations)
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt == 0:
                        self.sleep(1)
            if candidates is None:
                errors.append({"source_id": source.get("id", "?"), "error": str(last_error)})
            else:
                headers = captured.get("headers") or {}
                if captured.get("status") in (200, 304):
                    self.state.update_source_cache(
                        source.get("id", ""),
                        headers.get("ETag") or cached.get("etag"),
                        headers.get("Last-Modified") or cached.get("last_modified"),
                    )
                entries.extend((item, int(source.get("priority", 50))) for item in candidates)
            if source_index + 1 < len(enabled):
                self.sleep(1)
        return entries, errors

    def new_report(self, mode, started):
        return {
            "schema_version": 1,
            "run_id": str(uuid.uuid4()),
            "started_at": utc_iso(started),
            "finished_at": None,
            "mode": mode,
            "source_commit": source_commit(),
            "counts": {
                "discovered": 0,
                "deduplicated": 0,
                "submitted": 0,
                "published": 0,
                "failed": 0,
                "pending": 0,
                "unknown": 0,
            },
            "items": [],
            "source_errors": [],
            "source_observations": [],
            "sources": [],
        }

    def _record_existing(self, report, candidate, key, existing):
        report["counts"]["deduplicated"] += 1
        report["items"].append({
            "dedup_key": key,
            "source_id": candidate.source_id,
            "share_id": existing.get("share_id"),
            "status": existing["status"],
            "reason": "already_seen",
        })

    def _resume_existing(self, report):
        for item in self.state.inflight():
            key = item["dedup_key"]
            if item["status"] == "submitting":
                self.state.set_status(key, "submit_unknown", error="crash_recovery")
                item["status"] = "submit_unknown"
            if item["status"] == "submit_unknown":
                try: candidate_url=json.loads(item.get("candidate_json") or "{}").get("url") or item["normalized_url"]
                except (TypeError,json.JSONDecodeError): candidate_url=item["normalized_url"]
                match = self.client.reconcile_marker(item.get("note_marker") or "", candidate_url)
                if not match:
                    report["counts"]["unknown"] += 1
                    continue
                self.state.set_status(key, match.get("status", "queued"), share_id=match.get("id"),
                                      poll_url=match.get("poll_url"))
                item["share_id"] = match.get("id")
                item["poll_url"] = match.get("poll_url") or item.get("poll_url")
            if not item.get("share_id"):
                continue
            try:
                poll_target = self._poll_target(item["share_id"], item.get("poll_url"))
            except (TypeError, ValueError, RuntimeError) as exc:
                self.state.set_status(key, item.get("status", "queued"), error=str(exc))
                report["counts"]["pending"] += 1
                continue
            try:
                result = self.client.poll(poll_target)
            except APIError as exc:
                if exc.status == 404:
                    self.state.set_status(key, "deleted_or_missing", error="poll_404")
                else:
                    report["counts"]["pending"] += 1
                continue
            status = result.get("status", "queued")
            self.state.set_status(key, status, share_id=item["share_id"], error=result.get("error"))
            if status == "published": report["counts"]["published"] += 1
            elif status in ("failed", "review"): report["counts"]["failed"] += 1
            else: report["counts"]["pending"] += 1


    def _poll_submitted(self, report, submitted):
        if not submitted or self.poll_budget <= 0:
            report["counts"]["pending"] += len(submitted)
            return
        deadline = self.monotonic() + self.poll_budget
        pending = dict(submitted)
        while pending and self.monotonic() < deadline:
            for key, task in list(pending.items()):
                share_id, poll_target = task
                try:
                    result = self.client.poll(poll_target)
                except APIError as exc:
                    if exc.status == 404:
                        self.state.set_status(key, "deleted_or_missing", error="poll_404")
                        pending.pop(key, None)
                    continue
                except Exception:
                    continue
                status = result.get("status", "queued")
                self.state.set_status(key, status, share_id=share_id, error=result.get("error"))
                if status == "published":
                    report["counts"]["published"] += 1
                    pending.pop(key, None)
                elif status in ("failed", "review"):
                    report["counts"]["failed"] += 1
                    pending.pop(key, None)
            if pending and self.monotonic() < deadline:
                jitter = random.uniform(-1.0, 1.0)
                self.sleep(max(0.1, self.poll_interval + jitter))
        report["counts"]["pending"] += len(pending)

    def run(self, mode="dry-run"):
        started = self.now_fn()
        report = self.new_report(mode, started)
        if mode == "submit":
            self.state.recover_crashed_submitting()
        entries, errors = self.collect()
        report["source_errors"] = errors
        report["source_observations"] = list(getattr(self, "source_observations", []))
        error_ids = {item.get("source_id") for item in errors}
        for source in (s for s in self.config.get("sources", []) if s.get("enabled", False)):
            cache = self.state.source_cache(source.get("id", ""))
            updated = cache.get("updated")
            last_success = utc_iso(datetime.fromtimestamp(updated, timezone.utc)) if updated else None
            report["sources"].append({
                "source_id": source.get("id"),
                "kind": source.get("kind"),
                "coverage": source.get("coverage"),
                "last_success_at": last_success,
                "status": "error" if source.get("id") in error_ids else "ok",
            })
        seen = set()
        eligible = []
        for candidate, priority in entries:
            try:
                normalized = normalize_url(candidate.url)
                key = dedup_key(candidate.url)
            except Exception as exc:
                report["items"].append({"source_id": candidate.source_id,
                                        "status": "rejected", "reason": str(exc)})
                continue
            if key in seen:
                report["counts"]["deduplicated"] += 1
                continue
            seen.add(key)
            existing_key = self.state.existing(key)
            existing_source = self.state.existing_source_item(candidate.source_id, candidate.source_item_id)
            existing = existing_key or existing_source
            if mode != "dry-run" and not existing_source:
                self.state.discover(candidate, key, normalized)
            report["counts"]["discovered"] += 1
            if existing_source and existing_source["dedup_key"] != key:
                self._record_existing(report, candidate, key, existing_source)
                continue
            if existing and existing["status"] not in RETRYABLE_LOCAL_STATES:
                self._record_existing(report, candidate, key, existing)
                continue
            first_seen = existing.get("first_seen") if existing else started.timestamp()
            fresh = is_fresh(candidate, started, self.config["freshness_hours"])
            if not candidate.published_at and started.timestamp() - first_seen > self.config["freshness_hours"] * 3600:
                fresh = False
            if not fresh:
                if mode != "dry-run":
                    self.state.set_status(key, "rejected", error="stale")
                report["items"].append({"dedup_key": key, "source_id": candidate.source_id,
                                        "share_id": None, "status": "rejected", "reason": "stale"})
                continue
            eligible.append((candidate, priority))

        selected = fair_select(eligible, self.config["per_run_limit"])
        selected_map = {dedup_key(item.url): item for item in selected}
        if mode == "dry-run":
            for key, candidate in selected_map.items():
                report["items"].append({
                    "dedup_key": key,
                    "source_id": candidate.source_id,
                    "share_id": None,
                    "status": "discovered",
                    "reason": "dry_run",
                    "url": public_report_url(candidate.url),
                    "region": candidate.region,
                    "coverage": candidate.coverage,
                    "metric_name": candidate.metric_name,
                    "metric_value": candidate.metric_value,
                    "metric_observed_at": candidate.metric_observed_at,
                })
            report["finished_at"] = utc_iso(self.now_fn())
            return report

        if self.client is None:
            raise RuntimeError("submit mode requires API client")
        self._transport_login()
        self._resume_existing(report)
        system = self.client.system()
        if not system.get("worker_recent_contact", False):
            for key, candidate in selected_map.items():
                self.state.set_status(key, "deferred", error="worker_unavailable")
                report["items"].append({"dedup_key": key, "source_id": candidate.source_id,
                                        "share_id": None, "status": "deferred",
                                        "reason": "worker_unavailable"})
            report["finished_at"] = utc_iso(self.now_fn())
            return report

        day = started.astimezone(resolve_timezone(self.config.get("timezone", "UTC"))).date().isoformat()
        submitted_now = {}
        stop_new = False
        for key, candidate in selected_map.items():
            if stop_new:
                self.state.set_status(key, "deferred", error="round_paused")
                continue
            if len(self.state.inflight()) >= self.config["max_inflight"]:
                self.state.set_status(key, "deferred", error="max_inflight")
                continue
            marker = f"[hotspot:v1:{key}]"
            note = f"{marker} {candidate.source_name} · {candidate.title_hint}"[:1000]
            if not self.state.reserve_submission(key, day, self.config["daily_limit"], marker):
                continue
            try:
                created = self._transport_create(candidate, note)
            except APIError as exc:
                if exc.status == 429:
                    self.state.set_status(key, "deferred", error="queue_full")
                    stop_new = True
                elif exc.status in (400, 403, 413, 415):
                    self.state.set_status(key, "rejected", error=str(exc))
                elif exc.status in (503, 507):
                    self.state.set_status(key, "deferred", error=str(exc))
                    stop_new = True
                else:
                    self.state.set_status(key, "submit_unknown", error=str(exc))
                    report["counts"]["unknown"] += 1
                continue
            except Exception as exc:
                self.state.set_status(key, "submit_unknown", error=str(exc))
                report["counts"]["unknown"] += 1
                continue
            share_id = created.get("id")
            status = created.get("status", "queued")
            poll_url = created.get("poll_url")
            protocol_error = None
            poll_target = None
            if share_id and status in ("queued", "fetching"):
                try:
                    poll_target = self._poll_target(share_id, poll_url)
                except (TypeError, ValueError, RuntimeError) as exc:
                    protocol_error = str(exc)
            self.state.set_status(key, status, share_id=share_id, poll_url=poll_url, error=protocol_error)
            report["counts"]["submitted"] += 1
            if poll_target is not None:
                submitted_now[key] = (share_id, poll_target)
            elif share_id and status in ("queued", "fetching"):
                report["counts"]["pending"] += 1
            elif status == "published":
                report["counts"]["published"] += 1
            elif status in ("failed", "review"):
                report["counts"]["failed"] += 1
            report["items"].append({
                "dedup_key": key,
                "source_id": candidate.source_id,
                "share_id": share_id,
                "status": status,
                "reason": "submitted",
            })
        self._poll_submitted(report, submitted_now)
        report["finished_at"] = utc_iso(self.now_fn())
        return report


def build_from_env(config_path, state_dir=None):
    config = load_config(config_path)
    state = StateStore(state_dir or os.environ.get("HOTSPOT_STATE_DIR", ".hotspot-state"))
    system_base = os.environ.get("HOTSPOT_SYSTEM_API_BASE", "").strip()
    service_token = os.environ.get("HOTSPOT_SERVICE_TOKEN", "").strip()
    if system_base or service_token:
        if not system_base or not service_token:
            raise ValueError("HOTSPOT_SYSTEM_API_BASE and HOTSPOT_SERVICE_TOKEN must be configured together")
        return HotspotRunner(config, state, transport=SystemHotspotTransport(system_base, service_token))
    if os.environ.get("HOTSPOT_LEGACY_USER_SUBMIT") == "1":
        base,user,password=(os.environ.get("HOTSPOT_API_BASE"),os.environ.get("HOTSPOT_USER_ID"),os.environ.get("HOTSPOT_USER_PASSWORD"))
        client=TogetherClient(base,user,password) if base and user and password else None
        return HotspotRunner(config,state,client=client)
    return HotspotRunner(config,state)
