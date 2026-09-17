from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional


def resolve_timezone(name: str):
    if name in ("UTC", "Etc/UTC", "Z"):
        return timezone.utc
    if name == "Asia/Shanghai":
        return timezone(timedelta(hours=8), name="Asia/Shanghai")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {name}") from exc

def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Candidate:
    schema_version: int
    source_id: str
    source_name: str
    url: str
    title_hint: str
    discovered_at: str
    source_item_id: Optional[str] = None
    published_at: Optional[str] = None
    rank: Optional[int] = None
    summary_hint: Optional[str] = None
    region: Optional[str] = None
    metric_name: Optional[str] = None
    metric_value: Optional[float] = None
    metric_observed_at: Optional[str] = None
    coverage: Optional[str] = None

    def to_dict(self):
        return asdict(self)
