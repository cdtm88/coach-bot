"""OBS-13: read a model call back the way it happened. `coach-transcript`.

The ledger is worth nothing if answering "why did the coach say that" means
writing SQL against two tables and hand-decoding JSON. This is the half of
OBS-10 that makes the other half usable, and it is a console script rather than
a note in a runbook for the same reason.

**It reconstructs, it does not summarise.** The system blocks are printed as
they were sent, cache markers included, because the question this answers is
usually "what did it actually have in front of it" and a tidied version of that
is a different question. `--brief` is there for when it is not.

**A turn is the unit, not a call.** One athlete message can produce three calls:
ask for a tool, ask for another, answer. OBS-12's `turn_id` groups them, so a
turn prints as one exchange with its tool round trips in the middle rather than
as three rows that happen to be adjacent.

    coach-transcript                     the last turn
    coach-transcript --last 5            the last five
    coach-transcript --on 2026-08-03     everything that day
    coach-transcript --turn <uuid>       one exchange
    coach-transcript --purpose review    only the Sunday voicings
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg

from coach import db

# Long tool results and long system blocks are the normal case, and a transcript
# that scrolls a terminal off its buffer is one nobody reads twice. Full text is
# always one `--full` away.
TRUNCATE_AT = 2000


@dataclass
class Call:
    call_id: int
    purpose: str
    model: str
    created_at: Any
    cost_usd: Any
    latency_ms: int | None
    turn_id: str | None
    system: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] | None = None
    response: dict[str, Any] = field(default_factory=dict)
    missing_payload: bool = False


def fetch(
    conn: psycopg.Connection,
    last: int | None = None,
    on: date | None = None,
    turn_id: str | None = None,
    purpose: str | None = None,
) -> list[Call]:
    """The calls a filter selects, oldest first.

    `last` counts *turns* rather than calls when a turn id is present, because
    "the last five" means five exchanges and a turn that used three tools would
    otherwise eat the whole window. Calls with no turn id count as one each,
    which is right: a scheduled job is its own exchange.
    """
    where: list[str] = []
    params: list[Any] = []
    if on is not None:
        where.append("c.created_at >= %s::date and c.created_at < %s::date + 1")
        params += [on, on]
    if turn_id is not None:
        where.append("c.turn_id = %s::uuid")
        params.append(turn_id)
    if purpose is not None:
        where.append("c.purpose = %s")
        params.append(purpose)

    clause = f"where {' and '.join(where)}" if where else ""

    with conn.cursor() as cur:
        if last is not None and turn_id is None:
            # The window is chosen over grouped calls first, then the calls are
            # read back whole. Selecting rows and taking the newest N would cut
            # a turn in half and print an answer with no question above it.
            cur.execute(
                f"""
                with grouped as (
                  select coalesce(c.turn_id::text, 'call:' || c.id) as exchange,
                         max(c.created_at) as latest
                    from model_calls c
                    {clause}
                   group by 1
                   order by latest desc
                   limit %s
                )
                select c.id, c.purpose, c.model, c.created_at, c.cost_usd,
                       c.latency_ms, c.turn_id::text as turn_id,
                       p.system, p.messages, p.tools, p.response
                  from model_calls c
                  left join model_call_payloads p on p.call_id = c.id
                  join grouped g
                    on g.exchange = coalesce(c.turn_id::text, 'call:' || c.id)
                 order by c.created_at, c.id
                """,
                [*params, last],
            )
        else:
            cur.execute(
                f"""
                select c.id, c.purpose, c.model, c.created_at, c.cost_usd,
                       c.latency_ms, c.turn_id::text as turn_id,
                       p.system, p.messages, p.tools, p.response
                  from model_calls c
                  left join model_call_payloads p on p.call_id = c.id
                  {clause}
                 order by c.created_at, c.id
                """,
                params,
            )
        rows = cur.fetchall()

    return [
        Call(
            call_id=r["id"],
            purpose=r["purpose"],
            model=r["model"],
            created_at=r["created_at"],
            cost_usd=r["cost_usd"],
            latency_ms=r["latency_ms"],
            turn_id=r["turn_id"],
            system=r["system"] or [],
            messages=r["messages"] or [],
            tools=r["tools"],
            response=r["response"] or {},
            # The distinction matters when reading: a call with no payload is
            # one the ledger missed, not one that sent an empty prompt.
            missing_payload=r["system"] is None,
        )
        for r in rows
    ]


def _clip(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    return f"{text[:limit]}\n    ... [{len(text) - limit} more characters, use --full]"


def _render_content(content: Any, limit: int | None) -> list[str]:
    """One message's content, whatever shape it arrived in.

    A message is a string in the ordinary case and a list of blocks when tools
    are involved. Both are printed as what they are rather than normalised into
    one, because the shape is part of what a reader is checking.
    """
    if isinstance(content, str):
        return [_clip(content, limit)]

    lines: list[str] = []
    for block in content if isinstance(content, list) else []:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            lines.append(_clip(block.get("text", ""), limit))
        elif kind == "tool_use":
            arguments = json.dumps(block.get("input", {}), indent=2, default=str)
            lines.append(f"[calls {block.get('name')}]\n{_clip(arguments, limit)}")
        elif kind == "tool_result":
            lines.append(f"[tool result]\n{_clip(str(block.get('content', '')), limit)}")
        else:
            lines.append(_clip(json.dumps(block, default=str), limit))
    return lines


def render(calls: list[Call], brief: bool = False, full: bool = False) -> str:
    """The transcript as text. Pure, so the tests read it rather than a terminal."""
    limit = None if full else TRUNCATE_AT
    out: list[str] = []
    seen_turn: str | None = None

    for call in calls:
        if call.turn_id and call.turn_id != seen_turn:
            out.append("")
            out.append(f"{'=' * 70}")
            out.append(f"turn {call.turn_id}")
            out.append(f"{'=' * 70}")
            seen_turn = call.turn_id

        cost = f"${call.cost_usd}" if call.cost_usd is not None else "unpriced"
        latency = f"{call.latency_ms}ms" if call.latency_ms is not None else "?"
        out.append("")
        out.append(
            f"--- call {call.call_id}  {call.purpose}  {call.model}  "
            f"{call.created_at}  {cost}  {latency}"
        )

        if call.missing_payload:
            out.append("    (no payload recorded for this call)")
            continue

        if not brief:
            for block in call.system:
                text = block.get("text", "") if isinstance(block, dict) else str(block)
                cached = (
                    " [cached]" if isinstance(block, dict) and block.get("cache_control") else ""
                )
                out.append(f"\n  SYSTEM{cached}:")
                out.append(_indent(_clip(text, limit)))

            if call.tools:
                names = ", ".join(t.get("name", "?") for t in call.tools)
                out.append(f"\n  TOOLS OFFERED: {names}")

        for message in call.messages:
            role = str(message.get("role", "?")).upper()
            for rendered in _render_content(message.get("content"), limit):
                out.append(f"\n  {role}:")
                out.append(_indent(rendered))

        response = call.response
        if response.get("text"):
            out.append("\n  REPLY:")
            out.append(_indent(_clip(response["text"], limit)))
        for use in response.get("tool_uses") or []:
            arguments = json.dumps(use.get("input", {}), indent=2, default=str)
            out.append(f"\n  REPLY CALLS {use.get('name')}:")
            out.append(_indent(_clip(arguments, limit)))
        if response.get("stop_reason"):
            out.append(f"\n  stop_reason: {response['stop_reason']}")

    return "\n".join(out).strip() or "nothing recorded for that filter"


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coach-transcript",
        description="Read back what the model was sent and what it said (OBS-13).",
    )
    parser.add_argument("--last", type=int, help="the most recent N exchanges")
    parser.add_argument("--on", type=date.fromisoformat, help="everything on a date, UTC")
    parser.add_argument("--turn", help="one exchange, by turn id")
    parser.add_argument("--purpose", help="chat, consolidation, review, ...")
    parser.add_argument("--brief", action="store_true", help="skip the system blocks")
    parser.add_argument("--full", action="store_true", help="do not truncate anything")
    args = parser.parse_args(argv)

    # The default is the last exchange rather than the whole table, which is the
    # difference between a useful command and one that floods a terminal the
    # first time anyone runs it.
    last = args.last
    if last is None and args.on is None and args.turn is None:
        last = 1

    with db.connect() as conn:
        calls = fetch(conn, last=last, on=args.on, turn_id=args.turn, purpose=args.purpose)

    print(render(calls, brief=args.brief, full=args.full))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
