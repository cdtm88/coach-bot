"""Configuration, read from the environment only.

SEC-01: secrets live in environment variables, never in the repository. Nothing
here carries a credential default; a missing required value is an error at
startup rather than a silent fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

# CONS-07: confidence decays toward this floor and never reaches zero.
CONFIDENCE_FLOOR = Decimal("0.20")

# CONS-08: below this a fact is a candidate for natural verification.
VERIFICATION_THRESHOLD = Decimal("0.50")

# MEM-11: per turn assembled context ceiling, excluding conversation history.
CONTEXT_TOKEN_BUDGET = 4000


class ConfigError(RuntimeError):
    """A required environment variable is missing or malformed."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set. See .env.example and docs/setup.md section 4.")
    return value


@dataclass(frozen=True)
class Config:
    database_url: str
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

        # OBS-07: the daily hard stop. Configurable without a code change.
        raw_cap = os.environ.get("DAILY_SPEND_CAP_USD", "3.00")
        try:
            cap = Decimal(raw_cap)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"DAILY_SPEND_CAP_USD={raw_cap!r} is not a number") from exc
        if cap <= 0:
            raise ConfigError("DAILY_SPEND_CAP_USD must be positive")

        return cls(
            database_url=_require("DATABASE_URL"),
            timezone=tz,
            daily_spend_cap_usd=cap,
        )
