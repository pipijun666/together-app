from __future__ import annotations

import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser

from .models import Candidate, utc_iso

_TELEGRAM_CHANNEL = re.compile(r"^[A-Za-z0-9_]+$")
_COMPACT_NUMBER = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([KMB])?$", re.I)


def _parse_compact_number(value):
    if not value:
        return None
    text = str(value).replace(",", "").replace(" ", "").strip()
    match = _COMPACT_NUMBER.fullmatch(text)
    if not match:
        return None
    number = float(match.group(1))
    factor = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2).upper() if match.group(2) else ""]
    return int(number * factor)


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return utc_iso(parsed)


class _TelegramChannelParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.messages = []
        self.current = None
        self.div_depth = 0
        self.text_depth = None
        self.capture_views = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag == "div" and "tgme_widget_message" in classes and attrs.get("data-post"):
            self.current = {"post": attrs["data-post"], "text": [], "published_at": None, "views": None}
            self.div_depth = 1
            return
        if self.current is None:
            return
        if tag == "div":
            self.div_depth += 1
            if "tgme_widget_message_text" in classes:
                self.text_depth = self.div_depth
        elif tag == "span" and "tgme_widget_message_views" in classes:
            self.capture_views = True
        elif tag == "time" and attrs.get("datetime"):
            self.current["published_at"] = attrs["datetime"]
        elif tag == "br" and self.text_depth is not None:
            self.current["text"].append("\n")

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if tag == "span" and self.capture_views:
            self.capture_views = False
        if tag != "div":
            return
        if self.text_depth == self.div_depth:
            self.text_depth = None
        self.div_depth -= 1
        if self.div_depth == 0:
            self.messages.append(self.current)
            self.current = None

    def handle_data(self, data):
        if self.current is None:
            return
        if self.text_depth is not None:
            self.current["text"].append(data)
        if self.capture_views:
            self.current["views"] = (self.current.get("views") or "") + data


def _telegram_channel_from_source(source):
    configured = str(source.get("channel") or "").strip().lstrip("@")
    parsed = urllib.parse.urlsplit(source.get("url", ""))
    path = [part for part in parsed.path.split("/") if part]
    from_url = path[1] if len(path) == 2 and path[0] == "s" else ""
    channel = configured or from_url
    if parsed.scheme != "https" or parsed.hostname != "t.me" or not _TELEGRAM_CHANNEL.fullmatch(channel):
        raise ValueError("invalid Telegram public channel source")
    if configured and from_url and configured.lower() != from_url.lower():
        raise ValueError("Telegram channel does not match source URL")
    return channel


def parse_telegram_channel(data, source, now):
    channel = _telegram_channel_from_source(source)
    parser = _TelegramChannelParser()
    parser.feed(data.decode("utf-8", "replace"))
    rows = []
    for message in parser.messages:
        post = str(message.get("post") or "")
        if "/" not in post:
            continue
        post_channel, message_id = post.rsplit("/", 1)
        if post_channel.lower() != channel.lower() or not message_id.isdigit():
            continue
        text = " ".join("".join(message.get("text") or []).split())
        views = _parse_compact_number(message.get("views"))
        published = _parse_datetime(message.get("published_at"))
        rows.append((post, message_id, text, views, published))

    ranked = sorted(
        [row for row in rows if row[3] is not None],
        key=lambda row: (-row[3], -(int(row[1]) if row[1].isdigit() else 0)),
    )
    rank_by_post = {row[0]: index for index, row in enumerate(ranked, 1)}
    observed_at = utc_iso(now)
    result = []
    ordered = sorted(rows, key=lambda row: (row[3] is None, -(row[3] or 0), -int(row[1])))
    for post, message_id, text, views, published in ordered[:50]:
        title = text[:300] if text else f"Telegram post {message_id}"
        result.append(Candidate(
            1, source["id"], source["name"], f"https://t.me/{post}", title,
            observed_at, post, published, rank_by_post.get(post), text[:1000] or None,
            region=source.get("region"),
            metric_name="views" if views is not None else None,
            metric_value=float(views) if views is not None else None,
            metric_observed_at=observed_at,
            coverage=source.get("coverage") or "selected_public_channel",
        ))
    return result


_X_TOKEN_ENV = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_X_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_TIKTOK_PERMALINK = re.compile(r"https://(?:www\.)?tiktok\.com/@[A-Za-z0-9._-]+/video/\d+")


def _json_payload(raw, label):
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"invalid {label} JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid {label} payload")
    return payload


def _x_source_details(source):
    parsed = urllib.parse.urlsplit(source.get("url", ""))
    match = re.fullmatch(r"/2/trends/by/woeid/(\d+)", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "api.x.com" or not match:
        raise ValueError("invalid X trends API source URL")
    token_env = str(source.get("bearer_token_env") or "X_API_BEARER_TOKEN")
    if not _X_TOKEN_ENV.fullmatch(token_env):
        raise ValueError("invalid X bearer token environment name")
    token = os.environ.get(token_env)
    if not token:
        raise ValueError(f"X API bearer token is not configured in {token_env}")
    return int(match.group(1)), token


def _x_post_score(post):
    metrics = post.get("public_metrics") if isinstance(post, dict) else None
    if not isinstance(metrics, dict):
        return 0
    return sum(int(metrics.get(key) or 0) for key in
               ("like_count", "retweet_count", "reply_count", "quote_count"))


def fetch_x_trend_posts(source, fetcher, now):
    woeid, token = _x_source_details(source)
    max_trends = source.get("max_trends", 3)
    if isinstance(max_trends, bool) or not isinstance(max_trends, int) or not (1 <= max_trends <= 5):
        raise ValueError("X max_trends must be an integer from 1 to 5")
    auth = {"Authorization": f"Bearer {token}"}
    raw, _headers, _url, status = fetcher(source["url"], headers=auth)
    if status != 200:
        raise ValueError(f"X trends HTTP {status}")
    trends = _json_payload(raw, "X trends").get("data") or []
    if not isinstance(trends, list):
        raise ValueError("invalid X trends data")
    observed_at = utc_iso(now)
    results = []
    for rank, trend in enumerate(trends[:max_trends], 1):
        if not isinstance(trend, dict) or not isinstance(trend.get("trend_name"), str):
            continue
        name = trend["trend_name"].strip()
        if not name:
            continue
        query = urllib.parse.urlencode({
            "query": f"{name} -is:retweet",
            "max_results": "10",
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "username",
        })
        search_url = "https://api.x.com/2/tweets/search/recent?" + query
        search_raw, _h, _u, search_status = fetcher(search_url, headers=auth)
        if search_status != 200:
            continue
        payload = _json_payload(search_raw, "X recent search")
        posts = payload.get("data") or []
        if not isinstance(posts, list) or not posts:
            continue
        best = max((p for p in posts if isinstance(p, dict)), key=_x_post_score, default=None)
        if not best or not str(best.get("id") or "").isdigit():
            continue
        users = {str(u.get("id")): u for u in (payload.get("includes") or {}).get("users", [])
                 if isinstance(u, dict) and u.get("id") is not None}
        username = str((users.get(str(best.get("author_id"))) or {}).get("username") or "")
        post_id = str(best["id"])
        post_url = (f"https://x.com/{username}/status/{post_id}"
                    if _X_USERNAME.fullmatch(username) else f"https://x.com/i/web/status/{post_id}")
        text = str(best.get("text") or "").strip()
        if not text:
            continue
        tweet_count = trend.get("tweet_count")
        metric_value = float(tweet_count) if isinstance(tweet_count, (int, float)) else None
        results.append(Candidate(
            1, source["id"], source["name"], post_url, text[:300], observed_at,
            post_id, _parse_datetime(best.get("created_at")), rank, text[:1000],
            region=source.get("region"),
            metric_name="trend_tweet_count" if metric_value is not None else None,
            metric_value=metric_value,
            metric_observed_at=observed_at,
            coverage=source.get("coverage") or f"x_trends_woeid:{woeid}",
        ))
    return results


def tiktok_creative_observation(data, source, now):
    text = data.decode("utf-8", "replace")
    permalink_count = len(set(_TIKTOK_PERMALINK.findall(text)))
    if permalink_count:
        status = "permalinks_observed_not_published"
        reason = "original video permalinks were observed but no stable public parser is enabled"
    else:
        status = "topic_only"
        reason = "Creative Center server HTML exposes trends but no stable original video permalink"
    return {
        "source_id": source.get("id"),
        "platform": "tiktok",
        "status": status,
        "observed_at": utc_iso(now),
        "coverage": source.get("coverage") or "creative_center_trends",
        "original_post_url_count": permalink_count,
        "reason": reason,
    }
