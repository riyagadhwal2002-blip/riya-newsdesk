"""Posting hours. Outside the windows nothing is written or published; stories found then wait for the next opening."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def _parse(window: str) -> tuple[time, time]:
    start, end = (part.strip() for part in window.split("-"))
    return time.fromisoformat(start), time.fromisoformat(end)


def _starts(windows: list[str], now_local: datetime) -> list[tuple[datetime, datetime]]:
    """Concrete (start, end) datetimes for yesterday/today/tomorrow, handling windows that cross midnight."""
    spans = []
    for w in windows:
        s, e = _parse(w)
        for day_offset in (-1, 0, 1):
            day = (now_local + timedelta(days=day_offset)).date()
            start = datetime.combine(day, s, tzinfo=now_local.tzinfo)
            end = datetime.combine(day, e, tzinfo=now_local.tzinfo)
            if end <= start:  # e.g. 18:00-02:00 ends the next day
                end += timedelta(days=1)
            spans.append((start, end))
    return spans


def is_open(windows: list[str] | None, now: datetime, tz: str) -> bool:
    if not windows:
        return True
    local = now.astimezone(ZoneInfo(tz))
    return any(start <= local < end for start, end in _starts(windows, local))


def last_opening(windows: list[str] | None, now: datetime, tz: str) -> datetime | None:
    """Start of the window we are in now (None when always open or currently closed)."""
    if not windows:
        return None
    local = now.astimezone(ZoneInfo(tz))
    current = [start for start, end in _starts(windows, local) if start <= local < end]
    return max(current) if current else None


def effective_start(first_published: datetime, windows: list[str] | None, now: datetime, tz: str) -> datetime:
    """A story that broke while posting was closed counts as fresh from the moment the window opened."""
    opening = last_opening(windows, now, tz)
    if opening and first_published < opening:
        held_hours = (opening - first_published).total_seconds() / 3600
        if held_hours <= 10:  # only overnight/afternoon news, not week-old stories
            return opening
    return first_published
