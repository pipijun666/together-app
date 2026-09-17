"""Parse copied share text into a normalized public source reference."""
import html
import re
from urllib.parse import parse_qs, urlsplit


_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_KNOWN_BARE_URL = re.compile(
    r"(?<![\w@])((?:v\.)?(?:douyin\.com|iesdouyin\.com|tiktok\.com)|"
    r"(?:www\.)?(?:x\.com|twitter\.com)|(?:t\.me|telegram\.me))/[^\s<>\"'`]+",
    re.IGNORECASE,
)
_TRAILING = ",.!?;:\u3002\uff0c\uff01\uff1f\uff1b\uff1a\u3001\u300b\u3009\u3011\u300d\u300f\uff09\uff3d\uff5d"


def _clean_candidate(value):
    value = html.unescape(value).strip().rstrip(_TRAILING)
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while value.endswith(closing) and value.count(closing) > value.count(opening):
            value = value[:-1]
    return value


def extract_url(value):
    """Return the first HTTP(S) URL from a URL or a complete share message."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请粘贴包含链接的分享内容")
    if len(value) > 10000:
        raise ValueError("分享内容过长")
    decoded = html.unescape(value)
    match = _URL.search(decoded)
    if match:
        return _clean_candidate(match.group(0))
    match = _KNOWN_BARE_URL.search(decoded)
    if match:
        return "https://" + _clean_candidate(match.group(0))
    raise ValueError("没有找到 http 或 https 链接")


def platform_for_url(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "douyin.com" or host.endswith(".douyin.com") or host == "iesdouyin.com" or host.endswith(".iesdouyin.com"):
        return "douyin"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    if host in ("x.com", "twitter.com") or host.endswith(".x.com") or host.endswith(".twitter.com"):
        return "x"
    if host in ("t.me", "telegram.me") or host.endswith(".t.me") or host.endswith(".telegram.me"):
        return "telegram"
    if host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com"):
        return "youtube"
    return "web"


def content_id_for_url(url, platform=None):
    platform = platform or platform_for_url(url)
    parsed = urlsplit(url)
    patterns = {
        "douyin": r"/(?:video|note)/(\d{15,22})(?:/|$)",
        "tiktok": r"/video/(\d{10,24})(?:/|$)",
        "x": r"/status/(\d{5,24})(?:/|$)",
        "telegram": r"/(?:s/)?[^/]+/(\d+)(?:/|$)",
    }
    match = re.search(patterns.get(platform, r"(?!x)x"), parsed.path, re.IGNORECASE)
    if match:
        return match.group(1)
    if platform == "douyin":
        values = parse_qs(parsed.query).get("modal_id", [])
        if values and re.fullmatch(r"\d{15,22}", values[0]):
            return values[0]
    if platform == "youtube":
        # youtube.com/watch?v=、m.youtube.com/watch?v=、youtube.com/shorts/、youtu.be/<id>
        values = parse_qs(parsed.query).get("v", [])
        for value in values:
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
                return value
        parts = [p for p in parsed.path.split("/") if p]
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if hostname in ("youtu.be",) and parts and re.fullmatch(r"[A-Za-z0-9_-]{11}", parts[0]):
            return parts[0]
        if hostname.endswith("youtube.com") and len(parts) >= 2 and parts[0] == "shorts":
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", parts[1]):
                return parts[1]
    return ""


def source_reference(value):
    url = extract_url(value)
    platform = platform_for_url(url)
    return {"url": url, "platform": platform, "content_id": content_id_for_url(url, platform)}
