"""P10's acceptance, as the PRD words it.

    "A review generates with all five sections from rollups, daily nudges fire
    on seeded timings, a seeded break suppresses them, and a gym session closes
    from chat alone and reaches the rollups."

Four sentences, four tests, driven end to end through the scheduler rather than
by calling the pieces. The per-requirement tests live in `test_review.py`,
`test_notify.py`, `test_breaks.py` and `test_logbook.py`; these assert that the
wiring between them exists, which is the thing no unit test can see.

The seam this guards is real and has been wrong before: eight phases were merged
once with nothing constructing a model client, and every underlying test passed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

import conftest
from coach.health import breaks as breakmod
from coach.ingest import reconcile
from coach.logbook import capture
from coach.notify import charts, daily, outbox
from coach.review import weekly
from coach.runtime import scheduler

DUBAI = ZoneInfo("Asia/Dubai")
SUNDAY = date(2026, 8, 2)
MONDAY = date(2026, 8, 3)


def at(day: date, hour: int) -> datetime:
    """A moment in the athlete's zone, expressed the way the scheduler sees it."""
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, tzinfo=DUBAI)


def prescribe(conn: psycopg.Connection, on: date, discipline: str = "ride") -> int:
    block_id = conftest.ensure_block(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status) "
            "values (%s, %s, %s, %s, 'planned') returning id",
            (
                block_id,
                datetime.combine(on, datetime.min.time()).replace(hour=18, tzinfo=UTC),
                discipline,
                Jsonb({"duration_s": 3600, "purpose": "Aerobic endurance"}),
            ),
        )
        return int(cur.fetchone()["id"])


def p10_jobs(sent: list[str]) -> dict[str, scheduler.Job]:
    """The three jobs `scheduler.main` registers, built the same way.

    Through an `Outbox`, because that is now what `main` hands them and the
    whole value of this helper is that it is not a second way of building the
    same thing. It also means these acceptance tests exercise the recording
    path rather than only the sending one.
    """
    box = outbox.Outbox(sent.append)
    return {
        "morning": scheduler.Job(run=daily.morning_job(box), schedule=scheduler.morning_schedule()),
        "follow_up": scheduler.Job(
            run=daily.follow_up_job(box), schedule=scheduler.follow_up_schedule()
        ),
        "review": scheduler.Job(
            run=lambda conn, on: weekly.run(
                conn, on, send=box.bind(conn, "review", on.isoformat())
            ),
            schedule=scheduler.review_schedule(),
        ),
    }


# --- "a review generates with all five sections from rollups" ---------------


def test_the_review_generates_from_the_scheduler_on_a_sunday(
    conn: psycopg.Connection,
) -> None:
    """All six sections, from rollups, in the record the Sunday leaves behind.

    The record rather than the message, and the difference is the point. REV-02
    is a requirement about what the review *knows*, and the stored note is what
    anything later reads to find out what was known on a given Sunday. The
    message is what the athlete reads, and it does not spend a labelled line on
    each of five sections that have nothing in them.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into rollups (as_of, load_7d, load_28d) values (%s, 420, 1500) "
            "on conflict (as_of) do nothing",
            (SUNDAY,),
        )
    sent: list[str] = []

    scheduler.run_due(conn, at(SUNDAY, 19), DUBAI, p10_jobs(sent))

    with conn.cursor() as cur:
        cur.execute("select body from notes where kind = 'review'")
        record = cur.fetchone()["body"]
    for title in weekly.SECTIONS:
        assert f"{title}:" in record

    review = next(m for m in sent if "Week ending" in m)
    assert "420 over the week" in review


def test_the_review_does_not_fire_on_a_weekday(conn: psycopg.Connection) -> None:
    sent: list[str] = []

    scheduler.run_due(conn, at(MONDAY, 19), DUBAI, p10_jobs(sent))

    assert not any("Week ending" in m for m in sent)


# --- "daily nudges fire on seeded timings" ----------------------------------


def test_the_morning_message_fires_at_its_configured_hour(
    conn: psycopg.Connection, monkeypatch
) -> None:
    """NOTIF-05: seeded timings, not hardcoded ones."""
    monkeypatch.setenv("COACH_MORNING_HOUR", "5")
    prescribe(conn, MONDAY)
    sent: list[str] = []

    scheduler.run_due(conn, at(MONDAY, 4), DUBAI, p10_jobs(sent))
    assert sent == []

    scheduler.run_due(conn, at(MONDAY, 6), DUBAI, p10_jobs(sent))
    assert any("Today:" in m for m in sent)


def test_the_evening_follow_up_fires_after_the_morning_message(
    conn: psycopg.Connection,
) -> None:
    """Two jobs, two hours, one tick each — and the later one waits its turn."""
    prescribe(conn, MONDAY)
    sent: list[str] = []
    jobs = p10_jobs(sent)

    scheduler.run_due(conn, at(MONDAY, 7), DUBAI, jobs)
    assert len(sent) == 1

    scheduler.run_due(conn, at(MONDAY, 22), DUBAI, jobs)
    assert len(sent) == 2
    assert "still on today's plan" in sent[1]


# --- "a seeded break suppresses them" ---------------------------------------


def test_a_seeded_break_suppresses_both_nudges(conn: psycopg.Connection) -> None:
    """NOTIF-03, through the scheduler rather than by calling the functions."""
    prescribe(conn, MONDAY)
    breakmod.create(conn, "holiday", MONDAY - timedelta(days=1), MONDAY + timedelta(days=6))
    sent: list[str] = []

    scheduler.run_due(conn, at(MONDAY, 22), DUBAI, p10_jobs(sent))

    assert sent == []


def test_the_break_tool_suspends_the_week_it_covers(conn: psycopg.Connection) -> None:
    """BREAK-01 and BREAK-02 in one call, which is how the athlete meets them.

    A break recorded without suspending the week would leave the coach messaging
    about sessions it has just agreed are not happening.
    """
    from coach.agent import tools

    prescription_id = prescribe(conn, MONDAY + timedelta(days=2))

    result = tools.dispatch(
        conn,
        "set_break",
        {
            "kind": "holiday",
            "starts_on": MONDAY.isoformat(),
            "ends_on": (MONDAY + timedelta(days=6)).isoformat(),
        },
    )

    assert result["suspended"] == 1
    with conn.cursor() as cur:
        cur.execute("select status from prescriptions where id = %s", (prescription_id,))
        assert cur.fetchone()["status"] == "suspended"


# --- "a gym session closes from chat alone and reaches the rollups" ---------


def test_a_gym_session_closes_from_chat_and_reaches_the_rollups(
    conn: psycopg.Connection,
) -> None:
    """The whole LOG path, ending in the number the review will quote."""
    from coach.agent import tools

    prescription_id = prescribe(conn, MONDAY, discipline="weighttraining")

    result = tools.dispatch(
        conn,
        "log_session",
        {
            "discipline": "gym",
            "occurred_on": MONDAY.isoformat(),
            "detail": {
                "movements": ["goblet squat 3x10", "dead bug 3x8"],
                "duration_minutes": 45,
                "rpe": 7,
            },
        },
    )
    reconcile.recompute_rollups(conn)

    assert result["recorded"] is True
    assert result["closed_prescription_id"] == prescription_id

    with conn.cursor() as cur:
        cur.execute("select status from prescriptions where id = %s", (prescription_id,))
        assert cur.fetchone()["status"] == "completed"
        cur.execute("select load_7d from rollups where as_of = %s", (MONDAY,))
        assert cur.fetchone()["load_7d"] == Decimal("63.00")


def test_the_captured_session_reaches_the_review(conn: psycopg.Connection) -> None:
    """The end of the loop: a chat-only session shows up in Sunday's adherence."""
    prescribe(conn, SUNDAY - timedelta(days=2), discipline="weighttraining")
    capture.record(
        conn,
        "gym",
        SUNDAY - timedelta(days=2),
        {"movements": ["squat"], "duration_minutes": 40, "rpe": 6},
        DUBAI,
    )
    reconcile.recompute_rollups(conn)

    assert "1 of 1" in weekly.adherence_section(conn, SUNDAY).body


# --- NOTIF-04 ----------------------------------------------------------------


def test_a_chart_request_returns_a_working_link(conn: psycopg.Connection, monkeypatch) -> None:
    """NOTIF-04's acceptance: any chart request returns a working link.

    "Working" is checked by fetching what the link points at through the same
    handler that serves it, rather than by asserting the string looks like a URL.
    """
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://coach.example")
    for offset in range(5):
        day = SUNDAY - timedelta(days=offset)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "insert into rollups (as_of, load_7d) values (%s, %s) "
                "on conflict (as_of) do update set load_7d = excluded.load_7d",
                (day, 400 + offset * 10),
            )

    url = charts.link("load")
    assert url == "https://coach.example/charts/load"

    code, body = charts.page(conn, "load", SUNDAY)
    assert code == 200
    assert "<svg" in body
    assert "7 day load" in body


def test_an_unknown_chart_is_a_404_not_a_traceback(conn: psycopg.Connection) -> None:
    code, _ = charts.page(conn, "everything", SUNDAY)

    assert code == 404


def test_a_chart_with_nothing_to_draw_still_renders(conn: psycopg.Connection) -> None:
    """A new deployment has no rollups, and a 500 there reads as a broken link."""
    code, body = charts.page(conn, "weight", SUNDAY)

    assert code == 200
    assert "Not enough data yet" in body
