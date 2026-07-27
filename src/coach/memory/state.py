"""Working memory and the pending write queue.

MEM-09: a single row of conversation state, rewritten per turn. Consolidation
clears ``today_uncommitted`` and regenerates the continuity fields from the day
it has just consolidated. It never empties the row, because CHAT-05 opens the
next conversation from ``open_threads`` rather than cold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

ORIGINS = ("in_turn", "consolidation", "feed")


@dataclass(frozen=True)
class ConversationState:
    rolling_summary: str | None
    open_threads: list[Any] | None
    today_uncommitted: list[Any] | None
    last_topic: str | None
    updated_at: datetime


def get(conn: psycopg.Connection) -> ConversationState:
    with conn.cursor() as cur:
        cur.execute(
            """
            select rolling_summary, open_threads, today_uncommitted, last_topic, updated_at
            from conversation_state where id = true
            """
        )
        return ConversationState(**cur.fetchone())


def update(
    conn: psycopg.Connection,
    *,
    rolling_summary: str | None = None,
    open_threads: list[Any] | None = None,
    today_uncommitted: list[Any] | None = None,
    last_topic: str | None = None,
) -> ConversationState:
    """Rewrite the turn's state. Only the fields passed are touched."""
    sets: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("rolling_summary", rolling_summary),
        ("open_threads", Jsonb(open_threads) if open_threads is not None else None),
        ("today_uncommitted", Jsonb(today_uncommitted) if today_uncommitted is not None else None),
        ("last_topic", last_topic),
    ):
        if value is not None:
            sets.append(f"{column} = %s")
            params.append(value)

    if sets:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                f"update conversation_state set {', '.join(sets)}, updated_at = now() "  # noqa: S608
                "where id = true",
                params,
            )
    return get(conn)


def clear_for_consolidation(
    conn: psycopg.Connection,
    *,
    rolling_summary: str | None,
    open_threads: list[Any] | None,
    last_topic: str | None,
) -> ConversationState:
    """The nightly clear (MEM-09).

    ``today_uncommitted`` empties; the continuity fields are replaced with what
    consolidation made of the day. The row itself survives. An earlier revision
    of the design said "wiped", which would have taken the continuity note with
    it and broken CHAT-05 every morning.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            update conversation_state
            set today_uncommitted = null,
                rolling_summary = %s,
                open_threads = %s,
                last_topic = %s,
                updated_at = now()
            where id = true
            """,
            (
                rolling_summary,
                Jsonb(open_threads) if open_threads is not None else None,
                last_topic,
            ),
        )
    return get(conn)


def queue_write(
    conn: psycopg.Connection,
    proposal: dict[str, Any],
    origin: str = "in_turn",
) -> int:
    """Queue a proposed fact change for the night (CONS-06).

    A same day correction to the same key supersedes the earlier pending row
    rather than queuing twice, per design section 6.
    """
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}, got {origin!r}")
    key = proposal.get("key")
    with conn.transaction(), conn.cursor() as cur:
        if key is not None:
            cur.execute(
                """
                update pending_writes set status = 'expired'
                where status = 'pending' and proposal->>'key' = %s
                """,
                (key,),
            )
        cur.execute(
            "insert into pending_writes (proposal, origin) values (%s, %s) returning id",
            (Jsonb(proposal), origin),
        )
        return cur.fetchone()["id"]


def pending(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "select id, proposal, origin, created_at from pending_writes "
            "where status = 'pending' order by created_at"
        )
        return cur.fetchall()
