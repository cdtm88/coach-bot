"""Context assembly: MEM-10, MEM-11, MEM-13."""

from __future__ import annotations

import psycopg
import pytest

from coach.config import CONTEXT_TOKEN_BUDGET
from coach.memory import context, facts


def test_standing_memory_loads_in_full(conn: psycopg.Connection) -> None:
    """MEM-10: assembled context contains every active fact."""
    facts.ratify(conn, "profile.height_cm", 183, "stated", reason="seed")
    facts.ratify(conn, "goal.target_weight_kg", 72.0, "stated", reason="seed")
    facts.ratify(conn, "availability.weekday_minutes", 45, "observed", reason="seed")
    conn.commit()

    active = facts.active(conn)
    body = "\n".join(f"{f.key} = {f.value}" for f in active)
    assembled = context.assemble({"facts": body})

    for fact in active:
        assert fact.key in assembled.render()


def test_steady_state_fits_the_budget() -> None:
    """MEM-11: the design's own steady state sits under 4,000 tokens.

    Component sizes are the estimates in docs/memory-design.md section 3.
    """
    parts = {
        "constraints": "c" * 300 * 4,
        "facts": "f" * 600 * 4,
        "rollups": "r" * 400 * 4,
        "block_detail": "b" * 700 * 4,
        "continuity_note": "n" * 200 * 4,
    }
    assembled = context.assemble(parts)
    assert assembled.tokens <= CONTEXT_TOKEN_BUDGET
    assert assembled.shed == []


def test_episodic_recall_sheds_first() -> None:
    """MEM-13: the fixed shedding order, episodic recall before anything else."""
    parts = {
        "constraints": "c" * 300 * 4,
        "facts": "f" * 600 * 4,
        "block_detail": "b" * 700 * 4,
        "continuity_note": "n" * 200 * 4,
        "episodic_recall": "e" * 3000 * 4,
    }
    assembled = context.assemble(parts)
    assert assembled.shed == ["episodic_recall"]
    assert assembled.tokens <= CONTEXT_TOKEN_BUDGET
    assert "constraints" in assembled.names()


def test_shedding_follows_the_stated_order() -> None:
    """MEM-13: recall, then block detail, then the continuity note."""
    parts = {
        "constraints": "c" * 300 * 4,
        "facts": "f" * 900 * 4,
        "episodic_recall": "e" * 1500 * 4,
        "block_detail": "b" * 1500 * 4,
        "continuity_note": "n" * 1500 * 4,
    }
    assembled = context.assemble(parts)
    assert assembled.shed == ["episodic_recall", "block_detail"]
    assert "continuity_note" in assembled.names()


def test_constraints_are_never_shed() -> None:
    """MEM-13 and SAFE-01: constraints survive, or the assembly fails loudly.

    Silently dropping a movement restriction to fit a budget is the one failure
    mode this requirement exists to prevent, so overflow raises rather than
    truncates.
    """
    parts = {
        "constraints": "c" * 3000 * 4,
        "facts": "f" * 3000 * 4,
        "episodic_recall": "e" * 500 * 4,
    }
    with pytest.raises(context.BudgetExceeded) as excinfo:
        context.assemble(parts)

    assert "MEM-13" in str(excinfo.value)
    assert "episodic_recall" in str(excinfo.value)


def test_tool_results_count_against_the_budget() -> None:
    """MEM-11: the budget counts preloaded context plus same turn tool results.

    Under the other reading — preload only — the cap could never be breached and
    the requirement would be unfalsifiable.
    """
    preload = {"constraints": "c" * 300 * 4, "facts": "f" * 600 * 4}
    assert context.assemble(preload).tokens < CONTEXT_TOKEN_BUDGET

    with_tool_result = {**preload, "episodic_recall": "e" * 5000 * 4}
    assembled = context.assemble(with_tool_result)
    assert assembled.shed == ["episodic_recall"]
