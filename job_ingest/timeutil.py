"""Shared timestamp normalization for the API/RSS sources.

Expect a mix of epoch seconds, epoch milliseconds and ISO-8601 (plus
RFC-822 from RSS feeds). Everything becomes an ISO-8601 string in UTC,
or None when unparseable — one consistent format before insert.
"""
from __future__ import annotations

import email.utils
from datetime import datetime, timezone


def normalize_timestamp(value: object) -> str | None:
    """Epoch (s/ms, int/float/digit-string) or date string -> ISO-8601 UTC."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    s = str(value).strip()
    if not s:
        return None
    if s.lstrip("+-").replace(".", "", 1).isdigit():
        return _from_epoch(float(s))
    # ISO-8601 (fromisoformat needs +00:00 instead of Z)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    # RFC-822 / email dates (RSS pubDate) + common variants
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _from_epoch(ts: float) -> str | None:
    if ts <= 0:
        return None
    if ts >= 1e12:  # milliseconds
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
