"""Episodic notes: day summaries, observations, reviews, archived blocks.

MEM-07: stored with a generated tsvector and a GIN index. MEM-10: retrieved on
demand only, never preloaded, and reached through a tool rather than a vector
search.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

KINDS = ("day_summary", "observation", "review", "block_archive")


@dataclass(frozen=True)
class Note:
    id: int
    kind: str
    body: str
    occurred_on: date
    refs: dict[str, Any] | None
    created_at: datetime


_SELECT = "select id, kind, body, occurred_on, refs, created_at from notes "


def add(
    conn: psycopg.Connection,
    kind: str,
    body: str,
    occurred_on: date,
    refs: dict[str, Any] | None = None,
) -> Note:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into notes (kind, body, occurred_on, refs)
            values (%s, %s, %s, %s)
            returning id, kind, body, occurred_on, refs, created_at
            """,
            (kind, body, occurred_on, Jsonb(refs) if refs else None),
        )
        return Note(**cur.fetchone())


def upsert_day_summary(conn: psycopg.Connection, body: str, occurred_on: date) -> Note:
    """Write the day summary for a date, replacing any existing one.

    CONS-09 wants one per qualifying date and CONS-10 wants consolidation to be
    idempotent, so re-running a night rewrites rather than duplicating. The
    partial unique index makes the conflict target exact.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into notes (kind, body, occurred_on)
            values ('day_summary', %s, %s)
            on conflict (occurred_on) where kind = 'day_summary'
            do update set body = excluded.body
            returning id, kind, body, occurred_on, refs, created_at
            """,
            (body, occurred_on),
        )
        return Note(**cur.fetchone())


def search(conn: psycopg.Connection, query: str, limit: int = 10) -> list[Note]:
    """Full text search over the episodic archive (MEM-07)."""
    with conn.cursor() as cur:
        cur.execute(
            _SELECT
            + """
            where tsv @@ plainto_tsquery('english', %s)
            order by ts_rank(tsv, plainto_tsquery('english', %s)) desc, occurred_on desc
            limit %s
            """,
            (query, query, limit),
        )
        return [Note(**row) for row in cur.fetchall()]


def on_date(conn: psycopg.Connection, occurred_on: date) -> list[Note]:
    with conn.cursor() as cur:
        cur.execute(_SELECT + "where occurred_on = %s order by created_at", (occurred_on,))
        return [Note(**row) for row in cur.fetchall()]
