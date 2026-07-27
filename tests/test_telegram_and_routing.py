"""P01 acceptance: the allowlist, offline catch-up, and model routing.

CHAT-01, CHAT-08, SEC-03, MODEL-01, MODEL-02, MODEL-03.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from coach.llm import router
from coach.telegram import bot

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
ALLOWED = 4242


def update(chat_id: int, message_id: int, text: str, at: datetime = NOW) -> dict:
    return {
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id},
            "date": int(at.timestamp()),
            "text": text,
        }
    }


@pytest.fixture
def allowlist() -> bot.Allowlist:
    return bot.Allowlist(ALLOWED)


# --- CHAT-01 / SEC-03 -------------------------------------------------------


def test_allowlisted_chat_is_accepted(conn: psycopg.Connection, allowlist: bot.Allowlist) -> None:
    accepted = bot.accept(conn, allowlist, update(ALLOWED, 1, "morning"))
    conn.commit()
    assert accepted is not None and accepted.body == "morning"


def test_second_chat_id_is_refused(conn: psycopg.Connection, allowlist: bot.Allowlist) -> None:
    """CHAT-01 and SEC-03: verified with a second account."""
    assert bot.accept(conn, allowlist, update(9999, 1, "let me in")) is None
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from messages")
        assert cur.fetchone()["n"] == 0


def test_allowlist_requires_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_ID", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_ALLOWED_CHAT_ID"):
        bot.Allowlist()


def test_non_message_updates_are_ignored(
    conn: psycopg.Connection, allowlist: bot.Allowlist
) -> None:
    assert bot.accept(conn, allowlist, {"poll": {"id": "1"}}) is None


def test_voice_notes_are_persisted_with_modality(
    conn: psycopg.Connection, allowlist: bot.Allowlist
) -> None:
    """VOICE-02: modality is on the row from P01, before transcription lands."""
    raw = {
        "message": {
            "message_id": 7,
            "chat": {"id": ALLOWED},
            "date": int(NOW.timestamp()),
            "voice": {"file_id": "abc123"},
        }
    }
    accepted = bot.accept(conn, allowlist, raw)
    conn.commit()
    assert accepted is not None and accepted.modality == "voice"


# --- CHAT-08: one catch-up, not one reply per queued message ----------------


def test_outage_backlog_produces_one_response(
    conn: psycopg.Connection, allowlist: bot.Allowlist
) -> None:
    """CHAT-08: a simulated 6 hour outage produces one catch-up response."""
    for i in range(7):
        bot.accept(
            conn, allowlist, update(ALLOWED, i, f"message {i}", NOW - timedelta(hours=6 - i))
        )
    conn.commit()

    calls: list[tuple[int, bool]] = []

    def respond(pending: list[dict], catch_up: bool) -> str:
        calls.append((len(pending), catch_up))
        return "caught up"

    reply = bot.drain(conn, respond, NOW)
    conn.commit()

    assert reply == "caught up"
    assert calls == [(7, True)], "backlog must be answered once, with all of it in hand"
    assert bot.backlog(conn) == []


def test_a_single_message_is_not_a_catch_up(
    conn: psycopg.Connection, allowlist: bot.Allowlist
) -> None:
    bot.accept(conn, allowlist, update(ALLOWED, 1, "hi"))
    conn.commit()

    seen: list[bool] = []
    bot.drain(conn, lambda pending, catch_up: seen.append(catch_up) or "hello", NOW)
    conn.commit()
    assert seen == [False]


def test_draining_twice_answers_nothing_the_second_time(
    conn: psycopg.Connection, allowlist: bot.Allowlist
) -> None:
    bot.accept(conn, allowlist, update(ALLOWED, 1, "hi"))
    conn.commit()

    bot.drain(conn, lambda p, c: "first", NOW)
    conn.commit()
    assert bot.drain(conn, lambda p, c: "second", NOW) is None


def test_redelivered_update_is_not_stored_twice(
    conn: psycopg.Connection, allowlist: bot.Allowlist
) -> None:
    """A restart that replays the same update must not answer it again."""
    assert bot.accept(conn, allowlist, update(ALLOWED, 1, "hi")) is not None
    conn.commit()
    assert bot.accept(conn, allowlist, update(ALLOWED, 1, "hi")) is None
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from messages where role = 'athlete'")
        assert cur.fetchone()["n"] == 1


# --- MODEL-01 / 02 / 03 -----------------------------------------------------


def test_conversation_and_consolidation_route_differently() -> None:
    """MODEL-01: a lightweight model per turn, a heavier one for the night."""
    assert router.route("chat").model != router.route("consolidation").model
    assert router.route("consolidation").model == router.HEAVY


def test_effort_is_lower_on_a_conversation_turn() -> None:
    """PERF-01 wants streaming inside 4 seconds; the nightly pass has no such bound."""
    assert router.route("chat").effort == "low"
    assert router.route("consolidation").effort == "high"


def test_model_choice_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODEL-02: changing configuration changes the model used."""
    monkeypatch.setenv("MODEL_CHAT", "claude-haiku-4-5")
    assert router.route("chat").model == "claude-haiku-4-5"


def test_unknown_purpose_is_rejected() -> None:
    with pytest.raises(router.UnknownPurpose):
        router.route("vibes")


def test_fallback_is_the_heavier_model() -> None:
    """MODEL-03: router failures fall back rather than failing the turn."""
    fallback = router.fallback_for(router.route("chat"))
    assert fallback is not None and fallback.model == router.HEAVY


def test_the_heavy_model_has_no_fallback() -> None:
    """Nowhere further to fall back to — the caller must see the error."""
    assert router.fallback_for(router.route("consolidation")) is None


def test_cost_is_computed_per_call() -> None:
    """OBS-01: tokens, model and cost are all recoverable per call."""
    cost = router.cost_usd("claude-opus-5", input_tokens=1_000_000, output_tokens=0)
    assert cost == Decimal("5.000000")

    cached = router.cost_usd("claude-opus-5", 0, 0, cache_read_tokens=1_000_000)
    assert cached == Decimal("0.500000")


def test_unknown_model_prices_at_zero_rather_than_guessing() -> None:
    assert router.cost_usd("some-future-model", 1000, 1000) == Decimal("0")
