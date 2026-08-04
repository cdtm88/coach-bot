"""What the calendar means, as opposed to what it contains.

CALR-03: busy blocks derive observed availability facts through consolidation.
CALR-05: publication lag is expected, so scheduling is advisory and the weekly
review confirms the week ahead.

**Nothing here writes a fact.** CONS-06 allows exactly one direct writer outside
consolidation and it is the SAFE-06 safety path, so an observation from the
calendar lands in `pending_writes` with origin `feed` and waits for the night.
That is not ceremony: an evening blocked for three weeks running is evidence
about the athlete's availability, and evidence is exactly the kind of thing the
conflict matrix exists to arbitrate. Design section 8 says observed beats stated
for behavioural keys, and `availability.*` is behavioural — so this proposal will
usually win, and it should win through the matrix rather than by writing first.

**The absence of an event is not the presence of free time.** CALR-05 is the
requirement, and Google's iCal feeds publish on a cache, so a commitment added an
hour ago may be invisible. Everything this module produces is therefore framed as
what the feed showed, never as what is true.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

import psycopg

from coach.memory import state as statemod

log = logging.getLogger(__name__)

# The window a training session realistically competes for. Not configuration:
# it is the question being asked of the calendar, and a different window would be
# a different question.
EVENING_START = time(17, 0)
EVENING_END = time(22, 0)

# How much of the evening has to be blocked before the evening counts as gone.
# A 30 minute call at six does not cost a ride.
EVENING_BLOCKED_MINUTES = 60

# CALR-03's acceptance is "a week with three evening commitments". Two
# occurrences of the same weekday is a pattern; one is a Tuesday.
MIN_OCCURRENCES = 2
BLOCKED_SHARE = 0.5

WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass(frozen=True)
class Busy:
    """One block of time the feed showed as busy."""

    local_date: date
    starts_at: object
    ends_at: object
    summary: str | None
    all_day: bool


def busy_between(conn: psycopg.Connection, since: date, until: date, tz: ZoneInfo) -> list[Busy]:
    """Busy blocks overlapping a local date range. CALR-04 has already filtered."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select local_date, starts_at, ends_at, summary, all_day
              from calendar_events
             where busy and local_date between %s and %s
             order by starts_at
            """,
            (since, until),
        )
        return [Busy(**row) for row in cur.fetchall()]


def evening_blocked_minutes(conn: psycopg.Connection, day: date, tz: ZoneInfo) -> int:
    """How much of the evening window a day's busy blocks cover.

    Overlap rather than sum, so two meetings running at the same time do not
    count twice and consume an evening that is only half gone.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            with evening as (
                select (%s::date + %s::time) at time zone %s as window_start,
                       (%s::date + %s::time) at time zone %s as window_end
            ),
            clipped as (
                select tstzrange(
                           greatest(e.starts_at, evening.window_start),
                           least(e.ends_at, evening.window_end)
                       ) as span
                  from calendar_events e, evening
                 where e.busy
                   and e.starts_at < evening.window_end
                   and e.ends_at > evening.window_start
            )
            select coalesce(
                extract(epoch from sum(upper(span) - lower(span))) / 60, 0
            )::int as minutes
              from (
                -- Merge overlapping blocks before measuring them.
                select unnest(range_agg(span)) as span from clipped where not isempty(span)
              ) merged
            """,
            (day, EVENING_START, str(tz), day, EVENING_END, str(tz)),
        )
        row = cur.fetchone()
    return int(row["minutes"] or 0) if row else 0


@dataclass(frozen=True)
class WeekdayPattern:
    weekday: int
    occurrences: int
    blocked: int

    @property
    def name(self) -> str:
        return WEEKDAY_NAMES[self.weekday]

    @property
    def usually_blocked(self) -> bool:
        return (
            self.occurrences >= MIN_OCCURRENCES and self.blocked / self.occurrences > BLOCKED_SHARE
        )

    @property
    def usually_free(self) -> bool:
        return (
            self.occurrences >= MIN_OCCURRENCES and self.blocked / self.occurrences <= BLOCKED_SHARE
        )


def weekday_patterns(
    conn: psycopg.Connection, since: date, until: date, tz: ZoneInfo
) -> list[WeekdayPattern]:
    """For each weekday in the range, how often its evening was blocked.

    Counted over days the feed actually covered. A weekday with no data is a
    weekday with no pattern, not a free one — CALR-05 again.
    """
    covered = _covered_days(conn, since, until)
    tally: dict[int, tuple[int, int]] = {}
    for day in covered:
        blocked = evening_blocked_minutes(conn, day, tz) >= EVENING_BLOCKED_MINUTES
        seen, hits = tally.get(day.weekday(), (0, 0))
        tally[day.weekday()] = (seen + 1, hits + (1 if blocked else 0))

    return [
        WeekdayPattern(weekday=weekday, occurrences=seen, blocked=hits)
        for weekday, (seen, hits) in sorted(tally.items())
    ]


def _covered_days(conn: psycopg.Connection, since: date, until: date) -> list[date]:
    """Days the feed has been read for, not days on the calendar.

    A day with no events is free *if the feed covered it*, and unknown if it did
    not. Deriving that from the fetch history rather than from the events keeps
    an outage from reading as a clear diary.
    """
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from calendar_fetches where ok")
        if not cur.fetchone()["n"]:
            return []

    day, days = since, []
    while day <= until:
        days.append(day)
        day += timedelta(days=1)
    return days


def observe(
    conn: psycopg.Connection, today: date, tz: ZoneInfo, lookback_days: int = 28
) -> list[int]:
    """CALR-03: propose observed availability from the weeks that have happened.

    Returns the ids of the queued proposals. They are proposals: CONS-06 keeps
    every path but SAFE-06 out of `facts`, so consolidation ratifies these
    against the conflict matrix on the night they are queued.
    """
    patterns = weekday_patterns(conn, today - timedelta(days=lookback_days), today, tz)
    if not patterns:
        return []

    free = [p.name for p in patterns if p.usually_free]
    blocked = [p.name for p in patterns if p.usually_blocked]
    if not free and not blocked:
        return []

    evidence = {p.name: f"{p.blocked}/{p.occurrences} evenings blocked" for p in patterns}

    queued = []
    if free:
        queued.append(
            statemod.queue_write(
                conn,
                {
                    "key": "availability.days",
                    "value": free,
                    "provenance": "observed",
                    "reason": (
                        "evenings free on these weekdays across the last "
                        f"{lookback_days} days of calendar data"
                    ),
                    "evidence": evidence,
                },
                origin="feed",
            )
        )
    if blocked:
        queued.append(
            statemod.queue_write(
                conn,
                {
                    "key": "availability.blackouts",
                    "value": blocked,
                    "provenance": "observed",
                    "reason": (
                        "evenings committed on these weekdays across the last "
                        f"{lookback_days} days of calendar data"
                    ),
                    "evidence": evidence,
                },
                origin="feed",
            )
        )
    return queued


# The heading said THE WEEK AHEAD, and the coach read it as the training week
# ahead. It is the athlete's own diary: meetings, travel, and anything else he
# put in his calendar, including entries whose names sound exactly like
# sessions. Asked what session was on, the coach answered "Zwift Ride Test,
# 16:45 to 17:15" out of this block, because nothing else in the prompt was
# shaped like a schedule. The TODAY block now carries the plan and this says
# what it is instead of leaving it to be inferred from a heading.
HEADING = "HIS DIARY"
PREAMBLE = (
    "Commitments from his own calendar feeds. These are not training sessions and "
    "none of them was prescribed by you, whatever they are named. They are what he "
    "is busy doing."
)

# Repeated on every line, which looks redundant against the preamble two lines
# above and is not. On 3 August 2026 the coach quoted "Zwift Ride Test, 16:45 to
# 17:15" out of this block as "today's session" — with the heading renamed, this
# preamble present, the TODAY block carrying the plan, and the persona saying a
# diary commitment is never a session it set. Four statements at the block level
# and it still read one line and answered from it.
#
# So the caveat now sits in the same sentence as the thing being quoted, because
# that is the unit a model lifts. Twenty entries at a handful of tokens each is
# affordable inside MEM-11 and the alternative demonstrably was not.
NOT_A_SESSION = "[his diary, not a session you set]"


def context(conn: psycopg.Connection, today: date, tz: ZoneInfo, days: int = 7) -> str:
    """The week ahead, as the feed showed it. CALR-05 is the last line.

    Summaries are included because the coach reasons better about "flight to
    London" than about "busy 06:00-10:00", and this is the athlete's own calendar
    being read back to him rather than anything being disclosed.
    """
    until = today + timedelta(days=days)
    blocks = busy_between(conn, today, until, tz)
    if not blocks:
        with conn.cursor() as cur:
            cur.execute("select count(*) as n from calendar_fetches where ok")
            if not cur.fetchone()["n"]:
                return ""
        return (
            f"{HEADING}\n"
            f"{PREAMBLE}\n"
            "Nothing on the calendar for the next week. The feed publishes on a cache, "
            "so treat that as what it showed rather than as a clear diary, and confirm "
            "before planning around it."
        )

    lines = [HEADING, PREAMBLE]
    for block in blocks[:20]:
        when = block.local_date.strftime("%a %d %b")
        if block.all_day:
            lines.append(f"- {when}: {block.summary or 'busy'} {NOT_A_SESSION} (all day)")
        else:
            start = block.starts_at.astimezone(tz).strftime("%H:%M")
            end = block.ends_at.astimezone(tz).strftime("%H:%M")
            lines.append(f"- {when} {start}-{end}: {block.summary or 'busy'} {NOT_A_SESSION}")

    lines.append(
        "This is what the feed published, which lags. Something added today may not "
        "be here. Plan around it, say so as provisional, and confirm the week ahead "
        "rather than asserting he is free."
    )
    return "\n".join(lines)
