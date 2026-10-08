"""Time windows for analytics.

A window is `[start, end)` in UTC ending "now". The wire form is a compact
label: `<n>h` or `<n>d` (e.g. `24h`, `7d`), bounded to `MAX_WINDOW`.

The LIVE HORIZON is UTC midnight of `(now - 48h)`. Raw tables are always
authoritative from the horizon onwards (late-arriving attributed executions
land there), and rollup rows are only ever used for whole UTC days strictly
before it (see `analytics.rollup`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

LIVE_HOURS = 48
MAX_WINDOW = timedelta(days=365)
DEFAULT_WINDOW = "7d"
WINDOW_PATTERN = r"^[1-9][0-9]{0,3}[hd]$"
_WINDOW_RE = re.compile(WINDOW_PATTERN)


class InvalidWindow(ValueError):
    """Curated: the message is safe to return to the caller."""


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    label: str


def parse_window(label: str, now: datetime) -> Window:
    if not _WINDOW_RE.fullmatch(label):
        raise InvalidWindow("window must look like '24h' or '7d'.")
    n, unit = int(label[:-1]), label[-1]
    span = timedelta(hours=n) if unit == "h" else timedelta(days=n)
    if span > MAX_WINDOW:
        raise InvalidWindow("window must be at most 365d.")
    return Window(start=now - span, end=now, label=label)


def all_time(now: datetime) -> Window:
    return Window(start=datetime(1970, 1, 1, tzinfo=UTC), end=now, label="all")


def midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=UTC)


def live_horizon(now: datetime) -> date:
    """First UTC day that is ALWAYS computed live (never from rollups)."""
    return (now.astimezone(UTC) - timedelta(hours=LIVE_HOURS)).date()


def utc_today(now: datetime) -> date:
    return now.astimezone(UTC).date()
