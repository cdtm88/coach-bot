"""OBS-10 to OBS-14: the model call ledger.

`model_calls` has recorded the shape of every call since P01 and none of its
content, so "why did the coach say that" was unanswerable: the system prompt is
assembled per turn from facts that change nightly and cannot be reconstructed
afterwards, and the tool results that shaped a reply were never stored anywhere.

The tests that matter most here are the ones asserting what the ledger must
never do. It must not cost a call, and it must not become a second copy of the
credentials.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from coach.llm import client as llmmod
from coach.observe import transcript
from coach.runtime import scheduler


class FakeMessage:
    def __init__(self, text: str, tool_uses: list[Any] | None = None) -> None:
        self.content = [Block("text", text=text), *(tool_uses or [])]
        self.model = "claude-sonnet-5"
        self.stop_reason = "tool_use" if tool_uses else "end_turn"
        self.usage = Usage()


class Usage:
    input_tokens = 120
    output_tokens = 45
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class Block:
    def __init__(self, type_: str, **kw: Any) -> None:
        self.type = type_
        for key, value in kw.items():
            setattr(self, key, value)


class ToolUse:
    type = "tool_use"

    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.id = f"toolu_{name}"
        self.name = name
        self.input = arguments


class FakeStream:
    def __init__(self, message: FakeMessage) -> None:
        self._message = message
        self.text_stream: list[str] = []

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_final_message(self) -> FakeMessage:
        return self._message


class FakeClient:
    """Enough of `anthropic.Anthropic` for `client.complete` to run."""

    def __init__(self, messages: list[FakeMessage]) -> None:
        self._messages = list(messages)
        self.seen: list[dict[str, Any]] = []
        self.messages = self

    def stream(self, **kwargs: Any) -> FakeStream:
        self.seen.append(kwargs)
        return FakeStream(self._messages.pop(0))


SYSTEM = [{"type": "text", "text": "You are his coach.", "cache_control": {"type": "ephemeral"}}]


def call(conn: psycopg.Connection, **kw: Any) -> Any:
    return llmmod.complete(
        FakeClient([FakeMessage(kw.pop("reply", "Fine. How did the hip feel?"))]),
        kw.pop("purpose", "chat"),
        SYSTEM,
        kw.pop("messages", [{"role": "user", "content": "did the ride land?"}]),
        conn=conn,
        **kw,
    )


def payloads(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "select p.*, c.purpose, c.turn_id::text as turn_id from model_call_payloads p "
            "join model_calls c on c.id = p.call_id order by p.call_id"
        )
        return cur.fetchall()


# --- OBS-10: what was sent and what came back ------------------------------


def test_a_call_records_its_prompt_and_its_reply(conn: psycopg.Connection) -> None:
    call(conn)

    stored = payloads(conn)
    assert len(stored) == 1
    assert stored[0]["system"] == SYSTEM
    assert stored[0]["messages"] == [{"role": "user", "content": "did the ride land?"}]
    assert stored[0]["response"]["text"] == "Fine. How did the hip feel?"
    assert stored[0]["response"]["stop_reason"] == "end_turn"


def test_the_system_blocks_are_stored_as_sent_including_cache_markers(
    conn: psycopg.Connection,
) -> None:
    """A prompt reconstructed later is not evidence of what was sent.

    The cache_control marker is the specific thing worth keeping: whether the
    stable prefix was actually marked is exactly the kind of question this
    ledger exists to answer, and it is invisible in the reply.
    """
    call(conn)

    assert payloads(conn)[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_the_tools_on_offer_are_recorded(conn: psycopg.Connection) -> None:
    """Which surface the model was given, not which surface exists today."""
    schemas = [{"name": "get_plan", "description": "...", "input_schema": {"type": "object"}}]

    llmmod.complete(
        FakeClient([FakeMessage("ok")]),
        "chat",
        SYSTEM,
        [{"role": "user", "content": "what is on this week?"}],
        tools=schemas,
        conn=conn,
    )

    assert payloads(conn)[0]["tools"] == schemas


def test_a_tool_call_is_recorded_as_data_rather_than_as_sdk_objects(
    conn: psycopg.Connection,
) -> None:
    """The stored shape must not depend on the SDK's block classes."""
    use = ToolUse("get_plan", {"since": "2026-08-03", "until": "2026-08-09"})

    llmmod.complete(
        FakeClient([FakeMessage("let me look", [use])]),
        "chat",
        SYSTEM,
        [{"role": "user", "content": "what is on?"}],
        conn=conn,
    )

    recorded = payloads(conn)[0]["response"]["tool_uses"]
    assert recorded == [
        {
            "id": "toolu_get_plan",
            "name": "get_plan",
            "input": {"since": "2026-08-03", "until": "2026-08-09"},
        }
    ]


# --- OBS-11: the ledger never costs a call ---------------------------------


def test_a_failing_payload_write_keeps_the_cost_row_and_the_reply(
    conn: psycopg.Connection,
) -> None:
    """The whole reason the payload is a second table and a second transaction.

    OBS-01 and OBS-07 are accounting: a call that happened must be billed
    whether or not a copy of it survived. Simulated by dropping the payload
    table, which is also what a deployment mid-migration looks like.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("drop table model_call_payloads")

    completion = call(conn)

    assert completion.text == "Fine. How did the hip feel?"
    with conn.cursor() as cur:
        cur.execute("select count(*) as n, sum(cost_usd) as spent from model_calls")
        row = cur.fetchone()
    assert row["n"] == 1
    assert row["spent"] > 0


def test_a_call_without_a_connection_still_works(conn: psycopg.Connection) -> None:
    """`conn` is optional on `complete` and the ledger must not change that."""
    completion = llmmod.complete(
        FakeClient([FakeMessage("no database here")]),
        "chat",
        SYSTEM,
        [{"role": "user", "content": "hello"}],
        conn=None,
    )

    assert completion.text == "no database here"


# --- OBS-12: a turn is the unit, not a call --------------------------------


def test_the_calls_of_one_turn_share_a_turn_id(conn: psycopg.Connection) -> None:
    turn_id = str(uuid.uuid4())

    call(conn, turn_id=turn_id, reply="first")
    call(conn, turn_id=turn_id, reply="second")

    assert {p["turn_id"] for p in payloads(conn)} == {turn_id}


def test_a_scheduled_call_has_no_turn_id(conn: psycopg.Connection) -> None:
    """A job is a call and not an exchange, and inventing one would say otherwise."""
    call(conn, purpose="review")

    assert payloads(conn)[0]["turn_id"] is None


def test_the_turn_loop_gives_every_call_in_one_exchange_the_same_id(
    conn: psycopg.Connection,
) -> None:
    """OBS-12 through the loop that owns the boundary, not the unit below it.

    Two rounds — one asking for a tool, one answering — is the case the id
    exists for, and it is the case a unit test of `complete` cannot see.
    """
    from coach.runtime import turn as turnmod

    client = FakeClient(
        [
            FakeMessage("", [ToolUse("get_plan", {"since": "2026-08-03", "until": "2026-08-09"})]),
            FakeMessage("Nothing until Thursday."),
        ]
    )

    result = turnmod.respond(
        conn,
        client,
        [{"body": "what is on this week?"}],
        datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
    )

    with conn.cursor() as cur:
        cur.execute("select distinct turn_id::text as turn_id from model_calls")
        ids = [r["turn_id"] for r in cur.fetchall()]

    assert len(ids) == 1
    assert ids[0] == result.turn_id
    assert len(payloads(conn)) == 2


# --- the ledger must not become a second copy of the credentials -----------


def test_no_credential_reaches_a_payload(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-01 and CALR-06, asserted at the new place content is written down.

    CALR-06 keeps the secret iCal URLs out of the database entirely, because a
    secret in a column is a secret in the nightly pg_dump. This table is a new
    column that holds whatever was in the prompt, so it is a new way for that
    rule to be broken by accident. The prompt is assembled from facts and the
    persona and should never carry an environment secret; this fails loudly on
    the day something starts putting one there.
    """
    secret = "https://calendar.google.com/private-abc123/basic.ics"
    monkeypatch.setenv("CALENDAR_ICS_URLS", secret)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "8100:AAH-supersecret")
    monkeypatch.setenv("INTERVALS_API_KEY", "intervals-key-xyz")

    call(conn, messages=[{"role": "user", "content": "what does my calendar say?"}])

    with conn.cursor() as cur:
        cur.execute(
            "select system::text || messages::text || response::text as everything "
            "from model_call_payloads"
        )
        blob = cur.fetchone()["everything"]

    for leaked in (secret, "AAH-supersecret", "intervals-key-xyz"):
        assert leaked not in blob


# --- OBS-14: retention ------------------------------------------------------


def age(conn: psycopg.Connection, days: int) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update model_call_payloads set created_at = now() - make_interval(days => %s)",
            (days,),
        )


def test_the_prune_removes_old_payloads(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COACH_PAYLOAD_RETENTION_DAYS", "30")
    call(conn)
    age(conn, 40)

    scheduler.prune_payloads_job(conn, None)

    assert payloads(conn) == []


def test_the_prune_leaves_the_cost_rows_alone(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OBS-01 is a claim about the whole history and outlives the payloads."""
    monkeypatch.setenv("COACH_PAYLOAD_RETENTION_DAYS", "30")
    call(conn)
    age(conn, 40)

    scheduler.prune_payloads_job(conn, None)

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from model_calls")
        assert cur.fetchone()["n"] == 1


def test_a_payload_inside_the_window_is_kept(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COACH_PAYLOAD_RETENTION_DAYS", "90")
    call(conn)
    age(conn, 40)

    scheduler.prune_payloads_job(conn, None)

    assert len(payloads(conn)) == 1


def test_a_malformed_retention_falls_back_rather_than_pruning_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not be able to delete the ledger."""
    from coach import config

    for bad in ("nonsense", "0", "-5", ""):
        monkeypatch.setenv("COACH_PAYLOAD_RETENTION_DAYS", bad)
        assert config.payload_retention_days() == config.DEFAULT_PAYLOAD_RETENTION_DAYS


# --- OBS-13: it has to be readable -----------------------------------------


def test_the_transcript_reconstructs_the_exchange(conn: psycopg.Connection) -> None:
    turn_id = str(uuid.uuid4())
    call(conn, turn_id=turn_id, messages=[{"role": "user", "content": "did the ride land?"}])

    rendered = transcript.render(transcript.fetch(conn, last=1))

    assert "did the ride land?" in rendered
    assert "Fine. How did the hip feel?" in rendered
    assert "You are his coach." in rendered
    assert turn_id in rendered


def test_brief_drops_the_system_blocks_and_keeps_the_exchange(
    conn: psycopg.Connection,
) -> None:
    call(conn)

    rendered = transcript.render(transcript.fetch(conn, last=1), brief=True)

    assert "You are his coach." not in rendered
    assert "Fine. How did the hip feel?" in rendered


def test_last_counts_exchanges_rather_than_calls(conn: psycopg.Connection) -> None:
    """A turn with three tool rounds is one exchange, not three.

    Taking the newest N rows would cut a turn in half and print an answer with
    no question above it, which is the failure this query shape avoids.
    """
    busy = str(uuid.uuid4())
    call(conn, turn_id=busy, reply="round one")
    call(conn, turn_id=busy, reply="round two")
    call(conn, turn_id=busy, reply="round three")
    call(conn, turn_id=str(uuid.uuid4()), reply="a later, simpler turn")

    rendered = transcript.render(transcript.fetch(conn, last=2))

    for text in ("round one", "round two", "round three", "a later, simpler turn"):
        assert text in rendered


def test_a_call_with_no_payload_says_so_rather_than_looking_empty(
    conn: psycopg.Connection,
) -> None:
    """An unrecorded prompt and an empty prompt are different facts."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("insert into model_calls (purpose, model, cost_usd) values ('chat', 'x', 0.01)")

    rendered = transcript.render(transcript.fetch(conn, last=1))

    assert "no payload recorded" in rendered


def test_filtering_by_purpose(conn: psycopg.Connection) -> None:
    call(conn, purpose="chat", reply="a chat turn")
    call(conn, purpose="review", reply="the sunday review")

    rendered = transcript.render(transcript.fetch(conn, purpose="review"))

    assert "the sunday review" in rendered
    assert "a chat turn" not in rendered


def test_an_empty_filter_says_so(conn: psycopg.Connection) -> None:
    assert "nothing recorded" in transcript.render(transcript.fetch(conn, last=5))


def test_long_content_truncates_unless_asked(conn: psycopg.Connection) -> None:
    call(conn, reply="x" * (transcript.TRUNCATE_AT + 500))

    calls = transcript.fetch(conn, last=1)

    assert "use --full" in transcript.render(calls)
    assert "use --full" not in transcript.render(calls, full=True)
