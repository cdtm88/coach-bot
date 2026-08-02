"""The daily rhythm: NOTIF-01, NOTIF-02, NOTIF-03, NOTIF-06.

Most of these assert that the coach says *nothing*. That is the shape of the
requirement — NOTIF-02 is a list of conditions under which a follow-up would be
wrong, and the failure mode it guards against is a bot that asks every evening
whether you trained.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb

import conftest
from coach.health import breaks as breakmod
from coach.notify import daily
from coach.runtime import scheduler

TODAY = date(2026, 8, 3)
DUBAI = ZoneInfo("Asia/Dubai")


def prescribe(conn: psycopg.Connection, on: date, status: str = "planned", **spec: object) -> int:
    block_id = conftest.ensure_block(conn)
    body = {"duration_s": 3600, "purpose": "Aerobic endurance", **spec}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status) "
            "values (%s, %s, 'ride', %s, %s) returning id",
            (
                block_id,
                datetime.combine(on, datetime.min.time()).replace(hour=18, tzinfo=UTC),
                Jsonb(body),
                status,
            ),
        )
        return int(cur.fetchone()["id"])


def upload(conn: psycopg.Connection, on: date) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (discipline, started_at, local_date, source) "
            "values ('ride', %s, %s, 'intervals')",
            (datetime.combine(on, datetime.min.time()).replace(hour=17, tzinfo=UTC), on),
        )


def wellness(conn: psycopg.Connection, on: date, atl_load: float | None) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into wellness (local_date, atl_load) values (%s, %s) "
            "on conflict (local_date) do update set atl_load = excluded.atl_load",
            (on, atl_load),
        )


# --- NOTIF-01: the morning message ------------------------------------------


def test_the_morning_message_states_the_day_s_session(conn: psycopg.Connection) -> None:
    prescribe(conn, TODAY)

    message = daily.morning(conn, TODAY)

    assert message is not None
    assert "60 min" in message
    assert "Aerobic endurance" in message


def test_a_rest_day_is_confirmed_rather_than_passed_over(
    conn: psycopg.Connection,
) -> None:
    """NOTIF-01 says "or confirms it is a rest day", and silence is not that.

    "Nothing today" is information the athlete acts on. A morning with no message
    is indistinguishable from a broken scheduler.
    """
    assert daily.morning(conn, TODAY) == "Rest day today. Nothing prescribed."


def test_two_sessions_in_a_day_are_both_named(conn: psycopg.Connection) -> None:
    prescribe(conn, TODAY, purpose="Endurance")
    prescribe(conn, TODAY, purpose="Gym")

    message = daily.morning(conn, TODAY)

    assert "Endurance" in message and "Gym" in message


# --- NOTIF-02: the evening follow-up ----------------------------------------


def test_a_prescribed_session_with_nothing_uploaded_gets_one_follow_up(
    conn: psycopg.Connection,
) -> None:
    prescribe(conn, TODAY)

    message = daily.follow_up(conn, TODAY)

    assert message is not None
    assert "still on today's plan" in message


def test_no_follow_up_when_the_activity_has_landed(conn: psycopg.Connection) -> None:
    """NOTIF-02's acceptance: "not when an activity has already landed"."""
    prescribe(conn, TODAY)
    upload(conn, TODAY)

    assert daily.follow_up(conn, TODAY) is None


def test_load_recorded_with_no_activity_suppresses_it_entirely(
    conn: psycopg.Connection,
) -> None:
    """NOTIF-02's acceptance, and the clause that is easiest to miss.

    "Load recorded with no activity means the upload is missing, not the
    session." Asking whether the athlete trained, when the platform can already
    see that they did, is the exact thing this requirement forbids.
    """
    prescribe(conn, TODAY)
    wellness(conn, TODAY, atl_load=42.0)

    assert daily.follow_up(conn, TODAY) is None


def test_no_wellness_row_is_not_read_as_no_training(conn: psycopg.Connection) -> None:
    """Absence of data is never evidence of absence of activity.

    `load_recorded_on` returns None here — the feed has nothing for the day —
    and None must not act like False *or* like True. It is the coach not
    knowing, which is precisely the case the follow-up exists for.
    """
    prescribe(conn, TODAY)

    assert daily.follow_up(conn, TODAY) is not None


def test_a_zero_load_day_still_gets_the_follow_up(conn: psycopg.Connection) -> None:
    """False means the feed covered the day and recorded none. That warrants asking."""
    prescribe(conn, TODAY)
    wellness(conn, TODAY, atl_load=0.0)

    assert daily.follow_up(conn, TODAY) is not None


def test_no_follow_up_when_nothing_was_prescribed(conn: psycopg.Connection) -> None:
    assert daily.follow_up(conn, TODAY) is None


def test_a_completed_prescription_is_not_chased(conn: psycopg.Connection) -> None:
    prescribe(conn, TODAY, status="completed")

    assert daily.follow_up(conn, TODAY) is None


def test_the_follow_up_is_an_offer_not_a_chase(conn: psycopg.Connection) -> None:
    """RECOV-06 decides the framing, which is the whole difference.

    Under-recovered, the message offers to move or drop the session. It never
    asks the athlete to account for themselves.
    """
    prescribe(conn, TODAY)
    _score_recovery(conn, TODAY, deviation=-2.0)

    message = daily.follow_up(conn, TODAY)

    assert "Happy to move it or drop it" in message


# --- NOTIF-03: breaks suppress both -----------------------------------------


def test_a_break_suppresses_the_morning_message(conn: psycopg.Connection) -> None:
    prescribe(conn, TODAY)
    breakmod.create(conn, "holiday", TODAY - timedelta(days=1), TODAY + timedelta(days=5))

    assert daily.morning(conn, TODAY) is None


def test_a_break_suppresses_the_follow_up(conn: psycopg.Connection) -> None:
    """NOTIF-03's acceptance: a break suppresses both NOTIF-01 and NOTIF-02."""
    prescribe(conn, TODAY)
    breakmod.create(conn, "holiday", TODAY - timedelta(days=1), TODAY + timedelta(days=5))

    assert daily.follow_up(conn, TODAY) is None


# --- "fires once" -----------------------------------------------------------


def test_the_follow_up_fires_once_not_repeatedly(conn: psycopg.Connection) -> None:
    """NOTIF-02's acceptance, through the scheduler that actually enforces it.

    The ledger's (job, local_date) key is what makes "once" true, which is why
    `follow_up` itself can be a pure function of the day.
    """
    prescribe(conn, TODAY)
    sent: list[str] = []
    jobs = {
        "follow_up": scheduler.Job(
            run=daily.follow_up_job(sent.append),
            schedule=scheduler.Schedule(hour=21, covers="today"),
        )
    }
    evening = datetime.combine(TODAY, datetime.min.time()).replace(hour=17, tzinfo=UTC)

    scheduler.run_due(conn, evening, DUBAI, jobs)
    scheduler.run_due(conn, evening, DUBAI, jobs)

    assert len(sent) == 1


# --- NOTIF-06: one owner for weigh-in prompting -----------------------------


def test_nothing_here_mentions_weight(conn: psycopg.Connection) -> None:
    """NOTIF-06: no separate notification path exists.

    `health/bodymass.py` owns weigh-in prompting, emits at most one mention per
    gap, and goes through the CHAT-11 interruption budget. A second path here
    would produce two mentions for one gap, and neither would know about the
    other — which is exactly the failure "owned solely by" forbids.
    """
    from pathlib import Path

    source = Path(daily.__file__).read_text().lower()
    body = source.split('"""', 2)[2]  # past the module docstring, which explains the rule

    assert "weigh" not in body
    assert "body_mass" not in body


def _score_recovery(conn: psycopg.Connection, on: date, deviation: float) -> None:
    """Enough wellness history for `for_day` to return a usable deviation.

    The baseline needs *spread*, not just length: the deviation SQL requires
    `sd > 0` per metric, so a month of identical readings scores nothing at all.
    """
    for offset in range(1, 30):
        day = on - timedelta(days=offset)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "insert into wellness (local_date, hrv, resting_hr, sleep_secs) "
                "values (%s, %s, %s, %s) on conflict (local_date) do nothing",
                (day, 58 + offset % 5, 49 + offset % 3, 27000 + (offset % 4) * 900),
            )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into wellness (local_date, hrv, resting_hr, sleep_secs) "
            "values (%s, 30, 62, 18000) on conflict (local_date) do update set "
            "hrv = excluded.hrv, resting_hr = excluded.resting_hr, "
            "sleep_secs = excluded.sleep_secs",
            (on,),
        )


@pytest.fixture(autouse=True)
def _clean(conn: psycopg.Connection) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("delete from breaks")
