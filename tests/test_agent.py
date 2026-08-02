"""P01 acceptance: prompt assembly, naturalness, interruption budget, tools.

The phase is "implemented when" the regression suite passes on narration, one
question per message and the interruption budget; constraints are present
verbatim in every prompt; a stated constraint lands through SAFE-06; and a
second chat id is refused. These are those assertions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

import conftest
from coach.agent import interruptions, naturalness, persona, prompt, tools
from coach.memory import facts

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


# --- SAFE-01: constraints verbatim, at the top, always ---------------------


def test_constraints_lead_every_prompt(conn: psycopg.Connection) -> None:
    """SAFE-01: removing all other context still leaves the constraints."""
    facts.state_constraint(
        conn,
        "constraint.movement_restrictions",
        ["no loaded spinal flexion"],
        reason="seed",
        confirmed=True,
    )
    conn.commit()

    assembled = prompt.assemble(conn, NOW)
    rendered = assembled.render()

    assert "no loaded spinal flexion" in rendered
    names = assembled.names()
    assert "constraints" in names
    if "facts" in names:
        assert names.index("constraints") < names.index("facts")


def test_constraints_survive_an_empty_memory(conn: psycopg.Connection) -> None:
    """With no facts at all, the constraints block is still assembled."""
    facts.state_constraint(
        conn, "constraint.medical_flags", ["exercise induced asthma"], reason="s", confirmed=True
    )
    conn.commit()
    assembled = prompt.assemble(conn, NOW)
    assert "exercise induced asthma" in assembled.render()


def test_constraints_are_never_summarised(conn: psycopg.Connection) -> None:
    """SAFE-01 and MEM-13: an oversized assembly still carries them verbatim."""
    facts.state_constraint(
        conn, "constraint.injury_history", ["L4/L5 disc"], reason="seed", confirmed=True
    )
    conn.commit()

    # Large enough that the budget must shed something to fit.
    assembled = prompt.assemble(conn, NOW, episodic="e " * 8000)
    assert "L4/L5 disc" in assembled.render()
    assert "episodic_recall" in assembled.shed


def test_safety_facts_are_not_repeated_in_the_facts_block(conn: psycopg.Connection) -> None:
    """A constraint appears once, in the constraints block, not twice."""
    facts.state_constraint(
        conn, "constraint.injury_history", ["L4/L5 disc"], reason="seed", confirmed=True
    )
    facts.ratify(conn, "goal.target_weight_kg", 72.0, "stated", reason="seed")
    conn.commit()

    assert prompt.render_facts(conn).count("constraint.injury_history") == 0
    assert "goal.target_weight_kg" in prompt.render_facts(conn)


def test_stated_constraint_lands_through_the_safety_path(conn: psycopg.Connection) -> None:
    """P01 done-when: a stated constraint reaches the prompt in the same turn."""
    before = prompt.render_constraints(conn)
    assert "None recorded" in before

    facts.state_constraint(
        conn,
        "constraint.movement_restrictions",
        ["no overhead press"],
        reason="flare up",
        confirmed=True,
    )
    conn.commit()

    assert "no overhead press" in prompt.render_constraints(conn)


# --- CHAT-09: staleness is context, not an interruption --------------------


def test_stale_feed_is_surfaced_as_context(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update feeds set last_success_at = %s where name = 'activities'",
            (NOW - timedelta(days=30),),
        )
    conn.commit()

    rendered = prompt.render_staleness(conn, NOW)
    assert "activities" in rendered
    assert "Absence of data is not evidence" in rendered


def test_fresh_feed_produces_no_staleness_block(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("update feeds set last_success_at = %s", (NOW,))
    conn.commit()
    assert prompt.render_staleness(conn, NOW) == ""


# --- CHAT-11: one interruption per conversation ----------------------------


def _message(conn: psycopg.Connection, at: datetime) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into messages (chat_id, role, body, occurred_at) "
            "values (1, 'athlete', 'hi', %s)",
            (at,),
        )


def test_four_qualifying_interruptions_yield_exactly_one(conn: psycopg.Connection) -> None:
    """CHAT-11: counted across the conversation, not per category."""
    _message(conn, NOW - timedelta(minutes=5))
    conn.commit()

    candidates = [
        interruptions.Candidate("verification", "availability.days"),
        interruptions.Candidate("pending_mention", "availability.weekday_minutes"),
        interruptions.Candidate("body_mass_gap"),
        interruptions.Candidate("outlier_confirmation"),
    ]

    first = interruptions.claim(conn, candidates, NOW)
    conn.commit()
    assert first is not None
    # Highest priority present wins: outlier confirmation outranks the rest.
    assert first.kind == "outlier_confirmation"

    second = interruptions.claim(conn, candidates, NOW + timedelta(minutes=1))
    conn.commit()
    assert second is None


def test_safety_confirmation_outranks_everything(conn: psycopg.Connection) -> None:
    _message(conn, NOW - timedelta(minutes=5))
    conn.commit()
    claimed = interruptions.claim(
        conn,
        [
            interruptions.Candidate("verification"),
            interruptions.Candidate("safety_confirmation"),
            interruptions.Candidate("body_mass_gap"),
        ],
        NOW,
    )
    conn.commit()
    assert claimed is not None and claimed.kind == "safety_confirmation"


def test_a_new_conversation_gets_a_fresh_budget(conn: psycopg.Connection) -> None:
    """The budget is per conversation, not per lifetime."""
    _message(conn, NOW - timedelta(minutes=5))
    conn.commit()
    assert interruptions.claim(conn, [interruptions.Candidate("verification")], NOW) is not None
    conn.commit()

    later = NOW + timedelta(hours=9)
    _message(conn, later)
    conn.commit()
    assert interruptions.claim(conn, [interruptions.Candidate("verification")], later) is not None


def test_no_candidates_claims_nothing(conn: psycopg.Connection) -> None:
    _message(conn, NOW - timedelta(minutes=5))
    conn.commit()
    assert interruptions.claim(conn, [], NOW) is None
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from interruptions")
        assert cur.fetchone()["n"] == 0


def test_staleness_never_consumes_the_budget(conn: psycopg.Connection) -> None:
    """CHAT-11 states feed staleness is not an interruption. Nothing can claim it."""
    assert "staleness" not in interruptions.PRIORITY
    assert "feed_staleness" not in interruptions.PRIORITY


# --- CHAT-03 / CHAT-04 / CHAT-10 naturalness -------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Noted.",
        "I've saved that.",
        "Got it, I'll remember that for next time.",
        "That's now recorded against your profile.",
        "Updating that in my notes.",
        "I have logged that.",
    ],
)
def test_narration_is_detected(text: str) -> None:
    """CHAT-03: probes that explicitly invite narration."""
    assert naturalness.narrates_memory(text)


@pytest.mark.parametrize(
    "text",
    [
        "Weeknights are 45 minutes now, so Thursday moves to Saturday.",
        "That knee sounds like the same thing as March.",
        "Fine. Ride easy tomorrow.",
        "Your threshold went up 12 watts, which tracks with the last three weeks.",
    ],
)
def test_clean_responses_are_not_flagged(text: str) -> None:
    assert not naturalness.narrates_memory(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("How did the knee feel?", 1),
        ("How did the knee feel? And did you sleep?", 2),
        ("Was it the knee or the hip?", 1),  # one answer resolves it
        ("Tell me how the knee felt.", 1),  # imperative request, no question mark
        ("Ride easy tomorrow.", 0),
        ("Let me know how it goes and tell me what the hip did.", 2),
    ],
)
def test_question_counting_ignores_punctuation(text: str, expected: int) -> None:
    """CHAT-04: counts requests for information, however punctuated.

    The v2.1 rewrite exists because "no two question marks" passes for two
    questions in one sentence and for a question phrased as an imperative.
    """
    assert naturalness.count_questions(text) == expected


def test_two_questions_are_a_violation() -> None:
    found = naturalness.violations("How did the knee feel? And did you sleep?")
    assert any("CHAT-04" in v for v in found)


def test_markup_is_a_violation() -> None:
    """CHAT-10: no headers or bullet dumps unless asked."""
    assert any(
        "CHAT-10" in v for v in naturalness.violations("Week ahead:\n- Mon: easy\n- Tue: rest")
    )
    assert naturalness.violations("Easy Monday, rest Tuesday, then the hard one Thursday.") == []


# --- CHAT-02: persona -------------------------------------------------------


def test_persona_loads_from_the_versioned_file() -> None:
    body = persona.load()
    assert "at most one question per message" in body
    assert "never increase it on your own" in body


def test_editing_the_persona_changes_behaviour_without_a_deploy(tmp_path) -> None:
    """CHAT-02: the file is read per call, not imported once."""
    path = tmp_path / "persona.md"
    path.write_text("first")
    assert persona.load(path) == "first"
    path.write_text("second")
    assert persona.load(path) == "second"


# --- CHAT-06: the tool surface ----------------------------------------------


def test_every_tool_has_a_schema() -> None:
    assert len(tools.SCHEMAS) == 9  # P10 added set_break
    for schema in tools.SCHEMAS:
        assert schema["name"] and schema["description"]
        assert schema["input_schema"]["type"] == "object"
        assert "properties" in schema["input_schema"]
        assert "required" in schema["input_schema"]


def test_get_context_returns_provenance_and_history(conn: psycopg.Connection) -> None:
    """CHAT-07: three historical values come back with their dates."""
    for value in (60, 50, 45):
        facts.ratify(conn, "availability.weekday_minutes", value, "observed", reason="drift")
    conn.commit()

    result = tools.dispatch(conn, "get_context", {"key": "availability.weekday_minutes"})
    assert len(result["history"]) == 3
    assert result["history"][0]["value"] == 45
    assert result["history"][0]["status"] == "active"
    assert all(h["provenance"] == "observed" for h in result["history"])
    assert all(h["valid_from"] for h in result["history"])


def test_search_memory_finds_a_note(conn: psycopg.Connection) -> None:
    from datetime import date

    from coach.memory import notes

    notes.add(
        conn, "observation", "The knee complained on the Thursday intervals.", date(2026, 7, 20)
    )
    conn.commit()
    result = tools.dispatch(conn, "search_memory", {"query": "knee intervals"})
    assert len(result["notes"]) == 1


def test_propose_fact_queues_and_never_writes(conn: psycopg.Connection) -> None:
    """CONS-06: no path from a chat turn to a facts insert."""
    result = tools.dispatch(
        conn,
        "propose_fact",
        {
            "key": "goal.target_weight_kg",
            "value": 71.0,
            "provenance": "stated",
            "reason": "said so",
        },
    )
    conn.commit()

    assert result["queued"] is True
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from facts")
        assert cur.fetchone()["n"] == 0
        cur.execute("select count(*) as n from pending_writes where status = 'pending'")
        assert cur.fetchone()["n"] == 1


@pytest.mark.parametrize("name", sorted(tools.DEFERRED))
def test_deferred_tools_say_so_rather_than_guessing(conn: psycopg.Connection, name: str) -> None:
    result = tools.dispatch(conn, name, {})
    assert result["available"] is False
    assert tools.DEFERRED[name] in result["reason"]


def test_unknown_tool_is_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(tools.UnknownTool):
        tools.dispatch(conn, "delete_everything", {})


# --- Activity the platform will not serve ----------------------------------

DUBAI_TZ = ZoneInfo("Asia/Dubai")


def _unreadable_session(conn: psycopg.Connection, on: date, at: datetime) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into sessions
                (source, discipline, name, started_at, local_date, data_unavailable)
            values ('intervals', 'other', 'Strava activity (data not available)', %s, %s, true)
            returning id
            """,
            (at, on),
        )
        return cur.fetchone()["id"]


def _planned(conn: psycopg.Connection, at: datetime, discipline: str = "gym") -> int:
    from psycopg.types.json import Jsonb

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec) "
            "values (%s, %s, %s, %s) returning id",
            (conftest.ensure_block(conn), at, discipline, Jsonb({"duration_s": 3600})),
        )
        return cur.fetchone()["id"]


def test_an_unreadable_activity_against_a_plan_is_a_question(conn: psycopg.Connection) -> None:
    """Something was done. What it was is not knowable, so the coach asks.

    These are the gym and golf sessions Whoop writes to Strava, which intervals
    returns as a placeholder it will never serve. Scoring the planned session
    either way off one is an inference from an empty row.
    """
    when = datetime(2026, 7, 26, 13, 18, tzinfo=UTC)
    _unreadable_session(conn, date(2026, 7, 26), when)
    _planned(conn, when)
    conn.commit()

    rendered = prompt.render_unreadable(conn, NOW, DUBAI_TZ)
    assert "2026-07-26" in rendered
    assert "planned gym" in rendered
    assert "Ask" in rendered
    assert "neither completed nor missed" in rendered


def test_an_unreadable_activity_with_nothing_planned_is_not_raised(
    conn: psycopg.Connection,
) -> None:
    """A golf round the coach neither prescribed nor has anything to say about.

    Listing it would be the nagging CHAT-11 exists to prevent.
    """
    _unreadable_session(conn, date(2026, 7, 26), datetime(2026, 7, 26, 13, 18, tzinfo=UTC))
    conn.commit()
    assert prompt.render_unreadable(conn, NOW, DUBAI_TZ) == ""


def test_a_matched_prescription_stops_the_question(conn: psycopg.Connection) -> None:
    """Once he says what it was, there is nothing left to ask."""
    when = datetime(2026, 7, 26, 13, 18, tzinfo=UTC)
    session_id = _unreadable_session(conn, date(2026, 7, 26), when)
    prescription_id = _planned(conn, when)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update prescriptions set session_id = %s, status = 'completed' where id = %s",
            (session_id, prescription_id),
        )
    conn.commit()

    assert prompt.render_unreadable(conn, NOW, DUBAI_TZ) == ""


def test_the_question_never_spends_the_interruption_budget() -> None:
    """CHAT-09's precedent: absence of data shapes reasoning, it does not interrupt."""
    assert "unreadable" not in interruptions.PRIORITY
    assert "unreadable_activity" not in interruptions.PRIORITY
