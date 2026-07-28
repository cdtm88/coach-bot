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
    """FIT-05: find the prescription this session satisfies, by date and discipline.

    Only unmatched prescriptions are eligible, so two rides on one day cannot both
    claim the same prescribed session.
    """
    with conn.cursor() as cur:
        cur.execute("select local_date, discipline from sessions where id = %s", (session_id,))
        session = cur.fetchone()
        if session is None:
            return None

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
    """Prescriptions old enough to call missed (FIT-12).

    Two gates. The grace window covers the overnight upload, and the load cross
    check covers the ride that happened with a broken sync: a session on the day
    with no prescription attached is evidence the athlete trained, so the
    prescription is unmatched rather than skipped.
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
        verdicts.append(
            {
                "prescription_id": candidate["id"],
                "planned_for": candidate["planned_for"],
                "local_date": day,
                "missed": same_day == 0,
                "reason": (
                    "no activity of any kind on the day"
                    if same_day == 0
                    else f"{same_day} session(s) on the day; unmatched rather than missed"
                ),
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
