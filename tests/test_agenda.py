"""The plan, read back, and the four defects that made the coach useless.

All four turned up in one screenshot. The athlete asked "what session?" at half
past seven on a Monday and got a clarifying question, then a commitment from his
own diary reported as a training session in the past tense. His calendar
meanwhile held three entries reading only "cycling", with no duration, no
target, and a week showing zero load.

None of it was the model reasoning badly. It was not told the time, it was not
told the plan, the only schedule-shaped thing in its prompt was the athlete's
diary, and the tool that writes prescriptions accepted an empty one.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

import conftest
from coach.agent import prompt, tools
from coach.blocks import document as blockmod
from coach.plans import agenda, events

DUBAI = ZoneInfo("Asia/Dubai")
MONDAY = date(2026, 8, 3)
# 07:32 local, which is when he asked. The session below is at 18:00.
MORNING = datetime(2026, 8, 3, 7, 32, tzinfo=DUBAI)

RIDE: dict[str, Any] = {
    "duration_s": 2700,
    "purpose": "Base endurance, conversational",
    "target_watts": 78,
    "ftp_watts": 115,
    "intensity_factor": 0.68,
}


@pytest.fixture(autouse=True)
def _zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """TZ-01's zone, set rather than inherited.

    `_planned_at` stores a naive wall clock time in the *configured* zone, so
    every assertion about a clock time in this file depends on what that zone
    is. Reading it from the ambient environment passed on a development box
    where COACH_TZ happened to be set and failed in CI where it is not — a test
    that asserts on a timezone has to state the timezone.
    """
    monkeypatch.setenv("COACH_TZ", "Asia/Dubai")


@pytest.fixture(autouse=True)
def _block(conn: psycopg.Connection) -> None:
    block_id = conftest.ensure_block(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update blocks set starts_on = %s, weeks = 8, title = 'Base block' where id = %s",
            (MONDAY - timedelta(days=7), block_id),
        )
    blockmod.activate(conn, block_id)


def write(conn: psycopg.Connection, **over: Any) -> dict[str, Any]:
    event = {
        "planned_for": "2026-08-03T18:00:00",
        "discipline": "ride",
        "spec": dict(RIDE),
    }
    event.update(over)
    return tools.dispatch(conn, "write_session_events", {"events": [event]})


def busy(conn: psycopg.Connection, summary: str) -> None:
    """A commitment on his own calendar, named like a session. It was."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into calendar_feeds (id, name, url_fingerprint, position) "
            "values ('f', 'cal', 'fp', 0) on conflict do nothing"
        )
        cur.execute("insert into calendar_fetches (feed, ok) values ('f', true)")
        cur.execute(
            "insert into calendar_events (feed, uid, summary, starts_at, ends_at, "
            "local_date, busy) values ('f', %s, %s, '2026-08-03T12:45Z', "
            "'2026-08-03T13:15Z', %s, true)",
            (summary, summary, MONDAY),
        )


# --- the blank sessions -----------------------------------------------------


def test_a_session_with_no_detail_is_refused(conn: psycopg.Connection) -> None:
    """What actually reached his calendar: three entries reading "cycling".

    `blocks.generate.validate` has enforced BLOCK-04 since P07. This is the
    other door into the same table and it checked nothing at all.
    """
    result = write(conn, spec={})

    assert result["written"] == 0
    assert any("duration_s is missing" in r for r in result["rejected"])
    assert any("purpose is missing" in r for r in result["rejected"])
    with conn.cursor() as cur:
        cur.execute("select count(*)::int as n from prescriptions")
        assert cur.fetchone()["n"] == 0


def test_nothing_is_written_when_one_session_is_bad(conn: psycopg.Connection) -> None:
    """A partial write leaves him unable to tell which sessions landed."""
    result = tools.dispatch(
        conn,
        "write_session_events",
        {
            "events": [
                {"planned_for": "2026-08-03T18:00:00", "discipline": "ride", "spec": dict(RIDE)},
                {"planned_for": "2026-08-05T18:00:00", "discipline": "ride", "spec": {}},
            ]
        },
    )

    assert result["written"] == 0
    with conn.cursor() as cur:
        cur.execute("select count(*)::int as n from prescriptions")
        assert cur.fetchone()["n"] == 0


def test_a_session_needs_an_intensity_target(conn: psycopg.Connection) -> None:
    """BLOCK-04. A duration and a name is a block of time, not a prescription."""
    result = write(conn, spec={"duration_s": 2700, "purpose": "ride about a bit"})

    assert any("intensity target" in r for r in result["rejected"])


def test_every_reason_is_returned_not_the_first(conn: psycopg.Connection) -> None:
    """The caller is a model, and it will fix exactly what it is told about."""
    assert len(write(conn, spec={})["rejected"]) == 3


def test_a_good_session_is_written_with_its_load(conn: psycopg.Connection) -> None:
    """`planned_load` was never set on this path, so it was null.

    BLOCK-07's ramp and GYM-05's ceiling both read that column. A session
    written without it costs nothing against the coach's own limits, and the
    platform shows the week as zero load — which is what his did.
    """
    result = write(conn)

    assert result["written"] == 1
    with conn.cursor() as cur:
        cur.execute("select planned_load from prescriptions")
        assert cur.fetchone()["planned_load"] > 0


def test_gym_movements_need_sets_reps_and_an_rpe(conn: psycopg.Connection) -> None:
    """GYM-01: the movements are the session."""
    result = write(
        conn,
        discipline="gym",
        spec={
            "duration_s": 2700,
            "purpose": "Lower body, first load",
            "rpe_target": 6,
            "movements": [{"name": "goblet squat", "sets": 3}],
        },
    )

    assert any("sets, reps and an RPE target" in r for r in result["rejected"])


# --- the vocabulary ---------------------------------------------------------


def test_cycling_is_stored_as_a_ride(conn: psycopg.Connection) -> None:
    """ "cycling" is not in `events.TYPES`, so it published as `Workout`.

    Upstream's catch-all: it renders with a dumbbell and the platform computes
    no cycling load for it. Every ride the coach planned through chat went up
    that way.
    """
    write(conn, discipline="cycling")

    with conn.cursor() as cur:
        cur.execute("select discipline from prescriptions")
        assert cur.fetchone()["discipline"] == "ride"


def test_an_already_stored_alias_still_publishes_as_a_ride() -> None:
    """Rows written before the alias existed heal on their next publish pass."""
    assert events.activity_type("cycling") == "Ride"
    assert events.activity_type("zwift") == "VirtualRide"


def test_an_unknown_discipline_is_refused_rather_than_guessed(conn: psycopg.Connection) -> None:
    result = write(conn, discipline="jetskiing")

    assert any("not a discipline the calendar understands" in r for r in result["rejected"])


# --- the timezone -----------------------------------------------------------


def test_a_local_wall_clock_time_is_stored_as_local(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TZ-01, at the door that did not apply it.

    `fromisoformat` returns a naive value and `planned_for` is `timestamptz`, so
    Postgres read "18:00" in the session zone and stored 18:00 UTC. In Dubai
    every session this tool wrote sat four hours late, on the calendar and in
    the morning message.
    """
    monkeypatch.setenv("COACH_TZ", "Asia/Dubai")

    write(conn)

    with conn.cursor() as cur:
        cur.execute("select planned_for from prescriptions")
        assert cur.fetchone()["planned_for"].astimezone(DUBAI).hour == 18


def test_an_explicit_offset_is_respected(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COACH_TZ", "Asia/Dubai")

    write(conn, planned_for="2026-08-03T18:00:00+00:00")

    with conn.cursor() as cur:
        cur.execute("select planned_for from prescriptions")
        assert cur.fetchone()["planned_for"].astimezone(DUBAI).hour == 22


# --- the coach can now see the plan -----------------------------------------


def test_the_surface_can_read_the_plan_back(conn: psycopg.Connection) -> None:
    """It could write a prescription and had no way to see one.

    So "what session?" had no answer in the tool surface either, not just in the
    prompt.
    """
    write(conn)

    result = tools.dispatch(conn, "get_plan", {"since": "2026-08-03", "until": "2026-08-09"})

    assert len(result["prescribed"]) == 1
    assert "78 W" in result["prescribed"][0]["described"]
    assert "Base endurance" in result["prescribed"][0]["described"]


def test_a_described_session_carries_its_numbers(conn: psycopg.Connection) -> None:
    """ "cycling" was the whole of the morning message. This is the fix."""
    write(conn)

    described = agenda.on(conn, MONDAY)[0].describe()

    assert described == "ride, 45 min, 78 W (IF 0.68). Base endurance, conversational"


def test_a_cancelled_session_is_not_offered_as_the_plan(conn: psycopg.Connection) -> None:
    write(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update prescriptions set status = 'cancelled'")

    assert agenda.on(conn, MONDAY) == []


# --- the TODAY block --------------------------------------------------------


def test_the_prompt_says_what_day_and_time_it_is(conn: psycopg.Connection) -> None:
    """It reported a session six hours away as having already happened.

    Not a reasoning failure. Nothing in the prompt said what time it was.
    """
    said = prompt.render_today(conn, MORNING, DUBAI)

    assert "Monday 3 August 2026" in said
    assert "07:32" in said


def test_the_prompt_says_where_in_the_block_he_is(conn: psycopg.Connection) -> None:
    """He asked where he was in the plan and nothing could have answered."""
    said = prompt.render_today(conn, MORNING, DUBAI)

    assert 'Week 2 of 8 in "Base block"' in said


def test_the_prompt_carries_todays_session(conn: psycopg.Connection) -> None:
    write(conn)

    said = prompt.render_today(conn, MORNING, DUBAI)

    assert "Today at 18:00" in said
    assert "45 min" in said
    assert "78 W" in said
    assert "not done yet" in said


def test_the_prompt_carries_the_rest_of_the_week(conn: psycopg.Connection) -> None:
    write(conn)
    write(conn, planned_for="2026-08-05T18:00:00", discipline="virtualride")

    said = prompt.render_today(conn, MORNING, DUBAI)

    assert "Still to come in the next seven days" in said
    assert "Wed 05 Aug" in said


def test_a_rest_day_says_so_rather_than_saying_nothing(conn: psycopg.Connection) -> None:
    assert "Nothing is prescribed for today." in prompt.render_today(conn, MORNING, DUBAI)


def test_an_uploaded_day_is_not_reported_as_undone(conn: psycopg.Connection) -> None:
    """Otherwise the coach asks whether he trained on a day he has uploaded."""
    write(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (source, discipline, started_at, local_date, duration_s) "
            "values ('intervals', 'ride', '2026-08-03T14:00Z', %s, 2700)",
            (MONDAY,),
        )

    said = prompt.render_today(conn, MORNING, DUBAI)

    assert "not done yet" not in said
    assert "something was uploaded" in said


# --- the diary is not the plan ----------------------------------------------


def test_the_diary_is_labelled_as_the_diary(conn: psycopg.Connection) -> None:
    """The heading said THE WEEK AHEAD and the coach read it as the plan.

    Asked what session was on, it answered "Zwift Ride Test, 16:45 to 17:15" —
    a thirty minute commitment from his own calendar, reported as training.
    """
    busy(conn, "Zwift Ride Test")

    said = prompt.render_calendar(conn, MORNING, DUBAI)

    assert "HIS DIARY" in said
    assert "not training sessions" in said
    assert "THE WEEK AHEAD" not in said


def test_the_plan_block_says_the_diary_is_not_it(conn: psycopg.Connection) -> None:
    """Said in both places, because the confusion ran in one direction."""
    said = prompt.render_today(conn, MORNING, DUBAI)

    assert "HIS DIARY" in said
    assert "never a session you prescribed" in said


def test_both_blocks_reach_the_prompt(conn: psycopg.Connection) -> None:
    """And in that order: the plan before the diary."""
    busy(conn, "Zwift Ride Test")
    write(conn)

    rendered = prompt.assemble(conn, MORNING, tz=DUBAI).render()

    assert rendered.index("TODAY") < rendered.index("HIS DIARY")
    assert "Base endurance" in rendered


# --- re-planning a week rather than doubling it -----------------------------


def test_replanning_withdraws_the_old_plan(conn: psycopg.Connection) -> None:
    """Without this the tool only ever added.

    The three blank sessions on his calendar could not be removed by anything
    the coach could reach: `write_session_events` inserts, and no tool cancels.
    Asking it to re-plan the week would have left him looking at both plans at
    once, the old one still publishing as empty blocks.
    """
    write(conn, spec={**RIDE, "purpose": "the old plan"})

    result = tools.dispatch(
        conn,
        "write_session_events",
        {
            "replaces": {"since": "2026-08-03", "until": "2026-08-09"},
            "events": [
                {
                    "planned_for": "2026-08-04T18:00:00",
                    "discipline": "ride",
                    "spec": {**RIDE, "purpose": "the new plan"},
                }
            ],
        },
    )

    assert result["withdrawn"] == 1
    assert result["written"] == 1
    with conn.cursor() as cur:
        cur.execute("select spec->>'purpose' as purpose from prescriptions")
        assert [r["purpose"] for r in cur.fetchall()] == ["the new plan"]


def test_replanning_never_touches_a_session_he_has_done(conn: psycopg.Connection) -> None:
    """BLOCK-08's guard: a prescription with a session attached is history."""
    write(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (source, discipline, started_at, local_date, duration_s) "
            "values ('intervals', 'ride', '2026-08-03T14:00Z', %s, 2700) returning id",
            (MONDAY,),
        )
        session_id = cur.fetchone()["id"]
        cur.execute("update prescriptions set status = 'completed', session_id = %s", (session_id,))

    result = tools.dispatch(
        conn,
        "write_session_events",
        {
            "replaces": {"since": "2026-08-03", "until": "2026-08-09"},
            "events": [
                {"planned_for": "2026-08-05T18:00:00", "discipline": "ride", "spec": dict(RIDE)}
            ],
        },
    )

    assert result["withdrawn"] == 0
    with conn.cursor() as cur:
        cur.execute("select count(*)::int as n from prescriptions where status = 'completed'")
        assert cur.fetchone()["n"] == 1


def test_a_rejected_replan_does_not_empty_the_week(conn: psycopg.Connection) -> None:
    """Delete and insert are one transaction, after validation.

    Otherwise a bad re-plan withdraws the week and writes nothing back, which is
    worse than either plan.
    """
    write(conn)

    result = tools.dispatch(
        conn,
        "write_session_events",
        {
            "replaces": {"since": "2026-08-03", "until": "2026-08-09"},
            "events": [{"planned_for": "2026-08-05T18:00:00", "discipline": "ride", "spec": {}}],
        },
    )

    assert result["written"] == 0
    with conn.cursor() as cur:
        cur.execute("select count(*)::int as n from prescriptions")
        assert cur.fetchone()["n"] == 1


def test_writing_without_replaces_still_only_adds(conn: psycopg.Connection) -> None:
    """Adding a session to a week is the common case and must stay the default."""
    write(conn)

    result = write(conn, planned_for="2026-08-06T18:00:00")

    assert result["withdrawn"] == 0
    with conn.cursor() as cur:
        cur.execute("select count(*)::int as n from prescriptions")
        assert cur.fetchone()["n"] == 2


def test_replanning_leaves_another_week_alone(conn: psycopg.Connection) -> None:
    write(conn, planned_for="2026-08-12T18:00:00")

    result = tools.dispatch(
        conn,
        "write_session_events",
        {
            "replaces": {"since": "2026-08-03", "until": "2026-08-09"},
            "events": [
                {"planned_for": "2026-08-05T18:00:00", "discipline": "ride", "spec": dict(RIDE)}
            ],
        },
    )

    assert result["withdrawn"] == 0
    with conn.cursor() as cur:
        cur.execute("select count(*)::int as n from prescriptions")
        assert cur.fetchone()["n"] == 2
