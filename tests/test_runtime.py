"""The runtime wiring: the three seams that made the merged phases runnable.

Nothing here is a new requirement. These tests assert that the loop calls the
rules that already exist, in the right order, and that the two processes survive
the things that will actually happen to them — a transport that fails, a model
that keeps asking for tools, a spend cap that trips, a night that was missed.

The model and the transport are both fakes. That is not a compromise: the point
of `coach.llm.client` and `coach.telegram.bot` taking their client as a
parameter is that the loop around them can be driven without a network, and
these are the tests that cash that in.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest

from coach.memory import facts
from coach.runtime import agent, models, scheduler, transport, turn
from coach.telegram import bot as botmod

DUBAI = ZoneInfo("Asia/Dubai")
NOW = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)
CHAT_ID = 4242


# --- fakes -------------------------------------------------------------------


@dataclass
class FakeUse:
    """One tool_use block, shaped like the SDK's."""

    name: str
    input: dict[str, Any]
    id: str = "toolu_1"
    type: str = "tool_use"


@dataclass
class FakeReply:
    text: str = ""
    tool_uses: list[FakeUse] = field(default_factory=list)


class FakeModel:
    """A model that returns a scripted sequence and records what it was asked.

    Deliberately not an `anthropic.Anthropic` stand-in: it replaces
    `coach.llm.client.complete`, because everything below the wrapper — routing,
    streaming, cost accounting — has its own tests and is not what this suite is
    about.
    """

    def __init__(self, *replies: FakeReply):
        self.replies = list(replies) or [FakeReply("Fine. How did the hip feel?")]
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        client,
        purpose,
        system,
        messages,
        tools=None,
        conn=None,
        on_text=None,
        turn_id=None,
    ):
        from coach.llm.client import Completion

        self.calls.append(
            {
                "purpose": purpose,
                "system": system,
                "messages": list(messages),
                "tools": tools,
                # OBS-12. Recorded rather than ignored, so the tests below can
                # assert that every call of one exchange carries the same id.
                "turn_id": turn_id,
            }
        )
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return Completion(
            text=reply.text,
            model="claude-sonnet-5",
            purpose=purpose,
            stop_reason="tool_use" if reply.tool_uses else "end_turn",
            tool_uses=list(reply.tool_uses),
            input_tokens=100,
            output_tokens=50,
        )


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    fake = FakeModel()
    monkeypatch.setattr("coach.runtime.turn.llmmod.complete", fake)
    return fake


def use_model(monkeypatch: pytest.MonkeyPatch, *replies: FakeReply) -> FakeModel:
    fake = FakeModel(*replies)
    monkeypatch.setattr("coach.runtime.turn.llmmod.complete", fake)
    return fake


def message(conn: psycopg.Connection, body: str, at: datetime | None = None) -> None:
    """An unprocessed athlete message, as the transport would have stored it."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into messages (chat_id, telegram_message_id, role, body, occurred_at) "
            "values (%s, %s, 'athlete', %s, %s)",
            (CHAT_ID, None, body, at or NOW),
        )


def sent(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("select body from messages where role = 'coach' order by id")
        return [r["body"] for r in cur.fetchall()]


# --- the turn ----------------------------------------------------------------


def test_a_message_produces_a_reply(conn, model) -> None:
    message(conn, "Did the ride land?")
    result = turn.respond(conn, None, [{"body": "Did the ride land?"}], NOW, tz=DUBAI)

    assert result.reply == "Fine. How did the hip feel?"
    assert model.calls[0]["purpose"] == "chat"


def test_the_prompt_carries_the_constraints(conn, model) -> None:
    """SAFE-01: removing all other context still leaves the constraints present."""
    facts.state_constraint(
        conn,
        "constraint.movement_restrictions",
        ["no barbell deadlifts"],
        reason="intake",
        confirmed=True,
    )
    turn.respond(conn, None, [{"body": "what should I do today?"}], NOW, tz=DUBAI)

    system = "".join(block["text"] for block in model.calls[0]["system"])
    assert "CONSTRAINTS" in system
    assert "no barbell deadlifts" in system


def test_the_whole_tool_surface_is_offered(conn, model) -> None:
    """CHAT-06: the surface is stable, so the prompt prefix is too."""
    turn.respond(conn, None, [{"body": "hello"}], NOW, tz=DUBAI)
    assert [t["name"] for t in model.calls[0]["tools"]] == list(
        n for n in __import__("coach.agent.tools", fromlist=["x"]).TOOL_NAMES
    )


def test_a_tool_call_is_dispatched_and_answered(conn, monkeypatch) -> None:
    fake = use_model(
        monkeypatch,
        FakeReply(tool_uses=[FakeUse("search_memory", {"query": "hip"})]),
        FakeReply("Nothing on the hip since the ramp test."),
    )
    result = turn.respond(conn, None, [{"body": "what did I say about my hip?"}], NOW, tz=DUBAI)

    assert result.tool_calls == ["search_memory"]
    assert result.reply == "Nothing on the hip since the ramp test."
    # The second call carries the assistant turn and the tool result.
    assert fake.calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_an_unknown_key_is_an_empty_history_not_an_error(conn, monkeypatch) -> None:
    """A key with no facts has no history. That is an answer, not a failure."""
    fake = use_model(
        monkeypatch,
        FakeReply(tool_uses=[FakeUse("get_context", {"key": "not.a.key"})]),
        FakeReply("Nothing on that."),
    )
    turn.respond(conn, None, [{"body": "?"}], NOW, tz=DUBAI)
    assert '"history": []' in fake.calls[1]["messages"][-1]["content"][0]["content"]


def test_a_failing_tool_is_reported_to_the_model_not_raised(conn, monkeypatch) -> None:
    """Raising would lose the whole turn to one bad argument."""
    fake = use_model(
        monkeypatch,
        FakeReply(tool_uses=[FakeUse("get_context", {})]),  # no `key`
        FakeReply("I cannot see that one."),
    )
    result = turn.respond(conn, None, [{"body": "?"}], NOW, tz=DUBAI)

    assert result.reply == "I cannot see that one."
    assert "error" in fake.calls[1]["messages"][-1]["content"][0]["content"]


def test_the_tool_loop_is_bounded(conn, monkeypatch) -> None:
    """A model that keeps asking would otherwise bill without limit."""
    fake = use_model(monkeypatch, FakeReply(tool_uses=[FakeUse("search_memory", {"query": "x"})]))
    result = turn.respond(conn, None, [{"body": "?"}], NOW, tz=DUBAI)

    assert len(fake.calls) == turn.MAX_TOOL_ROUNDS
    assert result.reply == ""


# --- the behavioural rules, enforced at the seam ------------------------------


def test_a_narrating_reply_is_retried_once(conn, monkeypatch) -> None:
    """CHAT-03, caught before it reaches the athlete."""
    fake = use_model(
        monkeypatch,
        FakeReply("Noted. I've saved that to your profile."),
        FakeReply("Right, that changes Thursday."),
    )
    result = turn.respond(conn, None, [{"body": "my knee hurts on the left"}], NOW, tz=DUBAI)

    assert result.retried
    assert result.violations == []
    assert result.reply == "Right, that changes Thursday."
    assert "CHAT-03" in fake.calls[1]["messages"][-1]["content"]


def test_a_retry_belongs_to_the_same_exchange(conn, monkeypatch) -> None:
    """OBS-12, on the path most likely to get it wrong.

    The naturalness retry is a second model call for the same athlete message.
    Reading the ledger back, it has to appear inside the turn it is retrying —
    a retry filed as its own exchange is exactly the thing that would make a
    transcript misleading, because it reads as the coach answering twice.
    """
    fake = use_model(
        monkeypatch,
        FakeReply("Noted. I've saved that to your profile."),
        FakeReply("Right, that changes Thursday."),
    )
    result = turn.respond(conn, None, [{"body": "my knee hurts on the left"}], NOW, tz=DUBAI)

    assert len(fake.calls) == 2
    assert {c["turn_id"] for c in fake.calls} == {result.turn_id}


def test_a_second_violation_is_sent_anyway_and_logged(conn, monkeypatch, caplog) -> None:
    """A coach that says nothing because it could not phrase itself is worse."""
    import logging

    use_model(
        monkeypatch,
        FakeReply("I've saved that."),
        FakeReply("I've noted that."),
    )
    with caplog.at_level(logging.ERROR):
        result = turn.respond(conn, None, [{"body": "hi"}], NOW, tz=DUBAI)

    assert result.reply
    assert result.violations
    assert any("still violates" in r.getMessage() for r in caplog.records)


def test_two_questions_are_retried(conn, monkeypatch) -> None:
    """CHAT-04: at most one question per message."""
    use_model(
        monkeypatch,
        FakeReply("How did it feel? And what was the cadence?"),
        FakeReply("How did it feel?"),
    )
    result = turn.respond(conn, None, [{"body": "done"}], NOW, tz=DUBAI)

    assert result.retried
    assert result.reply == "How did it feel?"


# --- CHAT-11: one interruption ------------------------------------------------


def test_the_turn_claims_at_most_one_interruption(conn, model) -> None:
    """The budget is claimed here because this is where a conversation happens."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into body_mass_readings (local_date, weight_kg) values (%s, 84.0)",
            (date(2026, 6, 1),),
        )
    facts.ratify(conn, "profile.height_cm", 179, "stated", "intake")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update facts set mention_pending = true where key = 'profile.height_cm'")

    offered = turn.candidates(conn, date(2026, 7, 28))
    assert {c.kind for c in offered} == {"body_mass_gap", "pending_mention"}

    result = turn.respond(conn, None, [{"body": "hi"}], NOW, tz=DUBAI)
    # Body mass outranks the pending mention in CHAT-11's priority order.
    assert result.interruption == "body_mass_gap"

    second = turn.respond(conn, None, [{"body": "still here"}], NOW, tz=DUBAI)
    assert second.interruption is None


def test_the_claimed_interruption_reaches_the_prompt(conn, model) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into body_mass_readings (local_date, weight_kg) values (%s, 84.0)",
            (date(2026, 6, 1),),
        )
    turn.respond(conn, None, [{"body": "hi"}], NOW, tz=DUBAI)

    system = "".join(block["text"] for block in model.calls[0]["system"])
    assert "ONE THING TO RAISE" in system
    assert "body_mass_gap" in system


# --- CHAT-08: the backlog -----------------------------------------------------


def test_a_backlog_produces_one_reply(conn, model) -> None:
    """CHAT-08: a six hour outage produces one catch-up response."""
    for i in range(4):
        message(conn, f"message {i}", NOW - timedelta(hours=4 - i))

    delivered: list[str] = []
    turn.handle(conn, None, delivered.append, NOW, DUBAI)

    assert len(delivered) == 1
    assert len(sent(conn)) == 1
    assert "arrived while you were offline" in model.calls[0]["messages"][-1]["content"]


def test_a_single_message_is_not_a_catch_up(conn, model) -> None:
    message(conn, "one")
    turn.handle(conn, None, lambda _: None, NOW, DUBAI)
    assert "arrived while you were offline" not in model.calls[0]["messages"][-1]["content"]


def test_handling_twice_answers_nothing_the_second_time(conn, model) -> None:
    message(conn, "one")
    assert turn.handle(conn, None, lambda _: None, NOW, DUBAI) is not None
    assert turn.handle(conn, None, lambda _: None, NOW, DUBAI) is None


def test_prior_turns_are_in_the_history(conn, model) -> None:
    message(conn, "first")
    turn.handle(conn, None, lambda _: None, NOW, DUBAI)
    message(conn, "second")
    turn.handle(conn, None, lambda _: None, NOW + timedelta(minutes=5), DUBAI)

    roles = [m["role"] for m in model.calls[-1]["messages"]]
    assert "assistant" in roles


# --- OBS-07: the spend guard --------------------------------------------------


def spend(conn: psycopg.Connection, usd: str) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into model_calls (purpose, model, cost_usd) values ('chat', 'x', %s)",
            (Decimal(usd),),
        )


def test_the_cap_stops_the_call_before_it_is_billed(conn) -> None:
    spend(conn, "3.50")
    with pytest.raises(models.SpendCapReached):
        models.check_spend(conn, cap=Decimal("3.00"))


def test_under_the_cap_passes(conn) -> None:
    spend(conn, "0.40")
    assert models.check_spend(conn, cap=Decimal("3.00")) == Decimal("0.40")


def test_a_capped_turn_says_so_rather_than_going_silent(conn, model, monkeypatch) -> None:
    """OBS-07: "On trip the coach says it is capped rather than going silent"."""
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "1.00")
    spend(conn, "1.50")

    result = turn.respond(conn, None, [{"body": "hi"}], NOW, tz=DUBAI)

    assert result.capped
    assert "spending limit" in result.reply
    assert model.calls == []  # not billed


def test_the_cap_is_configurable_without_a_deploy(conn, monkeypatch) -> None:
    spend(conn, "1.50")
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "1.00")
    with pytest.raises(models.SpendCapReached):
        models.check_spend(conn)
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "5.00")
    models.check_spend(conn)


def test_a_missing_api_key_fails_at_startup(monkeypatch) -> None:
    """Not at the first message, when the athlete has already sent one."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(models.NotConfigured):
        models.build_client()


# --- the transport ------------------------------------------------------------


def telegram(handler) -> transport.Telegram:
    return transport.Telegram(
        token="t", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_the_offset_advances_only_after_acknowledgement() -> None:
    """A crash between polling and answering must redeliver, not lose."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 7}]})

    with telegram(handler) as api:
        updates = api.updates()
        assert api.offset is None  # not yet acknowledged
        api.acknowledge(updates)
        assert api.offset == 8


def test_a_telegram_error_does_not_leak_the_token() -> None:
    """The bot token is in the URL, which is what raise_for_status would print."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False})

    with telegram(handler) as api, pytest.raises(httpx.HTTPError) as caught:
        api.updates()
    assert "/bott" not in str(caught.value)
    assert "500" in str(caught.value)


def test_a_long_reply_is_split_rather_than_truncated() -> None:
    body = " ".join(["word"] * 3000)
    chunks = list(transport._split(body, limit=100))

    assert len(chunks) > 1
    assert " ".join(chunks).split() == body.split()  # nothing lost
    assert all(len(c) <= 100 for c in chunks)


def test_a_missing_token_fails_at_construction(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(transport.NotConfigured):
        transport.Telegram()


# --- the agent process --------------------------------------------------------


class FakeTransport:
    """A transport that yields one batch of updates then blocks."""

    def __init__(self, batches: list[list[dict]]):
        self.batches = batches
        self.sent: list[str] = []
        self.acknowledged: list[int] = []
        self.fail_next = False

    def updates(self, timeout: int = 0) -> list[dict]:
        if self.fail_next:
            self.fail_next = False
            raise httpx.HTTPError("boom")
        return self.batches.pop(0) if self.batches else []

    def acknowledge(self, updates: list[dict]) -> None:
        self.acknowledged.extend(u["update_id"] for u in updates)

    def send(self, chat_id: int, text: str) -> None:
        self.sent.append(text)


def update(text: str, message_id: int = 1, chat_id: int = CHAT_ID) -> dict:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id},
            "date": int(NOW.timestamp()),
            "text": text,
        },
    }


def connector(conn: psycopg.Connection):
    from contextlib import contextmanager

    @contextmanager
    def connect():
        yield conn

    return connect


def run_once(conn, fake: FakeTransport, allowlist) -> None:
    """One pass of the agent loop, then stop."""
    stop = threading.Event()
    original = fake.updates

    def updates_then_stop(timeout: int = 0):
        result = original(timeout)
        if not fake.batches:
            stop.set()
        return result

    fake.updates = updates_then_stop
    agent.serve(stop, fake, None, allowlist, connect=connector(conn), tz=DUBAI, poll_timeout_s=0)


def test_the_agent_answers_an_allowlisted_message(conn, model) -> None:
    fake = FakeTransport([[update("did the ride land?")]])
    run_once(conn, fake, botmod.Allowlist(CHAT_ID))

    assert fake.sent == ["Fine. How did the hip feel?"]
    assert fake.acknowledged == [1]


def test_the_agent_ignores_a_foreign_chat_id(conn, model) -> None:
    """SEC-03, verified through the loop rather than the unit."""
    fake = FakeTransport([[update("let me in", chat_id=9999)]])
    run_once(conn, fake, botmod.Allowlist(CHAT_ID))

    assert fake.sent == []
    assert fake.acknowledged == [1]  # acknowledged, so it is not redelivered forever


def test_a_transport_failure_does_not_end_the_loop(conn, model) -> None:
    fake = FakeTransport([[update("hello")]])
    fake.fail_next = True
    run_once(conn, fake, botmod.Allowlist(CHAT_ID))

    assert fake.sent == ["Fine. How did the hip feel?"]


def test_the_agent_drains_a_backlog_with_no_new_update(conn, model) -> None:
    """The case a restart produces: messages waiting, nothing arriving."""
    message(conn, "sent while you were down")
    fake = FakeTransport([[]])
    run_once(conn, fake, botmod.Allowlist(CHAT_ID))

    assert len(fake.sent) == 1


# --- the scheduler ------------------------------------------------------------


def test_a_job_runs_once_per_local_date(conn) -> None:
    """CONS-10 and OBS-08: at most once per date."""
    ran: list[date] = []
    jobs = {"decay": lambda c, d: ran.append(d)}
    at_three = datetime(2026, 7, 28, 3, 30, tzinfo=DUBAI).astimezone(UTC)

    scheduler.run_due(conn, at_three, DUBAI, jobs)
    scheduler.run_due(conn, at_three, DUBAI, jobs)

    assert ran == [date(2026, 7, 27)]  # the day that finished, once


def test_nothing_runs_before_the_hour(conn) -> None:
    """A process started at midnight waits rather than consolidating today."""
    ran: list[date] = []
    before = datetime(2026, 7, 28, 1, 0, tzinfo=DUBAI).astimezone(UTC)

    assert scheduler.run_due(conn, before, DUBAI, {"decay": lambda c, d: ran.append(d)}) == {}
    assert ran == []


def test_the_hour_is_local_not_utc(conn) -> None:
    """TZ-01: 03:00 Dubai is 23:00 UTC the day before, and it is Dubai that counts."""
    assert scheduler.due(datetime(2026, 7, 27, 23, 30, tzinfo=UTC), DUBAI) == date(2026, 7, 27)
    assert scheduler.due(datetime(2026, 7, 27, 22, 30, tzinfo=UTC), DUBAI) is None


def test_a_missed_night_runs_when_the_process_comes_back(conn) -> None:
    """The reason this is a loop and not a cron entry."""
    ran: list[date] = []
    late = datetime(2026, 7, 28, 11, 0, tzinfo=DUBAI).astimezone(UTC)

    scheduler.run_due(conn, late, DUBAI, {"decay": lambda c, d: ran.append(d)})
    assert ran == [date(2026, 7, 27)]


def test_a_job_can_be_about_today_rather_than_yesterday(conn) -> None:
    """NOTIF-01's morning message names *today's* session, not yesterday's.

    Consolidation's "the day that finished" is right for consolidation and wrong
    for everything P10 adds, which is why the hour became a `Schedule`.
    """
    ran: list[date] = []
    job = scheduler.Job(
        run=lambda c, d: ran.append(d),
        schedule=scheduler.Schedule(hour=6, covers="today"),
    )
    at_seven = datetime(2026, 7, 28, 7, 0, tzinfo=DUBAI).astimezone(UTC)

    scheduler.run_due(conn, at_seven, DUBAI, {"morning": job})

    assert ran == [date(2026, 7, 28)]


def test_two_jobs_at_different_hours_do_not_wait_for_each_other(conn) -> None:
    """The reason each job carries its own schedule rather than sharing one.

    A single due date for the whole tick would mean the 21:00 follow-up either
    dragged the 06:00 message with it or was held back by it.
    """
    ran: list[str] = []
    jobs = {
        "morning": scheduler.Job(
            run=lambda c, d: ran.append("morning"),
            schedule=scheduler.Schedule(hour=6, covers="today"),
        ),
        "follow_up": scheduler.Job(
            run=lambda c, d: ran.append("follow_up"),
            schedule=scheduler.Schedule(hour=21, covers="today"),
        ),
    }
    midday = datetime(2026, 7, 28, 12, 0, tzinfo=DUBAI).astimezone(UTC)

    scheduler.run_due(conn, midday, DUBAI, jobs)

    assert ran == ["morning"]


def test_a_today_job_and_a_yesterday_job_keep_separate_ledger_rows(conn) -> None:
    """They key on the date they are *about*, so the same tick cannot collide."""
    ran: list[tuple[str, date]] = []
    jobs = {
        "consolidate": lambda c, d: ran.append(("consolidate", d)),
        "morning": scheduler.Job(
            run=lambda c, d: ran.append(("morning", d)),
            schedule=scheduler.Schedule(hour=6, covers="today"),
        ),
    }
    at_seven = datetime(2026, 7, 28, 7, 0, tzinfo=DUBAI).astimezone(UTC)

    scheduler.run_due(conn, at_seven, DUBAI, jobs)

    assert sorted(ran) == [("consolidate", date(2026, 7, 27)), ("morning", date(2026, 7, 28))]


def test_a_weekly_job_only_fires_on_its_weekday(conn) -> None:
    """REV-01. 2026-07-28 is a Tuesday; 2026-08-02 is a Sunday."""
    ran: list[date] = []
    review = scheduler.Job(
        run=lambda c, d: ran.append(d),
        schedule=scheduler.Schedule(hour=18, weekday=6, covers="today"),
    )

    tuesday = datetime(2026, 7, 28, 19, 0, tzinfo=DUBAI).astimezone(UTC)
    scheduler.run_due(conn, tuesday, DUBAI, {"review": review})
    assert ran == []

    sunday = datetime(2026, 8, 2, 19, 0, tzinfo=DUBAI).astimezone(UTC)
    scheduler.run_due(conn, sunday, DUBAI, {"review": review})
    assert ran == [date(2026, 8, 2)]


def test_the_notification_times_move_without_a_deploy(monkeypatch) -> None:
    """NOTIF-05, and the fallback that keeps a typo from stopping the nightly pass."""
    monkeypatch.setenv("COACH_MORNING_HOUR", "5")
    assert scheduler.morning_schedule().hour == 5

    monkeypatch.setenv("COACH_MORNING_HOUR", "not-an-hour")
    assert scheduler.morning_schedule().hour == 6

    monkeypatch.setenv("COACH_MORNING_HOUR", "99")
    assert scheduler.morning_schedule().hour == 6


def test_a_failing_job_retries_once_and_then_stops(conn) -> None:
    """OBS-08: "a failing run cannot loop"."""
    attempts: list[int] = []

    def always_fails(c, d):
        attempts.append(1)
        raise RuntimeError("nope")

    at_three = datetime(2026, 7, 28, 3, 30, tzinfo=DUBAI).astimezone(UTC)
    for _ in range(5):
        scheduler.run_due(conn, at_three, DUBAI, {"decay": always_fails})

    assert len(attempts) == scheduler.MAX_ATTEMPTS


def test_one_failing_job_does_not_stop_the_others(conn) -> None:
    ran: list[str] = []
    jobs = {
        "decay": lambda c, d: (_ for _ in ()).throw(RuntimeError("nope")),
        "export": lambda c, d: ran.append("export"),
    }
    at_three = datetime(2026, 7, 28, 3, 30, tzinfo=DUBAI).astimezone(UTC)
    outcomes = scheduler.run_due(conn, at_three, DUBAI, jobs)

    assert ran == ["export"]
    assert outcomes["export"] == "succeeded"
    assert outcomes["decay"].startswith("failed")


def test_the_decay_job_runs_the_real_decay(conn) -> None:
    """CONS-07, through the scheduler rather than the unit."""
    facts.ratify(conn, "availability.weekday_minutes", 60, "stated", "intake")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update facts set last_confirmed_at = now() - interval '90 days' "
            "where key = 'availability.weekday_minutes'"
        )

    at_three = datetime(2026, 7, 28, 3, 30, tzinfo=DUBAI).astimezone(UTC)
    scheduler.run_due(conn, at_three, DUBAI, {"decay": scheduler.decay_job})

    fact = facts.active_for(conn, "availability.weekday_minutes")
    assert fact.confidence < Decimal("0.40")  # 30 day half life, 90 days unconfirmed


def test_the_export_job_writes_the_markdown(conn, tmp_path, monkeypatch) -> None:
    """MEM-12's readable half."""
    monkeypatch.setenv("COACH_EXPORT_DIR", str(tmp_path))
    facts.ratify(conn, "profile.height_cm", 179, "stated", "intake")

    at_three = datetime(2026, 7, 28, 3, 30, tzinfo=DUBAI).astimezone(UTC)
    scheduler.run_due(conn, at_three, DUBAI, {"export": scheduler.export_job})

    written = list(tmp_path.glob("*.md"))
    assert written and "179" in written[0].read_text()


def test_the_ledger_records_what_happened(conn) -> None:
    """OBS-04 needs "did last night run?" to be answerable."""
    at_three = datetime(2026, 7, 28, 3, 30, tzinfo=DUBAI).astimezone(UTC)
    scheduler.run_due(conn, at_three, DUBAI, {"decay": scheduler.decay_job})

    with conn.cursor() as cur:
        cur.execute("select job, local_date, status, attempts from scheduled_runs")
        row = cur.fetchone()
    assert (row["job"], row["status"], row["attempts"]) == ("decay", "succeeded", 1)


# --- the seams are closed -----------------------------------------------------


def test_every_process_is_a_console_script() -> None:
    """The gap this package closed: `coach-ingest` was the only one.

    `coach-transcript` is the one entry here that is not a process. It is an
    operator command, OBS-13's way into the call ledger, and it is in the same
    list because the reason for the list is that an entry point nobody declared
    is an entry point nobody can run.
    """
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]

    assert set(scripts) == {
        "coach-migrate",
        "coach-seed",
        "coach-ingest",
        "coach-agent",
        "coach-scheduler",
        "coach-transcript",
    }


def test_the_model_client_has_exactly_one_construction_site() -> None:
    """So the spend guard has nowhere to be routed around."""
    import re
    from pathlib import Path

    src = Path(__file__).parents[1] / "src"
    sites = [
        f"{p.relative_to(src)}:{n}"
        for p in src.rglob("*.py")
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if re.search(r"anthropic\.Anthropic\s*\(", line)
    ]
    assert sites == ["coach/runtime/models.py:57"], sites


def test_only_the_transport_talks_to_telegram() -> None:
    """Everything that decides anything is elsewhere and holds no network."""
    import re
    from pathlib import Path

    src = Path(__file__).parents[1] / "src"
    offenders = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if p.name != "transport.py" and re.search(r"api\.telegram\.org", p.read_text())
    ]
    assert offenders == []


# --- the tools and jobs that existed and were never reachable ----------------


def test_no_tool_is_still_marked_deferred() -> None:
    """Found on a live deployment: the coach said it could not see session history.

    `get_sessions` was flagged "deferred to P03" and `write_session_events`
    "deferred to P08", both long since shipped, so `dispatch` returned an
    unavailable payload and the model reported that honestly. A phase marker
    that outlives its phase is worse than no marker: it makes the model lie
    accurately.
    """
    from coach.agent import tools

    assert tools.DEFERRED == {}


def test_the_publish_pass_is_registered_as_a_job() -> None:
    """PLAN-01 was built, tested, and called by nothing.

    Same class of gap as the consolidation proposer: a whole path with no
    caller, invisible to every unit test because each piece works.
    """
    assert callable(scheduler.publish_job)

    published: list[str] = []

    class FakeApi:
        def upsert_events(self, events):
            published.extend(e.get("external_id", "?") for e in events)
            return events

    job = scheduler.publish_job(FakeApi(), DUBAI)
    assert callable(job)
