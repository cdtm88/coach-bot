"""Matching sessions to prescriptions, and reviewing them.

FIT-05 (match by date and discipline, compute compliance), FIT-06 (a review with
one forward looking note), FIT-12 (a session is only missed after a grace window
and a cross check).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from coach import clock
from coach.health import recovery as recoverymod
from coach.ingest.activities import uses_power_analysis
from coach.memory import notes as notemod

log = logging.getLogger(__name__)

# FIT-12: a prescribed session is only missed 18 hours past the local day end.
# An overnight upload is late, not absent.
GRACE_HOURS = 18


@dataclass
class Compliance:
    duration_delta_s: int | None = None
    duration_ratio: float | None = None
    intensity_delta_w: float | None = None
    intensity_ratio: float | None = None
    completed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def match(conn: psycopg.Connection, session_id: int) -> int | None:
    """FIT-05 and PLAN-07: find the prescription this session satisfies.

    Two paths, in the order PLAN-07 sets: "the upstream pairing where available,
    falling back to local date and discipline matching". The platform's own
    `paired_event_id` is the better evidence when it exists — it survives a session
    ridden after midnight, a discipline recorded differently upstream, and two rides
    on one day, all of which the date-and-discipline path can only guess at.

    The fallback is not a degraded path. Most activities are not paired upstream:
    nothing pairs a ride that had no planned workout, and a Zwift file arriving
    through the watched folder never went past the platform at all.

    Only unmatched prescriptions are eligible either way, so two rides on one day
    cannot both claim the same prescribed session.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select local_date, discipline, paired_event_id from sessions where id = %s",
            (session_id,),
        )
        session = cur.fetchone()
        if session is None:
            return None

        if session["paired_event_id"]:
            cur.execute(
                """
                select p.id from prescriptions p
                 where p.session_id is null
                   and p.calendar_event_id = %s
                 limit 1
                """,
                (str(session["paired_event_id"]),),
            )
            paired = cur.fetchone()
            if paired:
                return int(paired["id"])
            # Paired upstream to an event we do not hold: the athlete planned it in
            # the app themselves. Not ours to claim, but the date fallback below
            # would happily claim it, so say so once and let it.
            log.info(
                "session %s is paired to upstream event %s, which is not a coach "
                "prescription; falling back to date and discipline",
                session_id,
                session["paired_event_id"],
            )

        cur.execute(
            """
            select p.id from prescriptions p
            where p.session_id is null
              and p.status in ('planned', 'adjusted')
              and lower(p.discipline) = %s
              and p.planned_for::date = %s
            order by p.planned_for
            limit 1
            """,
            (session["discipline"], session["local_date"]),
        )
        row = cur.fetchone()
    return row["id"] if row else None


def compliance(conn: psycopg.Connection, session_id: int, prescription_id: int) -> Compliance:
    """Duration and intensity deltas against what was prescribed."""
    with conn.cursor() as cur:
        cur.execute(
            "select duration_s, avg_power_w, np_power_w, discipline from sessions where id = %s",
            (session_id,),
        )
        session = cur.fetchone()
        cur.execute("select spec from prescriptions where id = %s", (prescription_id,))
        spec = (cur.fetchone() or {}).get("spec") or {}

    result = Compliance(completed=True)

    planned_s = spec.get("duration_s")
    if planned_s and session["duration_s"]:
        result.duration_delta_s = int(session["duration_s"]) - int(planned_s)
        result.duration_ratio = round(session["duration_s"] / planned_s, 3)

    # FIT-07: intensity only where power analysis applies at all.
    planned_w = spec.get("target_watts")
    actual_w = session["np_power_w"] or session["avg_power_w"]
    if planned_w and actual_w and uses_power_analysis(session["discipline"]):
        result.intensity_delta_w = round(float(actual_w) - float(planned_w), 1)
        result.intensity_ratio = round(float(actual_w) / float(planned_w), 3)

    return result


def attach(conn: psycopg.Connection, session_id: int, prescription_id: int) -> Compliance:
    """Link the two and mark the prescription completed (FIT-05)."""
    result = compliance(conn, session_id, prescription_id)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update sessions set prescription_id = %s where id = %s", (prescription_id, session_id)
        )
        cur.execute(
            "update prescriptions set status = 'completed', session_id = %s where id = %s",
            (session_id, prescription_id),
        )
    return result


def review(
    conn: psycopg.Connection,
    session_id: int,
    write_note: Callable[[dict[str, Any]], str],
) -> str | None:
    """FIT-06: a review covering compliance and one forward looking note.

    Returns None for a backfilled session. FIT-09 is explicit that loading
    history produces session rows and rollups only, so the same code path must
    not review its way through three years of riding.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select s.*, p.spec as prescribed
            from sessions s left join prescriptions p on p.id = s.prescription_id
            where s.id = %s
            """,
            (session_id,),
        )
        session = cur.fetchone()

    if session is None:
        return None
    if session["backfilled"]:
        log.debug("session %s is backfilled; no review (FIT-09)", session_id)
        return None
    if session["reviewed_at"] is not None:
        return None

    context = {
        "discipline": session["discipline"],
        "name": session["name"],
        "local_date": session["local_date"].isoformat(),
        "duration_s": session["duration_s"],
        "parsed": {
            "avg_power_w": float(session["avg_power_w"]) if session["avg_power_w"] else None,
            "np_power_w": float(session["np_power_w"]) if session["np_power_w"] else None,
            "avg_hr": session["avg_hr"],
            "max_hr": session["max_hr"],
            "avg_cadence": float(session["avg_cadence"]) if session["avg_cadence"] else None,
            "sample_count": session["sample_count"],
        },
        # FIT-03: offered as the platform's opinion, clearly labelled.
        "derived_by_platform": session["derived"],
        "prescribed": session["prescribed"],
        "power_analysis_applies": uses_power_analysis(session["discipline"]),
    }

    body = write_note(context)
    if not body:
        return None

    notemod.add(conn, "observation", body, session["local_date"], refs={"session_id": session_id})
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update sessions set reviewed_at = now() where id = %s", (session_id,))
    return body


def missed(conn: psycopg.Connection, now: datetime, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Prescriptions old enough to call missed (FIT-12, RECOV-06).

    Three gates now, and the third is RECOV-06. The grace window covers the
    overnight upload. A session on the day with no prescription attached is
    evidence the athlete trained, so the prescription is unmatched rather than
    skipped. And **training load recorded upstream with no local activity means
    the upload is missing, not the session** — that is the exact case RECOV-06
    names, and without it a broken watcher reads as a fortnight of skipped
    sessions.

    The verdict carries its signals rather than only its conclusion. P09's ADJ-08
    forbids restructuring on a missing activity before the recovery and load
    signal has been checked, and it needs to know not just what was decided but
    what was known — `safe_to_act` is false when the feed had nothing for the
    day, because an absent wellness row is the coach not knowing rather than a
    recorded zero.
    """
    cutoff = now - timedelta(hours=GRACE_HOURS)
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.id, p.planned_for, p.discipline
            from prescriptions p
            where p.session_id is null
              and p.status in ('planned', 'adjusted')
              and p.planned_for < %s
            order by p.planned_for
            """,
            (cutoff,),
        )
        candidates = cur.fetchall()

    verdicts = []
    for candidate in candidates:
        day = clock.local_day(candidate["planned_for"], tz)
        with conn.cursor() as cur:
            cur.execute(
                "select count(*) as n from sessions where local_date = %s",
                (day,),
            )
            same_day = cur.fetchone()["n"]

        # RECOV-06: check the load signal before drawing a conclusion, not after.
        load_recorded = recoverymod.load_recorded_on(conn, day)
        deviation = recoverymod.for_day(conn, day)

        if same_day:
            is_missed = False
            reason = f"{same_day} session(s) on the day; unmatched rather than missed"
        elif load_recorded:
            is_missed = False
            reason = "load recorded upstream with no local activity; the upload is missing"
        elif load_recorded is None:
            is_missed = True
            reason = "no activity, and the wellness feed had nothing for the day"
        else:
            is_missed = True
            reason = "no activity and no load recorded on the day"

        verdicts.append(
            {
                "prescription_id": candidate["id"],
                "planned_for": candidate["planned_for"],
                "local_date": day,
                "missed": is_missed,
                "reason": reason,
                # What was known when the verdict was reached. ADJ-08 reads this.
                "signals": {
                    "sessions_on_day": same_day,
                    "load_recorded": load_recorded,
                    "recovery_deviation": (
                        float(deviation.deviation)
                        if deviation is not None and deviation.usable
                        else None
                    ),
                },
                # ADJ-08: "With wellness unavailable, the system asks rather than
                # acts." Marking a prescription missed is bookkeeping and happens
                # anyway; restructuring the week off it is an action and does not.
                "safe_to_act": load_recorded is not None,
            }
        )
    return verdicts


def mark_missed(conn: psycopg.Connection, prescription_ids: list[int]) -> int:
    if not prescription_ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update prescriptions set status = 'missed' where id = any(%s) and session_id is null",
            (prescription_ids,),
        )
        return cur.rowcount


def local_day_of(moment: datetime, tz: ZoneInfo) -> date:
    return clock.local_day(moment, tz)
