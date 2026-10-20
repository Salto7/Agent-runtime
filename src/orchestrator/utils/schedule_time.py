"""Next-run helpers for durable schedules (interval + 5-field cron).

Pure datetime math — no free-text scraping of logs. Cron expressions are
parsed and iterated by ``croniter``. Fields:
minute hour day-of-month month day-of-week (0=Sun or 7=Sun).
Day-of-month and day-of-week must both match (AND), as Peon has always done.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from croniter import croniter


def aware_utc(dt: datetime | None = None) -> datetime:
    """Return an aware UTC datetime (``dt`` or now)."""
    now = dt or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def next_cron_after(expr: str, after: datetime, *, max_days: int = 366) -> datetime:
    """Return the next UTC-aware instant matching ``expr`` strictly after ``after``."""
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    text = (expr or "").strip()
    if len(text.split()) != 5:
        raise ValueError("cron must have 5 fields: minute hour day month weekday")
    start = after.astimezone(timezone.utc).replace(second=0, microsecond=0)
    nxt = croniter(text, start, day_or=False).get_next(datetime)
    if nxt > after + timedelta(days=max_days):
        raise ValueError(f"no cron match within {max_days} days for {expr!r}")
    return nxt


def next_interval_after(
    interval_seconds: int, after: datetime, *, anchor: datetime | None = None
) -> datetime:
    sec = max(1, int(interval_seconds or 0))
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    base = anchor or after
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    # Next boundary strictly after ``after``.
    elapsed = max(0, int((after - base).total_seconds()))
    steps = (elapsed // sec) + 1
    return base + timedelta(seconds=steps * sec)


def compute_next_run(
    *,
    kind: str,
    cron: str = "",
    interval_seconds: int = 0,
    after: datetime | None = None,
    anchor: datetime | None = None,
) -> datetime | None:
    """Compute next_run_at for a schedule kind. ``once`` returns None after first fire."""
    now = aware_utc(after)
    k = (kind or "").strip().lower()
    if k == "once":
        return now if anchor is None else None
    if k == "interval":
        if interval_seconds <= 0:
            raise ValueError("interval_seconds required for interval schedules")
        return next_interval_after(interval_seconds, now, anchor=aware_utc(anchor) if anchor else now)
    if k == "cron":
        return next_cron_after(cron, now)
    raise ValueError(f"unknown schedule kind {kind!r}")
