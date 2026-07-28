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

from dataclasses import dataclass
from datetime import date

import psycopg


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
