"""Shared normalized job record."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Job:
    title: str = ""
    company: str = ""
    description: str = ""
    location: str = ""
    url: str = ""
    posted_date: str | None = None  # ISO string or original value, normalized when possible
    source: str = ""  # e.g. "linkedin", "greenhouse:acme", "lever:acme"
    region: str = ""  # e.g. "nordics", "nigeria"; "" -> derived from location
    remote_scope: str = ""  # worldwide | restricted | unknown (remote only)
    is_remote: bool = False
    salary_min: float | None = None
    salary_max: float | None = None
    seniority: str | None = None  # native seniority label, when a source provides one

    def clean(self) -> "Job":
        """Strip whitespace from string fields in place."""
        for f in ("title", "company", "description", "location", "url",
                  "source", "seniority", "region", "remote_scope"):
            v = getattr(self, f, "")
            if isinstance(v, str):
                setattr(self, f, v.strip())
        if self.posted_date is not None and not isinstance(self.posted_date, str):
            # pandas Timestamp / datetime -> ISO
            try:
                if hasattr(self.posted_date, "isoformat"):
                    self.posted_date = self.posted_date.isoformat()  # type: ignore[union-attr]
                else:
                    self.posted_date = str(self.posted_date)
            except Exception:
                self.posted_date = str(self.posted_date)
        return self


def parse_posted_date(value: object) -> str | None:
    """Best-effort normalization to ISO string. Returns None if unparseable/empty."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    # Try a few common formats, else return raw string
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    try:
        # pandas / dateutil fallback without hard dependency here
        from datetime import datetime as _dt  # noqa: F401
        import email.utils as _eu
        parsed = _eu.parsedate_to_datetime(s)
        if parsed is not None:
            return parsed.isoformat()
    except Exception:
        pass
    return s
