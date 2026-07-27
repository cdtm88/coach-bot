"""The nightly consolidation pass.

CONS-01 to CONS-10, plus SAFE-02 and SAFE-03. This is the only writer to long
term memory apart from the SAFE-06 athlete path.

The ten steps are design section 6, in order. The model appears at exactly one
of them — step 2, proposing candidate diffs. Everything that decides what lands
is code.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from coach.consolidation import conflict
from coach.memory import facts as factmod
from coach.memory import keys as keymod
from coach.memory import notes as notemod
from coach.memory import state as statemod

log = logging.getLogger(__name__)

# CONS-02: the model emits candidate diffs as strict JSON. Enforced as a
# structured output schema so a malformed emission is caught at the tool-call
# layer and retried, rather than parsed hopefully.
DIFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "diffs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {},
                    "provenance": {
                        "type": "string",
                        "enum": ["stated", "observed", "computed", "inferred"],
                    },
                    "reason": {"type": "string"},
                    "evidence": {
                        "type": "object",
                        "description": "Message ids, session ids or fact ids this rests on.",
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["key", "value", "provenance", "reason", "evidence"],
                "additionalProperties": False,
            },
        },
        "day_summary": {"type": "string"},
        "rolling_summary": {"type": "string"},
        "open_threads": {"type": "array", "items": {"type": "string"}},
        "last_topic": {"type": "string"},
    },
    "required": ["diffs", "day_summary", "rolling_summary", "open_threads"],
    "additionalProperties": False,
}


class MalformedProposal(ValueError):
    """CONS-02: the model's output did not match the diff schema."""


@dataclass
class Inputs:
    """Step 1: what the pass reads."""

    consolidated_on: date
    messages: list[dict[str, Any]] = field(default_factory=list)
    telemetry: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    active_facts: list[factmod.Fact] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.messages or self.telemetry or self.pending)


@dataclass
class Result:
    consolidated_on: date
    applied: int = 0
    rejected: int = 0
    held: int = 0
    mentions: int = 0
    decayed: int = 0
    skipped: bool = False
    reasons: list[str] = field(default_factory=list)


def gather(conn: psycopg.Connection, consolidated_on: date, tz_offset: timedelta) -> Inputs:
    """Step 1. CONS-01: the day's messages, telemetry, pending writes, facts.

    The window is the local day, per TZ-01 — a session ridden at 23:30 local
    belongs to that local day whatever UTC says.
    """
    start = datetime.combine(consolidated_on, datetime.min.time()) - tz_offset
    end = start + timedelta(days=1)

    with conn.cursor() as cur:
        cur.execute(
            "select id, role, body, modality, occurred_at from messages "
            "where occurred_at >= %s and occurred_at < %s order by occurred_at",
            (start, end),
        )
        messages = cur.fetchall()

        # Telemetry deltas: notes written by ingest during the window. Feeds
        # land from P03 onward; before then this is legitimately empty.
        cur.execute(
            "select id, kind, body from notes where occurred_on = %s and kind = 'observation'",
            (consolidated_on,),
        )
        telemetry = cur.fetchall()

    return Inputs(
        consolidated_on=consolidated_on,
        messages=messages,
        telemetry=telemetry,
        pending=statemod.pending(conn),
        active_facts=factmod.active(conn),
    )


def _open_run(conn: psycopg.Connection, inputs: Inputs) -> bool:
    """Claim the run for this date. False when it has already succeeded.

    CONS-10 and OBS-08 together: at most once per date, at most one retry.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "select status, attempts from consolidation_runs where consolidated_on = %s for update",
            (inputs.consolidated_on,),
        )
        existing = cur.fetchone()

        if existing is not None:
            if existing["status"] == "succeeded":
                return False
            if existing["attempts"] >= 2:
                log.warning(
                    "consolidation for %s already failed twice; waiting for the next night",
                    inputs.consolidated_on,
                )
                return False
            cur.execute(
                "update consolidation_runs set attempts = attempts + 1, status = 'running', "
                "started_at = now() where consolidated_on = %s",
                (inputs.consolidated_on,),
            )
            return True

        cur.execute(
            """
            insert into consolidation_runs
                (consolidated_on, messages_in, telemetry_in, pending_in, active_facts_in)
            values (%s, %s, %s, %s, %s)
            """,
            (
                inputs.consolidated_on,
                len(inputs.messages),
                len(inputs.telemetry),
                len(inputs.pending),
                len(inputs.active_facts),
            ),
        )
        return True


def _close_run(
    conn: psycopg.Connection, on: date, status: str, result: Result, error: str | None = None
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            update consolidation_runs
            set status = %s, finished_at = now(), diffs_applied = %s,
                diffs_rejected = %s, diffs_proposed = %s, error = %s
            where consolidated_on = %s
            """,
            (
                status,
                result.applied,
                result.rejected,
                result.applied + result.rejected + result.held,
                error,
                on,
            ),
        )


def validate(conn: psycopg.Connection, raw: Any) -> dict[str, Any]:
    """CONS-02: reject anything that is not a well formed proposal."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MalformedProposal(f"not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise MalformedProposal(f"expected an object, got {type(raw).__name__}")

    for required in ("diffs", "day_summary"):
        if required not in raw:
            raise MalformedProposal(f"missing {required!r}")
    if not isinstance(raw["diffs"], list):
        raise MalformedProposal("diffs must be a list")

    for diff in raw["diffs"]:
        missing = {"key", "value", "provenance", "reason"} - set(diff)
        if missing:
            raise MalformedProposal(f"diff missing {sorted(missing)}")
        if diff["provenance"] not in factmod.PROVENANCE:
            raise MalformedProposal(f"bad provenance {diff['provenance']!r}")
    return raw


def apply_diffs(conn: psycopg.Connection, diffs: list[dict[str, Any]], result: Result) -> None:
    """Steps 3 to 5, and step 8.

    Validation, then the conflict matrix, then the write. The model's reason
    string rides along into fact_events; its opinion on precedence does not.
    """
    for diff in diffs:
        key = diff["key"]
        try:
            fact_key = keymod.load(conn, key)
        except keymod.UnknownKey:
            result.rejected += 1
            result.reasons.append(f"{key}: not in the controlled vocabulary")
            log.warning("rejected diff for unknown key %s", key)
            continue

        confidence = Decimal(str(diff.get("confidence", 1.0)))
        current = factmod.active_for(conn, key)
        decision = conflict.resolve(fact_key, current, diff["provenance"], confidence)

        if decision.outcome is conflict.Outcome.REJECT:
            result.rejected += 1
            result.reasons.append(f"{key}: {decision.reason}")
            if fact_key.safety:
                # SAFE-02 wants the refused attempt in fact_events with actor
                # and reason. ratify() records it that way, then raises.
                try:
                    factmod.ratify(
                        conn, key, diff["value"], diff["provenance"], reason=diff["reason"]
                    )
                except factmod.SafetyKeyViolation:
                    pass
            continue

        if decision.outcome is conflict.Outcome.HOLD:
            result.held += 1
            result.reasons.append(f"{key}: {decision.reason}")
            statemod.queue_write(conn, diff, origin="consolidation")
            continue

        try:
            written = factmod.ratify(
                conn,
                key,
                diff["value"],
                diff["provenance"],
                reason=f"{diff['reason']} [{decision.reason}]",
                evidence=diff.get("evidence"),
                confidence=confidence,
            )
        except keymod.WrongValueType as exc:
            result.rejected += 1
            result.reasons.append(f"{key}: {exc}")
            continue

        result.applied += 1

        if decision.mentions:
            # Design section 8: observed superseding stated is mentioned once,
            # as an aside, expiring after 72 hours. The fact change stands
            # whether or not the mention is ever delivered.
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "update facts set mention_pending = true, mention_expires = now() + "
                    "interval '72 hours' where id = %s",
                    (written.id,),
                )
            result.mentions += 1


def run(
    conn: psycopg.Connection,
    consolidated_on: date,
    propose: Callable[[Inputs], Any],
    tz_offset: timedelta = timedelta(0),
) -> Result:
    """The nightly pass. Returns what it did.

    CONS-10: re-running for the same date produces no duplicate facts or notes.
    A date that already succeeded returns immediately with ``skipped`` set.
    """
    inputs = gather(conn, consolidated_on, tz_offset)
    result = Result(consolidated_on=consolidated_on)

    if not _open_run(conn, inputs):
        result.skipped = True
        return result
    conn.commit()

    if inputs.is_empty:
        # CONS-09 writes a day summary for every day with at least one message
        # or telemetry event. A silent day gets neither a summary nor a diff.
        _close_run(conn, consolidated_on, "succeeded", result)
        conn.commit()
        return result

    try:
        # Step 2, the one place the model appears. CONS-02: malformed output is
        # retried once, then logged as a failed run without partial writes.
        try:
            proposal = validate(conn, propose(inputs))
        except MalformedProposal as first:
            log.warning("malformed proposal for %s, retrying once: %s", consolidated_on, first)
            proposal = validate(conn, propose(inputs))

        apply_diffs(conn, proposal["diffs"], result)  # steps 3 to 5, and 8

        # Step 7. Upsert rather than insert so a re-run rewrites (CONS-09/10).
        notemod.upsert_day_summary(conn, proposal["day_summary"], consolidated_on)

        # MEM-09: today_uncommitted clears, the continuity fields are
        # regenerated from the day just consolidated. The row survives.
        statemod.clear_for_consolidation(
            conn,
            rolling_summary=proposal.get("rolling_summary"),
            open_threads=proposal.get("open_threads"),
            last_topic=proposal.get("last_topic"),
        )

        # Ratified pending writes are spent.
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "update pending_writes set status = 'ratified' where status = 'pending' "
                "and created_at < now()"
            )

        result.decayed = factmod.apply_decay(conn)  # step 9

    except Exception as exc:  # noqa: BLE001 - the run is the boundary
        conn.rollback()
        _close_run(conn, consolidated_on, "failed", result, error=str(exc))
        conn.commit()
        log.error("consolidation for %s failed: %s", consolidated_on, exc)
        raise

    _close_run(conn, consolidated_on, "succeeded", result)
    conn.commit()
    return result
