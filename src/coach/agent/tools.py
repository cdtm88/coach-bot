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
from psycopg.types.json import Jsonb

from coach import clock
from coach.blocks import document as blockmod
from coach.calendars import availability as calmod
from coach.health import breaks as breakmod
from coach.logbook import capture as capturemod
from coach.memory import facts as factmod
from coach.memory import notes as notemod
from coach.memory import state as statemod

# Phase that lands each deferred tool, surfaced in its unavailable result.
DEFERRED: dict[str, str] = {}

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
        "name": "set_break",
        "description": (
            "Record a scheduled break for holiday, travel or illness. Suspends the "
            "prescriptions it covers and cancels their planned events. Illness breaks "
            "are open ended by default and never resume on their own."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["holiday", "travel", "illness"]},
                "starts_on": {"type": "string", "format": "date"},
                "ends_on": {
                    "type": "string",
                    "format": "date",
                    "description": "Omit for an open ended break.",
                },
                "reason": {"type": "string"},
            },
            "required": ["kind", "starts_on"],
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
                "section": {
                    "type": "string",
                    "enum": list(blockmod.SECTIONS),
                    "description": "Which section to replace. Everything else is left alone.",
                },
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

    if name == "log_session":
        # LOG-01 to LOG-05. The local record first and the upstream write not at
        # all from here: LOG-08 forbids the conversation waiting on a network
        # call, so `push_upstream` is the ingest loop's job rather than the
        # turn's.
        try:
            captured = capturemod.record(
                conn,
                arguments["discipline"],
                date.fromisoformat(arguments["occurred_on"]),
                arguments.get("detail") or {},
                clock.configured_tz(),
            )
        except capturemod.Incomplete as exc:
            # LOG-03 and LOG-04: the one question that would make this
            # recordable, handed back so the coach asks it rather than
            # inventing a value.
            return {"recorded": False, "ask": exc.question}
        return {
            "recorded": True,
            "session_id": captured.session_id,
            "load": float(captured.load),
            "closed_prescription_id": captured.prescription_id,
        }

    if name == "set_break":
        # BREAK-01's conversational front end, and BREAK-02 in the same call:
        # a break that is recorded but leaves the week's prescriptions live
        # would have the coach messaging about sessions it has agreed are not
        # happening. The upstream cancellation is not done here (LOG-08's
        # reasoning applies equally) — the sweep removes the orphans.
        starts_on = date.fromisoformat(arguments["starts_on"])
        ends_on = arguments.get("ends_on")
        break_id = breakmod.create(
            conn,
            arguments["kind"],
            starts_on,
            date.fromisoformat(ends_on) if ends_on else None,
            arguments.get("reason"),
        )
        created = breakmod.active_on(conn, starts_on)
        suspended = breakmod.suspend(conn, created) if created else None
        return {
            "break_id": break_id,
            "suspended": len(suspended.prescription_ids) if suspended else 0,
            "open_ended": ends_on is None or arguments["kind"] == "illness",
        }

    if name == "get_sessions":
        # FIT-01. Individual rows for discussion, never for arithmetic: the
        # description says so and the rollups in the prompt are where totals
        # come from. Capped because a year of rides would eat the MEM-11 budget
        # in one tool result.
        until = arguments.get("until")
        with conn.cursor() as cur:
            cur.execute(
                """
                select id, local_date, discipline, activity_type, name, duration_s,
                       avg_power_w, np_power_w, avg_hr, max_hr, avg_cadence, source,
                       derived, data_unavailable
                  from sessions
                 where local_date >= %s
                   and (%s::date is null or local_date <= %s)
                   and (%s::text is null or discipline = %s)
                 order by local_date desc, id desc
                 limit 60
                """,
                (
                    date.fromisoformat(arguments["since"]),
                    until,
                    until,
                    arguments.get("discipline"),
                    arguments.get("discipline"),
                ),
            )
            rows = cur.fetchall()
        return {
            "sessions": [
                {
                    "id": r["id"],
                    "local_date": _serialise(r["local_date"]),
                    "discipline": r["discipline"],
                    "name": r["name"],
                    "duration_s": r["duration_s"],
                    "avg_power_w": _serialise(r["avg_power_w"]),
                    "np_power_w": _serialise(r["np_power_w"]),
                    "avg_hr": r["avg_hr"],
                    "max_hr": r["max_hr"],
                    "avg_cadence": _serialise(r["avg_cadence"]),
                    "source": r["source"],
                    "load": (r["derived"] or {}).get("icu_training_load"),
                    # The row means "he did something here", not "he did nothing".
                    # Without this the empty columns read as a session that went
                    # wrong rather than one the platform will not describe.
                    "data_unavailable": r["data_unavailable"],
                }
                for r in rows
            ],
            "caveat": "Individual rows. Totals come from the rollups already in context.",
        }

    if name == "write_session_events":
        # PLAN-01, written locally and published by the scheduler rather than
        # from inside the turn. The same reasoning as LOG-08: a conversation
        # must not wait on intervals.icu, and a network failure must not lose a
        # plan the athlete has just agreed to.
        block = blockmod.active(conn)
        if block is None:
            return {"available": False, "reason": "there is no active training block yet"}

        written = []
        with conn.transaction(), conn.cursor() as cur:
            for event in arguments["events"]:
                cur.execute(
                    "insert into prescriptions (block_id, planned_for, discipline, spec, "
                    "status) values (%s, %s, %s, %s, 'planned') returning id",
                    (
                        block.id,
                        datetime.fromisoformat(event["planned_for"]),
                        event["discipline"],
                        Jsonb(event["spec"]),
                    ),
                )
                written.append(int(cur.fetchone()["id"]))
        return {
            "written": len(written),
            "prescription_ids": written,
            "note": "Recorded against the active block. They reach the calendar on the "
            "next publish pass rather than immediately.",
        }

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

    if name == "update_block":
        # BLOCK-02: rewrite the affected section rather than regenerating the
        # whole document. The tool takes a section for that reason — a whole
        # document parameter would make the wholesale rewrite the easy path.
        block = blockmod.active(conn)
        if block is None:
            return {"available": False, "reason": "there is no active training block yet"}
        version = blockmod.rewrite(
            conn,
            block.id,
            arguments["section"],
            arguments["content"],
            arguments["reason"],
        )
        return {"block_id": block.id, "version": version, "section": arguments["section"]}

    raise UnknownTool(name)  # pragma: no cover - unreachable given TOOL_NAMES
