"""The agent's tool surface.

CHAT-06 names eight tools and requires each to carry a JSON schema and an
integration test. All eight are defined here from P01 so the surface is stable
and the schemas are reviewable; the four that read data no feed has produced yet
return an explicit not-yet-available result naming the phase that fills them in.
That is deliberately a real, tested return value rather than a missing tool — a
tool that vanishes between phases would change the prompt prefix and invalidate
the cache on every deploy.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import psycopg

from coach import clock
from coach.calendars import availability as calmod
from coach.memory import facts as factmod
from coach.memory import notes as notemod
from coach.memory import state as statemod

# Phase that lands each deferred tool, surfaced in its unavailable result.
DEFERRED = {
    "log_session": "P10",
    "get_sessions": "P03",
    "update_block": "P07",
    "write_session_events": "P08",
}

SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_context",
        "description": (
            "Retrieve what you currently hold on a topic: the active value, its "
            "provenance, when it last changed, and what it replaced. Call this when "
            "the athlete asks what you know, or when you need a fact's history "
            "rather than its current value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Fact key, e.g. availability.weekday_minutes.",
                }
            },
            "required": ["key"],
        },
    },
    {
        "name": "search_memory",
        "description": (
            "Full text search over the episodic archive: day summaries, coach "
            "observations, past reviews. Call this when the athlete refers to "
            "something from a previous week that is not in your standing memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to search for."},
                "limit": {"type": "integer", "description": "Max notes to return.", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "propose_fact",
        "description": (
            "Queue a change to what you know, for the nightly pass to ratify. Call "
            "this on an explicit instruction or a direct correction only. It does "
            "not take effect now and you must not tell the athlete you used it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"description": "The new value, typed to match the key."},
                "provenance": {
                    "type": "string",
                    "enum": ["stated", "observed", "computed", "inferred"],
                },
                "reason": {"type": "string", "description": "Why this change, in one line."},
            },
            "required": ["key", "value", "provenance", "reason"],
        },
    },
    {
        "name": "log_session",
        "description": (
            "Record a gym session or golf round captured in conversation, including "
            "which prescribed movements were completed and how they sat against "
            "active constraints."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "discipline": {"type": "string", "enum": ["gym", "golf", "other"]},
                "occurred_on": {"type": "string", "format": "date"},
                "detail": {
                    "type": "object",
                    "description": "Movements, sets, reps, RPE, duration.",
                },
            },
            "required": ["discipline", "occurred_on", "detail"],
        },
    },
    {
        "name": "get_sessions",
        "description": (
            "Fetch individual training sessions for discussion. Never use these rows "
            "to compute a total — aggregates come from the rollups already in your "
            "context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "format": "date"},
                "until": {"type": "string", "format": "date"},
                "discipline": {"type": "string"},
            },
            "required": ["since"],
        },
    },
    {
        "name": "update_block",
        "description": (
            "Rewrite part of the current training block document. Rewrite the "
            "affected section rather than regenerating the whole block."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {"type": "string"},
                "content": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["section", "content", "reason"],
        },
    },
    {
        "name": "get_calendar",
        "description": (
            "Read busy time from the calendar feeds across a date range. The feeds "
            "lag, so an empty result means nothing was published, not that he is free."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "format": "date"},
                "until": {"type": "string", "format": "date"},
            },
            "required": ["since", "until"],
        },
    },
    {
        "name": "write_session_events",
        "description": (
            "Publish or update planned workouts on the training calendar. Every "
            "event carries a stable coach id so a change updates rather than "
            "duplicates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "coach_id": {"type": "string"},
                            "planned_for": {"type": "string", "format": "date-time"},
                            "discipline": {"type": "string"},
                            "spec": {"type": "object"},
                        },
                        "required": ["coach_id", "planned_for", "discipline", "spec"],
                    },
                }
            },
            "required": ["events"],
        },
    },
]

TOOL_NAMES = tuple(s["name"] for s in SCHEMAS)


class UnknownTool(ValueError):
    """The model called a tool that is not in the surface."""


def _unavailable(name: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": (
            f"{name} lands in {DEFERRED[name]}. The data it reads does not exist yet — "
            "say you cannot see that yet rather than guessing."
        ),
    }


def _serialise(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def dispatch(conn: psycopg.Connection, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool call and return its result payload."""
    if name not in TOOL_NAMES:
        raise UnknownTool(f"{name!r} is not in the tool surface {TOOL_NAMES}")

    if name in DEFERRED:
        return _unavailable(name)

    if name == "get_context":
        # CHAT-07: active value, provenance, when it changed, what it replaced.
        history = factmod.history(conn, arguments["key"])
        return {
            "key": arguments["key"],
            "history": [
                {
                    "value": f.value,
                    "provenance": f.provenance,
                    "status": f.status,
                    "confidence": float(f.confidence),
                    "valid_from": _serialise(f.valid_from),
                    "valid_to": _serialise(f.valid_to),
                }
                for f in history
            ],
        }

    if name == "search_memory":
        hits = notemod.search(conn, arguments["query"], limit=arguments.get("limit", 5))
        return {
            "notes": [
                {"kind": n.kind, "occurred_on": _serialise(n.occurred_on), "body": n.body}
                for n in hits
            ]
        }

    if name == "propose_fact":
        # CONS-06: this reaches pending_writes and waits for the night. There is
        # no path from here to a facts insert.
        queued = statemod.queue_write(
            conn,
            {
                "key": arguments["key"],
                "value": arguments["value"],
                "provenance": arguments["provenance"],
                "reason": arguments["reason"],
            },
            origin="in_turn",
        )
        return {"queued": True, "pending_write_id": queued}

    if name == "get_calendar":
        # CALR-05: what the feed published, labelled as such. The tool result
        # counts against the MEM-11 budget in the same turn, so it returns the
        # blocks and not the whole calendar.
        blocks = calmod.busy_between(
            conn,
            date.fromisoformat(arguments["since"]),
            date.fromisoformat(arguments["until"]),
            clock.configured_tz(),
        )
        return {
            "busy": [
                {
                    "local_date": _serialise(b.local_date),
                    "starts_at": _serialise(b.starts_at),
                    "ends_at": _serialise(b.ends_at),
                    "summary": b.summary,
                    "all_day": b.all_day,
                }
                for b in blocks[:50]
            ],
            "caveat": (
                "Google publishes these feeds on a cache. This is what it showed, not "
                "a guarantee that he is free at any other time."
            ),
        }

    raise UnknownTool(name)  # pragma: no cover - unreachable given TOOL_NAMES
