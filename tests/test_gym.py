"""The constraint gate and gym programming: SAFE-04, GYM-01 to GYM-07.

Every constraint in this suite is one the athlete actually stated. They are in
`seeds/athlete.json` against an L5-S1 repair, and a test below asserts that the
seeded set is readable by the vocabulary — because a constraint the system
cannot read is the failure this module exists to prevent, and the seeded
constraints are the ones it will meet first.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest

from coach.blocks import constraints, document, generate, library
from coach.memory import facts

REPO = Path(__file__).resolve().parents[1]
DUBAI = ZoneInfo("Asia/Dubai")
MONDAY = date(2026, 8, 3)

# Verbatim from seeds/athlete.json, set at intake against the L5-S1 repair.
# The athlete's real words, copied from `seeds/athlete.json` rather than
# paraphrased. A test below asserts they still match the committed file, so a
# constraint that changes there fails here rather than silently going untested.
SEEDED_RESTRICTIONS = [
    "no barbell deadlifts",
    "no heavy barbell back squats",
    "no loaded twists",
    "no sit-ups or crunches",
    "loaded hip hinge (RDL, single leg RDL, conventional deadlift) withheld pending "
    "review around week 8, because the hinge produces the highest lumbar shear of any "
    "common gym pattern and technique errors are expensive",
]

SEEDED_HISTORY = [
    "L5-S1 herniated disc repair, November 2025. Recovery complete, occasional "
    "stiffness. Discharged by physio, cleared for golf and other activity."
]


@pytest.fixture
def restricted(conn: psycopg.Connection) -> None:
    """The athlete's real constraints, through the real SAFE-06 path."""
    facts.state_constraint(
        conn,
        "constraint.movement_restrictions",
        SEEDED_RESTRICTIONS,
        reason="set at intake against the L5-S1 repair",
        confirmed=True,
    )
    facts.state_constraint(
        conn,
        "constraint.injury_history",
        SEEDED_HISTORY,
        reason="stated at intake",
        confirmed=True,
    )


@pytest.fixture
def goals(conn: psycopg.Connection) -> None:
    facts.ratify(conn, "goal.target_weight_kg", 100.0, "stated", "intake")
    facts.ratify(conn, "goal.fitness_preservation", "lean mass retention", "stated", "intake")


def load_constraints(conn: psycopg.Connection) -> constraints.Constraints:
    return constraints.load(conn, library.known_movements(conn))


# --- reading the constraints -------------------------------------------------


def test_the_seeded_constraints_are_all_readable(conn, restricted) -> None:
    """The set the system will actually meet first must parse.

    If this fails, gym generation refuses entirely — which is the correct
    behaviour and a broken product. The fix is to widen the vocabulary, never to
    reword the athlete's constraint.
    """
    resolved = load_constraints(conn)

    assert resolved.unreadable == [], resolved.unreadable
    resolved.require_readable()  # does not raise


def test_the_seeded_constraints_match_the_committed_seed_file(conn) -> None:
    """This suite's fixtures are the athlete's real words, not paraphrases."""
    seed = json.loads((REPO / "seeds" / "athlete.json").read_text())
    committed = {c["key"]: c["value"] for c in seed["constraints"]}

    assert committed["constraint.movement_restrictions"] == SEEDED_RESTRICTIONS
    assert committed["constraint.injury_history"] == SEEDED_HISTORY


def test_the_restrictions_exclude_the_patterns_they_name(conn, restricted) -> None:
    resolved = load_constraints(conn)

    assert "hinge" in resolved.patterns
    assert "squat" in resolved.patterns
    assert "core_rotation" in resolved.patterns
    assert "core_flexion" in resolved.patterns


def test_the_useful_half_of_the_trunk_survives(conn, restricted) -> None:
    """The reason the trunk is three patterns and not one.

    Flexion and loaded rotation are out; anti-extension and anti-rotation are
    exactly what a repaired disc wants. Collapsing them into "core" would have
    banned the useful half along with the harmful half.
    """
    resolved = load_constraints(conn)

    assert resolved.blocks("core_antiextension", "plank") is None
    assert resolved.blocks("core_antirotation", "pallof press") is None
    assert resolved.blocks("core_flexion", "sit up") is not None
    assert resolved.blocks("core_rotation", "russian twist") is not None


def test_an_injury_history_is_prose_and_does_not_block_generation(conn, restricted) -> None:
    """A constraint fact carries an injury history as well as restrictions.

    Treating "discharged by physio, cleared for golf" as an unreadable movement
    would refuse to program for a perfectly ordinary athlete.
    """
    resolved = load_constraints(conn)
    assert not any("physio" in phrase for phrase in resolved.unreadable)


def test_an_unreadable_constraint_refuses_rather_than_ignores(conn, goals) -> None:
    """The decision worth arguing with, asserted.

    A constraint naming something outside the vocabulary is not a constraint
    that does not apply. Generation fails closed.
    """
    facts.state_constraint(
        conn,
        "constraint.movement_restrictions",
        ["no zercher yoke walks"],
        reason="stated",
        confirmed=True,
    )
    resolved = load_constraints(conn)

    assert resolved.unreadable
    with pytest.raises(constraints.ConstraintNotUnderstood, match="zercher"):
        resolved.require_readable()


def test_generation_refuses_on_an_unreadable_constraint(conn, goals) -> None:
    """And it refuses at the point of generating, not somewhere downstream."""
    facts.state_constraint(
        conn,
        "constraint.movement_restrictions",
        ["no zercher yoke walks"],
        reason="stated",
        confirmed=True,
    )
    block_id = document.create(conn, "Block 1", MONDAY, "# Block 1\n\n## plan\n\nWeek 1.\n")

    with pytest.raises(constraints.ConstraintNotUnderstood):
        generate.build(conn, block_id, MONDAY, [], DUBAI)


def test_no_constraints_at_all_is_not_an_unreadable_constraint(conn, goals) -> None:
    resolved = load_constraints(conn)
    assert resolved.exclusions == []
    resolved.require_readable()


# --- GYM-02 and SAFE-04: nothing excluded is ever prescribed -----------------


def gym_session(patterns: list[str]) -> generate.PlannedSession:
    return generate.PlannedSession(
        week=1,
        weekday=1,
        discipline="gym",
        purpose="strength",
        duration_s=45 * 60,
        rpe_target=6.0,
        patterns=patterns,
        sets=3,
        reps="8-10",
    )


def test_an_excluded_pattern_is_never_prescribed(conn, goals, restricted) -> None:
    """GYM-02's acceptance: blocked before publish and logged."""
    block_id = document.create(conn, "Block 1", MONDAY, "# B\n\n## plan\n\nWeek 1.\n")
    built = generate.build(conn, block_id, MONDAY, [gym_session(["hinge"])], DUBAI)
    generate.validate(conn, built)
    generate.publish(conn, built)

    with conn.cursor() as cur:
        cur.execute("select spec from prescriptions")
        rows = cur.fetchall()

    for row in rows:
        for movement in row["spec"].get("movements", []):
            assert movement["movement_pattern"] != "hinge"


def test_a_blocked_movement_is_logged_as_a_row(conn, goals, restricted) -> None:
    """GYM-02 and SAFE-04: "blocked and logged", as evidence the review can read."""
    block_id = document.create(conn, "Block 1", MONDAY, "# B\n\n## plan\n\nWeek 1.\n")
    built = generate.build(conn, block_id, MONDAY, [gym_session(["hinge"])], DUBAI)
    generate.publish(conn, built)

    with conn.cursor() as cur:
        cur.execute("select * from constraint_blocks")
        row = cur.fetchone()

    assert row["pattern"] == "hinge"
    assert "hip hinge" in row["constraint_text"] or "deadlift" in row["constraint_text"]
    assert row["substituted_with"]


def test_every_movement_in_a_published_session_passes_the_gate(conn, goals, restricted) -> None:
    """The property that matters, asserted over every pattern at once."""
    block_id = document.create(conn, "Block 1", MONDAY, "# B\n\n## plan\n\nWeek 1.\n")
    every_pattern = list(constraints.PATTERN_WORDS)
    built = generate.build(conn, block_id, MONDAY, [gym_session(every_pattern)], DUBAI)
    generate.publish(conn, built)

    resolved = load_constraints(conn)
    with conn.cursor() as cur:
        cur.execute("select spec from prescriptions")
        for row in cur.fetchall():
            for movement in row["spec"].get("movements", []):
                assert (
                    resolved.blocks(movement["movement_pattern"], movement["exercise"]) is None
                ), movement


def test_a_named_movement_is_blocked_even_where_the_pattern_is_not(conn, goals) -> None:
    """A constraint can name one exercise without banning its whole pattern."""
    facts.state_constraint(
        conn,
        "constraint.movement_restrictions",
        ["no lat pulldown"],
        reason="shoulder",
        confirmed=True,
    )
    resolved = load_constraints(conn)

    assert resolved.blocks("pull_vertical", "lat pulldown") is not None
    assert resolved.blocks("pull_vertical", "band pulldown") is not None  # pattern word matched


# --- GYM-03: substitution ----------------------------------------------------


def test_a_blocked_pattern_yields_a_substitute_not_an_empty_slot(conn, goals, restricted) -> None:
    """GYM-03's acceptance, on the constraint path."""
    resolved = load_constraints(conn)
    chosen, exclusion = library.resolve(conn, "hinge", resolved)

    assert exclusion is not None
    assert chosen is not None
    assert chosen.movement_pattern != "hinge"


def test_a_substitute_never_comes_from_the_excluded_pattern(conn, goals, restricted) -> None:
    """The failure this module exists to prevent.

    Swapping a barbell deadlift for a kettlebell swing satisfies the letter of
    "no barbell deadlifts" and violates the hinge restriction it expresses.
    """
    resolved = load_constraints(conn)
    chosen, _ = library.resolve(conn, "hinge", resolved)

    assert chosen.movement_pattern != "hinge"
    assert resolved.blocks(chosen.movement_pattern, chosen.name) is None


def test_removing_the_equipment_yields_a_substitute(conn, goals, monkeypatch) -> None:
    """GYM-03's acceptance as written: the *unavailable* path, not the blocked one."""
    resolved = load_constraints(conn)

    with_dumbbells = library.choose(conn, "pull_horizontal", resolved, ("dumbbell", "band"))
    assert with_dumbbells is not None

    bodyweight_only = library.choose(conn, "pull_horizontal", resolved, ("bodyweight",))
    assert bodyweight_only is not None
    assert bodyweight_only.equipment == "bodyweight"
    # Same pattern: only the implement changed, so the training intent is kept.
    assert bodyweight_only.movement_pattern == with_dumbbells.movement_pattern


def test_the_available_equipment_is_configuration(monkeypatch) -> None:
    """The apartment gym is a deliberate adherence choice that will change once."""
    assert library.available_equipment() == library.DEFAULT_EQUIPMENT
    monkeypatch.setenv("COACH_GYM_EQUIPMENT", "bodyweight, band, dumbbell, barbell")
    assert "barbell" in library.available_equipment()


def test_a_session_whose_every_pattern_is_excluded_is_not_published(conn, goals) -> None:
    """A gym session with no movements is not a session."""
    facts.state_constraint(
        conn,
        "constraint.movement_restrictions",
        ["no squat", "no lunge", "no hip extension", "no hinge"],
        reason="stated",
        confirmed=True,
    )
    block_id = document.create(conn, "Block 1", MONDAY, "# B\n\n## plan\n\nWeek 1.\n")
    built = generate.build(conn, block_id, MONDAY, [gym_session(["squat"])], DUBAI)

    assert built.prescriptions == []
    assert built.blocked


# --- GYM-01, GYM-04, GYM-07 --------------------------------------------------


def test_every_movement_carries_pattern_sets_reps_and_an_rpe_target(
    conn, goals, restricted
) -> None:
    """GYM-01's acceptance: all four for every movement."""
    block_id = document.create(conn, "Block 1", MONDAY, "# B\n\n## plan\n\nWeek 1.\n")
    built = generate.build(
        conn, block_id, MONDAY, [gym_session(["push_horizontal", "pull_horizontal"])], DUBAI
    )
    generate.validate(conn, built)

    movements = built.prescriptions[0]["spec"]["movements"]
    assert len(movements) == 2
    for movement in movements:
        assert movement["movement_pattern"]
        assert movement["sets"] == 3
        assert movement["reps"] == "8-10"
        assert movement["rpe_target"] == 6.0


def test_a_movement_without_sets_is_rejected(conn, goals, restricted) -> None:
    block_id = document.create(conn, "Block 1", MONDAY, "# B\n\n## plan\n\nWeek 1.\n")
    session = gym_session(["push_horizontal"])
    session.sets = None

    built = generate.build(conn, block_id, MONDAY, [session], DUBAI)
    with pytest.raises(generate.GenerationRejected, match="GYM-01"):
        generate.validate(conn, built)


def test_a_gym_session_is_never_exported_as_a_workout_file(conn, goals, restricted) -> None:
    """GYM-07: duration and purpose only, no structured export."""
    block_id = document.create(conn, "Block 1", MONDAY, "# B\n\n## plan\n\nWeek 1.\n")
    built = generate.build(conn, block_id, MONDAY, [gym_session(["push_horizontal"])], DUBAI)

    spec = built.prescriptions[0]["spec"]
    assert spec["export"] == "none"
    assert "file_contents" not in spec
    assert "workout_steps" not in spec


def test_gym_load_is_not_tonnage(conn) -> None:
    """GYM-04: session count, RPE and duration rather than tonnage.

    Asserted against the schema: there is nowhere to put a weight lifted, so
    nothing can start tracking one by accident.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select column_name from information_schema.columns "
            "where table_schema = 'public' and table_name in ('prescriptions', 'exercises')"
        )
        columns = {r["column_name"] for r in cur.fetchall()}

    assert not {"tonnage", "weight_kg", "load_kg", "volume_load"} & columns


def test_the_library_has_no_barbell_only_pattern(conn) -> None:
    """Every pattern needs at least one movement the apartment gym can do.

    Otherwise GYM-03 substitutes correctly and still yields nothing, and the
    athlete gets a session with a hole in it.
    """
    available = ("bodyweight", "band", "dumbbell")
    patterns = {e.movement_pattern for e in library.all_exercises(conn)}

    empty = [p for p in patterns if not library.in_pattern(conn, p, available)]
    assert empty == [], f"no available movement for: {empty}"
