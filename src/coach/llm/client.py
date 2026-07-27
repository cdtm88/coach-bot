"""The Anthropic client wrapper: routing, streaming, accounting, fallback.

Every model call in the system goes through :func:`complete`. That is what makes
MODEL-01's "routing is recorded per call" and OBS-01's per-call cost logging
true by construction rather than by discipline.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import anthropic
import psycopg

from coach.llm import router

log = logging.getLogger(__name__)

# Streaming is the default. PERF-01 measures time to first token, and a
# non-streaming call at a large max_tokens risks an HTTP timeout besides.
MAX_TOKENS = 4096


@dataclass
class Completion:
    text: str
    model: str
    purpose: str
    stop_reason: str | None
    tool_uses: list[Any] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    routed_from: str | None = None
    latency_ms: int = 0


def _record(conn: psycopg.Connection | None, completion: Completion) -> None:
    """MODEL-01 and OBS-01: one row per call, whatever the outcome."""
    if conn is None:
        return
    cost = router.cost_usd(
        completion.model,
        completion.input_tokens,
        completion.output_tokens,
        completion.cache_read_tokens,
        completion.cache_write_tokens,
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into model_calls (purpose, model, routed_from, input_tokens,
                                     output_tokens, cache_read_tokens,
                                     cache_write_tokens, cost_usd, latency_ms)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                completion.purpose,
                completion.model,
                completion.routed_from,
                completion.input_tokens,
                completion.output_tokens,
                completion.cache_read_tokens,
                completion.cache_write_tokens,
                cost,
                completion.latency_ms,
            ),
        )


def _call(
    client: anthropic.Anthropic,
    route: router.Route,
    system: list[dict[str, Any]],
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
    on_text: Any | None,
) -> Completion:
    started = time.perf_counter()

    kwargs: dict[str, Any] = {
        "model": route.model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": list(messages),
        "output_config": {"effort": route.effort},
    }
    if route.thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    if tools:
        kwargs["tools"] = list(tools)

    with client.messages.stream(**kwargs) as stream:
        if on_text is not None:
            for chunk in stream.text_stream:
                on_text(chunk)
        message = stream.get_final_message()

    text = "".join(b.text for b in message.content if b.type == "text")
    usage = message.usage
    return Completion(
        text=text,
        model=message.model,
        purpose=route.purpose,
        stop_reason=message.stop_reason,
        tool_uses=[b for b in message.content if b.type == "tool_use"],
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def complete(
    client: anthropic.Anthropic,
    purpose: str,
    system: list[dict[str, Any]],
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None = None,
    conn: psycopg.Connection | None = None,
    on_text: Any | None = None,
) -> Completion:
    """Run one model call, recording the route and its cost.

    MODEL-03: a failure on the configured model retries on the heavy model
    rather than failing the turn. Only the retry's route is reported as the one
    that served the call, with ``routed_from`` naming what was tried first.
    """
    route = router.route(purpose)
    try:
        completion = _call(client, route, system, messages, tools, on_text)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        fallback = router.fallback_for(route)
        if fallback is None:
            log.error("model call failed on %s with no fallback available", route.model)
            raise
        log.warning(
            "model call failed on %s, falling back to %s: %s", route.model, fallback.model, exc
        )
        completion = _call(client, fallback, system, messages, tools, on_text)
        completion.routed_from = route.model

    _record(conn, completion)
    return completion


def count_tokens(
    client: anthropic.Anthropic,
    purpose: str,
    system: list[dict[str, Any]],
    messages: Sequence[dict[str, Any]],
) -> int:
    """Real token count for the MEM-11 budget.

    The context module ships a character-based estimator so the budget is
    testable without a network call; this is what P01 passes in its place.
    """
    route = router.route(purpose)
    result = client.messages.count_tokens(model=route.model, system=system, messages=list(messages))
    return result.input_tokens


def stream_text(completion_text: str, chunk: int = 400) -> Iterator[str]:
    """Split a reply into Telegram-sized chunks without cutting mid-word."""
    words = completion_text.split(" ")
    buffer: list[str] = []
    length = 0
    for word in words:
        if length + len(word) + 1 > chunk and buffer:
            yield " ".join(buffer)
            buffer, length = [], 0
        buffer.append(word)
        length += len(word) + 1
    if buffer:
        yield " ".join(buffer)
