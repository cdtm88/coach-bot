"""Talking to Telegram, and nothing else.

CHAT-01 says the bot runs Telegram long polling. `coach.telegram.bot` already
holds everything that decides anything — the allowlist (SEC-03), the backlog
catch-up (CHAT-08), the message rows — and takes its updates from a caller. This
is that caller, and it is deliberately thin: it holds no database connection and
no model client, so it cannot accidentally acquire an opinion.

**Long polling rather than a webhook**, for the same reason ingest polls: a
webhook needs a public route and a registered app, and this needs neither. The
`timeout` parameter is server side — Telegram holds the request open until an
update arrives or the timeout elapses — so an idle coach costs one open
connection rather than a request every second.

**The offset is the acknowledgement.** Telegram redelivers an update until you
ask for one past it, so the offset is only advanced after the update has been
handed on. A crash between the two means a redelivery, and CHAT-08's dedup on
`telegram_message_id` is what makes that a no-op rather than a second reply.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.telegram.org"

# Telegram's own cap is 4096 characters per message. Replies are meant to be
# short (CHAT-10 wants a median under 120 words), so hitting this means
# something has gone wrong upstream — but truncation would be a silent failure,
# and splitting is not.
MAX_MESSAGE_CHARS = 4000

# How long Telegram holds an idle request open. Long enough that an idle coach
# is nearly free, short enough that a stop signal is noticed promptly.
POLL_TIMEOUT_S = 25


class NotConfigured(RuntimeError):
    """No bot token. Raised at startup, not at the first message."""


class Telegram:
    """The Telegram HTTP surface: two calls, and the offset between them."""

    def __init__(
        self,
        token: str | None = None,
        client: httpx.Client | None = None,
        base: str = BASE,
    ) -> None:
        resolved = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        if not resolved:
            raise NotConfigured("TELEGRAM_BOT_TOKEN is not set. See docs/setup.md step 4.")
        self._url = f"{base}/bot{resolved}"
        # The timeout is the long poll plus headroom; a read timeout equal to the
        # poll timeout would fire on every idle cycle.
        self._client = client or httpx.Client(timeout=POLL_TIMEOUT_S + 15)
        self.offset: int | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Telegram:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _post(self, method: str, **payload: Any) -> Any:
        response = self._client.post(f"{self._url}/{method}", json=payload)
        if response.status_code >= 400:
            # The URL carries the bot token, so it must not reach the message.
            # httpx would put it there via raise_for_status.
            raise httpx.HTTPError(f"telegram {method} returned {response.status_code}")
        body = response.json()
        if not body.get("ok"):
            raise httpx.HTTPError(f"telegram {method} refused: {body.get('description')}")
        return body.get("result")

    def updates(self, timeout: int = POLL_TIMEOUT_S) -> list[dict[str, Any]]:
        """One long poll. Returns raw updates and does not advance the offset.

        Advancing is :meth:`acknowledge`, called after the updates have been
        handed on, so a crash in between redelivers rather than loses.
        """
        return (
            self._post(
                "getUpdates",
                offset=self.offset,
                timeout=timeout,
                allowed_updates=["message", "edited_message"],
            )
            or []
        )

    def acknowledge(self, updates: list[dict[str, Any]]) -> None:
        """Tell Telegram not to send these again."""
        if updates:
            self.offset = max(u["update_id"] for u in updates) + 1

    def send(self, chat_id: int, text: str) -> None:
        """Send a reply, split if it is somehow enormous.

        Splitting rather than truncating: a reply that hits the cap is already a
        bug, and a silently halved answer is a worse one to debug than two
        messages.
        """
        for chunk in _split(text):
            self._post("sendMessage", chat_id=chat_id, text=chunk)


def _split(text: str, limit: int = MAX_MESSAGE_CHARS) -> Iterator[str]:
    remaining = text.strip()
    if not remaining:
        return
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        yield remaining[:cut].strip()
        remaining = remaining[cut:].strip()
    if remaining:
        yield remaining
