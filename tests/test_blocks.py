"""Training blocks and the load ceiling: BLOCK-01 to BLOCK-08, GYM-05, GYM-08.

The constraint gate has its own module (test_gym.py); this one covers the block
document, the combined load unit and the rules that reject a generation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
import pytest

from coach.agent import tools
from coach.blocks import document, generate, load
from coach.memory import facts

DUBAI = ZoneInfo("Asia/Dubai")
MONDAY = date(2026, 8, 3)


@pytest.fixture
def goals(conn: psycopg.Connection) -> None:
    """BLOCK-05's two goals, as active facts."""
    facts.ratify(conn, "goal.target_weight_kg", 100.0, "stated", "intake")
    facts.ratify(
        conn,
        "goal.fitness_preservation",
        "weight that stays off; lean mass retention is the mechanism",
        "stated",
        "intake",
    )


DOCUMENT = """# Block 1

## goals

Under 100 kg, holding lean mass.

## constraints

None recorded.

## plan

Week 1: ramp test, three endurance rides.
Week 2: as week one plus one gym session.
"""


def make_block(conn: psycopg.Connection, content: str = DOCUMENT) -> int:
    return document.create(conn, "Block 1", MONDAY, content)


def ride(week: int, weekday: int, minutes: int, intensity: float, **kw) -> generate.PlannedSession:
    return generate.PlannedSession(
        week=week,
        weekday=weekday,
        discipline="ride",
        purpose="endurance",
        duration_s=minutes * 60,
        intensity_factor=intensity,
        **kw,
    )


def gym(week: int, weekday: int, minutes: int, rpe: float, patterns: list[str] | None = None):
    return generate.PlannedSession(
        week=week,
        weekday=weekday,
        discipline="gym",
        purpose="strength",
        duration_s=minutes * 60,
        rpe_target=rpe,
        patterns=patterns or ["push_horizontal", "pull_horizontal"],
        sets=3,
        reps="8-10",
    )


# --- BLOCK-01 and BLOCK-02: the document -------------------------------------


def test_a_block_returns_current_content_and_full_history(conn, goals) -> None:
    """BLOCK-01's acceptance, exactly as written."""
    block_id = make_block(conn)
    document.rewrite(conn, block_id, "plan", "Week 1: rewritten.", "ramp test moved")
    document.rewrite(conn, block_id, "plan", "Week 1: rewritten again.", "second change")

    block = document.get(conn, block_id)

    assert block.version == 3
    assert "rewritten again" in block.content
    assert [v.version for v in block.history] == [3, 2, 1]
    assert [v.reason for v in block.history][-1] == "block created"


def test_a_rewrite_touches_one_section(conn, goals) -> None:
    """BLOCK-02: diffs between versions are localised, not wholesale."""
    block_id = make_block(conn)
    before = document.get(conn, block_id).content

    document.rewrite(conn, block_id, "plan", "Week 1: something else entirely.", "changed")
    after = document.get(conn, block_id).content

    assert document.section_of(after, "goals") == document.section_of(before, "goals")
    assert document.section_of(after, "constraints") == document.section_of(before, "constraints")
    assert "something else entirely" in document.section_of(after, "plan")
    assert document.diff_size(before, after) < 8


def test_a_wholesale_replacement_is_a_visibly_larger_diff(conn, goals) -> None:
    """The measure BLOCK-02 is asserted with has to distinguish the two."""
    block_id = make_block(conn)
    before = document.get(conn, block_id).content

    document.rewrite(conn, block_id, "plan", "Week 1: new plan.", "localised")
    localised = document.diff_size(before, document.get(conn, block_id).content)

    document.replace(
        conn, block_id, "# Block 1\n\n## plan\n\nEverything is different.\n", "wholesale"
    )
    wholesale = document.diff_size(before, document.get(conn, block_id).content)

    assert wholesale > localised


def test_a_missing_section_is_added_rather_than_failing(conn, goals) -> None:
    """A block written before a section existed gains it on the first rewrite."""
    block_id = make_block(conn, "# Block 1\n\n## plan\n\nWeek 1.\n")
    document.rewrite(conn, block_id, "review", "Nothing yet.", "adding the review section")

    assert document.section_of(document.get(conn, block_id).content, "review") == "Nothing yet."


def test_only_one_block_is_active(conn, goals) -> None:
    first, second = make_block(conn), make_block(conn)
    document.activate(conn, first)
    document.activate(conn, second)

    assert document.active(conn).id == second
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from blocks where status = 'active'")
        assert cur.fetchone()["n"] == 1


# --- BLOCK-05: both goals ----------------------------------------------------


def test_a_block_carries_the_weight_goal_and_the_preservation_goal(conn, goals) -> None:
    """BLOCK-05's acceptance: the block document contains both."""
    block = document.get(conn, make_block(conn))

    assert block.goals["target_weight_kg"] == 100.0
    assert "lean mass" in block.goals["fitness_preservation"]


def test_a_block_without_a_preservation_goal_is_refused(conn) -> None:
    """Optimising weight alone is optimising the wrong thing."""
    facts.ratify(conn, "goal.target_weight_kg", 100.0, "stated", "intake")

    with pytest.raises(document.MissingGoal, match="fitness preservation"):
        document.create(conn, "Block 1", MONDAY, DOCUMENT)


def test_the_constraints_section_is_verbatim_from_the_safety_facts(conn, goals) -> None:
    """SAFE-01: copied into the document rather than referenced."""
    facts.state_constraint(
        conn,
        "constraint.movement_restrictions",
        ["no barbell deadlifts", "no loaded twists"],
        reason="intake",
        confirmed=True,
    )
    rendered = document.render_constraints(conn)

    assert "no barbell deadlifts" in rendered
    assert "no loaded twists" in rendered


# --- GYM-08: one scale for two disciplines -----------------------------------


def test_gym_load_is_rpe_times_minutes_times_the_coefficient(monkeypatch) -> None:
    """GYM-08, stated as the requirement states it."""
    monkeypatch.setenv("COACH_GYM_LOAD_COEFFICIENT", "0.20")
    assert load.gym_load(7, 45) == Decimal("63.00")


def test_the_coefficient_changes_the_trade_without_a_deploy(monkeypatch) -> None:
    """GYM-08's acceptance: changing the coefficient changes the trade."""
    monkeypatch.setenv("COACH_GYM_LOAD_COEFFICIENT", "0.20")
    before = load.gym_load(7, 45)
    monkeypatch.setenv("COACH_GYM_LOAD_COEFFICIENT", "0.40")
    assert load.gym_load(7, 45) == before * 2


def test_an_hour_at_threshold_is_one_hundred() -> None:
    """The cycling side is the standard model, which is what makes the
    coefficient meaningful rather than arbitrary."""
    assert load.cycling_load(1.0, 3600) == Decimal("100.00")


def test_equal_computed_loads_are_interchangeable(conn, goals, monkeypatch) -> None:
    """GYM-08's acceptance: a gym and a cycling session of equal computed load
    are interchangeable against the weekly ceiling."""
    monkeypatch.setenv("COACH_GYM_LOAD_COEFFICIENT", "0.20")

    a_ride = load.of_spec("ride", {"duration_s": 3600, "intensity_factor": 0.6})
    a_gym = load.of_spec("gym", {"duration_s": 45 * 60, "rpe_target": 4.0})

    assert a_ride == Decimal("36.00")
    assert a_gym == Decimal("36.00")


def test_a_spec_with_nothing_to_compute_from_costs_nothing(conn) -> None:
    """A rest day has no load, and neither does a half-written spec."""
    assert load.of_spec("ride", {"duration_s": 3600}) == Decimal("0.00")
    assert load.of_spec("gym", {"duration_s": 3600}) == Decimal("0.00")


# --- BLOCK-07 and GYM-05: the weekly ceiling ---------------------------------


def test_a_block_inside_the_ramp_limit_generates(conn, goals) -> None:
    """BLOCK-03: four weeks, prescriptions for every planned session."""
    block_id = make_block(conn)
    sessions = [ride(week, day, 60, 0.60) for week in (1, 2, 3, 4) for day in (1, 3, 5)]
    built = generate.build(conn, block_id, MONDAY, sessions, DUBAI, ftp_watts=115)
    generate.validate(conn, built)
    ids = generate.publish(conn, built)

    assert len(ids) == 12
    with conn.cursor() as cur:
        cur.execute(
            "select count(distinct date_trunc('week', planned_for)) as n from prescriptions"
        )
        assert cur.fetchone()["n"] == 4


def test_a_week_over_the_ramp_limit_is_rejected(conn, goals) -> None:
    """BLOCK-07: generation is rejected if the limit is breached."""
    block_id = make_block(conn)
    sessions = [ride(1, day, 60, 0.60) for day in (1, 3, 5)]
    sessions += [ride(2, day, 120, 0.75) for day in (1, 3, 5)]

    built = generate.build(conn, block_id, MONDAY, sessions, DUBAI, ftp_watts=115)
    with pytest.raises(generate.GenerationRejected, match="ceiling"):
        generate.validate(conn, built)


def test_a_rejected_generation_writes_nothing(conn, goals) -> None:
    """Weeks one to three must not survive a week four that breaches."""
    block_id = make_block(conn)
    sessions = [ride(1, day, 60, 0.60) for day in (1, 3, 5)]
    sessions += [ride(2, day, 60, 0.60) for day in (1, 3, 5)]
    sessions += [ride(3, day, 180, 0.85) for day in (1, 3, 5)]

    built = generate.build(conn, block_id, MONDAY, sessions, DUBAI, ftp_watts=115)
    with pytest.raises(generate.GenerationRejected):
        generate.validate(conn, built)

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from prescriptions")
        assert cur.fetchone()["n"] == 0


def test_the_breach_can_come_from_added_gym_volume(conn, goals, monkeypatch) -> None:
    """BLOCK-07's acceptance names this case: "including where the breach comes
    from added gym volume"."""
    monkeypatch.setenv("COACH_GYM_LOAD_COEFFICIENT", "0.20")
    block_id = make_block(conn)

    # Two identical cycling weeks: no breach.
    steady = [ride(w, d, 60, 0.60) for w in (1, 2) for d in (1, 3, 5)]
    generate.validate(conn, generate.build(conn, block_id, MONDAY, steady, DUBAI, ftp_watts=115))

    # The same two weeks, with three gym sessions added to the second.
    with_gym = steady + [gym(2, d, 60, 8.0) for d in (0, 2, 4)]
    built = generate.build(conn, block_id, MONDAY, with_gym, DUBAI, ftp_watts=115)
    with pytest.raises(generate.GenerationRejected, match="gym"):
        generate.validate(conn, built)


def test_a_week_at_the_ceiling_cannot_add_a_gym_session(conn, goals, monkeypatch) -> None:
    """GYM-05's acceptance, against stored rows rather than a generation."""
    monkeypatch.setenv("COACH_GYM_LOAD_COEFFICIENT", "0.20")
    block_id = make_block(conn)

    sessions = [ride(1, d, 60, 0.60) for d in (1, 3, 5)]
    sessions += [ride(2, d, 60, 0.62) for d in (1, 3, 5)]
    built = generate.build(conn, block_id, MONDAY, sessions, DUBAI, ftp_watts=115)
    generate.validate(conn, built)
    generate.publish(conn, built)

    second_week = MONDAY + timedelta(weeks=1)
    breach = load.would_breach(conn, second_week, load.gym_load(8, 60), block_id)

    assert breach is not None and "over the ceiling" in breach


def test_a_first_week_has_no_ceiling_to_breach(conn, goals) -> None:
    """A block has nothing to ramp from, and refusing to generate one would make
    the rule impossible to satisfy rather than safe."""
    assert load.ceiling_for(None) is None

    tiny = load.Week(
        starts_on=MONDAY, total=Decimal("5"), cycling=Decimal("5"), gym=Decimal("0"), sessions=1
    )
    assert load.ceiling_for(tiny) is None  # too small to be a baseline


def test_the_ramp_percentage_is_configurable(conn, goals, monkeypatch) -> None:
    week_one = load.Week(MONDAY, Decimal("100"), Decimal("100"), Decimal("0"), 3)

    monkeypatch.setenv("COACH_WEEKLY_LOAD_RAMP_PCT", "10")
    assert load.ceiling_for(week_one) == Decimal("110.00")
    monkeypatch.setenv("COACH_WEEKLY_LOAD_RAMP_PCT", "25")
    assert load.ceiling_for(week_one) == Decimal("125.00")


def test_the_stored_weeks_agree_with_the_generated_weeks(conn, goals) -> None:
    """The in-memory validation and the SQL rollup must not drift apart."""
    block_id = make_block(conn)
    sessions = [ride(w, d, 60, 0.60) for w in (1, 2) for d in (1, 3)]
    sessions += [gym(2, 4, 45, 6.0)]

    built = generate.build(conn, block_id, MONDAY, sessions, DUBAI, ftp_watts=115)
    # A generous ramp limit: this test is about the two computations agreeing,
    # not about the ceiling, and the default limit would reject the fixture.
    generate.validate(conn, built, pct=Decimal("100"))
    generate.publish(conn, built)

    in_memory = {w.starts_on: w.total for w in generate._weeks_of(built)}
    in_sql = {w.starts_on: w.total for w in load.planned_weeks(conn, block_id)}
    assert in_memory == in_sql


# --- BLOCK-04: every field on publish ----------------------------------------


def test_every_prescription_carries_duration_intensity_discipline_and_purpose(conn, goals) -> None:
    """BLOCK-04's acceptance: all fields non-null on publish."""
    block_id = make_block(conn)
    built = generate.build(
        conn, block_id, MONDAY, [ride(1, 1, 60, 0.60, route="Watopia Flat")], DUBAI, ftp_watts=115
    )
    generate.validate(conn, built)
    generate.publish(conn, built)

    with conn.cursor() as cur:
        cur.execute("select discipline, spec from prescriptions")
        row = cur.fetchone()

    assert row["discipline"] == "ride"
    assert row["spec"]["duration_s"] == 3600
    assert row["spec"]["purpose"] == "endurance"
    assert row["spec"]["target_watts"] == 69
    assert row["spec"]["route"] == "Watopia Flat"


def test_a_prescription_missing_a_purpose_is_rejected(conn, goals) -> None:
    block_id = make_block(conn)
    session = ride(1, 1, 60, 0.60)
    session.purpose = ""

    built = generate.build(conn, block_id, MONDAY, [session], DUBAI, ftp_watts=115)
    with pytest.raises(generate.GenerationRejected, match="purpose"):
        generate.validate(conn, built)


def test_a_rejection_reports_every_reason_at_once(conn, goals) -> None:
    """A generator told one problem at a time will fix one problem at a time."""
    block_id = make_block(conn)
    first, second = ride(1, 1, 60, 0.60), ride(1, 3, 60, 0.60)
    first.purpose = ""
    second.purpose = ""

    built = generate.build(conn, block_id, MONDAY, [first, second], DUBAI, ftp_watts=115)
    with pytest.raises(generate.GenerationRejected) as caught:
        generate.validate(conn, built)
    assert len(caught.value.reasons) == 2


# --- BLOCK-06: the ramp test -------------------------------------------------


def test_week_one_must_contain_a_ramp_test(conn, goals) -> None:
    """BLOCK-06: the test appears in the plan."""
    without = [ride(1, 1, 60, 0.60)]
    with pytest.raises(generate.GenerationRejected, match="ramp test"):
        generate.require_ramp_test(without)

    generate.require_ramp_test([ride(1, 1, 30, 0.70, is_ramp_test=True)])


def test_a_ramp_test_in_week_two_does_not_satisfy_block_06(conn, goals) -> None:
    """Everything else in the block is scaled off the threshold it sets."""
    with pytest.raises(generate.GenerationRejected):
        generate.require_ramp_test([ride(2, 1, 30, 0.70, is_ramp_test=True)])


def test_the_ramp_test_result_supersedes_an_inferred_threshold(conn, goals) -> None:
    """BLOCK-06 and CONS-05: measured replaces inferred, silently."""
    facts.ratify(conn, "physiology.ftp_watts", 101, "inferred", "zwift's guess")

    queued = generate.record_ramp_test(conn, ftp_watts=115, threshold_hr=152, max_hr=169)
    assert len(queued) == 3

    # CONS-06: it is a proposal, not a write.
    assert facts.active_for(conn, "physiology.ftp_watts").value == 101

    with conn.cursor() as cur:
        cur.execute(
            "select proposal from pending_writes where proposal->>'key' = 'physiology.ftp_watts'"
        )
        proposal = cur.fetchone()["proposal"]
    assert proposal["value"] == 115
    assert proposal["provenance"] == "computed"

    # And once the night ratifies it, measured has replaced inferred.
    facts.ratify(conn, "physiology.ftp_watts", 115, "computed", "ramp test")
    assert facts.active_for(conn, "physiology.ftp_watts").value == 115
    assert facts.active_for(conn, "physiology.ftp_watts").provenance == "computed"


# --- BLOCK-08: restructuring the remaining block ------------------------------


def test_a_restructure_updates_every_remaining_week(conn, goals) -> None:
    """BLOCK-08: not only the coming week."""
    block_id = make_block(conn)
    original = [ride(w, d, 60, 0.60) for w in (1, 2, 3, 4) for d in (1, 3)]
    built = generate.build(conn, block_id, MONDAY, original, DUBAI, ftp_watts=115)
    generate.validate(conn, built)
    generate.publish(conn, built)

    # Week 2 onward becomes shorter sessions on different days.
    replacement = [ride(w, d, 45, 0.55) for w in (2, 3, 4) for d in (2, 4)]
    generate.restructure(
        conn,
        block_id,
        from_date=MONDAY + timedelta(weeks=1),
        sessions=replacement,
        starts_on=MONDAY,
        tz=DUBAI,
        ftp_watts=115,
    )

    with conn.cursor() as cur:
        cur.execute("select planned_for, spec from prescriptions order by planned_for")
        rows = cur.fetchall()

    assert len(rows) == 2 + 6  # week one untouched, three weeks regenerated
    later = [r for r in rows if r["planned_for"].date() >= MONDAY + timedelta(weeks=1)]
    assert all(r["spec"]["duration_s"] == 45 * 60 for r in later)


def test_a_restructure_never_touches_a_completed_session(conn, goals) -> None:
    """A prescription with a session attached is history."""
    block_id = make_block(conn)
    built = generate.build(conn, block_id, MONDAY, [ride(2, 1, 60, 0.60)], DUBAI, ftp_watts=115)
    generate.validate(conn, built)
    [prescription_id] = generate.publish(conn, built)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (discipline, started_at, local_date) "
            "values ('ride', %s, %s) returning id",
            (datetime(2026, 8, 11, 18, 0, tzinfo=DUBAI), date(2026, 8, 11)),
        )
        session_id = cur.fetchone()["id"]
        cur.execute(
            "update prescriptions set session_id = %s, status = 'completed' where id = %s",
            (session_id, prescription_id),
        )

    generate.restructure(
        conn,
        block_id,
        from_date=MONDAY,
        sessions=[ride(2, 3, 45, 0.55)],
        starts_on=MONDAY,
        tz=DUBAI,
        ftp_watts=115,
    )

    with conn.cursor() as cur:
        cur.execute("select id, status from prescriptions where id = %s", (prescription_id,))
        assert cur.fetchone()["status"] == "completed"


def test_a_failed_restructure_leaves_the_old_plan_intact(conn, goals) -> None:
    """The delete and the insert are one transaction, and validation precedes both."""
    block_id = make_block(conn)
    original = [ride(w, d, 60, 0.60) for w in (1, 2) for d in (1, 3)]
    built = generate.build(conn, block_id, MONDAY, original, DUBAI, ftp_watts=115)
    generate.validate(conn, built)
    generate.publish(conn, built)

    reckless = [ride(1, d, 60, 0.60) for d in (1, 3)]
    reckless += [ride(2, d, 240, 0.90) for d in (1, 3, 5)]

    with pytest.raises(generate.GenerationRejected):
        generate.restructure(conn, block_id, MONDAY, reckless, MONDAY, DUBAI, ftp_watts=115)

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from prescriptions")
        assert cur.fetchone()["n"] == 4


# --- CHAT-06: the tool -------------------------------------------------------


def test_update_block_is_no_longer_deferred(conn, goals) -> None:
    block_id = make_block(conn)
    document.activate(conn, block_id)

    result = tools.dispatch(
        conn,
        "update_block",
        {"section": "plan", "content": "Week 1: revised.", "reason": "the athlete is travelling"},
    )

    assert "update_block" not in tools.DEFERRED
    assert result["version"] == 2
    assert "revised" in document.get(conn, block_id).content


def test_update_block_says_so_when_there_is_no_block(conn, goals) -> None:
    result = tools.dispatch(
        conn, "update_block", {"section": "plan", "content": "x", "reason": "y"}
    )
    assert result["available"] is False
