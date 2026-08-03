"""The one door a message the athlete did not ask for goes out through.

Three requirements meet here and none of them was being met.

**A message the coach sent is something the coach said.** `runtime.turn._history`
builds the model's conversation from `messages`, and until now the only rows in
it were athlete messages and replies to them. The morning message, the evening
follow-up and the Sunday review went straight down the transport. So the coach
would ask at 21:00 whether Thursday's session was done or wanted moving, the
athlete would answer "move it", and the model would read that answer with no
idea what it was answering.

**A retry must not repeat it.** `runtime.scheduler.claim` is a genuine ledger and
covers the ordinary case, but it re-claims a job whose status is 'failed' while
attempts remain. A job that posted its message and then failed on the next line
sends twice. `period_key` is claimed here *before* the post, so the retry runs
the job again and says nothing. training-tracker sent the same Saturday message
three times for want of this.

**A claim that was never spoken must be given back.** The row is written first,
which means a transport failure would otherwise leave `messages` asserting the
coach said something it did not, and that assertion would go into the next
turn's context as fact. So a failed post deletes the row it claimed and
re-raises, which both keeps the history honest and lets the scheduler's own
retry work.

Ordering, then: claim, post, keep. Not post, then record — that fails the other
way, and a duplicate message is the failure being fixed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime

import psycopg

log = logging.getLogger(__name__)


def chat_id() -> int:
    """SEC-03's single allowlisted chat, read the way `bot.record_reply` reads it.

    Not `bot.Allowlist`, deliberately: that raises when the variable is unset,
    and this is on the write path of a message that has already been composed.
    A recording that refuses because of configuration would turn a missing
    environment variable into a lost message rather than a mislabelled row.
    """
    return int(os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", 0))


class Outbox:
    """Post a proactive message and record it, or decline because it was sent.

    `post` is a one-argument callable. Injected rather than reached for, so the
    suite drives the whole path with a list's `append` and no transport, which
    is the same shape `adjust.apply.execute` and `review.weekly.run` already
    use.
    """

    def __init__(self, post: Callable[[str], None]) -> None:
        self._post = post

    def send(
        self,
        conn: psycopg.Connection,
        body: str,
        kind: str,
        period_key: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """True when this went out, False when the period was already spoken for.

        False is a normal outcome and not an error: it is what the second
        attempt at a job that already delivered looks like.
        """
        said = (body or "").strip()
        if not said:
            return False

        moment = now or datetime.now(UTC)
        message_id = self._claim(conn, said, kind, period_key, moment)
        if message_id is None:
            log.info("%s for %s was already sent; not sending it again", kind, period_key)
            return False

        try:
            self._post(said)
        except Exception:
            # The row claimed that the coach spoke. It did not.
            self._release(conn, message_id)
            raise
        return True

    def bind(
        self,
        conn: psycopg.Connection,
        kind: str,
        period_key: str | None = None,
        now: datetime | None = None,
    ) -> Callable[[str], None]:
        """This outbox as a plain one-argument sender.

        `review.weekly.run` and `adjust.apply.execute` both take a
        `Callable[[str], None]` and neither should have to learn what a period
        key is. Binding here keeps their signatures, and their tests, untouched.
        """

        def send(text: str) -> None:
            self.send(conn, text, kind=kind, period_key=period_key, now=now)

        return send

    def _claim(
        self,
        conn: psycopg.Connection,
        body: str,
        kind: str,
        period_key: str | None,
        moment: datetime,
    ) -> int | None:
        """Write the row, or return None because this period already has one.

        The conflict target names the partial index's predicate so Postgres can
        infer it. A row with a null `period_key` is not in that index and
        therefore never conflicts, which is how an ADJ-06 notice gets recorded
        without claiming anything.
        """
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                insert into messages (chat_id, telegram_message_id, role, body,
                                      modality, occurred_at, kind, period_key)
                values (%s, null, 'coach', %s, 'text', %s, %s, %s)
                on conflict (kind, period_key) where period_key is not null
                do nothing
                returning id
                """,
                (chat_id(), body, moment, kind, period_key),
            )
            row = cur.fetchone()
        return int(row["id"]) if row else None

    def _release(self, conn: psycopg.Connection, message_id: int) -> None:
        """Take back a claim the transport did not honour.

        Best effort and never raising over the original failure: the caller is
        already handling a transport exception, and losing that exception to a
        database error while tidying up would hide the thing that actually went
        wrong.
        """
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("delete from messages where id = %s", (message_id,))
        except Exception:  # noqa: BLE001 - the send failure is the real news
            log.exception("could not release the unsent message row %s", message_id)
