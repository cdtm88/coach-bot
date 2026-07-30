"""Outbound: prescriptions onto the intervals.icu calendar.

PLAN-01 (they publish), PLAN-02 (keyed on our own id, so twice is once) and
PLAN-04 (never into busy time).

**PLAN-04 is the whole of the difficulty.** "Sessions are never planned into busy
time visible in the calendar feeds at the moment of scheduling" — and the
acceptance is that a seeded conflict "causes a move or shortening, not an
overlap". So the resolution order here is: move within the evening, then shorten,
then give up and say so. Never publish the overlap.

Two things it deliberately does not do.

It does not treat an unresolvable conflict as a failure of the publish. The other
sessions in the block still go up; the one that could not be placed is reported.
A block that refuses to publish because Thursday is busy would be worse than a
block with a hole in it that the athlete can be told about.

And it does not move a session to another day. The weekday is the plan's decision
— BLOCK made it against observed availability — and shifting Thursday's intervals
onto Friday changes the training week rather than accommodating a meeting. Moving
*within* the evening is accommodation; moving across days is re-planning, and
belongs to whatever asked for the block.

**"At the moment of scheduling"** is load-bearing and CALR-05 explains it: the
feed lags, so this is the best view available and not a guarantee. A commitment
added after publication is the weekly review's problem, not a bug here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from coach.calendars import availability as availmod
from coach.plans import events as eventsmod

log = logging.getLogger(__name__)

# The window a session may be moved within, matching the one CALR-03 measures
# availability over. Moving outside it would be inventing time the athlete has
# never trained in.
WINDOW_START = availmod.EVENING_START
WINDOW_END = availmod.EVENING_END

# How far a session may be shortened before it stops being the session that was
# planned. Below this the honest answer is that it does not fit.
MIN_RETAINED = 0.6

# Search granularity when looking for a gap. Fifteen minutes because a calendar
# is written in quarter hours and a finer search buys nothing real.
STEP = timedelta(minutes=15)


@dataclass
class Placement:
    """Where one session ended up, and what it cost to put it there."""

    prescription_id: int
    starts_at: datetime
    duration_s: int
    moved: bool = False
    shortened: bool = False
    reason: str = ""

    @property
    def adjusted(self) -> bool:
        return self.moved or self.shortened


@dataclass
class Published:
    placements: list[Placement] = field(default_factory=list)
    unplaceable: list[dict[str, Any]] = field(default_factory=list)
    upstream: list[dict[str, Any]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.placements)


def _busy_spans(
    conn: psycopg.Connection, day: date, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """Busy intervals on one local day, as concrete times in the athlete's zone.

    An all-day event blocks the whole window. That is a judgement rather than a
    reading of the data — an all-day entry is usually travel or leave, and
    treating it as free because it has no hours would put a session inside a
    flight.
    """
    spans: list[tuple[datetime, datetime]] = []
    for busy in availmod.busy_between(conn, day, day, tz):
        if busy.local_date != day:
            continue
        if busy.all_day:
            spans.append(
                (
                    datetime.combine(day, WINDOW_START, tzinfo=tz),
                    datetime.combine(day, WINDOW_END, tzinfo=tz),
                )
            )
            continue
        start, end = busy.starts_at, busy.ends_at
        if start is None or end is None:
            continue
        spans.append((start.astimezone(tz), end.astimezone(tz)))
    return sorted(spans)


def _clashes(start: datetime, duration_s: int, spans: list[tuple[datetime, datetime]]) -> bool:
    end = start + timedelta(seconds=duration_s)
    # Touching is not overlapping: a session starting exactly when a meeting ends
    # is fine, and treating it as a clash would lose an hour to arithmetic.
    return any(start < busy_end and busy_start < end for busy_start, busy_end in spans)


def place(conn: psycopg.Connection, prescription: dict[str, Any], tz: ZoneInfo) -> Placement | None:
    """PLAN-04: find a slot, or report that there is none.

    Order matters. The planned time is tried first — an unbusy evening must not be
    rearranged for tidiness. Then a move within the window, at full duration,
    because keeping the session intact is worth more than keeping its start time.
    Only then a shortening, and only down to :data:`MIN_RETAINED`.
    """
    planned: datetime = prescription["planned_for"].astimezone(tz)
    duration_s = int((prescription.get("spec") or {}).get("duration_s") or 0)
    pid = int(prescription["id"])
    day = planned.date()
    spans = _busy_spans(conn, day, tz)

    if not duration_s or not _clashes(planned, duration_s, spans):
        return Placement(pid, planned, duration_s)

    window_start = datetime.combine(day, WINDOW_START, tzinfo=tz)
    window_end = datetime.combine(day, WINDOW_END, tzinfo=tz)

    # A move, at full duration.
    candidate = window_start
    while candidate + timedelta(seconds=duration_s) <= window_end:
        if not _clashes(candidate, duration_s, spans):
            log.info("prescription %s moved from %s to %s (PLAN-04)", pid, planned, candidate)
            return Placement(
                pid,
                candidate,
                duration_s,
                moved=True,
                reason=f"moved from {planned:%H:%M} to avoid a commitment",
            )
        candidate += STEP

    # A shortening, in the largest gap the evening has left. Largest rather than
    # first so the session keeps as much of itself as possible.
    floor = int(duration_s * MIN_RETAINED)
    best: tuple[datetime, int] | None = None
    for gap_start, gap_end in _gaps(window_start, window_end, spans):
        available = int((gap_end - gap_start).total_seconds())
        if available >= floor and (best is None or available > best[1]):
            best = (gap_start, min(available, duration_s))

    if best is not None:
        start, kept = best
        log.info(
            "prescription %s shortened from %ds to %ds to fit %s (PLAN-04)",
            pid,
            duration_s,
            kept,
            day,
        )
        return Placement(
            pid,
            start,
            kept,
            moved=start != planned,
            shortened=True,
            reason=f"shortened from {duration_s // 60} to {kept // 60} min to fit the evening",
        )

    return None


def _gaps(
    window_start: datetime, window_end: datetime, spans: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Free intervals inside the window, given the busy ones."""
    gaps: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for busy_start, busy_end in spans:
        if busy_start > cursor:
            gaps.append((cursor, min(busy_start, window_end)))
        cursor = max(cursor, busy_end)
        if cursor >= window_end:
            break
    if cursor < window_end:
        gaps.append((cursor, window_end))
    return [(a, b) for a, b in gaps if b > a]


def pending(conn: psycopg.Connection, block_id: int | None = None) -> list[dict[str, Any]]:
    """Prescriptions eligible to publish: planned or adjusted, not yet done.

    Completed and cancelled rows are excluded, and so is anything in the past —
    publishing yesterday's session onto the calendar helps nobody and would make
    PLAN-05's sweep fight with this.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, block_id, planned_for, discipline, spec, external_id, calendar_event_id
              from prescriptions
             where status in ('planned', 'adjusted')
               and planned_for >= now() - interval '1 day'
               and (%s::bigint is null or block_id = %s)
             order by planned_for
            """,
            (block_id, block_id),
        )
        return cur.fetchall()


def publish(
    conn: psycopg.Connection,
    api: Any,
    tz: ZoneInfo,
    block_id: int | None = None,
    prescriptions: list[dict[str, Any]] | None = None,
) -> Published:
    """PLAN-01: put the plan on the calendar. One API call for the batch.

    The local row is updated to whatever was actually published, before the call
    rather than after: if a PLAN-04 move happened, the calendar and the database
    have to agree, and the version that must not be lost is the one the athlete
    will see. A failed call rolls the transaction back and nothing is recorded as
    published — which is right, because nothing was.
    """
    rows = prescriptions if prescriptions is not None else pending(conn, block_id)
    result = Published()
    bodies: list[dict[str, Any]] = []

    for row in rows:
        placement = place(conn, row, tz)
        if placement is None:
            result.unplaceable.append(
                {
                    "prescription_id": int(row["id"]),
                    "planned_for": row["planned_for"],
                    # Said in terms the coach can repeat to the athlete. PLAN-04
                    # forbids the overlap; it does not promise the session happens.
                    "reason": "the evening is fully committed and the session "
                    "cannot be shortened enough to fit",
                }
            )
            continue

        result.placements.append(placement)
        spec = dict(row.get("spec") or {})
        if placement.shortened:
            spec["duration_s"] = placement.duration_s
            spec["shortened_for_calendar"] = True

        bodies.append(
            eventsmod.payload(
                {**row, "spec": spec},
                start_local=placement.starts_at,
            )
        )
        _record(conn, row, placement, spec)

    if not bodies:
        return result

    result.upstream = api.upsert_events(bodies)
    _record_upstream_ids(conn, result.upstream)
    return result


def _record(
    conn: psycopg.Connection,
    row: dict[str, Any],
    placement: Placement,
    spec: dict[str, Any],
) -> None:
    """Write back what is about to be published, and why if it changed.

    An adjustment_events row only when PLAN-04 actually moved something. ADJ-01
    wants every adjustment recorded with its trigger and evidence, and "the
    calendar said no" is a trigger like any other — but a session that published
    exactly as planned is not an adjustment and logging one would make the
    athlete's adjustment history mostly noise.
    """
    from psycopg.types.json import Jsonb

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update prescriptions set planned_for = %s, spec = %s, external_id = %s where id = %s",
            (
                placement.starts_at,
                Jsonb(spec),
                eventsmod.external_id(int(row["id"])),
                row["id"],
            ),
        )
        if placement.adjusted:
            cur.execute(
                """
                insert into adjustment_events
                    (prescription_id, trigger, evidence, before_spec, after_spec, announced)
                values (%s, 'calendar_conflict', %s, %s, %s, false)
                """,
                (
                    row["id"],
                    Jsonb(
                        {
                            "planned_for": row["planned_for"].isoformat(),
                            "published_for": placement.starts_at.isoformat(),
                            "requirement": "PLAN-04",
                            "reason": placement.reason,
                        }
                    ),
                    Jsonb(dict(row.get("spec") or {})),
                    Jsonb(spec),
                ),
            )


def _record_upstream_ids(conn: psycopg.Connection, upstream: list[dict[str, Any]]) -> None:
    """Store the event ids the upsert returned. PLAN-07 needs them.

    Not the key we publish on — `external_id` is — but `paired_event_id` on a
    completed activity is an upstream event id, and without this there is nothing
    to resolve it against.
    """
    pairs = [
        (str(event["id"]), pid)
        for event in upstream
        if event.get("id") and (pid := eventsmod.prescription_id_of(event)) is not None
    ]
    if not pairs:
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "update prescriptions set calendar_event_id = %s where id = %s",
            pairs,
        )
