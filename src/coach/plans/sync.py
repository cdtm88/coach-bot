"""Inbound: what the athlete did to the calendar, brought back into the plan.

PLAN-06 (edits are detected and recorded as observed evidence), PLAN-12 (the local
prescription is updated so the two never diverge), and PLAN-07's upstream pairing.

**Why the local row yields.** PLAN-12 says the two must never diverge, and it does
not say which side wins — but the athlete moving a session in the app *is* a
decision, and the alternative is the coach republishing over it every cycle and the
athlete moving it back. Behaviour outranks the plan here for the same reason design
section 8 says it outranks a statement: what someone does is better evidence than
what was written down. So the edit is accepted, and the coach's opinion about it is
expressed as evidence for the *next* block rather than as a fight over this one.

**PLAN-06's "observed evidence" is not one edit.** The requirement's acceptance is
"moving a planned session twice on the same weekday updates availability with
observed provenance" — twice, deliberately. One move is a dentist appointment. The
threshold lives in :data:`REPEAT_THRESHOLD` and the counting is over
`adjustment_events`, which already holds one row per change.

Like every other observation, the result is a `pending_writes` row and not a fact.
CONS-06 keeps every path but SAFE-06 out of `facts`, so consolidation ratifies it
against the conflict matrix on the night it is queued — where, for a behavioural
key, observed beats stated and gets mentioned once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from coach.calendars import availability as availmod
from coach.memory import state as statemod
from coach.plans import events as eventsmod

log = logging.getLogger(__name__)

# PLAN-06: how many moves on the same weekday make a pattern. Two, matching
# CALR-03's `MIN_OCCURRENCES` — the same judgement about the same kind of evidence,
# and they should not drift apart.
REPEAT_THRESHOLD = availmod.MIN_OCCURRENCES

# How far either side of today to compare. Behind, because an edit to yesterday
# still tells you something about that weekday; ahead, because that is where the
# plan is.
LOOKBACK_DAYS = 28
HORIZON_DAYS = 60

# Below this, a time difference is not an edit. Upstream returns seconds and a
# republication can round; a minute of drift is not the athlete moving anything.
DRIFT_TOLERANCE = timedelta(minutes=1)

# The sync loop's cadence. Hourly, and floored well above that: PLAN-12 asks for
# "within one sync" rather than immediately, and this reads a calendar a person
# edits by hand — polling it hard would spend rate limit on nothing. A webhook
# would do better and needs a registered application, which SEC-04 rules out.
DEFAULT_INTERVAL_S = 3600


def interval_s() -> int:
    """PLAN-06's cadence, configurable, floored at 15 minutes."""
    from coach.ingest import reconcile

    return reconcile.env_interval("COACH_PLAN_SYNC_INTERVAL_S", DEFAULT_INTERVAL_S, 900)


@dataclass
class Edit:
    """One divergence between what the coach published and what is there now."""

    prescription_id: int
    was: datetime
    now: datetime
    external_id: str

    @property
    def moved_day(self) -> bool:
        return self.was.date() != self.now.date()

    @property
    def weekday(self) -> int:
        """The weekday the session moved *away* from. That is the evidence.

        Moving Thursday's session to Friday says something about Thursdays. Keying
        the pattern on the destination would count a busy Thursday as evidence
        about Friday.
        """
        return self.was.weekday()


@dataclass
class Synced:
    edits: list[Edit] = field(default_factory=list)
    queued: list[int] = field(default_factory=list)
    deleted_upstream: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.edits)


def detect(
    conn: psycopg.Connection, upstream: list[dict[str, Any]], tz: ZoneInfo
) -> tuple[list[Edit], list[int]]:
    """Compare upstream against the local rows. PLAN-06's detection, no writes.

    Returns the edits, and separately the prescription ids whose event has gone
    from upstream entirely — the athlete deleted the session, which is an edit of a
    different kind and handled differently below.
    """
    published = _published(conn)
    seen: set[int] = set()
    edits: list[Edit] = []

    for event in upstream:
        pid = eventsmod.prescription_id_of(event)
        if pid is None or pid not in published:
            continue
        seen.add(pid)

        raw = event.get("start_date_local")
        if not raw:
            continue
        try:
            now = datetime.fromisoformat(str(raw))
        except ValueError:
            log.warning("event %s has an unreadable start_date_local %r", event.get("id"), raw)
            continue
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz)

        was = published[pid].astimezone(tz)
        if abs(now.astimezone(tz) - was) <= DRIFT_TOLERANCE:
            continue

        edits.append(
            Edit(
                prescription_id=pid,
                was=was,
                now=now.astimezone(tz),
                external_id=eventsmod.external_id(pid),
            )
        )

    return edits, sorted(set(published) - seen)


def _published(conn: psycopg.Connection) -> dict[int, datetime]:
    """Future prescriptions the coach has actually published, by id.

    `external_id is not null` is the test for published. Unpublished rows have no
    upstream counterpart, so an absence upstream says nothing about them — without
    this filter every unpublished prescription would look like one the athlete had
    deleted.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, planned_for from prescriptions
             where external_id is not null
               and status in ('planned', 'adjusted')
               and planned_for between now() - make_interval(days => %s)
                                   and now() + make_interval(days => %s)
            """,
            (LOOKBACK_DAYS, HORIZON_DAYS),
        )
        return {int(row["id"]): row["planned_for"] for row in cur.fetchall()}


def apply(conn: psycopg.Connection, edits: list[Edit]) -> None:
    """PLAN-12: bring the local rows into line, and record why.

    One `adjustment_events` row per edit with trigger `athlete_edit`. That table is
    ADJ's, and this writes to it deliberately rather than inventing a parallel log:
    an athlete moving a session is an adjustment to a prescription, and a coach
    reviewing the week should see it beside the ones the system made itself.
    """
    if not edits:
        return
    with conn.transaction(), conn.cursor() as cur:
        for edit in edits:
            cur.execute("select spec from prescriptions where id = %s", (edit.prescription_id,))
            row = cur.fetchone()
            spec = dict((row or {}).get("spec") or {})

            cur.execute(
                "update prescriptions set planned_for = %s, status = 'adjusted' where id = %s",
                (edit.now, edit.prescription_id),
            )
            cur.execute(
                """
                insert into adjustment_events
                    (prescription_id, trigger, evidence, before_spec, after_spec, announced)
                values (%s, 'athlete_edit', %s, %s, %s, false)
                """,
                (
                    edit.prescription_id,
                    Jsonb(
                        {
                            "was": edit.was.isoformat(),
                            "now": edit.now.isoformat(),
                            "moved_day": edit.moved_day,
                            "weekday": edit.weekday,
                            "requirement": "PLAN-12",
                        }
                    ),
                    Jsonb(spec),
                    Jsonb(spec),
                ),
            )
    log.info("accepted %d athlete edit(s) upstream (PLAN-12)", len(edits))


def cancel_locally(conn: psycopg.Connection, prescription_ids: list[int]) -> None:
    """The athlete deleted the planned event. Take the hint.

    Cancelled rather than deleted: MEM-02's "nothing is ever hard deleted" is about
    facts, but the same reasoning holds for a session the athlete declined — the
    fact that it was planned and rejected is worth more than a tidy table, and
    PLAN-05 stops republishing it either way.
    """
    if not prescription_ids:
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update prescriptions set status = 'cancelled' where id = any(%s) "
            "and status in ('planned', 'adjusted')",
            (prescription_ids,),
        )
    log.info("cancelled %d prescription(s) the athlete removed upstream", len(prescription_ids))


def _repeated_weekdays(conn: psycopg.Connection, since: date) -> dict[int, int]:
    """How many times a session has been moved off each weekday. PLAN-06's counting.

    Read from `adjustment_events` rather than accumulated in memory, so the count
    survives a restart and spans however many sync passes the edits arrived over —
    "twice on the same weekday" is not "twice in one pass".
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select (evidence->>'weekday')::int as weekday, count(*) as moves
              from adjustment_events
             where trigger = 'athlete_edit'
               and created_at >= %s
               and evidence ? 'weekday'
               and (evidence->>'moved_day')::boolean
             group by 1
            """,
            (since,),
        )
        return {int(row["weekday"]): int(row["moves"]) for row in cur.fetchall()}


def observe(conn: psycopg.Connection, today: date) -> list[int]:
    """PLAN-06: repeated edits become observed availability evidence.

    Only day moves count. Shifting Thursday's session from 18:00 to 20:00 says the
    evening is tight, not that Thursday is unavailable, and treating the two the
    same would blacklist every weekday the athlete ever rescheduled within.

    Queued as a proposal, never written as a fact: CONS-06. The conflict matrix
    decides whether it supersedes what the athlete once stated, and design
    section 8 has it mentioned once in passing when it does.
    """
    counts = _repeated_weekdays(conn, today - timedelta(days=LOOKBACK_DAYS))
    repeated = sorted(day for day, moves in counts.items() if moves >= REPEAT_THRESHOLD)
    if not repeated:
        return []

    names = [availmod.WEEKDAY_NAMES[day] for day in repeated]
    evidence = {
        availmod.WEEKDAY_NAMES[day]: f"{moves} planned sessions moved off this weekday"
        for day, moves in sorted(counts.items())
        if moves >= REPEAT_THRESHOLD
    }

    return [
        statemod.queue_write(
            conn,
            {
                "key": "availability.blackouts",
                "value": names,
                "provenance": "observed",
                "reason": (
                    "planned sessions repeatedly moved off these weekdays in the "
                    f"last {LOOKBACK_DAYS} days (PLAN-06)"
                ),
                "evidence": evidence,
            },
            origin="feed",
        )
    ]


def run(conn: psycopg.Connection, api: Any, now: datetime, tz: ZoneInfo) -> Synced:
    """One sync pass: read upstream, accept what changed, propose what it means."""
    today = now.astimezone(tz).date()
    upstream = api.events(
        today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=HORIZON_DAYS)
    )

    edits, vanished = detect(conn, upstream, tz)
    apply(conn, edits)
    cancel_locally(conn, vanished)
    queued = observe(conn, today) if edits else []

    return Synced(edits=edits, queued=queued, deleted_upstream=vanished)
