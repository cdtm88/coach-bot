"""The interruption budget.

CHAT-11: a conversation carries at most one interruption — one item the coach
raises that the athlete did not.

Six rules each grant "one per conversation" independently: the pending mention
(design section 8), the verification candidate (CONS-08), the outlier
confirmation (HLTH-11), the body mass gap mention (HLTH-15), safety
confirmation, and feed staleness. Left alone they compose into four
interruptions in a single conversation while each reports compliance. They
share one budget here, claimed in priority order.

Feed staleness is deliberately absent: CHAT-09 shapes what the coach reasons
from, and is never itself an interruption.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

# Highest priority first. A safety confirmation outranks everything; a
# verification candidate is the first thing to lose its slot.
PRIORITY = (
    "safety_confirmation",
    "outlier_confirmation",
    "body_mass_gap",
    "pending_mention",
    "verification",
)

# A conversation is a run of messages with no gap longer than this. Two
# exchanges either side of a working day are two conversations, so each may
# carry its own single interruption.
CONVERSATION_GAP = timedelta(hours=4)


@dataclass(frozen=True)
class Candidate:
    kind: str
    ref: str | None = None

    @property
    def rank(self) -> int:
        return PRIORITY.index(self.kind)


def conversation_started_at(conn: psycopg.Connection, now: datetime) -> datetime:
    """The start of the current conversation, walking back through the gap.

    Returns ``now`` when there is no recent traffic, which makes the first
    message of a fresh conversation the start of its own budget window.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select occurred_at from messages
            where occurred_at <= %s
            order by occurred_at desc
            limit 200
            """,
            (now,),
        )
        rows = [r["occurred_at"] for r in cur.fetchall()]

    start = now
    for occurred in rows:
        if start - occurred > CONVERSATION_GAP:
            break
        start = occurred
    return start


def budget_available(conn: psycopg.Connection, now: datetime) -> bool:
    """True when this conversation has not yet spent its one interruption."""
    since = conversation_started_at(conn, now)
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) as n from interruptions where claimed_at >= %s",
            (since,),
        )
        return cur.fetchone()["n"] == 0


def claim(conn: psycopg.Connection, candidates: list[Candidate], now: datetime) -> Candidate | None:
    """Claim the budget for the highest priority candidate, or nothing.

    Returns the claimed candidate, or None when the budget is already spent or
    nothing qualifies. The claim is recorded, so a second call within the same
    conversation returns None however many candidates are offered.
    """
    if not candidates or not budget_available(conn, now):
        return None

    winner = min(candidates, key=lambda c: c.rank)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into interruptions (kind, ref, claimed_at) values (%s, %s, %s)",
            (winner.kind, winner.ref, now),
        )
    return winner


def mark_delivered(conn: psycopg.Connection, kind: str) -> None:
    """Record that the claimed interruption actually reached the athlete.

    A claim that is never delivered still consumes the budget for the
    conversation. That is deliberate: the alternative is retrying within the
    same exchange, which is how one mention becomes three.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            update interruptions set delivered = true
            where id = (select id from interruptions where kind = %s
                        order by claimed_at desc limit 1)
            """,
            (kind,),
        )
