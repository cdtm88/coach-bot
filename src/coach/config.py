"""Configuration, read from the environment only.

SEC-01: secrets live in environment variables, never in the repository. Nothing
here carries a credential default; a missing required value is an error at
startup rather than a silent fallback.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# CONS-07: confidence decays toward this floor and never reaches zero.
CONFIDENCE_FLOOR = Decimal("0.20")

# CONS-08: below this a fact is a candidate for natural verification.
VERIFICATION_THRESHOLD = Decimal("0.50")

# MEM-11: per turn assembled context ceiling, excluding conversation history.
CONTEXT_TOKEN_BUDGET = 4000

# OBS-14, and the resolution of open item 8. Raw prompts and replies are kept for
# this many days and then pruned; `model_calls` itself is never pruned, because
# OBS-01's cost history is the thing the table exists for.
#
# Ninety days is long enough to investigate anything anyone realistically
# investigates and to build a trust corpus from real conversation, and short
# enough that MEM-12's nightly pg_dump does not grow without bound carrying a
# copy of the persona and the fact blocks for every call ever made.
DEFAULT_PAYLOAD_RETENTION_DAYS = 90


class ConfigError(RuntimeError):
    """A required environment variable is missing or malformed."""


# OBS-07's default, here rather than inline so the two readers below cannot
# disagree about it.
DEFAULT_DAILY_SPEND_CAP = Decimal("3.00")


def daily_spend_cap() -> Decimal:
    """OBS-07's hard stop, read on its own.

    Separate from :meth:`Config.from_env` because the spend guard needs this and
    nothing else, and going through the whole config would make the cap depend on
    `DATABASE_URL` being set. A guard that stops working because an unrelated
    variable is missing is a guard that fails open, which is the wrong direction
    for this one.
    """
    raw = os.environ.get("DAILY_SPEND_CAP_USD")
    if not raw:
        return DEFAULT_DAILY_SPEND_CAP
    try:
        cap = Decimal(raw)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"DAILY_SPEND_CAP_USD={raw!r} is not a number") from exc
    if cap <= 0:
        raise ConfigError("DAILY_SPEND_CAP_USD must be positive")
    return cap


def payload_retention_days() -> int:
    """OBS-14's window, read on its own for the same reason the cap is.

    A malformed value falls back rather than raising. The cost of the wrong
    window is payloads kept a bit too long; the cost of refusing to start is the
    whole nightly pass, and this is the least important job in it.

    Zero or negative is refused rather than treated as "prune everything": a
    typo that silently deleted the entire ledger is not a failure mode worth
    having, and turning the ledger off is what not scheduling the job is for.
    """
    raw = os.environ.get("COACH_PAYLOAD_RETENTION_DAYS", "").strip()
    if not raw:
        return DEFAULT_PAYLOAD_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        log.warning("COACH_PAYLOAD_RETENTION_DAYS=%r is not a number; using the default", raw)
        return DEFAULT_PAYLOAD_RETENTION_DAYS
    if days < 1:
        log.warning("COACH_PAYLOAD_RETENTION_DAYS=%d is not positive; using the default", days)
        return DEFAULT_PAYLOAD_RETENTION_DAYS
    return days


def _database_url() -> str | None:
    """Where the database is, or None to let libpq answer that itself.

    `DATABASE_URL` when it is set. Otherwise `PGHOST` being present is taken as
    the deployment saying "the libpq variables are configured, use them" — which
    is the convention a Postgres deployed independently of this repository is
    likely to already have, with the password in a mounted file exported as
    `PGPASSWORD` rather than interpolated into a URI.

    Only when neither is present is this a missing configuration, and then it
    says so naming both routes rather than only the one it happened to check.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    if os.environ.get("PGHOST"):
        return None
    raise ConfigError(
        "no database configured. Set DATABASE_URL, or the libpq variables "
        "PGHOST/PGUSER/PGDATABASE with PGPASSWORD. See docs/deploy.md."
    )


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set. See .env.example and docs/setup.md section 4.")
    return value


@dataclass(frozen=True)
class Config:
    # None means "libpq will work it out from PGHOST and friends", which is a
    # real configuration and not a missing one. `coach.db.connect` treats it that
    # way and raises only when neither route is available.
    database_url: str | None
    timezone: ZoneInfo
    daily_spend_cap_usd: Decimal

    @classmethod
    def from_env(cls) -> Config:
        # TZ-01: all day and week boundaries use the athlete's configured local
        # timezone, whatever the upstream data says.
        tz_name = os.environ.get("TZ", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001 - surfaced as a config error
            raise ConfigError(f"TZ={tz_name!r} is not a known timezone") from exc

        return cls(
            database_url=_database_url(),
            timezone=tz,
            # OBS-07: the daily hard stop. Configurable without a code change.
            daily_spend_cap_usd=daily_spend_cap(),
        )
