"""Model routing.

MODEL-01: a lightweight model handles conversation turns; a heavier model
handles consolidation and session analysis. MODEL-02: model choice is
configurable per purpose without a code change. MODEL-03: router failures fall
back to the heavier model rather than failing the turn.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

# Purposes the router knows about. Kept in step with the check constraint on
# model_calls.purpose.
PURPOSES = ("chat", "consolidation", "session_review", "transcription", "recall_test", "review")

# The heavier model. MODEL-03 falls back here, so it is also the safety net when
# a configured model is unavailable.
HEAVY = "claude-opus-5"

# Defaults. Conversation runs on the lighter tier because PERF-01 wants
# streaming inside 4 seconds at p95 and a turn is not a reasoning problem;
# consolidation and session analysis run heavy because they are.
DEFAULTS = {
    "chat": "claude-sonnet-5",
    "consolidation": HEAVY,
    "session_review": HEAVY,
    "transcription": HEAVY,
    "recall_test": HEAVY,
    # The Sunday review is voiced once a week, nobody is waiting on the stream,
    # and it is the most quotable message the system sends. That combination
    # buys the heavy model outright.
    "review": HEAVY,
}

# USD per million tokens, by model. Used for OBS-01 cost logging and the OBS-07
# daily cap. Cache reads bill at ~0.1x input, cache writes at ~1.25x.
PRICING = {
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}


class UnknownPurpose(ValueError):
    """The caller asked to route a purpose the router does not know."""


@dataclass(frozen=True)
class Route:
    purpose: str
    model: str
    effort: str
    # Adaptive thinking. Conversation turns still think — disabling it makes the
    # model markedly less willing to reach for tools, which a coach that runs on
    # get_context and search_memory cannot afford.
    thinking: bool = True


def _env_key(purpose: str) -> str:
    return f"MODEL_{purpose.upper()}"


def route(purpose: str) -> Route:
    """Resolve a purpose to a model. MODEL-02: environment, not code."""
    if purpose not in PURPOSES:
        raise UnknownPurpose(f"purpose must be one of {PURPOSES}, got {purpose!r}")

    model = os.environ.get(_env_key(purpose)) or DEFAULTS[purpose]
    # Effort is the cost and latency lever. A turn is short and interactive; the
    # nightly pass is neither.
    effort = "low" if purpose == "chat" else "high"
    return Route(purpose=purpose, model=model, effort=effort)


def fallback_for(route_: Route) -> Route | None:
    """The route to retry on when the configured model fails (MODEL-03).

    Returns None when the failing route is already the heavy model, since there
    is nowhere further to fall back to and the caller should surface the error.
    """
    if route_.model == HEAVY:
        return None
    return Route(purpose=route_.purpose, model=HEAVY, effort=route_.effort)


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Cost of one call. Unknown models price at zero rather than guessing."""
    rates = PRICING.get(model)
    if rates is None:
        return Decimal("0")
    per_in, per_out = rates
    million = Decimal(1_000_000)
    return (
        per_in * Decimal(input_tokens) / million
        + per_out * Decimal(output_tokens) / million
        + per_in * Decimal("0.1") * Decimal(cache_read_tokens) / million
        + per_in * Decimal("1.25") * Decimal(cache_write_tokens) / million
    ).quantize(Decimal("0.000001"))
