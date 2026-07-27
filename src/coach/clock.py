"""Time and locale.

TZ-01: all scheduling, day boundaries and week boundaries use the athlete's
configured local timezone. TZ-02: timestamps are stored in UTC and rendered
local. TZ-03: travel across timezones does not shift the training week — the
configured timezone governs regardless of what an upstream feed says.

That last one is why every function here takes the configured zone and never
reads a tzinfo off the incoming value. An activity uploaded with a device
timezone of Asia/Tokyo still belongs to the athlete's local day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

# The training week starts Monday, which is what makes the Sunday review the
# close of a week rather than the middle of one.
WEEK_STARTS_ON = 0


def to_utc(moment: datetime) -> datetime:
    """TZ-02: everything is stored in UTC.

    A naive datetime is rejected rather than assumed — guessing a zone is how a
    23:30 ride ends up on the wrong day.
    """
    if moment.tzinfo is None:
        raise ValueError(f"{moment!r} is naive; attach a timezone before storing")
    return moment.astimezone(UTC)


def local_day(moment: datetime, tz: ZoneInfo) -> date:
    """The local date a moment belongs to.

    TZ-01: a session ridden at 23:30 local is attributed to that local day.
    TZ-03: the configured zone decides, not the moment's own offset.
    """
    return to_utc(moment).astimezone(tz).date()


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The UTC half-open interval covering one local day."""
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return to_utc(start_local), to_utc(end_local)


def week_bounds(day: date, tz: ZoneInfo) -> tuple[date, date]:
    """The local Monday-to-Sunday week containing a date, inclusive."""
    monday = day - timedelta(days=(day.weekday() - WEEK_STARTS_ON) % 7)
    return monday, monday + timedelta(days=6)


def render(moment: datetime, tz: ZoneInfo, fmt: str = "%a %d %b, %H:%M") -> str:
    """TZ-02: the database shows UTC; messages show local time."""
    return to_utc(moment).astimezone(tz).strftime(fmt)


def utc_offset(tz: ZoneInfo, at: datetime | None = None) -> timedelta:
    """The zone's offset from UTC, evaluated at a moment so DST is respected."""
    reference = at or datetime.now(UTC)
    return reference.astimezone(tz).utcoffset() or timedelta(0)
