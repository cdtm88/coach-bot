"""System prompt assembly.

SAFE-01: safety constrained keys load verbatim at the top of every prompt.
Removing all other context still leaves the constraints present.

MEM-10 loads standing memory in full; MEM-11 caps the assembled context; MEM-13
sheds in a fixed order and never touches constraints. The shedding itself lives
in :mod:`coach.memory.context` — this module renders the pieces it sheds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from coach.agent import persona
from coach.memory import context as ctxmod
from coach.memory import facts as factmod
from coach.memory import keys as keymod
from coach.memory import state as statemod


def render_constraints(conn: psycopg.Connection) -> str:
    """SAFE-01: verbatim, never summarised, always first.

    Rendered from the safety keys alone so that no change elsewhere in memory
    can displace or dilute them.
    """
    vocabulary = keymod.load_all(conn)
    safety = [f for f in factmod.active(conn) if vocabulary[f.key].safety]
    if not safety:
        return "CONSTRAINTS\nNone recorded."

    lines = ["CONSTRAINTS", "These are absolute. Never program against them."]
    for fact in sorted(safety, key=lambda f: f.key):
        lines.append(f"- {fact.key}: {fact.value}")
    return "\n".join(lines)


def render_facts(conn: psycopg.Connection) -> str:
    """Every active non-safety fact, with confidence shown to the model."""
    vocabulary = keymod.load_all(conn)
    rows = [f for f in factmod.active(conn) if not vocabulary[f.key].safety]
    if not rows:
        return ""

    lines = ["WHAT YOU KNOW"]
    for fact in sorted(rows, key=lambda f: f.key):
        confidence = "" if fact.confidence >= 1 else f" (confidence {fact.confidence})"
        lines.append(f"- {fact.key}: {fact.value}{confidence} [{fact.provenance}]")
    return "\n".join(lines)


def render_continuity(conn: psycopg.Connection) -> str:
    """CHAT-05: the coach opens from the last open thread, not cold."""
    state = statemod.get(conn)
    if not state.rolling_summary and not state.open_threads:
        return ""
    lines = ["WHERE YOU LEFT OFF"]
    if state.rolling_summary:
        lines.append(state.rolling_summary)
    if state.open_threads:
        for thread in state.open_threads:
            lines.append(f"- open: {thread}")
    return "\n".join(lines)


def render_staleness(conn: psycopg.Connection, now: datetime) -> str:
    """CHAT-09: a stale feed is surfaced so the agent asks rather than infers.

    This is context, never an interruption — CHAT-11 is explicit that feed
    staleness shapes reasoning and does not consume the budget.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select name, last_success_at, stale_after_hours from feeds
            where last_success_at is null
               or last_success_at < %s - (stale_after_hours * interval '1 hour')
            order by name
            """,
            (now,),
        )
        stale = cur.fetchall()
    if not stale:
        return ""

    lines = [
        "STALE FEEDS",
        "Absence of data is not evidence of absence of activity. Do not assert a",
        "missed session or a stalled trend from a feed listed here — ask.",
    ]
    for feed in stale:
        seen = feed["last_success_at"].strftime("%Y-%m-%d") if feed["last_success_at"] else "never"
        lines.append(f"- {feed['name']}: last success {seen}")
    return "\n".join(lines)


def render_interruption(claimed: Any | None) -> str:
    """The one item the coach may raise this conversation, if any (CHAT-11)."""
    if claimed is None:
        return ""
    return (
        "ONE THING TO RAISE\n"
        f"kind: {claimed.kind}"
        + (f", about: {claimed.ref}" if claimed.ref else "")
        + "\nFold it into a message you were sending anyway, as an aside. Never a "
        "standalone message, never a question, and only once."
    )


def assemble(
    conn: psycopg.Connection,
    now: datetime,
    claimed_interruption: Any | None = None,
    episodic: str = "",
    block_detail: str = "",
    counter: Any = None,
) -> ctxmod.AssembledContext:
    """Build the turn's system prompt within the MEM-11 budget.

    Ordering is load-bearing: persona and constraints come first because SAFE-01
    requires the constraints at the top of every prompt, and because a stable
    prefix is what makes prompt caching work — the persona rarely changes, the
    facts change nightly.
    """
    parts = {
        "persona": persona.load(),
        "constraints": render_constraints(conn),
        "facts": render_facts(conn),
        "block_detail": block_detail,
        "continuity_note": render_continuity(conn),
        "staleness": render_staleness(conn, now),
        "interruption": render_interruption(claimed_interruption),
        "episodic_recall": episodic,
    }
    parts = {name: body for name, body in parts.items() if body}

    kwargs: dict[str, Any] = {}
    if counter is not None:
        kwargs["counter"] = counter
    return ctxmod.assemble(parts, **kwargs)


def as_system_blocks(assembled: ctxmod.AssembledContext) -> list[dict[str, Any]]:
    """Render the assembled context as Anthropic system blocks.

    The cache breakpoint sits on the persona and constraints, which change
    rarely. Facts and continuity fall after it and change nightly, so they are
    re-read each day rather than invalidating the stable prefix.
    """
    blocks: list[dict[str, Any]] = []
    stable = [c for c in assembled.components if c.name in ("persona", "constraints")]
    volatile = [c for c in assembled.components if c.name not in ("persona", "constraints")]

    if stable:
        blocks.append(
            {
                "type": "text",
                "text": "\n\n".join(c.body for c in stable),
                "cache_control": {"type": "ephemeral"},
            }
        )
    if volatile:
        blocks.append({"type": "text", "text": "\n\n".join(c.body for c in volatile)})
    return blocks
