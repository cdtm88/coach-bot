"""The P00 invariants: MEM-01 to MEM-06, MEM-14, SAFE-02, SAFE-03, SAFE-06.

These are the acceptance criteria for the phase, written as assertions.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest

from coach.config import CONFIDENCE_FLOOR
from coach.memory import facts, keys


def test_unknown_key_is_rejected(conn: psycopg.Connection) -> None:
    """MEM-01: a write to a key absent from fact_keys is rejected."""
    with pytest.raises(keys.UnknownKey):
        facts.ratify(conn, "invented.namespace", 42, "stated", reason="test")


def test_foreign_key_blocks_a_raw_insert(conn: psycopg.Connection) -> None:
    """MEM-01 at the database level, not just in the application layer."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.cursor() as cur:
        cur.execute(
            "insert into facts (key, value, provenance) values ('nope.nope', '1', 'stated')"
        )


def test_one_active_row_per_key(conn: psycopg.Connection) -> None:
    """MEM-02: a second active row for a key raises a constraint violation."""
    facts.ratify(conn, "profile.height_cm", 183, "stated", reason="seed")
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation), conn.cursor() as cur:
        cur.execute(
            "insert into facts (key, value, provenance) "
            "values ('profile.height_cm', '184', 'stated')"
        )


def test_supersede_keeps_full_history(conn: psycopg.Connection) -> None:
    """MEM-03: after three changes, one active row and two superseded, in order."""
    for value in (70.0, 71.5, 72.0):
        facts.ratify(conn, "goal.target_weight_kg", value, "stated", reason="change")
    conn.commit()

    history = facts.history(conn, "goal.target_weight_kg")
    assert len(history) == 3
    assert [f.status for f in history] == ["active", "superseded", "superseded"]
    assert [f.value for f in history] == [72.0, 71.5, 70.0]

    # The supersession pointers resolve into a chain.
    assert history[1].superseded_by == history[0].id
    assert history[2].superseded_by == history[1].id
    assert history[0].superseded_by is None


def test_supersede_is_atomic(conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """MEM-03: a supersede interrupted part way leaves the prior row active.

    The audit write is forced to fail, which lands after the old row has been
    closed, the new row inserted and the pointer set. Without one transaction
    around the whole pair, the key would be left with no active row at all — the
    partial unique index of MEM-02 guarantees at most one, never at least one.
    """
    original = facts.ratify(conn, "physiology.ftp_watts", 240, "computed", reason="ramp test")
    conn.commit()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("interrupted mid supersede")

    monkeypatch.setattr(facts, "_log_event", boom)

    with pytest.raises(RuntimeError):
        facts.ratify(conn, "physiology.ftp_watts", 260, "computed", reason="new ramp test")

    monkeypatch.undo()

    still_active = facts.active_for(conn, "physiology.ftp_watts")
    assert still_active is not None
    assert still_active.id == original.id
    assert still_active.value == 240
    # And nothing partial was left behind.
    assert len(facts.history(conn, "physiology.ftp_watts")) == 1


def test_provenance_is_constrained(conn: psycopg.Connection) -> None:
    """MEM-04: a row cannot be inserted without a valid provenance value."""
    with pytest.raises(ValueError, match="provenance"):
        facts.ratify(conn, "profile.height_cm", 183, "guessed", reason="test")

    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        cur.execute(
            "insert into facts (key, value, provenance) "
            "values ('profile.height_cm', '183', 'vibes')"
        )


def test_confidence_and_last_confirmed_are_non_null(conn: psycopg.Connection) -> None:
    """MEM-05: both fields are non-null on every active row."""
    facts.ratify(conn, "prefs.coach_tone", "direct", "stated", reason="seed")
    conn.commit()
    for fact in facts.active(conn):
        assert fact.confidence is not None
        assert 0 <= fact.confidence <= 1
        assert fact.last_confirmed_at is not None


def test_every_change_writes_an_audit_row(conn: psycopg.Connection) -> None:
    """MEM-06: asserted per row, not as an aggregate count.

    Every fact has a created event; every superseded fact has a superseded event.
    """
    for value in (70.0, 71.0):
        facts.ratify(conn, "goal.target_weight_kg", value, "stated", reason="change")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            """
            select f.id, f.status,
                   count(*) filter (where e.action = 'created')    as created,
                   count(*) filter (where e.action = 'superseded') as superseded
            from facts f left join fact_events e on e.fact_id = f.id
            group by f.id, f.status
            """
        )
        rows = cur.fetchall()

    assert rows
    for row in rows:
        assert row["created"] == 1, f"fact {row['id']} has no created event"
        if row["status"] == "superseded":
            assert row["superseded"] == 1, f"fact {row['id']} superseded without an event"


def test_audit_records_actor_and_reason(conn: psycopg.Connection) -> None:
    facts.ratify(
        conn,
        "availability.weekday_minutes",
        45,
        "observed",
        reason="three weeks of 45 minute weeknight rides",
        evidence={"sessions": [11, 12, 13]},
    )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("select action, reason, actor, evidence from fact_events order by id desc")
        event = cur.fetchone()
    assert event["actor"] == "consolidation"
    assert "45 minute" in event["reason"]
    assert event["evidence"] == {"sessions": [11, 12, 13]}


# --- MEM-14 value typing -------------------------------------------------


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("profile.height_cm", "183"),  # number key, string value
        ("prefs.coach_tone", 7),  # text key, number value
        ("equipment.bikes", "Tarmac"),  # list key, string value
        ("prefs.notification_times", ["07:00"]),  # object key, list value
        ("profile.height_cm", True),  # bool is not a number, despite subclassing int
    ],
)
def test_wrong_value_type_is_rejected(
    conn: psycopg.Connection, key: str, bad_value: object
) -> None:
    """MEM-14: a wrong typed write is rejected and logged, never coerced."""
    with pytest.raises(keys.WrongValueType):
        facts.ratify(conn, key, bad_value, "stated", reason="test")
    assert facts.active_for(conn, key) is None


def test_correct_value_types_are_accepted(conn: psycopg.Connection) -> None:
    facts.ratify(conn, "profile.height_cm", 183, "stated", reason="t")
    facts.ratify(conn, "prefs.coach_tone", "direct", "stated", reason="t")
    facts.ratify(conn, "equipment.bikes", ["Tarmac", "trainer"], "stated", reason="t")
    facts.ratify(conn, "prefs.notification_times", {"morning": "07:00"}, "stated", reason="t")
    conn.commit()
    assert len(facts.active(conn)) == 4


# --- Safety keys: SAFE-02, SAFE-03, SAFE-06 ------------------------------


def test_consolidation_cannot_write_a_safety_key(conn: psycopg.Connection) -> None:
    """SAFE-02: rejected, and written to fact_events with actor and reason."""
    with pytest.raises(facts.SafetyKeyViolation):
        facts.ratify(
            conn,
            "constraint.movement_restrictions",
            ["no overhead press"],
            "observed",
            reason="inferred from session notes",
        )

    assert facts.active_for(conn, "constraint.movement_restrictions") is None

    with conn.cursor() as cur:
        cur.execute(
            """
            select e.action, e.actor, e.reason, f.status
            from fact_events e join facts f on f.id = e.fact_id
            where f.key = 'constraint.movement_restrictions'
            """
        )
        event = cur.fetchone()
    assert event["action"] == "rejected"
    assert event["actor"] == "consolidation"
    assert "SAFE-02" in event["reason"]
    assert event["status"] == "rejected"


def test_athlete_can_state_a_constraint(conn: psycopg.Connection) -> None:
    """SAFE-06: the one path that can write a safety key.

    Without this the store would be deadlocked: consolidation may not write
    safety keys and no chat turn may write facts directly, so a constraint could
    never be recorded after the initial seed.
    """
    fact = facts.state_constraint(
        conn,
        "constraint.movement_restrictions",
        ["no loaded spinal flexion"],
        reason="athlete reported a flare up",
        confirmed=True,
    )
    conn.commit()

    assert fact.provenance == "stated"
    assert fact.value == ["no loaded spinal flexion"]

    with conn.cursor() as cur:
        cur.execute(
            "select actor from fact_events where fact_id = %s and action = 'created'",
            (fact.id,),
        )
        assert cur.fetchone()["actor"] == "athlete"


def test_stating_a_constraint_requires_confirmation(conn: psycopg.Connection) -> None:
    """SAFE-06: the athlete confirms the restatement before it lands."""
    with pytest.raises(facts.ConfirmationRequired):
        facts.state_constraint(
            conn,
            "constraint.medical_flags",
            ["asthma"],
            reason="mentioned in passing",
            confirmed=False,
        )
    assert facts.active_for(conn, "constraint.medical_flags") is None


def test_safety_path_refuses_ordinary_keys(conn: psycopg.Connection) -> None:
    """SAFE-06 writes safety keys and nothing else."""
    with pytest.raises(facts.NotASafetyKey):
        facts.state_constraint(
            conn,
            "goal.target_weight_kg",
            72.0,
            reason="sneaking past consolidation",
            confirmed=True,
        )


def test_safety_keys_cannot_be_given_a_half_life(conn: psycopg.Connection) -> None:
    """SAFE-03: the schema will not let a safety key decay."""
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        cur.execute(
            "insert into fact_keys (key, category, value_type, decay_days, safety) "
            "values ('constraint.test', 'constraint', 'list', 30, true)"
        )


# --- CONS-07 decay -------------------------------------------------------


def test_decay_curve_matches_the_requirement() -> None:
    """CONS-07: an availability fact at 90 days against a 30 day half life is 0.30."""
    assert facts.decayed_confidence(90, 30) == Decimal("0.30")
    assert facts.decayed_confidence(0, 30) == Decimal("1.00")
    assert facts.decayed_confidence(30, 30) == Decimal("0.60")


def test_decay_never_reaches_zero() -> None:
    """CONS-07: confidence asymptotes to the floor; facts never silently vanish."""
    assert facts.decayed_confidence(365, 30) == CONFIDENCE_FLOOR
    assert facts.decayed_confidence(100_000, 30) >= CONFIDENCE_FLOOR


def test_safety_keys_never_decay() -> None:
    """SAFE-03: confidence remains 1.00 after a simulated 365 days."""
    assert facts.decayed_confidence(365, None) == Decimal("1.00")


def test_apply_decay_reduces_unconfirmed_facts(conn: psycopg.Connection) -> None:
    """CONS-07 end to end: 90 days without confirmation leaves the fact active."""
    fact = facts.ratify(conn, "availability.weekday_minutes", 60, "stated", reason="seed")
    with conn.cursor() as cur:
        cur.execute(
            "update facts set last_confirmed_at = now() - %s where id = %s",
            (timedelta(days=90), fact.id),
        )
    conn.commit()

    assert facts.apply_decay(conn) == 1
    conn.commit()

    decayed = facts.active_for(conn, "availability.weekday_minutes")
    assert decayed is not None
    assert decayed.status == "active"
    assert decayed.confidence == Decimal("0.30")


def test_safety_facts_survive_a_year_of_decay(conn: psycopg.Connection) -> None:
    """SAFE-03: confidence remains 1.00 after a simulated 365 days."""
    fact = facts.state_constraint(
        conn, "constraint.injury_history", ["L4/L5"], reason="seed", confirmed=True
    )
    with conn.cursor() as cur:
        cur.execute(
            "update facts set last_confirmed_at = now() - %s where id = %s",
            (timedelta(days=365), fact.id),
        )
    conn.commit()

    facts.apply_decay(conn)
    conn.commit()

    survived = facts.active_for(conn, "constraint.injury_history")
    assert survived is not None
    assert survived.confidence == Decimal("1.00")


def test_confirm_resets_decay(conn: psycopg.Connection) -> None:
    fact = facts.ratify(conn, "availability.days", ["mon", "wed"], "observed", reason="seed")
    with conn.cursor() as cur:
        cur.execute(
            "update facts set last_confirmed_at = now() - %s, confidence = 0.30 where id = %s",
            (timedelta(days=90), fact.id),
        )
    conn.commit()

    facts.confirm(conn, "availability.days", reason="athlete confirmed in conversation")
    conn.commit()

    refreshed = facts.active_for(conn, "availability.days")
    assert refreshed is not None
    assert refreshed.confidence == Decimal("1.00")


def test_verification_candidate_returns_at_most_one(conn: psycopg.Connection) -> None:
    """CONS-08: one candidate regardless of how many qualify."""
    for key in ("availability.days", "availability.weekday_minutes", "prefs.coach_tone"):
        value: object = ["mon"] if key == "availability.days" else (45 if "minutes" in key else "x")
        fact = facts.ratify(conn, key, value, "stated", reason="seed")
        with conn.cursor() as cur:
            cur.execute("update facts set confidence = 0.25 where id = %s", (fact.id,))
    conn.commit()

    candidate = facts.verification_candidate(conn, Decimal("0.50"))
    assert candidate is not None
    assert candidate.confidence < Decimal("0.50")
