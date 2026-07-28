"""The one place a model client is constructed, and the guard in front of it.

`coach.llm.client` takes an `anthropic.Anthropic` as a parameter, which is what
makes every model call testable without a network. Something still has to build
one, and this is it — a single constructor, so that the spend guard below has
nowhere to be routed around.

**On the spend guard.** OBS-07 is a P12 requirement and P12 is a long way off:
a daily hard stop at USD 3.00, configurable, which notifies and then explains
itself rather than going silent. What is here is the *stop* and nothing else.

Building it early is a deliberate exception to working phase by phase, and the
reason is narrow: this package is what makes the system able to call a model on a
loop for the first time. A runaway before P12 would be a real bill, and the
check is a query against `model_calls`, which P01 already writes. The
notification and the explanation are still P12's, and this raises rather than
speaking to the athlete — going quiet is exactly what OBS-07 forbids, so the
caller has to handle it, and :mod:`coach.runtime.turn` does.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from decimal import Decimal

import anthropic
import psycopg

from coach import config

log = logging.getLogger(__name__)


class SpendCapReached(RuntimeError):
    """OBS-07's hard stop. The caller must say so rather than go silent."""


class NotConfigured(RuntimeError):
    """No API key. Raised at construction rather than at the first call."""


def build_client(api_key: str | None = None) -> anthropic.Anthropic:
    """Construct the client. The only call site of `anthropic.Anthropic`.

    The SDK reads `ANTHROPIC_API_KEY` from the environment by itself, so this
    could be a bare constructor. It is not, because a missing key would then
    surface as an authentication error on the first turn — at which point the
    athlete has sent a message and received nothing. Failing at startup is the
    difference between a process that will not start and a coach that has gone
    quiet.
    """
    key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise NotConfigured("ANTHROPIC_API_KEY is not set. See docs/setup.md step 5.")
    return anthropic.Anthropic(api_key=key)


def spent_today(conn: psycopg.Connection, on: date | None = None) -> Decimal:
    """What the day has cost so far, from the OBS-01 rows.

    UTC days, matching how `model_calls.created_at` is stored. TZ-01 governs
    training days and this is not one — a spend cap is about a billing period,
    and the provider's is UTC.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select coalesce(sum(cost_usd), 0) as spent from model_calls "
            "where created_at >= coalesce(%s::date, current_date) "
            "  and created_at < coalesce(%s::date, current_date) + 1",
            (on, on),
        )
        return cur.fetchone()["spent"]


def check_spend(conn: psycopg.Connection, cap: Decimal | None = None) -> Decimal:
    """Raise before dispatch if the day is already over the cap.

    Returns what has been spent, so a caller that wants to log it can. Checked
    *before* the call rather than after, which is the same reasoning as OBS-09's
    pre-flight token ceiling: a call that is going to be refused should not be
    billed first.
    """
    limit = cap if cap is not None else _configured_cap()
    spent = spent_today(conn)
    if spent >= limit:
        raise SpendCapReached(
            f"spent {spent} today against a cap of {limit}. Model calls are stopped "
            "until the day rolls over, or until DAILY_SPEND_CAP_USD is raised."
        )
    return spent


def _configured_cap() -> Decimal:
    """OBS-07: configurable without a code change.

    Read per call rather than held, so raising the cap takes effect on the next
    turn instead of on the next restart — which is what "without a code change"
    is for when the coach has just told the athlete it is capped.

    `config.daily_spend_cap` rather than the whole `Config`, deliberately: going
    through `Config.from_env` would make the cap depend on `DATABASE_URL`, and a
    guard that stops working because an unrelated variable is missing is a guard
    that fails open.
    """
    try:
        return config.daily_spend_cap()
    except config.ConfigError:
        # A malformed cap must not disable the cap.
        log.warning("DAILY_SPEND_CAP_USD is malformed; falling back to the default")
        return config.DEFAULT_DAILY_SPEND_CAP
