"""The two messages a day the coach sends without being asked.

NOTIF-01 in the morning: what today is. NOTIF-02 in the evening: one follow-up
when a prescribed session has left no trace.

**NOTIF-02 is the one with teeth, and every clause of it is a way of not
nagging.** It fires once, never after an activity has landed, never during a
break, and — the clause that is easy to miss — not at all when the platform
recorded training load for the day with no activity attached. Load with no
activity means the ride happened and the *upload* did not, and a message asking
whether the athlete trained today would be both wrong and annoying.

**Absence of data is never evidence of absence of activity.** `load_recorded_on`
returns three values for that reason, and only `True` suppresses. `None` — the
feed has nothing for the day — is the coach not knowing, which is the case the
follow-up exists for.

**RECOV-06 first, then the message.** The recovery signal decides what kind of
message this is: an offer to move the session, not a question about compliance.
The requirement says "an offer rather than a chase" and the difference is
entirely in what is said when the athlete is under-recovered.

**No weigh-in prompting here** (NOTIF-06). `health/bodymass.py` owns it, emits at
most one mention per gap, and routes through the CHAT-11 interruption budget. A
second path that also mentioned weight would produce two mentions for one gap
and neither would know about the other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from coach.health import breaks as breakmod
from coach.health import recovery as recoverymod

log = logging.getLogger(__name__)

# RECOV-06: below this the follow-up is framed as an offer to move the session
# rather than as a question about whether it happened.
UNDER_RECOVERED = -1.0


@dataclass(frozen=True)
class Planned:
    prescription_id: int
    discipline: str
    spec: dict[str, Any]
    status: str


def planned_on(conn: psycopg.Connection, day: date) -> list[Planned]:
    """What the plan says about a local date, whatever has happened since."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, discipline, spec, status
              from prescriptions
             where (planned_for at time zone 'UTC')::date = %s
               and status in ('planned', 'adjusted', 'completed', 'missed')
             order by planned_for
            """,
            (day,),
        )
        return [
            Planned(
                prescription_id=row["id"],
                discipline=row["discipline"],
                spec=row["spec"] or {},
                status=row["status"],
            )
            for row in cur.fetchall()
        ]


def uploaded_on(conn: psycopg.Connection, day: date) -> bool:
    """Did anything land for this day, from any of the three ingest paths?"""
    with conn.cursor() as cur:
        cur.execute("select 1 from sessions where local_date = %s limit 1", (day,))
        return cur.fetchone() is not None


def _describe(planned: Planned) -> str:
    spec = planned.spec
    minutes = int((spec.get("duration_s") or 0) / 60) or None
    purpose = spec.get("purpose")
    parts = [planned.discipline]
    if minutes:
        parts.append(f"{minutes} min")
    described = ", ".join(parts)
    return f"{described} — {purpose}" if purpose else described


def morning(conn: psycopg.Connection, day: date) -> str | None:
    """NOTIF-01: today's session, or confirmation that today is a rest day.

    Returns None only when a break is running (NOTIF-03). A rest day is a
    message, not a silence — "nothing today" is information the athlete acts on.
    """
    if breakmod.active_on(conn, day) is not None:
        return None

    outstanding = [p for p in planned_on(conn, day) if p.status in ("planned", "adjusted")]
    if not outstanding:
        return "Rest day today. Nothing prescribed."
    if len(outstanding) == 1:
        return f"Today: {_describe(outstanding[0])}."
    listed = "; ".join(_describe(p) for p in outstanding)
    return f"Today: {listed}."


def follow_up(conn: psycopg.Connection, day: date) -> str | None:
    """NOTIF-02: at most one evening message, and only when it is warranted.

    Returns None — meaning say nothing — in every case where the coach either
    knows the session happened or has no business asking.
    """
    if breakmod.active_on(conn, day) is not None:
        return None  # NOTIF-03

    outstanding = [p for p in planned_on(conn, day) if p.status in ("planned", "adjusted")]
    if not outstanding:
        return None

    if uploaded_on(conn, day):
        return None  # "not when an activity has already landed"

    # RECOV-06, and the clause that stops the coach asking about a ride it can
    # already see. Load recorded with no activity is a missing upload.
    if recoverymod.load_recorded_on(conn, day) is True:
        log.info("suppressing follow-up for %s: load recorded with no activity", day)
        return None

    described = _describe(outstanding[0])
    deviation = _deviation(conn, day)
    if deviation is not None and deviation <= UNDER_RECOVERED:
        return (
            f"{described} is still on today's plan, and your recovery is well down on "
            "your own baseline. Happy to move it or drop it — say the word."
        )
    return f"{described} is still on today's plan. Want to move it, or is it done and unsynced?"


def _deviation(conn: psycopg.Connection, day: date) -> float | None:
    found = recoverymod.for_day(conn, day)
    if found is None or not found.usable or found.deviation is None:
        return None
    return float(found.deviation)


def morning_job(send: Any) -> Any:
    """NOTIF-01 as a scheduler job. The ledger makes "once" the scheduler's problem."""

    def job(conn: psycopg.Connection, on: date) -> str | None:
        message = morning(conn, on)
        if message:
            send(message)
        return message

    return job


def follow_up_job(send: Any) -> Any:
    """NOTIF-02 as a scheduler job.

    "Follow-up fires once, not repeatedly" is the ledger's `(job, local_date)`
    key rather than anything here — which is why this can be a pure function of
    the day and still satisfy a requirement about repetition.
    """

    def job(conn: psycopg.Connection, on: date) -> str | None:
        message = follow_up(conn, on)
        if message:
            send(message)
        return message

    return job
