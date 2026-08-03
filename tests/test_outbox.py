"""The outbox: what the coach says without being asked, and reads back later.

The bug these are the regression for is the third of its kind in this project,
after the runtime that nothing constructed and the plan the coach could write
and never read. `telegram.bot.record_reply` had one caller, inside `bot.drain`,
so a message the athlete did not prompt was posted to Telegram and written
nowhere. Every test in the first section fails against the old scheduler.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb

import conftest
from coach.notify import daily, outbox
from coach.runtime import scheduler, turn

TODAY = date(2026, 8, 3)
DUBAI = ZoneInfo("Asia/Dubai")
MORNING = datetime.combine(TODAY, datetime.min.time()).replace(hour=6, tzinfo=UTC)


def prescribe(conn: psycopg.Connection, on: date) -> int:
    block_id = conftest.ensure_block(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status) "
            "values (%s, %s, 'ride', %s, 'planned') returning id",
            (
                block_id,
                datetime.combine(on, datetime.min.time()).replace(hour=18, tzinfo=UTC),
                Jsonb({"duration_s": 3600, "purpose": "Aerobic endurance"}),
            ),
        )
        return int(cur.fetchone()["id"])


def coach_messages(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("select body, kind, period_key from messages where role = 'coach' order by id")
        return cur.fetchall()


# --- the defect itself ------------------------------------------------------


def test_a_proactive_message_is_recorded_as_something_the_coach_said(
    conn: psycopg.Connection,
) -> None:
    """The morning message reaches `messages`, not only Telegram."""
    prescribe(conn, TODAY)
    sent: list[str] = []

    daily.morning_job(outbox.Outbox(sent.append))(conn, TODAY)

    recorded = coach_messages(conn)
    assert len(sent) == 1
    assert len(recorded) == 1
    assert recorded[0]["body"] == sent[0]
    assert recorded[0]["kind"] == "morning"
    assert recorded[0]["period_key"] == TODAY.isoformat()


def test_the_coach_can_read_back_what_it_said_this_morning(
    conn: psycopg.Connection,
) -> None:
    """The regression that matters, stated as the athlete experiences it.

    The evening follow-up offers to move the session. The athlete answers "yes,
    move it". Before this, `turn._history` could not see the offer, so the model
    was reading a one word answer to a question it had no record of asking.
    """
    prescribe(conn, TODAY)
    sent: list[str] = []

    daily.follow_up_job(outbox.Outbox(sent.append))(conn, TODAY)

    history = turn._history(conn, [{"body": "yes, move it"}], is_catch_up=False)

    assert sent, "the follow-up should have gone out"
    offered = [m for m in history if m["role"] == "assistant" and m["content"] == sent[0]]
    assert offered, f"the coach's own offer is missing from {history}"
    # And it comes before the answer to it, or the model reads them backwards.
    assert history.index(offered[0]) < len(history) - 1


# --- the resend window `scheduled_runs` cannot close ------------------------


def test_a_job_that_sent_and_then_failed_does_not_send_twice(
    conn: psycopg.Connection,
) -> None:
    """The gap between the two ledgers, which is why both exist.

    `scheduler.claim` re-claims a job whose status is 'failed' while attempts
    remain. So a job that posted its message and then raised is run again, and
    without the outbox's own key the athlete gets the message twice. This is
    what training-tracker's athlete experienced three Saturdays running.
    """
    prescribe(conn, TODAY)
    sent: list[str] = []
    box = outbox.Outbox(sent.append)

    def sends_then_fails(conn: psycopg.Connection, on: date) -> None:
        daily.morning_job(box)(conn, on)
        raise RuntimeError("something after the send blew up")

    jobs = {
        "morning": scheduler.Job(
            run=sends_then_fails, schedule=scheduler.Schedule(hour=6, covers="today")
        )
    }

    scheduler.run_due(conn, MORNING, DUBAI, jobs)
    outcomes = scheduler.run_due(conn, MORNING, DUBAI, jobs)

    # The scheduler did re-run it, which is the precondition for the bug...
    assert outcomes.get("morning", "").startswith("failed")
    # ...and the athlete still heard it once.
    assert len(sent) == 1
    assert len(coach_messages(conn)) == 1


def test_the_same_period_is_declined_rather_than_raising(conn: psycopg.Connection) -> None:
    """A second attempt is a normal outcome, reported as False."""
    box = outbox.Outbox(lambda _: None)

    assert box.send(conn, "morning message", kind="morning", period_key="2026-08-03") is True
    assert box.send(conn, "morning message", kind="morning", period_key="2026-08-03") is False
    assert len(coach_messages(conn)) == 1


def test_a_different_period_is_a_different_message(conn: psycopg.Connection) -> None:
    box = outbox.Outbox(lambda _: None)

    box.send(conn, "monday", kind="morning", period_key="2026-08-03")
    box.send(conn, "tuesday", kind="morning", period_key="2026-08-04")

    assert [m["body"] for m in coach_messages(conn)] == ["monday", "tuesday"]


def test_two_kinds_on_one_day_do_not_collide(conn: psycopg.Connection) -> None:
    """The morning message and the evening follow-up share a date and not a key."""
    box = outbox.Outbox(lambda _: None)

    assert box.send(conn, "today: ride", kind="morning", period_key="2026-08-03") is True
    assert box.send(conn, "still on?", kind="follow_up", period_key="2026-08-03") is True

    assert len(coach_messages(conn)) == 2


# --- a claim that was never spoken ------------------------------------------


def test_a_failed_send_leaves_no_record_of_having_spoken(conn: psycopg.Connection) -> None:
    """The row is written first, so the failure path has to take it back.

    Leaving it would be worse than the bug being fixed: the coach's own history
    would assert it had said something it never said, and the next turn would
    read that as fact.
    """

    def explodes(_: str) -> None:
        raise RuntimeError("telegram is down")

    box = outbox.Outbox(explodes)

    with pytest.raises(RuntimeError):
        box.send(conn, "today: ride", kind="morning", period_key="2026-08-03")

    assert coach_messages(conn) == []


def test_a_released_period_can_be_sent_again(conn: psycopg.Connection) -> None:
    """Which is the point of releasing it: the scheduler's retry must work."""
    failing = True

    def flaky(_: str) -> None:
        if failing:
            raise RuntimeError("telegram is down")

    box = outbox.Outbox(flaky)
    with pytest.raises(RuntimeError):
        box.send(conn, "today: ride", kind="morning", period_key="2026-08-03")

    failing = False
    assert box.send(conn, "today: ride", kind="morning", period_key="2026-08-03") is True
    assert len(coach_messages(conn)) == 1


# --- an unkeyed message is recorded without claiming anything ---------------


def test_an_event_driven_notice_is_recorded_but_claims_no_period(
    conn: psycopg.Connection,
) -> None:
    """ADJ-06's shape: labelled, in the history, and not once-per-period.

    Its idempotency is `adjustment_events.announced`, so a period key here would
    be a second answer to a question that already has one.
    """
    box = outbox.Outbox(lambda _: None)

    box.send(conn, "Thursday is now 45 minutes", kind="adjustment")
    box.send(conn, "Friday is now 45 minutes", kind="adjustment")

    recorded = coach_messages(conn)
    assert len(recorded) == 2
    assert {m["kind"] for m in recorded} == {"adjustment"}
    assert {m["period_key"] for m in recorded} == {None}


def test_an_empty_message_is_not_recorded(conn: psycopg.Connection) -> None:
    sent: list[str] = []

    assert outbox.Outbox(sent.append).send(conn, "   ", kind="morning") is False
    assert sent == []
    assert coach_messages(conn) == []


# --- the scheduler builds one of these, not a bare sender -------------------


def test_the_scheduler_hands_the_jobs_an_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare sender is what the old `_send_or_none` returned, and is the bug.

    Asserting on the constructed object rather than on the source text, so this
    keeps working if the wiring moves.
    """
    posted: list[tuple[int, str]] = []

    class FakeTelegram:
        def send(self, chat_id: int, text: str) -> None:
            posted.append((chat_id, text))

    monkeypatch.setattr("coach.runtime.transport.Telegram", lambda *a, **k: FakeTelegram())
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "4242")

    box = scheduler._outbox_or_none()

    assert isinstance(box, outbox.Outbox)
    box._post("hello")
    assert posted == [(4242, "hello")]


def test_a_missing_telegram_token_costs_the_messages_and_not_the_night(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged behaviour, asserted because the constructor moved."""
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_ID", raising=False)

    assert scheduler._outbox_or_none() is None


# --- the review keeps its plain sender --------------------------------------


def test_binding_produces_a_one_argument_sender(conn: psycopg.Connection) -> None:
    """`review.weekly.run` takes a `Callable[[str], None]` and should keep doing so."""
    sent: list[str] = []
    send = outbox.Outbox(sent.append).bind(conn, "review", "2026-08-02")

    send("this week you rode three times")

    assert sent == ["this week you rode three times"]
    recorded = coach_messages(conn)
    assert recorded[0]["kind"] == "review"
    assert recorded[0]["period_key"] == "2026-08-02"


def test_a_second_review_for_the_same_sunday_is_declined(conn: psycopg.Connection) -> None:
    sent: list[str] = []
    box = outbox.Outbox(sent.append)

    box.bind(conn, "review", "2026-08-02")("the first one")
    box.bind(conn, "review", "2026-08-02")("the second one")

    assert sent == ["the first one"]


# --- ordering, which is what makes the history readable ---------------------


def test_recorded_messages_carry_the_moment_they_were_sent(
    conn: psycopg.Connection,
) -> None:
    """`turn._history` orders on `occurred_at`, so a wrong stamp reorders the day."""
    box = outbox.Outbox(lambda _: None)
    morning_at = datetime.combine(TODAY, datetime.min.time()).replace(hour=6, tzinfo=UTC)
    evening_at = morning_at + timedelta(hours=15)

    box.send(conn, "today: ride", kind="morning", period_key="a", now=morning_at)
    box.send(conn, "still on?", kind="follow_up", period_key="b", now=evening_at)

    with conn.cursor() as cur:
        cur.execute("select body, occurred_at from messages order by occurred_at")
        rows = cur.fetchall()

    assert [r["body"] for r in rows] == ["today: ride", "still on?"]
