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
from coach.blocks import load as loadmod
from coach.calendars import availability as calmod
from coach.consolidation import conflict
from coach.health import breaks as breakmod
from coach.logbook import capture as capturemod
from coach.memory import facts as factmod
from coach.memory import notes as notemod
from coach.memory import state as statemod
from coach.plans import agenda as agendamod
from coach.plans import events as eventmod

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
                # `computed` is absent on purpose and the list is not written out
                # here, so this door and the nightly proposer's cannot drift
                # apart. See `conflict.MODEL_PROVENANCE`.
                "provenance": {
                    "type": "string",
                    "enum": list(conflict.MODEL_PROVENANCE),
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
        # The surface could write a prescription and never read one. Everything
        # the coach knew about the plan came from whatever it had just written
        # in the same turn, which is to say nothing, one turn later.
        "name": "get_plan",
        "description": (
            "What is prescribed over a date range: the session, its duration, its "
            "target, its purpose, and whether it has been done. This is the training "
            "plan. Use it before answering anything about what he is meant to be "
            "doing. Do not answer that from the calendar feeds, which are his own "
            "diary."
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
            "duplicates. A session written here is what he will see on the "
            "calendar and what he will actually do, so give it the same detail "
            "you would give him in a message: how long, how hard, and what it is "
            "for. A session with no duration and no purpose publishes as an empty "
            "block on his calendar and is rejected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "planned_for": {
                                "type": "string",
                                "format": "date-time",
                                "description": "Local start time, ISO 8601.",
                            },
                            "discipline": {
                                "type": "string",
                                "enum": list(eventmod.DISCIPLINES),
                                "description": (
                                    "Use 'ride' for outdoor cycling and 'virtualride' "
                                    "for Zwift and the turbo. 'cycling' is not a "
                                    "discipline the calendar understands."
                                ),
                            },
                            # Spelled out rather than left as a free-form object.
                            # An unconstrained `spec` is what put empty sessions on
                            # his calendar: the model had nothing telling it what
                            # the field was for, and nothing checked what arrived.
                            "spec": {
                                "type": "object",
                                "properties": {
                                    "duration_s": {
                                        "type": "integer",
                                        "description": "Planned duration in seconds. Required.",
                                    },
                                    "purpose": {
                                        "type": "string",
                                        "description": (
                                            "What the session is for, in his words. "
                                            "'Base endurance, conversational' rather "
                                            "than 'Z2'. Required."
                                        ),
                                    },
                                    "target_watts": {"type": "integer"},
                                    "intensity_factor": {"type": "number"},
                                    "ftp_watts": {"type": "integer"},
                                    "rpe_target": {"type": "number"},
                                    "route": {"type": "string"},
                                    "movements": {
                                        "type": "array",
                                        "description": (
                                            "Gym only. Each needs sets, reps and an RPE."
                                        ),
                                        "items": {"type": "object"},
                                    },
                                },
                                "required": ["duration_s", "purpose"],
                            },
                        },
                        "required": ["planned_for", "discipline", "spec"],
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


def _planned_at(raw: str) -> datetime:
    """TZ-01: a wall clock time the model wrote is the athlete's wall clock.

    `datetime.fromisoformat` returns a naive value for the ISO strings a model
    actually produces, and `planned_for` is `timestamptz`. Handing Postgres a
    naive value has it read in the session zone, which is UTC — so "18:00" was
    stored as 18:00 UTC and came back as 22:00 in Dubai. Every session this tool
    wrote sat four hours late, on the calendar and in the morning message.

    `blocks.generate` never had the bug because it builds its own timestamp with
    `tzinfo=tz`. This is the same convention, applied at the other door.
    """
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=clock.configured_tz())
    return moment


def _reject_specs(events: list[dict[str, Any]]) -> list[str]:
    """BLOCK-04, on the path that was not checking it.

    `blocks.generate.validate` has enforced this since P07: a published session
    carries a duration, an intensity target and a purpose, and a generation that
    cannot produce them is rejected rather than written. `write_session_events`
    is the *other* way a prescription is created, it never had the check, and
    the difference was visible on the athlete's calendar — three entries reading
    only "cycling", no duration, no target, and a week showing zero load.

    The schema now asks for these too. Both, deliberately: a schema is a request
    the model usually honours and this is a check it cannot route around, and
    the failure mode here writes to the athlete's real calendar.

    Reasons rather than an exception, and every reason rather than the first,
    because the caller is a model that will fix exactly what it is told about.
    """
    reasons: list[str] = []
    for index, event in enumerate(events):
        where = str(event.get("planned_for") or f"event {index + 1}")
        spec = event.get("spec")
        if not isinstance(spec, dict):
            reasons.append(f"{where}: spec must be an object with duration_s and purpose")
            continue

        if not spec.get("duration_s"):
            reasons.append(f"{where}: duration_s is missing (BLOCK-04)")
        if not str(spec.get("purpose") or "").strip():
            reasons.append(f"{where}: purpose is missing (BLOCK-04)")

        discipline = eventmod.canonical(event.get("discipline") or "")
        if discipline not in eventmod.TYPES:
            reasons.append(
                f"{where}: {event.get('discipline')!r} is not a discipline the calendar "
                f"understands. Use one of {', '.join(eventmod.DISCIPLINES)}."
            )
        elif discipline in loadmod.GYM_DISCIPLINES:
            # GYM-01. The movements are the session: a gym entry without them is
            # a block of time he cannot act on.
            for movement in spec.get("movements") or []:
                if not all(movement.get(f) for f in ("sets", "reps", "rpe_target")):
                    name = movement.get("name") or movement.get("exercise") or "movement"
                    reasons.append(f"{where}: {name} needs sets, reps and an RPE target (GYM-01)")
        elif not (spec.get("intensity_factor") or spec.get("target_watts")):
            # BLOCK-04's intensity target. Gym states it as RPE per movement,
            # which the branch above covers; everything else needs a number the
            # session can be ridden to.
            reasons.append(
                f"{where}: needs an intensity target, either intensity_factor or "
                "target_watts (BLOCK-04)"
            )
    return reasons


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

    if name == "get_plan":
        scheduled = agendamod.between(
            conn,
            date.fromisoformat(arguments["since"]),
            date.fromisoformat(arguments["until"]),
        )
        return {
            "prescribed": [
                {
                    "id": s.id,
                    "planned_for": _serialise(s.planned_for),
                    "discipline": s.discipline,
                    "duration_min": s.minutes or None,
                    "spec": s.spec,
                    "status": s.status,
                    "described": s.describe(),
                    "activity_landed_that_day": s.done,
                }
                for s in scheduled
            ],
            "caveat": (
                "This is the plan. The calendar feeds are his own commitments and are "
                "a different thing."
            ),
        }

    if name == "write_session_events":
        # PLAN-01, written locally and published by the scheduler rather than
        # from inside the turn. The same reasoning as LOG-08: a conversation
        # must not wait on intervals.icu, and a network failure must not lose a
        # plan the athlete has just agreed to.
        block = blockmod.active(conn)
        if block is None:
            return {"available": False, "reason": "there is no active training block yet"}

        events = list(arguments["events"])
        rejected = _reject_specs(events)
        if rejected:
            # Nothing is written when anything is wrong. A partial write leaves
            # him with some sessions on the calendar and some not, and no way to
            # tell which from looking at it.
            return {
                "written": 0,
                "rejected": rejected,
                "reason": "No sessions were written. Fix these and call again.",
            }

        written = []
        with conn.transaction(), conn.cursor() as cur:
            for event in events:
                discipline = eventmod.canonical(event["discipline"])
                spec = dict(event["spec"])
                cur.execute(
                    "insert into prescriptions (block_id, planned_for, discipline, spec, "
                    "planned_load, status) values (%s, %s, %s, %s, %s, 'planned') returning id",
                    (
                        block.id,
                        _planned_at(event["planned_for"]),
                        discipline,
                        Jsonb(spec),
                        # Computed here, not left null. BLOCK-07's ramp check and
                        # GYM-05's ceiling both read this column, so a session
                        # written without it is a session the coach's own load
                        # limits cannot see — it costs nothing on paper and the
                        # platform shows the week as zero load.
                        loadmod.of_spec(discipline, spec),
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
