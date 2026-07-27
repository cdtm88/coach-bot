"""Working memory and the pending queue: MEM-09, CONS-06."""

from __future__ import annotations

import psycopg
import pytest

from coach.memory import state


def test_conversation_state_is_a_single_row(conn: psycopg.Connection) -> None:
    """MEM-09: one row, guaranteed by the primary key rather than by convention."""
    with pytest.raises(psycopg.errors.UniqueViolation), conn.cursor() as cur:
        cur.execute("insert into conversation_state (id) values (true)")


def test_consolidation_clears_uncommitted_but_keeps_continuity(
    conn: psycopg.Connection,
) -> None:
    """MEM-09: today_uncommitted empties and the continuity fields are populated.

    The design once said the row was "wiped", which would have taken open_threads
    with it and left CHAT-05 opening cold every morning.
    """
    state.update(
        conn,
        rolling_summary="Talked about the knee and Thursday's session.",
        open_threads=[{"topic": "knee", "opened": "2026-07-20"}],
        today_uncommitted=[{"key": "availability.days", "value": ["mon", "wed"]}],
        last_topic="knee",
    )
    conn.commit()

    after = state.clear_for_consolidation(
        conn,
        rolling_summary="Knee settled; Thursday moved to Friday.",
        open_threads=[{"topic": "knee", "opened": "2026-07-20"}],
        last_topic="knee",
    )
    conn.commit()

    assert after.today_uncommitted is None
    assert after.open_threads == [{"topic": "knee", "opened": "2026-07-20"}]
    assert after.last_topic == "knee"
    assert "Thursday moved" in after.rolling_summary


def test_in_turn_writes_go_to_the_queue(conn: psycopg.Connection) -> None:
    """CONS-06: no path from an ordinary chat turn to a direct facts insert."""
    state.queue_write(conn, {"key": "goal.target_weight_kg", "value": 72.0}, origin="in_turn")
    conn.commit()

    assert len(state.pending(conn)) == 1
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from facts")
        assert cur.fetchone()["n"] == 0


def test_same_day_correction_supersedes_the_pending_row(conn: psycopg.Connection) -> None:
    """Design section 6: a correction supersedes rather than queuing twice."""
    state.queue_write(conn, {"key": "goal.target_weight_kg", "value": 72.0})
    state.queue_write(conn, {"key": "goal.target_weight_kg", "value": 71.0})
    conn.commit()

    outstanding = state.pending(conn)
    assert len(outstanding) == 1
    assert outstanding[0]["proposal"]["value"] == 71.0


def test_corrections_to_other_keys_are_independent(conn: psycopg.Connection) -> None:
    state.queue_write(conn, {"key": "goal.target_weight_kg", "value": 72.0})
    state.queue_write(conn, {"key": "prefs.coach_tone", "value": "direct"})
    conn.commit()
    assert len(state.pending(conn)) == 2
