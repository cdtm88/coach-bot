"""PLAN-05: remove planned events the coach created that no longer have a plan.

"Orphan planned events carrying a coach id with no matching prescription are
removed on the nightly pass."

This is the only code in the system that deletes something the athlete can see, so
the reasoning is about what it refuses to touch rather than what it removes.

**Three conditions, all required.** An event is swept only if it matches
:data:`coach.plans.events.OURS` exactly, *and* its prescription id resolves to no
live row, *and* it is in the future. Any one of them failing leaves the event
alone.

The future test is the one that is easy to leave out and expensive to. A past
planned event is history: it may be what an activity was paired against, it is what
the athlete actually did or failed to do, and PLAN-07's compliance reads through
it. Sweeping it would delete the record of a session to tidy up a calendar nobody
is looking at any more.

**It does not filter upstream.** V1 verified that `bulk-delete` accepts an exact
`external_id` and returns a count; whether it accepts a prefix or a wildcard is
unverified, so this reads the window, decides locally, and deletes the specific ids
it decided on. Slower by one API call and correct without an assumption. See
`docs/state-of-build.md` open item 7.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from coach.plans import events as eventsmod

log = logging.getLogger(__name__)

# How far ahead to look. Wider than any block the coach publishes, so an event
# left behind by a cancelled block cannot hide beyond the horizon. Narrow enough
# that the read is one page.
HORIZON_DAYS = 120

# And how far back. Zero: the past is not swept, and the constant exists to say
# so rather than to be tuned.
LOOKBACK_DAYS = 0


@dataclass
class Swept:
    deleted: list[str] = field(default_factory=list)
    kept_past: list[str] = field(default_factory=list)
    foreign: int = 0
    reported: int = 0

    @property
    def count(self) -> int:
        return len(self.deleted)


def live_prescription_ids(conn: psycopg.Connection) -> set[int]:
    """Prescriptions that still justify an event upstream.

    Cancelled counts as gone: ADJ cancels a session by setting the status, and the
    calendar entry has to follow or the athlete sees a session the coach has
    already withdrawn. Completed and missed rows stay — they are in the past, which
    the sweep does not touch anyway, and belt and braces here costs nothing.
    """
    with conn.cursor() as cur:
        cur.execute("select id from prescriptions where status != 'cancelled'")
        return {int(row["id"]) for row in cur.fetchall()}


def orphans(
    conn: psycopg.Connection, upstream: list[dict[str, Any]], now: datetime, tz: ZoneInfo
) -> Swept:
    """Decide what to delete. Pure: no I/O, so the judgement is testable alone."""
    live = live_prescription_ids(conn)
    result = Swept()

    for event in upstream:
        if not eventsmod.is_ours(event):
            # The athlete's races, notes and anything another tool wrote.
            result.foreign += 1
            continue

        external = str(event["external_id"])
        pid = eventsmod.prescription_id_of(event)
        if pid in live:
            continue

        if not _is_future(event, now, tz):
            # An orphan, but a historical one. Left deliberately.
            result.kept_past.append(external)
            continue

        result.deleted.append(external)

    return result


def _is_future(event: dict[str, Any], now: datetime, tz: ZoneInfo) -> bool:
    """Is this event still ahead of the athlete?

    `start_date_local` comes back naive and local, which is what it means. It is
    read in the athlete's zone (TZ-01) rather than the server's — the two differ by
    four hours here, and an event at 18:00 tonight would otherwise look past at
    22:30 local.

    An unparseable or absent date is treated as *not* future, so the sweep leaves
    it. Deleting on a date we could not read would be the worst available outcome.
    """
    raw = event.get("start_date_local")
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        log.warning(
            "event %s has an unreadable start_date_local %r; leaving it", event.get("id"), raw
        )
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return when > now


def run(
    conn: psycopg.Connection,
    api: Any,
    now: datetime,
    tz: ZoneInfo,
    horizon_days: int = HORIZON_DAYS,
) -> Swept:
    """The nightly sweep. Reads the window, decides, deletes what it decided.

    Returns what it did in enough detail for OBS-04 to answer "what did last night
    remove", which for a destructive job is the difference between an audit trail
    and a shrug.
    """
    today: date = now.astimezone(tz).date()
    upstream = api.events(
        today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=horizon_days)
    )

    result = orphans(conn, upstream, now, tz)
    result.reported = api.delete_events(result.deleted) if result.deleted else 0

    if result.deleted:
        log.info(
            "swept %d orphan planned event(s) (upstream reported %d): %s",
            len(result.deleted),
            result.reported,
            ", ".join(result.deleted),
        )
        if result.reported != len(result.deleted):
            # Not an error: an event deleted in the app between the read and the
            # write is a legitimate mismatch. Logged because a persistent gap
            # means the delete is not doing what this thinks it is.
            log.warning(
                "asked to delete %d event(s), upstream reported %d",
                len(result.deleted),
                result.reported,
            )
    if result.kept_past:
        log.info(
            "left %d past orphan(s) alone: %s", len(result.kept_past), ", ".join(result.kept_past)
        )
    return result
