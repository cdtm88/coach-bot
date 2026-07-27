"""Telegram long polling.

CHAT-01: the bot responds only to an allowlisted chat id; anything else is
ignored and logged. SEC-03 is the same rule stated as a security requirement.
CHAT-08: messages received while the bot was offline are processed once on
restart, producing one catch-up response rather than one per queued message.

The transport is injectable so the allowlist and catch-up logic — the parts that
matter — are testable without a network.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

log = logging.getLogger(__name__)

# CHAT-08: a backlog older than this is answered as a catch-up rather than
# replied to message by message.
BACKLOG_THRESHOLD = 2


@dataclass(frozen=True)
class Inbound:
    chat_id: int
    telegram_message_id: int
    body: str
    occurred_at: datetime
    modality: str = "text"


class Allowlist:
    """SEC-03: exactly one chat id may interact with the bot."""

    def __init__(self, chat_id: int | None = None) -> None:
        raw = chat_id if chat_id is not None else os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")
        if raw is None or raw == "":
            raise RuntimeError("TELEGRAM_ALLOWED_CHAT_ID is not set. See docs/setup.md step 4.")
        self.chat_id = int(raw)

    def permits(self, candidate: int) -> bool:
        return candidate == self.chat_id


def parse_update(update: dict[str, Any]) -> Inbound | None:
    """Turn a raw Telegram update into an Inbound, or None if not a message."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None

    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return None

    occurred_at = datetime.fromtimestamp(message.get("date", 0), tz=UTC)

    if "text" in message:
        return Inbound(chat_id, message_id, message["text"], occurred_at, "text")
    if "voice" in message:
        # VOICE-01 downloads and transcribes in P11. Until then the body carries
        # the file id so the message is persisted rather than dropped.
        return Inbound(
            chat_id,
            message_id,
            f"[voice note {message['voice'].get('file_id', '')}]",
            occurred_at,
            "voice",
        )
    return None


def accept(
    conn: psycopg.Connection, allowlist: Allowlist, update: dict[str, Any]
) -> Inbound | None:
    """Persist an inbound message, or reject and log it.

    Returns None for anything the bot will not act on, which covers a non-message
    update, a foreign chat id (CHAT-01), and a redelivery of a message already
    stored (CHAT-08 must not answer the same message twice).
    """
    inbound = parse_update(update)
    if inbound is None:
        return None

    if not allowlist.permits(inbound.chat_id):
        log.warning("ignored message from non-allowlisted chat id %s", inbound.chat_id)
        return None

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into messages (chat_id, telegram_message_id, role, body, modality, occurred_at)
            values (%s, %s, 'athlete', %s, %s, %s)
            on conflict (chat_id, telegram_message_id) where telegram_message_id is not null
            do nothing
            returning id
            """,
            (
                inbound.chat_id,
                inbound.telegram_message_id,
                inbound.body,
                inbound.modality,
                inbound.occurred_at,
            ),
        )
        stored = cur.fetchone()

    if stored is None:
        log.info("skipping redelivered message %s", inbound.telegram_message_id)
        return None
    return inbound


def backlog(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Unprocessed athlete messages, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, body, modality, occurred_at from messages
            where processed_at is null and role = 'athlete'
            order by occurred_at
            """
        )
        return cur.fetchall()


def mark_processed(conn: psycopg.Connection, ids: list[int], now: datetime) -> None:
    if not ids:
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update messages set processed_at = %s where id = any(%s)", (now, ids))


def drain(
    conn: psycopg.Connection,
    respond: Callable[[list[dict[str, Any]], bool], str],
    now: datetime,
) -> str | None:
    """Answer the outstanding backlog exactly once.

    CHAT-08: a six hour outage produces one catch-up response, not one per queued
    message. The whole backlog is handed to ``respond`` together, and every
    message in it is marked processed whether or not it shaped the reply — so a
    second call after a crash mid-reply cannot answer the same messages again.
    """
    pending = backlog(conn)
    if not pending:
        return None

    is_catch_up = len(pending) >= BACKLOG_THRESHOLD
    reply = respond(pending, is_catch_up)
    mark_processed(conn, [m["id"] for m in pending], now)

    if reply:
        record_reply(conn, pending[0]["id"], reply, now)
    return reply


def record_reply(conn: psycopg.Connection, _in_reply_to: int, body: str, now: datetime) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into messages (chat_id, telegram_message_id, role, body, modality, occurred_at)
            values (%s, null, 'coach', %s, 'text', %s)
            """,
            (int(os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", 0)), body, now),
        )
