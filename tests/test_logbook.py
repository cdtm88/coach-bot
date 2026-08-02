"""Sessions captured from chat: LOG-01 to LOG-08.

Neither a gym session in an apartment building gym nor a round of golf produces
anything a device uploads, and both cost real load. The point of these tests is
that a session the athlete only ever *mentioned* ends up indistinguishable from
one a Garmin uploaded, everywhere it matters: the rollups, adherence, and the
week's ramp.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb

import conftest
from coach.agent import tools
from coach.ingest import reconcile
from coach.logbook import capture

DUBAI = ZoneInfo("Asia/Dubai")
TUESDAY = date(2026, 8, 4)


class FakeIntervals:
    def __init__(self, fail: bool = False) -> None:
        self.posted: list[dict[str, Any]] = []
        self.fail = fail

    def create_manual_activity(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("upstream is down")
        self.posted.append(payload)
        return {"id": "i9999"}


def gym_detail(**overrides: Any) -> dict[str, Any]:
    return {
        "movements": ["goblet squat 3x10", "dead bug 3x8", "McGill big three"],
        "duration_minutes": 45,
        "rpe": 7,
        "constraints_note": "no loaded flexion; hinge stayed light",
        **overrides,
    }


# --- LOG-01, LOG-02: gym from chat alone ------------------------------------


def test_a_gym_session_is_recorded_from_chat_alone(conn: psycopg.Connection) -> None:
    """LOG-01's acceptance, exactly as written."""
    captured = capture.record(conn, "gym", TUESDAY, gym_detail(), DUBAI)

    with conn.cursor() as cur:
        cur.execute(
            "select source, local_date, duration_s from sessions where id = %s",
            (captured.session_id,),
        )
        row = cur.fetchone()

    assert row["source"] == "chat"
    assert row["local_date"] == TUESDAY
    assert row["duration_s"] == 45 * 60


def test_the_captured_session_keeps_movement_level_detail(
    conn: psycopg.Connection,
) -> None:
    """LOG-02's acceptance: movement level detail, not just a duration.

    Kept whole rather than flattened into columns, because "how it sat against
    active constraints" is prose and the next question about it is not
    predictable enough to schematise.
    """
    captured = capture.record(conn, "gym", TUESDAY, gym_detail(), DUBAI)

    with conn.cursor() as cur:
        cur.execute("select derived from sessions where id = %s", (captured.session_id,))
        derived = cur.fetchone()["derived"]

    assert "McGill big three" in derived["detail"]["movements"]
    assert "no loaded flexion" in derived["detail"]["constraints_note"]


# --- LOG-03, LOG-04: one question -------------------------------------------


def test_a_round_without_transport_prompts_one_question(
    conn: psycopg.Connection,
) -> None:
    """LOG-03's acceptance: a round missing that detail prompts one question.

    Walked or carted roughly doubles the load, which is the difference between a
    golf day that costs nothing and one that costs an endurance ride.
    """
    with pytest.raises(capture.Incomplete) as raised:
        capture.record(conn, "golf", TUESDAY, {"holes": 18}, DUBAI)

    assert raised.value.question.count("?") == 1
    assert "cart" in raised.value.question


def test_never_more_than_one_question_at_a_time(conn: psycopg.Connection) -> None:
    """LOG-04: no capture turn contains more than one question.

    A gym session described with nothing at all is missing several things. It is
    still asked about one of them.
    """
    question = capture.missing_question("gym", {})

    assert question is not None
    assert question.count("?") == 1


def test_a_complete_round_needs_no_question(conn: psycopg.Connection) -> None:
    assert capture.missing_question("golf", {"transport": "walked"}) is None


def test_walking_costs_more_than_a_cart(conn: psycopg.Connection) -> None:
    """The reason LOG-03 makes it mandatory rather than optional."""
    walked, _ = capture.load_of("golf", {"transport": "walked", "holes": 18})
    carted, _ = capture.load_of("golf", {"transport": "carted", "holes": 18})

    assert walked > carted * 2


# --- LOG-05: it counts ------------------------------------------------------


def test_a_logged_gym_session_appears_in_the_rollups(conn: psycopg.Connection) -> None:
    """LOG-05's acceptance, exactly as written.

    The load lands in `derived` under the same key the activity feed uses, so
    the rollup picks it up without knowing this path exists.
    """
    capture.record(conn, "gym", TUESDAY, gym_detail(), DUBAI)
    reconcile.recompute_rollups(conn)

    with conn.cursor() as cur:
        cur.execute("select load_7d, gym_session_count from rollups where as_of = %s", (TUESDAY,))
        row = cur.fetchone()

    assert row["load_7d"] > 0
    assert row["gym_session_count"] == 1


def test_the_load_is_on_the_combined_scale(conn: psycopg.Connection) -> None:
    """GYM-08: RPE x minutes x coefficient, so it competes with a ride."""
    captured = capture.record(conn, "gym", TUESDAY, gym_detail(rpe=7, duration_minutes=45), DUBAI)

    assert captured.load == Decimal("7") * 45 * Decimal("0.20")


def test_a_captured_session_closes_the_prescription_it_satisfies(
    conn: psycopg.Connection,
) -> None:
    """LOG-05's other half: adherence.

    The matcher is the one feed ingest uses, so chat capture and the feed cannot
    drift apart on what counts as satisfying a prescription.
    """
    block_id = conftest.ensure_block(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status) "
            "values (%s, %s, 'weighttraining', %s, 'planned') returning id",
            (
                block_id,
                datetime.combine(TUESDAY, datetime.min.time()).replace(hour=18, tzinfo=UTC),
                Jsonb({"duration_s": 2700, "rpe": 7}),
            ),
        )
        prescription_id = int(cur.fetchone()["id"])

    captured = capture.record(conn, "gym", TUESDAY, gym_detail(), DUBAI)

    assert captured.prescription_id == prescription_id
    with conn.cursor() as cur:
        cur.execute("select status from prescriptions where id = %s", (prescription_id,))
        assert cur.fetchone()["status"] == "completed"


# --- LOG-06, LOG-07, LOG-08: upstream ---------------------------------------


def test_a_captured_session_is_written_upstream(conn: psycopg.Connection) -> None:
    """LOG-07: POST /activities/manual, verified in the live spec on 27 July 2026."""
    captured = capture.record(conn, "gym", TUESDAY, gym_detail(), DUBAI)
    api = FakeIntervals()

    result = capture.push_upstream(conn, api, captured)

    assert len(api.posted) == 1
    assert api.posted[0]["type"] == "weighttraining"
    assert api.posted[0]["icu_training_load"] == float(captured.load)
    assert result.external_ref == "i9999"


def test_an_upstream_outage_leaves_capture_working(conn: psycopg.Connection) -> None:
    """LOG-08's acceptance: simulated upstream outage leaves capture working.

    The local record is written before this is attempted at all, which is why
    the assertion is about the session still being there rather than about a
    retry.
    """
    captured = capture.record(conn, "gym", TUESDAY, gym_detail(), DUBAI)

    result = capture.push_upstream(conn, FakeIntervals(fail=True), captured)

    assert result.upstream_error is not None
    assert result.external_ref is None
    with conn.cursor() as cur:
        cur.execute("select count(*)::int as n from sessions where id = %s", (captured.session_id,))
        assert cur.fetchone()["n"] == 1


def test_the_conversation_never_waits_on_the_network(conn: psycopg.Connection) -> None:
    """LOG-08: "never blocks or delays ... the conversation".

    The turn's tool does not call upstream at all — that is the ingest loop's
    job. A test rather than a comment because the tempting refactor is to make
    the tool do both.
    """
    from pathlib import Path

    dispatcher = Path(tools.__file__).read_text()
    branch = dispatcher.split('if name == "log_session":', 1)[1].split("if name ==", 1)[0]
    # Comments in that branch explain the rule and name the function, so the
    # scan is over code only.
    code = "\n".join(line for line in branch.splitlines() if not line.strip().startswith("#"))

    assert "push_upstream" not in code


# --- the tool surface -------------------------------------------------------


def test_log_session_is_no_longer_deferred() -> None:
    assert "log_session" not in tools.DEFERRED


def test_the_tool_records_and_reports_the_load(conn: psycopg.Connection) -> None:
    result = tools.dispatch(
        conn,
        "log_session",
        {"discipline": "gym", "occurred_on": TUESDAY.isoformat(), "detail": gym_detail()},
    )

    assert result["recorded"] is True
    assert result["load"] == 63.0


def test_the_tool_hands_back_the_question_rather_than_guessing(
    conn: psycopg.Connection,
) -> None:
    """A capture that invented "carted" would put a wrong number in the rollups."""
    result = tools.dispatch(
        conn,
        "log_session",
        {"discipline": "golf", "occurred_on": TUESDAY.isoformat(), "detail": {"holes": 18}},
    )

    assert result["recorded"] is False
    assert "cart" in result["ask"]
