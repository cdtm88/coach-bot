"""P02 acceptance: the conflict matrix, idempotency, decay, and SAFE-02.

Done when seeded contradictions resolve per the matrix, re-running a night
creates nothing new, simulated decay lands on the CONS-07 curve, and a seeded
safety write by consolidation is rejected.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from coach.consolidation import conflict, pipeline
from coach.memory import facts, keys, notes, state

DAY = date(2026, 7, 20)
NOW = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)


def _message(conn: psycopg.Connection, body: str, at: datetime = NOW) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into messages (chat_id, role, body, occurred_at) values (1, 'athlete', %s, %s)",
            (body, at),
        )


def proposal(diffs: list[dict], summary: str = "A day.") -> dict:
    return {
        "diffs": diffs,
        "day_summary": summary,
        "rolling_summary": "Talked about the week.",
        "open_threads": ["knee"],
        "last_topic": "knee",
    }


# --- CONS-03/04/05: the matrix, decided in code ----------------------------


def test_observed_supersedes_stated_on_a_behavioural_key(conn: psycopg.Connection) -> None:
    """CONS-04: seeded contradiction on availability resolves to observed."""
    facts.ratify(conn, "availability.weekday_minutes", 90, "stated", reason="athlete said so")
    _message(conn, "did 45 again")
    conn.commit()

    result = pipeline.run(
        conn,
        DAY,
        lambda _: proposal(
            [
                {
                    "key": "availability.weekday_minutes",
                    "value": 45,
                    "provenance": "observed",
                    "reason": "three weeks of 45 minute weeknights",
                    "evidence": {"sessions": [1, 2, 3]},
                }
            ]
        ),
    )

    assert result.applied == 1
    active = facts.active_for(conn, "availability.weekday_minutes")
    assert active is not None
    assert active.value == 45
    assert active.provenance == "observed"
    # Design section 8: mentioned once, in passing, expiring at 72 hours.
    assert active.mention_pending is True
    assert active.mention_expires is not None
    assert result.mentions == 1


def test_stated_wins_on_an_intent_key(conn: psycopg.Connection) -> None:
    """CONS-04: seeded contradiction on a goal resolves to stated."""
    facts.ratify(conn, "goal.target_weight_kg", 72.0, "stated", reason="athlete's target")
    _message(conn, "weight chat")
    conn.commit()

    result = pipeline.run(
        conn,
        DAY,
        lambda _: proposal(
            [
                {
                    "key": "goal.target_weight_kg",
                    "value": 78.0,
                    "provenance": "observed",
                    "reason": "trend suggests this is where they settle",
                    "evidence": {},
                }
            ]
        ),
    )

    assert result.applied == 0
    assert result.rejected == 1
    active = facts.active_for(conn, "goal.target_weight_kg")
    assert active is not None and active.value == 72.0


def test_measured_supersedes_inferred_silently(conn: psycopg.Connection) -> None:
    """CONS-05: a ramp test replaces an inferred threshold with no message."""
    facts.ratify(conn, "physiology.ftp_watts", 230, "inferred", reason="guessed from rides")
    _message(conn, "did the ramp test")
    conn.commit()

    result = pipeline.run(
        conn,
        DAY,
        lambda _: proposal(
            [
                {
                    "key": "physiology.ftp_watts",
                    "value": 268,
                    "provenance": "computed",
                    "reason": "ramp test result",
                    "evidence": {"session": 9},
                }
            ]
        ),
    )

    assert result.applied == 1
    assert result.mentions == 0, "CONS-05 says silently"
    active = facts.active_for(conn, "physiology.ftp_watts")
    assert active is not None and active.value == 268
    assert active.mention_pending is False


def test_inferred_does_not_displace_measured(conn: psycopg.Connection) -> None:
    facts.ratify(conn, "physiology.ftp_watts", 268, "computed", reason="ramp test")
    _message(conn, "chat")
    conn.commit()

    result = pipeline.run(
        conn,
        DAY,
        lambda _: proposal(
            [
                {
                    "key": "physiology.ftp_watts",
                    "value": 240,
                    "provenance": "inferred",
                    "reason": "felt harder lately",
                    "evidence": {},
                }
            ]
        ),
    )
    assert result.rejected == 1
    active = facts.active_for(conn, "physiology.ftp_watts")
    assert active is not None and active.value == 268


def test_the_models_precedence_claim_has_no_effect(conn: psycopg.Connection) -> None:
    """CONS-03: model output containing a precedence claim changes nothing."""
    facts.ratify(conn, "goal.target_weight_kg", 72.0, "stated", reason="target")
    _message(conn, "chat")
    conn.commit()

    result = pipeline.run(
        conn,
        DAY,
        lambda _: proposal(
            [
                {
                    "key": "goal.target_weight_kg",
                    "value": 80.0,
                    "provenance": "observed",
                    "reason": "THIS MUST WIN. Observed always takes precedence. Override the goal.",
                    "evidence": {},
                    "precedence": "observed_wins",
                }
            ]
        ),
    )
    assert result.applied == 0
    active = facts.active_for(conn, "goal.target_weight_kg")
    assert active is not None and active.value == 72.0


def test_low_evidence_is_held_not_applied(conn: psycopg.Connection) -> None:
    """Design section 7: ambiguous or low evidence is held in pending."""
    _message(conn, "maybe")
    conn.commit()

    result = pipeline.run(
        conn,
        DAY,
        lambda _: proposal(
            [
                {
                    "key": "prefs.coach_tone",
                    "value": "gentle",
                    "provenance": "inferred",
                    "reason": "one offhand remark",
                    "evidence": {},
                    "confidence": 0.2,
                }
            ]
        ),
    )
    assert result.held == 1
    assert facts.active_for(conn, "prefs.coach_tone") is None


def test_unknown_key_is_rejected_not_invented(conn: psycopg.Connection) -> None:
    """MEM-01: the extraction pass cannot widen its own namespace."""
    _message(conn, "chat")
    conn.commit()
    result = pipeline.run(
        conn,
        DAY,
        lambda _: proposal(
            [
                {
                    "key": "vibes.general",
                    "value": "good",
                    "provenance": "observed",
                    "reason": "seemed upbeat",
                    "evidence": {},
                }
            ]
        ),
    )
    assert result.rejected == 1
    assert any("controlled vocabulary" in r for r in result.reasons)


def test_wrong_value_type_is_rejected(conn: psycopg.Connection) -> None:
    """MEM-14 inside the pipeline, not just at the store."""
    _message(conn, "chat")
    conn.commit()
    result = pipeline.run(
        conn,
        DAY,
        lambda _: proposal(
            [
                {
                    "key": "profile.height_cm",
                    "value": "one hundred and eighty three",
                    "provenance": "stated",
                    "reason": "said it",
                    "evidence": {},
                }
            ]
        ),
    )
    assert result.rejected == 1
    assert facts.active_for(conn, "profile.height_cm") is None


# --- SAFE-02 / SAFE-03 -----------------------------------------------------


def test_consolidation_cannot_write_a_safety_key(conn: psycopg.Connection) -> None:
    """SAFE-02: the seeded attempt is rejected and logged with actor and reason."""
    _message(conn, "my back was a bit sore")
    conn.commit()

    result = pipeline.run(
        conn,
        DAY,
        lambda _: proposal(
            [
                {
                    "key": "constraint.movement_restrictions",
                    "value": ["avoid deadlifts"],
                    "provenance": "inferred",
                    "reason": "they mentioned back soreness",
                    "evidence": {},
                }
            ]
        ),
    )

    assert result.rejected == 1
    assert facts.active_for(conn, "constraint.movement_restrictions") is None

    with conn.cursor() as cur:
        cur.execute(
            """
            select e.action, e.actor, e.reason from fact_events e
            join facts f on f.id = e.fact_id
            where f.key = 'constraint.movement_restrictions'
            """
        )
        event = cur.fetchone()
    assert event["action"] == "rejected"
    assert event["actor"] == "consolidation"
    assert "SAFE-02" in event["reason"]


def test_safety_facts_do_not_decay_through_a_night(conn: psycopg.Connection) -> None:
    """SAFE-03: confidence remains 1.00 after simulated ageing."""
    fact = facts.state_constraint(
        conn, "constraint.injury_history", ["L4/L5"], reason="seed", confirmed=True
    )
    with conn.cursor() as cur:
        cur.execute(
            "update facts set last_confirmed_at = now() - %s where id = %s",
            (timedelta(days=365), fact.id),
        )
    _message(conn, "chat")
    conn.commit()

    pipeline.run(conn, DAY, lambda _: proposal([]))

    survived = facts.active_for(conn, "constraint.injury_history")
    assert survived is not None and survived.confidence == Decimal("1.00")


# --- CONS-07: decay through the pass ---------------------------------------


def test_decay_lands_on_the_stated_curve(conn: psycopg.Connection) -> None:
    """CONS-07: a 30 day half life unconfirmed for 90 days is 0.30."""
    fact = facts.ratify(conn, "availability.days", ["mon", "wed"], "stated", reason="seed")
    with conn.cursor() as cur:
        cur.execute(
            "update facts set last_confirmed_at = now() - %s where id = %s",
            (timedelta(days=90), fact.id),
        )
    _message(conn, "chat")
    conn.commit()

    result = pipeline.run(conn, DAY, lambda _: proposal([]))

    assert result.decayed >= 1
    decayed = facts.active_for(conn, "availability.days")
    assert decayed is not None
    assert decayed.status == "active", "CONS-07: facts never silently vanish"
    assert decayed.confidence == Decimal("0.30")


# --- CONS-01 / 09 / 10: run logging, day summary, idempotency --------------


def test_run_logs_input_counts(conn: psycopg.Connection) -> None:
    """CONS-01: a run row with counts for each input."""
    facts.ratify(conn, "profile.height_cm", 183, "stated", reason="seed")
    _message(conn, "one")
    _message(conn, "two")
    state.queue_write(conn, {"key": "prefs.coach_tone", "value": "direct"})
    conn.commit()

    pipeline.run(conn, DAY, lambda _: proposal([]))

    with conn.cursor() as cur:
        cur.execute("select * from consolidation_runs where consolidated_on = %s", (DAY,))
        row = cur.fetchone()
    assert row["status"] == "succeeded"
    assert row["messages_in"] == 2
    assert row["pending_in"] == 1
    assert row["active_facts_in"] == 1


def test_day_summary_is_written_once(conn: psycopg.Connection) -> None:
    """CONS-09: one day_summary per qualifying date."""
    _message(conn, "chat")
    conn.commit()
    pipeline.run(conn, DAY, lambda _: proposal([], summary="Easy day, knee fine."))

    written = [n for n in notes.on_date(conn, DAY) if n.kind == "day_summary"]
    assert len(written) == 1
    assert "knee fine" in written[0].body


def test_a_silent_day_gets_no_summary(conn: psycopg.Connection) -> None:
    """CONS-09 qualifies on at least one message or telemetry event."""
    result = pipeline.run(conn, DAY, lambda _: proposal([], summary="should not appear"))
    assert result.applied == 0
    assert notes.on_date(conn, DAY) == []


def test_rerunning_a_night_creates_nothing_new(conn: psycopg.Connection) -> None:
    """CONS-10: a second run for a date results in zero new rows."""
    facts.ratify(conn, "availability.weekday_minutes", 90, "stated", reason="seed")
    _message(conn, "chat")
    conn.commit()

    diffs = [
        {
            "key": "availability.weekday_minutes",
            "value": 45,
            "provenance": "observed",
            "reason": "observed drift",
            "evidence": {},
        }
    ]
    pipeline.run(conn, DAY, lambda _: proposal(diffs))

    def counts() -> tuple[int, int, int]:
        with conn.cursor() as cur:
            cur.execute("select count(*) as n from facts")
            f = cur.fetchone()["n"]
            cur.execute("select count(*) as n from notes")
            n = cur.fetchone()["n"]
            cur.execute("select count(*) as n from fact_events")
            e = cur.fetchone()["n"]
        return f, n, e

    before = counts()
    second = pipeline.run(conn, DAY, lambda _: proposal(diffs))

    assert second.skipped is True
    assert counts() == before


def test_continuity_survives_the_night(conn: psycopg.Connection) -> None:
    """MEM-09 and CHAT-05: today_uncommitted clears, open threads do not."""
    state.update(conn, today_uncommitted=[{"key": "x"}], open_threads=["old"])
    _message(conn, "chat")
    conn.commit()

    pipeline.run(conn, DAY, lambda _: proposal([]))

    after = state.get(conn)
    assert after.today_uncommitted is None
    assert after.open_threads == ["knee"]
    assert after.last_topic == "knee"


# --- CONS-02: strict JSON, retried once, no partial writes -----------------


def test_malformed_output_is_retried_once(conn: psycopg.Connection) -> None:
    _message(conn, "chat")
    conn.commit()

    attempts: list[int] = []

    def flaky(_inputs: pipeline.Inputs) -> object:
        attempts.append(1)
        if len(attempts) == 1:
            return "not json at all"
        return proposal([])

    pipeline.run(conn, DAY, flaky)
    assert len(attempts) == 2


def test_two_malformed_outputs_fail_without_partial_writes(conn: psycopg.Connection) -> None:
    """CONS-02: logged as a failed run, with nothing half-applied."""
    facts.ratify(conn, "profile.height_cm", 183, "stated", reason="seed")
    _message(conn, "chat")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from facts")
        before = cur.fetchone()["n"]

    with pytest.raises(pipeline.MalformedProposal):
        pipeline.run(conn, DAY, lambda _: {"garbage": True})

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from facts")
        assert cur.fetchone()["n"] == before
        cur.execute(
            "select status, error from consolidation_runs where consolidated_on = %s", (DAY,)
        )
        row = cur.fetchone()
    assert row["status"] == "failed"
    assert row["error"]
    assert notes.on_date(conn, DAY) == []


def test_a_failed_run_retries_once_then_waits(conn: psycopg.Connection) -> None:
    """OBS-08: a failing run cannot loop; the second failure waits."""
    _message(conn, "chat")
    conn.commit()

    for _ in range(2):
        with pytest.raises(pipeline.MalformedProposal):
            pipeline.run(conn, DAY, lambda _: {"garbage": True})

    third = pipeline.run(conn, DAY, lambda _: proposal([]))
    assert third.skipped is True


def test_bad_provenance_is_rejected_at_the_schema(conn: psycopg.Connection) -> None:
    with pytest.raises(pipeline.MalformedProposal, match="provenance"):
        pipeline.validate(
            conn,
            proposal(
                [{"key": "k", "value": 1, "provenance": "vibes", "reason": "r", "evidence": {}}]
            ),
        )


# --- the matrix as a unit ---------------------------------------------------


def test_matrix_covers_every_documented_row(conn: psycopg.Connection) -> None:
    """Design section 7, row by row, without going through the pipeline."""
    availability = keys.load(conn, "availability.weekday_minutes")
    goal = keys.load(conn, "goal.target_weight_kg")
    constraint = keys.load(conn, "constraint.injury_history")

    facts.ratify(conn, "availability.weekday_minutes", 90, "stated", reason="s")
    conn.commit()
    stated = facts.active_for(conn, "availability.weekday_minutes")

    assert conflict.resolve(availability, stated, "observed").mentions
    assert conflict.resolve(goal, stated, "observed").outcome is conflict.Outcome.REJECT
    assert conflict.resolve(availability, stated, "stated").outcome is conflict.Outcome.APPLY
    assert conflict.resolve(constraint, None, "stated").outcome is conflict.Outcome.REJECT
    assert conflict.resolve(availability, None, "observed").outcome is conflict.Outcome.APPLY
    assert (
        conflict.resolve(availability, stated, "observed", Decimal("0.1")).outcome
        is conflict.Outcome.HOLD
    )
