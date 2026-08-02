"""The Sunday review: REV-01 to REV-05.

The review is the most quotable thing the system produces — a weekly message the
athlete will treat as authoritative — so these tests are mostly about where its
numbers come from. Every figure is read from a rollup or a fit that something
else computed and something else already tests. A review that did its own
arithmetic would be a second implementation of adherence, and the two would
diverge quietly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg.types.json import Jsonb

import conftest
from coach.blocks import document as blockmod
from coach.health import breaks as breakmod
from coach.review import weekly

SUNDAY = date(2026, 8, 2)


def prescribe(conn: psycopg.Connection, on: date, status: str) -> int:
    block_id = conftest.ensure_block(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status) "
            "values (%s, %s, 'ride', %s, %s) returning id",
            (
                block_id,
                datetime.combine(on, datetime.min.time()).replace(hour=18, tzinfo=UTC),
                Jsonb({"duration_s": 3600, "intensity_factor": 0.68}),
                status,
            ),
        )
        return int(cur.fetchone()["id"])


def rollup(conn: psycopg.Connection, on: date, **fields: object) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join(["%s"] * len(fields))
    updates = ", ".join(f"{c} = excluded.{c}" for c in fields)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"insert into rollups (as_of, {columns}) values (%s, {placeholders}) "
            f"on conflict (as_of) do update set {updates}",
            (on, *fields.values()),
        )


# --- REV-02: all five sections ----------------------------------------------


def test_the_review_has_every_section(conn: psycopg.Connection) -> None:
    """REV-02's acceptance: all five sections present, with figures from rollups.

    Six titles, because NUT-06 names the review explicitly and asks for intake
    alongside training and weight.
    """
    review = weekly.build(conn, SUNDAY)

    assert [s.title for s in review.sections] == list(weekly.SECTIONS)
    rendered = review.render()
    for title in weekly.SECTIONS:
        assert f"{title}:" in rendered


def test_an_empty_week_produces_a_review_rather_than_an_error(
    conn: psycopg.Connection,
) -> None:
    """The first Sunday after deployment has nothing in any table."""
    review = weekly.build(conn, SUNDAY)

    assert "nothing was prescribed" in review.render()
    assert "no training load recorded" in review.render()


def test_adherence_counts_completions_against_what_was_offered(
    conn: psycopg.Connection,
) -> None:
    prescribe(conn, SUNDAY - timedelta(days=5), "completed")
    prescribe(conn, SUNDAY - timedelta(days=3), "completed")
    prescribe(conn, SUNDAY - timedelta(days=1), "missed")

    body = weekly.adherence_section(conn, SUNDAY).body

    assert "2 of 3" in body


def test_a_suspended_session_is_reported_and_not_counted(conn: psycopg.Connection) -> None:
    """BREAK-02 seen from the review: the athlete should not read a miss."""
    prescribe(conn, SUNDAY - timedelta(days=5), "completed")
    prescribe(conn, SUNDAY - timedelta(days=2), "suspended")

    body = weekly.adherence_section(conn, SUNDAY).body

    assert "1 of 1" in body
    assert "1 suspended by a break and not counted" in body


def test_a_whole_week_inside_a_break_says_so(conn: psycopg.Connection) -> None:
    """Zero of zero is a number that reads as failure. This does not."""
    prescribe(conn, SUNDAY - timedelta(days=3), "suspended")
    prescribe(conn, SUNDAY - timedelta(days=2), "suspended")

    body = weekly.adherence_section(conn, SUNDAY).body

    assert "inside a break" in body
    assert "not a miss" in body


def test_load_is_read_from_the_rollup_and_compared_with_last_week(
    conn: psycopg.Connection,
) -> None:
    rollup(conn, SUNDAY - timedelta(days=7), load_7d=Decimal(400))
    rollup(conn, SUNDAY, load_7d=Decimal(440), load_28d=Decimal(1600), gym_session_count=2)

    body = weekly.load_section(conn, SUNDAY).body

    assert "440 over the week" in body
    assert "up 10%" in body
    assert "1600 over 28 days" in body
    assert "2 gym session(s)" in body


def test_a_rest_sunday_still_reports_the_week(conn: psycopg.Connection) -> None:
    """Rollups exist for days with sessions, so the Sunday itself often has none."""
    rollup(conn, SUNDAY - timedelta(days=2), load_7d=Decimal(320))

    assert "320 over the week" in weekly.load_section(conn, SUNDAY).body


def test_the_goals_section_quotes_the_block(conn: psycopg.Connection) -> None:
    block_id = conftest.ensure_block(conn)
    blockmod.rewrite(conn, block_id, "goals", "Hold 250 W for an hour by March.", reason="test")
    blockmod.activate(conn, block_id)

    assert "Hold 250 W" in weekly.goals_section(conn, SUNDAY).body


# --- REV-03: one question ---------------------------------------------------


def test_the_review_asks_exactly_one_question(conn: psycopg.Connection) -> None:
    """REV-03's acceptance: one question asked, not a questionnaire.

    CHAT-11 exempts the review from the interruption budget, which is not
    permission to ask several things — the exemption exists because the review's
    question is not an interruption.
    """
    prescribe(conn, SUNDAY - timedelta(days=2), "missed")

    assert weekly.build(conn, SUNDAY).render().count("?") == 1


def test_the_question_is_about_the_coming_week(conn: psycopg.Connection) -> None:
    assert "coming week" in weekly.build(conn, SUNDAY).question


# --- REV-04: decisions ------------------------------------------------------


def test_a_deferred_adjustment_is_surfaced_for_a_decision(
    conn: psycopg.Connection,
) -> None:
    """REV-04's acceptance: deferred items from ADJ-03 and ADJ-05 appear."""
    prescription_id = prescribe(conn, SUNDAY + timedelta(days=2), "planned")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into deferred_adjustments (prescription_id, trigger, deferred_by, "
            "proposal, evidence, for_week, status) "
            "values (%s, 'over_prescription', 'weekly_budget', %s, %s, %s, 'pending')",
            (
                prescription_id,
                Jsonb({"summary": "shorten Thursday to 45 minutes"}),
                Jsonb({}),
                SUNDAY,
            ),
        )

    review = weekly.build(conn, SUNDAY)

    assert any("shorten Thursday" in d for d in review.decisions)
    assert "Waiting on you:" in review.render()


def test_a_re_entry_proposal_is_surfaced_at_the_next_review(
    conn: psycopg.Connection,
) -> None:
    """BREAK-03's acceptance lands here: "at the next review"."""
    start = SUNDAY - timedelta(days=20)
    rollup(conn, start - timedelta(days=1), load_7d=Decimal(400))
    brk_id = breakmod.create(conn, "holiday", start, start + timedelta(days=13))
    breakmod.end(conn, brk_id, start + timedelta(days=13))

    review = weekly.build(conn, SUNDAY)

    assert any("Proposed re-entry" in d for d in review.decisions)


def test_the_re_entry_is_not_repeated_next_week(conn: psycopg.Connection) -> None:
    start = SUNDAY - timedelta(days=20)
    rollup(conn, start - timedelta(days=1), load_7d=Decimal(400))
    brk_id = breakmod.create(conn, "holiday", start, start + timedelta(days=13))
    breakmod.end(conn, brk_id, start + timedelta(days=13))

    weekly.run(conn, SUNDAY)
    later = weekly.build(conn, SUNDAY + timedelta(days=7))

    assert not any("Proposed re-entry" in d for d in later.decisions)


# --- REV-05: both artefacts -------------------------------------------------


def test_the_review_is_stored_as_a_note_and_appended_to_the_block(
    conn: psycopg.Connection,
) -> None:
    """REV-05's acceptance: both artefacts written."""
    block_id = conftest.ensure_block(conn)
    blockmod.activate(conn, block_id)

    review = weekly.run(conn, SUNDAY)

    with conn.cursor() as cur:
        cur.execute("select body, occurred_on from notes where kind = 'review'")
        note = cur.fetchone()
    assert note is not None
    assert note["occurred_on"] == SUNDAY

    block = blockmod.get(conn, block_id)
    assert "Week ending 2026-08-02" in blockmod.section_of(block.content, "review")
    assert review.week_ending == SUNDAY


def test_two_reviews_append_rather_than_overwrite(conn: psycopg.Connection) -> None:
    """A block's review section is a record of the block, not of last Sunday."""
    block_id = conftest.ensure_block(conn)
    blockmod.activate(conn, block_id)

    weekly.run(conn, SUNDAY)
    weekly.run(conn, SUNDAY + timedelta(days=7))

    section = blockmod.section_of(blockmod.get(conn, block_id).content, "review")
    assert "Week ending 2026-08-02" in section
    assert "Week ending 2026-08-09" in section


def test_a_review_without_an_active_block_is_still_recorded(
    conn: psycopg.Connection,
) -> None:
    """The note is the durable record and cannot fail for a reason outside itself."""
    weekly.run(conn, SUNDAY)

    with conn.cursor() as cur:
        cur.execute("select count(*)::int as n from notes where kind = 'review'")
        assert cur.fetchone()["n"] == 1


def test_the_review_posts_into_the_chat(conn: psycopg.Connection) -> None:
    """REV-01: it appears in the chat, not only in the database."""
    sent: list[str] = []

    weekly.run(conn, SUNDAY, send=sent.append)

    assert len(sent) == 1
    assert "Week ending" in sent[0]


def test_a_review_can_be_built_without_a_transport(conn: psycopg.Connection) -> None:
    """So a delivery failure does not mean a second note on the retry."""
    weekly.run(conn, SUNDAY)

    with conn.cursor() as cur:
        cur.execute("select count(*)::int as n from notes where kind = 'review'")
        assert cur.fetchone()["n"] == 1


@pytest.fixture(autouse=True)
def _clean(conn: psycopg.Connection) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("delete from breaks")
