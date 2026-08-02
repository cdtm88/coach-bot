"""Scheduled breaks, as much of them as HLTH-13 needs.

BREAK-01 to BREAK-04 are P10 requirements and build the conversational creation,
the upstream cancellation and the re-entry proposal. None of that is here. What
is here is the one question P04 has to be able to ask — is today inside a break —
because HLTH-13 suppresses weigh in prompting entirely during one, and a
suppression rule with nothing to read is a rule that has never run.

BREAK-04 is honoured even at this size: an illness break does not end when its
end date passes. That is a property of the query rather than a flag somewhere
later, so P10 cannot accidentally implement the resume it forbids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Break:
    id: int
    kind: str
    starts_on: date
    ends_on: date | None
    reason: str | None


def active_on(conn: psycopg.Connection, day: date) -> Break | None:
    """The break covering a date, if there is one.

    A break with no end date is open ended and covers everything from its start
    until someone ends it. An illness break covers everything from its start
    whatever its end date says, because BREAK-04 requires the athlete to say when
    it is over.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, kind, starts_on, ends_on, reason
              from breaks
             where ended_at is null
               and starts_on <= %s
               and (ends_on is null or ends_on >= %s or kind = 'illness')
             order by starts_on desc
             limit 1
            """,
            (day, day),
        )
        row = cur.fetchone()
    return Break(**row) if row else None


def create(
    conn: psycopg.Connection,
    kind: str,
    starts_on: date,
    ends_on: date | None = None,
    reason: str | None = None,
) -> int:
    """Record a break. BREAK-01 gives this a conversational front end in P10."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into breaks (kind, starts_on, ends_on, reason) values (%s, %s, %s, %s) "
            "returning id",
            (kind, starts_on, ends_on, reason),
        )
        return cur.fetchone()["id"]


def end(conn: psycopg.Connection, break_id: int, when: date | None = None) -> None:
    """Close a break. The only way an illness break ever ends (BREAK-04)."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update breaks set ended_at = now(), ends_on = coalesce(%s, ends_on, current_date) "
            "where id = %s",
            (when, break_id),
        )


# --- BREAK-02: what a break does to the plan ---------------------------------


_COVERS = """
      b.starts_on <= {day}
  and case
        -- Closed by hand: `end()` always leaves an ends_on, so the recorded
        -- range is exactly what it covered.
        when b.ended_at is not null then b.ends_on >= {day}
        -- BREAK-04: an open illness break covers everything from its start
        -- until the athlete says otherwise, whatever its end date claims.
        when b.kind = 'illness' then true
        when b.ends_on is null then true
        else b.ends_on >= {day}
      end
"""


def covered_days(conn: psycopg.Connection, since: date, until: date) -> list[date]:
    """Every day in the range that falls inside a break.

    In SQL rather than by iterating dates in Python, because the adherence
    rollup needs the same predicate and MEM-08 means that one has to be SQL
    anyway. Two implementations of "is this day inside a break" is exactly the
    kind of duplication that ends with the rollup and the message disagreeing.

    Unlike :func:`active_on` this looks *backwards* as well: a break that has
    since been closed still covered the days it covered, and adherence for those
    days must stay excluded forever rather than only while the break is running.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select d::date as day
              from generate_series(%s::date, %s::date, interval '1 day') d
             where exists (
                   select 1 from breaks b where {_COVERS.format(day="d::date")}
             )
             order by d
            """,
            (since, until),
        )
        return [row["day"] for row in cur.fetchall()]


@dataclass(frozen=True)
class Suspended:
    """What suspending a break's window actually did."""

    prescription_ids: list[int]
    external_ids: list[str]
    reported: int = 0


def suspend(conn: psycopg.Connection, brk: Break, until: date | None = None) -> Suspended:
    """BREAK-02: suspend the prescriptions a break covers, and name their events.

    Suspended rather than cancelled or missed. 'missed' would feed the ADJ-01
    triggers and depress adherence for a break the coach agreed to; 'cancelled'
    says the athlete declined a session they were never offered. Neither is what
    happened.

    Returns the upstream event ids rather than deleting them, because the API
    client belongs to the caller — the same separation PLAN-05's sweep uses, and
    the reason this is testable without a network.
    """
    horizon = until or brk.ends_on
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            update prescriptions
               set status = 'suspended'
             where status in ('planned', 'adjusted')
               and (planned_for at time zone 'UTC')::date >= %s
               and (%s::date is null or (planned_for at time zone 'UTC')::date <= %s)
            returning id, external_id
            """,
            (brk.starts_on, horizon, horizon),
        )
        rows = cur.fetchall()
    suspended = Suspended(
        prescription_ids=[r["id"] for r in rows],
        external_ids=[r["external_id"] for r in rows if r["external_id"]],
    )
    if suspended.prescription_ids:
        log.info(
            "break %d suspended %d prescription(s) from %s",
            brk.id,
            len(suspended.prescription_ids),
            brk.starts_on,
        )
    return suspended


def cancel_upstream(api: Any, suspended: Suspended) -> Suspended:
    """The other half of BREAK-02, kept separate so the database half is testable.

    An upstream failure is logged and not raised: the local plan is already
    suspended, and PLAN-05's sweep will remove the orphaned events on its next
    pass. Refusing to suspend locally because a network call failed would be the
    wrong way round.
    """
    if not suspended.external_ids:
        return suspended
    try:
        reported = api.delete_events(suspended.external_ids)
    except Exception:  # noqa: BLE001 - the local suspension stands regardless
        log.exception("could not cancel %d planned event(s) upstream", len(suspended.external_ids))
        return suspended
    return replace(suspended, reported=reported)


# --- BREAK-03: coming back ---------------------------------------------------

# How much of the pre-break weekly load the first weeks back are allowed to be.
#
# Invented numbers, and they should look like it — nothing in the PRD fixes
# them. What the PRD does fix is the direction: "proposes a re-entry rather than
# resuming the block at full load". So the table only ever ramps upward toward
# 1.0 and never starts there, and BLOCK-07's 10% weekly ramp still applies on
# top, which is what stops a generous first week from becoming a fast one.
#
# Keyed by the length of the break, longest first.
RE_ENTRY_LADDER: tuple[tuple[int, tuple[float, ...]], ...] = (
    (28, (0.50, 0.70, 0.85)),
    (14, (0.60, 0.80)),
    (7, (0.70,)),
)

# Under a week off is a light week, not a break to come back from. Proposing a
# re-entry for four days away would be the system being precious.
RE_ENTRY_MIN_DAYS = 7


@dataclass(frozen=True)
class ReEntry:
    """A proposal, never an application. REV-04 puts it in front of the athlete."""

    break_id: int
    days_away: int
    baseline_load: Decimal | None
    weeks: tuple[Decimal, ...]

    def render(self) -> str:
        if self.baseline_load is None:
            return (
                f"{self.days_away} days off. Coming back at a reduced volume for "
                f"{len(self.weeks)} week{'s' if len(self.weeks) != 1 else ''} — there is no "
                "pre-break load figure to scale from, so the first week is a conversation."
            )
        steps = ", ".join(f"week {i + 1}: {int(w)}" for i, w in enumerate(self.weeks))
        return (
            f"{self.days_away} days off, against a pre-break week of "
            f"{int(self.baseline_load)}. Proposed re-entry — {steps}."
        )


def _ladder(days_away: int) -> tuple[float, ...]:
    for threshold, fractions in RE_ENTRY_LADDER:
        if days_away >= threshold:
            return fractions
    return ()


def baseline_load(conn: psycopg.Connection, before: date) -> Decimal | None:
    """The 7 day load as it stood the day before the break started.

    Read from `rollups` rather than recomputed, so the figure the re-entry
    scales from is the same one every other part of the system quotes.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select load_7d from rollups where as_of <= %s and load_7d is not null "
            "order by as_of desc limit 1",
            (before,),
        )
        row = cur.fetchone()
    return row["load_7d"] if row else None


def re_entry(conn: psycopg.Connection, brk: Break, returning_on: date) -> ReEntry | None:
    """BREAK-03: what coming back should look like, or None if nothing is needed.

    Returns a proposal for the review to surface. It writes nothing to the plan:
    the governing asymmetry lets the system reduce load without asking, and this
    *increases* it from zero, so it is the athlete's decision by construction.
    """
    days_away = (returning_on - brk.starts_on).days
    if days_away < RE_ENTRY_MIN_DAYS:
        return None
    fractions = _ladder(days_away)
    if not fractions:
        return None

    base = baseline_load(conn, brk.starts_on - timedelta(days=1))
    weeks = (
        tuple((base * Decimal(str(f))).quantize(Decimal("1")) for f in fractions)
        if base is not None
        else ()
    )
    return ReEntry(break_id=brk.id, days_away=days_away, baseline_load=base, weeks=weeks)


def mark_re_entry_proposed(conn: psycopg.Connection, break_id: int, on: date) -> None:
    """So the review says it once rather than every Sunday until the athlete acts."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update breaks set re_entry_proposed_on = %s where id = %s "
            "and re_entry_proposed_on is null",
            (on, break_id),
        )


def awaiting_re_entry(conn: psycopg.Connection, on: date) -> Break | None:
    """A break that has finished and has not yet been offered a re-entry.

    An illness break is excluded until it has been closed by hand, because
    BREAK-04 says its end date does not end it — and offering a re-entry is a
    resumption in everything but name.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, kind, starts_on, ends_on, reason
              from breaks
             where re_entry_proposed_on is null
               and (
                     (ended_at is not null and ends_on <= %s)
                  or (ended_at is null and kind <> 'illness'
                      and ends_on is not null and ends_on < %s)
               )
             order by starts_on desc
             limit 1
            """,
            (on, on),
        )
        row = cur.fetchone()
    return Break(**row) if row else None
