"""Episodic notes: MEM-07, CONS-09, CONS-10."""

from __future__ import annotations

import time
from datetime import date

import psycopg
import pytest

from coach.memory import notes


def test_full_text_search_finds_a_phrase(conn: psycopg.Connection) -> None:
    """MEM-07: a search for a phrase present in a note returns that note."""
    notes.add(
        conn,
        "observation",
        "Threshold work on the trainer felt harder than the numbers suggested.",
        date(2026, 7, 20),
    )
    notes.add(conn, "observation", "Easy spin, legs fine.", date(2026, 7, 21))
    conn.commit()

    hits = notes.search(conn, "threshold trainer")
    assert len(hits) == 1
    assert "Threshold work" in hits[0].body


def test_search_is_fast(conn: psycopg.Connection) -> None:
    """MEM-07: under 200ms. The GIN index is the point of the requirement."""
    for day in range(1, 29):
        notes.add(
            conn, "day_summary", f"Day {day}: endurance ride, felt steady.", date(2026, 6, day)
        )
    conn.commit()

    started = time.perf_counter()
    hits = notes.search(conn, "endurance steady")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert hits
    assert elapsed_ms < 200, f"search took {elapsed_ms:.1f}ms"


def test_one_day_summary_per_date(conn: psycopg.Connection) -> None:
    """CONS-09: the schema enforces one, rather than trusting the job."""
    notes.add(conn, "day_summary", "first", date(2026, 7, 20))
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        notes.add(conn, "day_summary", "second", date(2026, 7, 20))


def test_other_note_kinds_are_not_limited_per_date(conn: psycopg.Connection) -> None:
    notes.add(conn, "observation", "one", date(2026, 7, 20))
    notes.add(conn, "observation", "two", date(2026, 7, 20))
    conn.commit()
    assert len(notes.on_date(conn, date(2026, 7, 20))) == 2


def test_reconsolidating_a_day_creates_nothing_new(conn: psycopg.Connection) -> None:
    """CONS-10: a second run for a date results in zero new rows."""
    notes.upsert_day_summary(conn, "ride and a gym session", date(2026, 7, 20))
    conn.commit()
    before = len(notes.on_date(conn, date(2026, 7, 20)))

    notes.upsert_day_summary(conn, "ride and a gym session, plus a walk", date(2026, 7, 20))
    conn.commit()

    after = notes.on_date(conn, date(2026, 7, 20))
    assert len(after) == before == 1
    assert "plus a walk" in after[0].body
