"""The Anthropic client wrapper: routing, streaming, accounting, fallback.

Every model call in the system goes through :func:`complete`. That is what makes
MODEL-01's "routing is recorded per call", OBS-01's per-call cost logging and
OBS-10's payload ledger true by construction rather than by discipline. Nothing
was added to any call site to make the ledger cover consolidation, the session
review and the Sunday voicing; they were already coming through here.

**The ledger never costs a call.** OBS-11. The payload is written after the cost
row, in its own transaction, and a failure to write it is logged and swallowed.
A `model_calls` row with no payload is a normal outcome; a turn the athlete
never got because the logging broke would not be.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import anthropic
import psycopg
from psycopg.types.json import Jsonb

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
    # OBS-12. Set by the caller that owns the exchange; None for a call that is
    # not part of one, which is every scheduled job.
    turn_id: str | None = None


def _record(conn: psycopg.Connection | None, completion: Completion) -> int | None:
    """MODEL-01 and OBS-01: one row per call, whatever the outcome.

    Returns the row's id so OBS-10's payload has something to hang off.
    """
    if conn is None:
        return None
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
                                     cache_write_tokens, cost_usd, latency_ms,
                                     turn_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
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
                completion.turn_id,
            ),
        )
        return int(cur.fetchone()["id"])


def _response_of(completion: Completion) -> dict[str, Any]:
    """What came back, as plain data.

    The SDK's content blocks are not JSON-serialisable and holding a reference to
    them in a stored payload would tie the ledger's schema to the SDK's. Tool
    inputs are copied into plain dicts for the same reason.
    """
    return {
        "text": completion.text,
        "model": completion.model,
        "stop_reason": completion.stop_reason,
        "tool_uses": [
            {"id": use.id, "name": use.name, "input": dict(use.input)}
            for use in completion.tool_uses
        ],
    }


def _record_payload(
    conn: psycopg.Connection | None,
    call_id: int | None,
    system: list[dict[str, Any]],
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
    completion: Completion,
) -> None:
    """OBS-10, and OBS-11's promise that it cannot cost a call.

    Deliberately not inside `_record`'s transaction. The cost row is an
    accounting record and the payload is a debugging one; a disk full, a value
    the serialiser chokes on, or a schema that has not been migrated yet should
    lose the second and keep the first.
    """
    if conn is None or call_id is None:
        return
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                insert into model_call_payloads (call_id, system, messages, tools, response)
                values (%s, %s, %s, %s, %s)
                """,
                (
                    call_id,
                    Jsonb(list(system)),
                    Jsonb(list(messages)),
                    Jsonb(list(tools)) if tools else None,
                    Jsonb(_response_of(completion)),
                ),
            )
    except Exception:  # noqa: BLE001 - a lost payload must never be a lost turn
        log.exception("could not record the payload for model_call %s", call_id)


def _call(
    client: anthropic.Anthropic,
    route: router.Route,
    system: list[dict[str, Any]],
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
    on_text: Any | None,
    tool_choice: dict[str, Any] | None = None,
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
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice

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
    tool_choice: dict[str, Any] | None = None,
    turn_id: str | None = None,
) -> Completion:
    """Run one model call, recording the route, its cost and what was exchanged.

    MODEL-03: a failure on the configured model retries on the heavy model
    rather than failing the turn. Only the retry's route is reported as the one
    that served the call, with ``routed_from`` naming what was tried first.

    `tool_choice` forces a named tool. Conversation leaves it unset — a coach
    that must call a tool before it may speak is not a coach. Consolidation sets
    it, because CONS-02's "strict JSON" is then a property of the call rather
    than a hope about the prompt.

    `turn_id` (OBS-12) joins the several calls one athlete message can produce
    into the exchange they belong to. Optional: a scheduled job is a call and
    not a turn, and giving it an invented id would make the ledger claim a
    conversation that never happened.
    """
    route = router.route(purpose)
    try:
        completion = _call(client, route, system, messages, tools, on_text, tool_choice)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        fallback = router.fallback_for(route)
        if fallback is None:
            log.error("model call failed on %s with no fallback available", route.model)
            raise
        log.warning(
            "model call failed on %s, falling back to %s: %s", route.model, fallback.model, exc
        )
        completion = _call(client, fallback, system, messages, tools, on_text, tool_choice)
        completion.routed_from = route.model

    completion.turn_id = turn_id
    call_id = _record(conn, completion)
    _record_payload(conn, call_id, system, messages, tools, completion)
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
