"""System prompt assembly.

SAFE-01: safety constrained keys load verbatim at the top of every prompt.
Removing all other context still leaves the constraints present.

MEM-10 loads standing memory in full; MEM-11 caps the assembled context; MEM-13
sheds in a fixed order and never touches constraints. The shedding itself lives
in :mod:`coach.memory.context` — this module renders the pieces it sheds.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from coach import clock
from coach.agent import persona
from coach.health import bodymass, recovery
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

    `body_mass` is excluded, and its exclusion is a requirement rather than a
    tidy-up. HLTH-15 says the weigh in mention is the only one in the system and
    that "the generic feed staleness mechanism never emits a body mass mention of
    its own". A block that lists the feed and invites the coach to ask about it is
    exactly such a mention. The feed row is still maintained for OBS-05; what
    changes is who is allowed to speak about it, which is
    :func:`render_body_mass` and nothing else.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select name, last_success_at, stale_after_hours from feeds
            where name <> 'body_mass'
              and (last_success_at is null
                   or last_success_at < %s - (stale_after_hours * interval '1 hour'))
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


def render_body_mass(conn: psycopg.Connection, now: datetime, tz: ZoneInfo) -> str:
    """The weight trend, rendered as permissions rather than as numbers.

    This is the load bearing half of P04. The HLTH requirements are almost all
    statements about what the coach may say — a direction needs three readings, a
    rate needs six across three weeks, a plateau needs four weeks with weekly
    coverage — and a model handed a list of readings will honour none of them,
    because the arithmetic is trivial and the restraint is not.

    So the readings never enter the context. What enters is a fitted slope, a
    range computed in SQL, and an explicit statement of which claims the current
    evidence supports. HLTH-09 then costs nothing to obey: there is no pair of
    readings in the prompt to compare.
    """
    return bodymass.context(conn, clock.local_day(now, tz))


def render_recovery(conn: psycopg.Connection, now: datetime, tz: ZoneInfo) -> str:
    """RECOV-04's local deviation, with the platform's score labelled as theirs.

    Empty when the feed has not carried enough history to standardise anything,
    which is the honest state rather than a zero.
    """
    return recovery.context(conn, clock.local_day(now, tz))


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


def configured_tz() -> ZoneInfo:
    """TZ-01: the athlete's zone, never the server's and never the data's."""
    return ZoneInfo(os.environ.get("COACH_TZ", "UTC"))


def assemble(
    conn: psycopg.Connection,
    now: datetime,
    claimed_interruption: Any | None = None,
    episodic: str = "",
    block_detail: str = "",
    counter: Any = None,
    tz: ZoneInfo | None = None,
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
        "body_mass": render_body_mass(conn, now, tz or configured_tz()),
        "recovery": render_recovery(conn, now, tz or configured_tz()),
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
