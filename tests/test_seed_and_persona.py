"""The seeded memory and the persona it is paired with.

docs/setup.md step 10 and CHAT-02. Closes open item 9.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from coach import seed
from coach.agent import naturalness, persona, prompt
from coach.memory import facts, keys

REPO = Path(__file__).resolve().parents[1]
NOW = __import__("datetime").datetime(2026, 7, 27, 9, 0, tzinfo=__import__("datetime").UTC)


# --- CHAT-02: the persona is real, not a scaffold --------------------------


def test_the_persona_is_seeded() -> None:
    """Open item 9 is closed: the TO BE SEEDED marker is gone."""
    assert persona.is_seeded()


def test_the_source_transcript_is_committed() -> None:
    """CHAT-02 names this path; the persona is unreviewable without it."""
    assert persona.SEED_PATH.exists()
    assert persona.SEED_PATH.stat().st_size > 10_000


def test_the_persona_carries_the_rules_the_suite_asserts() -> None:
    """Rewriting for voice must not drop the behaviour the requirements test."""
    body = persona.load().lower()
    for rule in (
        "at most one question per message",  # CHAT-04
        "never say you have saved",  # CHAT-03
        "open from where you left off",  # CHAT-05
        "no headers, no bullet lists",  # CHAT-10
        "constraints are absolute",  # SAFE-01
        "you do not diagnose",  # SAFE-05
        "never increase it on your own",  # the governing asymmetry
        "absence of data is not evidence",  # the absence trap
        "body mass is a trend",  # HLTH
    ):
        assert rule in body, f"persona lost: {rule}"


def test_the_persona_obeys_its_own_em_dash_rule() -> None:
    """The coaching brief forbids em dashes. The prompt should model that."""
    body = persona.load()
    assert "never use em dashes" in body.lower()
    assert "—" not in body


def test_the_persona_reads_as_prose_not_as_a_report() -> None:
    """CHAT-10 in the prompt itself: no tables, no numbered procedure."""
    assert "|" not in persona.load()


# --- the seed file ----------------------------------------------------------


def test_seed_file_is_valid_and_typed(conn: psycopg.Connection) -> None:
    """Every seeded key exists in the vocabulary and matches its declared type."""
    data = seed.load()
    vocabulary = keys.load_all(conn)

    for entry in data["constraints"]:
        fact_key = vocabulary.get(entry["key"])
        assert fact_key is not None, f"unknown key {entry['key']}"
        assert fact_key.safety, f"{entry['key']} is in constraints but is not a safety key"
        keys.validate(fact_key, entry["value"])

    for entry in data["facts"]:
        fact_key = vocabulary.get(entry["key"])
        assert fact_key is not None, f"unknown key {entry['key']}"
        assert not fact_key.safety, f"{entry['key']} is a safety key and must be in constraints"
        keys.validate(fact_key, entry["value"])
        assert entry["provenance"] in facts.PROVENANCE


def test_every_seeded_fact_carries_a_reason() -> None:
    """A seeded fact with no provenance in prose is unauditable later."""
    data = seed.load()
    for entry in data["constraints"] + data["facts"]:
        assert entry.get("reason"), f"{entry['key']} has no reason"


# --- applying the seed ------------------------------------------------------


def test_seeding_populates_the_store(conn: psycopg.Connection) -> None:
    counts = seed.apply(conn, seed.load())
    assert counts["constraints"] >= 2
    assert counts["facts"] >= 10

    active = {f.key: f for f in facts.active(conn)}
    assert active["physiology.ftp_watts"].value == 115
    assert active["physiology.ftp_watts"].provenance == "computed"
    assert active["goal.target_weight_kg"].value == 100.0


def test_constraints_are_written_by_the_athlete_path(conn: psycopg.Connection) -> None:
    """SAFE-06: a seeded constraint carries actor athlete and provenance stated."""
    seed.apply(conn, seed.load())

    fact = facts.active_for(conn, "constraint.injury_history")
    assert fact is not None
    assert fact.provenance == "stated"

    with conn.cursor() as cur:
        cur.execute(
            "select actor from fact_events where fact_id = %s and action = 'created'",
            (fact.id,),
        )
        assert cur.fetchone()["actor"] == "athlete"


def test_ordinary_facts_are_attributed_to_the_seed(conn: psycopg.Connection) -> None:
    """actor `rule` distinguishes a seed from a nightly pass in the audit trail."""
    seed.apply(conn, seed.load())
    fact = facts.active_for(conn, "physiology.ftp_watts")
    assert fact is not None
    with conn.cursor() as cur:
        cur.execute(
            "select actor, reason from fact_events where fact_id = %s and action = 'created'",
            (fact.id,),
        )
        event = cur.fetchone()
    assert event["actor"] == "rule"
    assert "ramp test" in event["reason"]


def test_seeding_is_idempotent(conn: psycopg.Connection) -> None:
    """Re-running must not manufacture a supersession chain out of nothing."""
    seed.apply(conn, seed.load())
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from facts")
        before = cur.fetchone()["n"]

    second = seed.apply(conn, seed.load())
    assert second["constraints"] == 0
    assert second["facts"] == 0
    assert second["notes"] == 0
    assert second["unchanged"] > 0

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from facts")
        assert cur.fetchone()["n"] == before


def test_a_malformed_seed_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"facts": []}')
    with pytest.raises(seed.SeedError, match="constraints"):
        seed.load(bad)

    worse = tmp_path / "worse.json"
    worse.write_text("not json")
    with pytest.raises(seed.SeedError, match="valid JSON"):
        seed.load(worse)


# --- the seeded store drives a real prompt ---------------------------------


def test_a_seeded_store_produces_a_prompt_with_constraints_first(
    conn: psycopg.Connection,
) -> None:
    """End to end: SAFE-01 against real seeded data rather than a fixture."""
    seed.apply(conn, seed.load())

    assembled = prompt.assemble(conn, NOW)
    rendered = assembled.render()

    assert "L5-S1" in rendered
    assert "no barbell deadlifts" in rendered
    assert "physiology.ftp_watts" in rendered

    names = assembled.names()
    assert names.index("constraints") < names.index("facts")
    assert names.index("persona") < names.index("constraints")


def test_the_seeded_prompt_fits_the_context_budget(conn: psycopg.Connection) -> None:
    """MEM-11: persona plus real seeded memory leaves room for a conversation."""
    seed.apply(conn, seed.load())
    assembled = prompt.assemble(conn, NOW)
    assert assembled.tokens < 4000
    assert assembled.shed == []


def test_the_persona_would_pass_its_own_naturalness_rules() -> None:
    """A sanity check on the prompt, not on model output.

    The persona describes the rules rather than obeying all of them, so this only
    asserts the two that a prompt can meaningfully satisfy: it names no condition,
    and it does not narrate a memory operation.
    """
    body = persona.load()
    assert not naturalness.diagnoses(body)
    assert not naturalness.narrates_memory(body)
