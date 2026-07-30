"""The conversational process. `coach-agent`.

Long polls Telegram, hands each update to `coach.telegram.bot` for the allowlist
and persistence, then drains the backlog through one turn. That is the whole
loop; everything it does is elsewhere.

**Why the drain runs on every pass, not only when an update arrived.** CHAT-08's
outage case is a backlog that accumulated while this process was *down*, so the
first pass after a restart has messages to answer and no new update to trigger
it. Draining unconditionally costs one indexed query per idle cycle.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import UTC, datetime

from coach import clock, db
from coach.runtime import models, transport, turn
from coach.telegram import bot as botmod

log = logging.getLogger(__name__)


def serve(
    stop: threading.Event,
    telegram: transport.Telegram,
    client,  # anthropic.Anthropic
    allowlist: botmod.Allowlist,
    connect=db.connect,
    tz=None,
    poll_timeout_s: int = transport.POLL_TIMEOUT_S,
) -> None:
    """The loop. Runs until `stop` is set.

    Every dependency is injected, which is what lets the test drive a full turn
    over a fake transport and a fake model without a network or a token.
    """
    zone = tz or clock.configured_tz()
    log.info("agent listening; allowlisted chat %s", allowlist.chat_id)

    while not stop.is_set():
        try:
            updates = telegram.updates(timeout=poll_timeout_s)
        except Exception:
            # A transport failure must not end the loop. The offset is not
            # advanced, so nothing is lost; the next pass asks again.
            log.exception("getUpdates failed; retrying")
            stop.wait(5)
            continue

        try:
            with connect() as conn:
                for update in updates:
                    botmod.accept(conn, allowlist, update)

                now = datetime.now(UTC)
                turn.handle(
                    conn,
                    client,
                    lambda text: telegram.send(allowlist.chat_id, text),
                    now,
                    zone,
                )
        except Exception:
            # The updates are deliberately *not* acknowledged on this path: an
            # exception between accepting and answering means Telegram should
            # send them again. CHAT-08's dedup on telegram_message_id makes the
            # redelivery a no-op for anything that did land.
            log.exception("turn failed; the update will be redelivered")
            stop.wait(5)
            continue

        telegram.acknowledge(updates)


def main() -> None:
    logging.basicConfig(level=os.environ.get("COACH_LOG_LEVEL", "INFO"))

    # Everything that can be missing is resolved before the loop starts. A coach
    # that dies on the athlete's first message is worse than one that will not
    # start, because only one of those is obviously broken.
    allowlist = botmod.Allowlist()
    client = models.build_client()
    stop = threading.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    with transport.Telegram() as telegram:
        serve(stop, telegram, client, allowlist)
    log.info("agent stopped")
