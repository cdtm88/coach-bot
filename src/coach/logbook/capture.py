"""Gym sessions and golf rounds, captured in chat. LOG-01 to LOG-08.

There is no feed for either. A gym session in an apartment building gym and a
round of golf both produce nothing a device uploads, and both cost real load —
GYM-08 exists precisely so they can sit on the same scale as a ride.

**The local record is the deliverable; the upstream write is a courtesy.**
LOG-08 says an upstream failure never blocks or delays the local record or the
conversation, so the two are separate calls and the second one swallows its
errors. Ordering them the other way round would mean a session the athlete told
the coach about could be lost because intervals.icu was down.

**One question at a time** (LOG-04). This module contributes the "what is still
missing" half — `missing_question` returns at most one — and the naturalness
check in `agent/naturalness.py` enforces the rest. Two mechanisms because they
guard different failures: a form generated in one turn, and a model that asks
three things in a sentence.

**Walked or carted is not a detail** (LOG-03). It roughly doubles the load of a
round, which is the difference between a golf day that costs nothing and one
that costs an endurance ride. So it is the one thing a round is not allowed to
be missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from coach.blocks import load as loadmod
from coach.ingest import review as reviewmod

log = logging.getLogger(__name__)

DISCIPLINES = ("gym", "golf", "other")

# What the sessions table calls them. 'weighttraining' is intervals.icu's own
# spelling and the one `recompute_rollups` counts for GYM-08's session count, so
# it is used here rather than a tidier local name.
SESSION_DISCIPLINE = {"gym": "weighttraining", "golf": "golf", "other": "workout"}

# LOG-03. Walking eighteen holes is four hours on your feet; a cart is not.
# These are minutes of equivalent effort per hole, and they are estimates —
# what matters is that the two differ enough for the choice to change the plan.
GOLF_MINUTES_PER_HOLE = {"walked": 13, "carted": 6}
GOLF_RPE = {"walked": Decimal("4"), "carted": Decimal("2")}
DEFAULT_HOLES = 18

# The default when a gym session is described without one. Deliberately mid
# scale: guessing high would let a vague report eat the week's ramp under
# BLOCK-07, and guessing low would let it disappear.
DEFAULT_GYM_RPE = Decimal("6")


class Incomplete(ValueError):
    """Not enough to record. Carries the single question that would fix it."""

    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


@dataclass(frozen=True)
class Captured:
    session_id: int
    discipline: str
    occurred_on: date
    load: Decimal
    prescription_id: int | None = None
    external_ref: str | None = None
    upstream_error: str | None = None


def missing_question(discipline: str, detail: dict[str, Any]) -> str | None:
    """LOG-03 and LOG-04: the one thing worth asking, or nothing.

    At most one, and never a list. A capture turn that asked for holes, mode of
    transport and duration at once would be a form, which is the thing LOG-04
    names.
    """
    if discipline == "golf":
        if detail.get("transport") not in GOLF_MINUTES_PER_HOLE:
            return "Did you walk it or take a cart?"
        return None

    if discipline == "gym":
        if not detail.get("movements"):
            return "What did you actually get through?"
        if not detail.get("duration_minutes"):
            return "Roughly how long were you in there?"
    return None


def _golf_load(detail: dict[str, Any]) -> tuple[Decimal, int]:
    transport = detail.get("transport")
    holes = int(detail.get("holes") or DEFAULT_HOLES)
    minutes = GOLF_MINUTES_PER_HOLE[transport] * holes
    return loadmod.gym_load(GOLF_RPE[transport], minutes), minutes


def _gym_load(detail: dict[str, Any]) -> tuple[Decimal, int]:
    minutes = int(detail.get("duration_minutes") or 0)
    rpe = Decimal(str(detail.get("rpe") or DEFAULT_GYM_RPE))
    return loadmod.gym_load(rpe, minutes), minutes


def load_of(discipline: str, detail: dict[str, Any]) -> tuple[Decimal, int]:
    """GYM-08's combined scale, so a captured session competes with a ride."""
    if discipline == "golf":
        return _golf_load(detail)
    if discipline == "gym":
        return _gym_load(detail)
    minutes = int(detail.get("duration_minutes") or 0)
    rpe = Decimal(str(detail.get("rpe") or DEFAULT_GYM_RPE))
    return loadmod.gym_load(rpe, minutes), minutes


def record(
    conn: psycopg.Connection,
    discipline: str,
    occurred_on: date,
    detail: dict[str, Any],
    tz: ZoneInfo,
) -> Captured:
    """LOG-01, LOG-02, LOG-05: the local record, which is the part that must not fail.

    The load goes into `derived` under the same key the activity feed uses, so
    `recompute_rollups` picks it up without knowing this path exists — which is
    what makes LOG-05's "counts toward weekly load" true rather than aspirational.
    """
    if discipline not in DISCIPLINES:
        raise ValueError(f"discipline must be one of {DISCIPLINES}, got {discipline!r}")
    question = missing_question(discipline, detail)
    if question is not None:
        raise Incomplete(question)

    load, minutes = load_of(discipline, detail)
    started_at = datetime.combine(occurred_on, time(hour=12), tzinfo=tz)
    derived = {
        "icu_training_load": float(load),
        "captured_in_chat": True,
        # LOG-02: kept whole rather than flattened, because "how it sat against
        # active constraints" is prose and the next question about it is not
        # predictable enough to schematise.
        "detail": detail,
    }

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into sessions
                (source, discipline, activity_type, name, started_at, local_date,
                 duration_s, derived)
            values ('chat', %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                SESSION_DISCIPLINE[discipline],
                discipline,
                detail.get("name") or discipline.title(),
                started_at,
                occurred_on,
                minutes * 60,
                Jsonb(derived),
            ),
        )
        session_id = int(cur.fetchone()["id"])

    # LOG-05's other half: adherence. A gym session the coach prescribed and the
    # athlete did should close that prescription, and the matcher already knows
    # how — reusing it means chat capture and feed ingest cannot drift apart.
    prescription_id = reviewmod.match(conn, session_id)
    if prescription_id is not None:
        reviewmod.attach(conn, session_id, prescription_id)

    log.info(
        "captured %s on %s from chat: load %s, prescription %s",
        discipline,
        occurred_on,
        load,
        prescription_id,
    )
    return Captured(
        session_id=session_id,
        discipline=discipline,
        occurred_on=occurred_on,
        load=load,
        prescription_id=prescription_id,
    )


def push_upstream(conn: psycopg.Connection, api: Any, captured: Captured) -> Captured:
    """LOG-06 and LOG-07, with LOG-08 as the reason this is a separate call.

    `POST /athlete/{id}/activities/manual`, verified present in the live spec on
    27 July 2026. Every failure is swallowed and recorded: the requirement says
    "fails without affecting the local record", and the local record is already
    written by the time this runs.
    """
    from dataclasses import replace

    payload = {
        "name": f"{captured.discipline.title()} (logged in chat)",
        "start_date_local": datetime.combine(captured.occurred_on, time(hour=12)).isoformat(),
        "type": SESSION_DISCIPLINE[captured.discipline],
        "elapsed_time": _duration_s(conn, captured.session_id),
        "icu_training_load": float(captured.load),
    }
    try:
        created = api.create_manual_activity(payload)
    except Exception as exc:  # noqa: BLE001 - LOG-08: never affects the local record
        log.warning("could not write session %s upstream: %s", captured.session_id, exc)
        return replace(captured, upstream_error=str(exc))

    external_ref = (created or {}).get("id")
    if external_ref:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "update sessions set external_ref = %s where id = %s and external_ref is null",
                (str(external_ref), captured.session_id),
            )
    return replace(captured, external_ref=str(external_ref) if external_ref else None)


def _duration_s(conn: psycopg.Connection, session_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("select duration_s from sessions where id = %s", (session_id,))
        row = cur.fetchone()
    return int((row or {}).get("duration_s") or 0)
