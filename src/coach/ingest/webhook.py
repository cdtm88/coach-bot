"""The intervals.icu webhook receiver.

FIT-01: ingest is triggered by `ACTIVITY_UPLOADED`. FIT-02 and SEC-02: payloads
are verified against the shared secret and are replay safe.

There is no HMAC signature on this API. intervals.icu authenticates the callback
by putting a shared secret in the body, which means two things that would be
wrong to assume otherwise. The check is a constant time comparison rather than a
digest, and replay protection has to come from recording deliveries here, because
an attacker replaying a captured body replays a valid secret with it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

# FIT-01 is explicit that ACTIVITY_UPLOADED is the trigger. ACTIVITY_ANALYZED is
# held 60 seconds upstream so multiple events for one activity consolidate, which
# would spend a fifth of the PERF-03 budget before any work started.
TRIGGER = "ACTIVITY_UPLOADED"

# Recorded and acted on, but not the ingest trigger.
KNOWN = {
    TRIGGER,
    "ACTIVITY_ANALYZED",
    "ACTIVITY_DELETED",
    "CALENDAR_UPDATED",
    "WELLNESS_UPDATED",
    "SPORT_SETTINGS_UPDATED",
}


class Rejected(Exception):
    """The payload is not a webhook this system will act on."""


@dataclass(frozen=True)
class CalendarChange:
    """The nested arrays a CALENDAR_UPDATED delivery carries instead of `activity`."""

    updated: list[str] = dc_field(default_factory=list)
    deleted: list[str] = dc_field(default_factory=list)
    oauth_client_id: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class Event:
    type: str
    athlete_id: str | None
    external_ref: str | None
    timestamp: datetime
    activity: dict[str, Any] | None = None
    calendar: CalendarChange | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_trigger(self) -> bool:
        return self.type == TRIGGER

    @property
    def delivery_key(self) -> str:
        """FIT-02's replay identity, defined for every event type.

        The previous key was (type, activity id, timestamp) enforced by an index
        partial on a non-null activity id, so a calendar delivery — which has no
        activity — carried a null and slipped past the uniqueness check entirely.
        Harmless while non-trigger events were dropped; a duplicate-application
        bug the moment PLAN-06 starts acting on them.

        Hashing the identifying fields gives one non-null key per event whatever
        its shape, so the index no longer has to be partial and nothing is
        exempt.
        """
        parts = [
            self.type,
            self.athlete_id or "",
            self.external_ref or "",
            self.timestamp.isoformat(),
        ]
        if self.calendar is not None:
            # Two calendar deliveries a second apart are different events; the
            # ids they name are what distinguish them.
            parts.append(",".join(sorted(self.calendar.updated)))
            parts.append(",".join(sorted(self.calendar.deleted)))
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _configured_secret(secret: str | None) -> str:
    value = secret if secret is not None else os.environ.get("INTERVALS_WEBHOOK_SECRET")
    if not value:
        raise Rejected(
            "INTERVALS_WEBHOOK_SECRET is not set. Without it every payload would be "
            "accepted, so ingest refuses to run rather than accepting anything."
        )
    return value


def verify(payload: dict[str, Any], secret: str | None = None) -> None:
    """SEC-02 and FIT-02: reject anything not carrying the shared secret.

    Compared in constant time. An absent secret is rejected exactly like a wrong
    one, so a payload that simply omits the field cannot slip through a truthiness
    check.
    """
    expected = _configured_secret(secret)
    supplied = payload.get("secret")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
        raise Rejected("webhook secret missing or incorrect")


def parse(payload: dict[str, Any]) -> list[Event]:
    """Split a verified payload into events.

    One POST can carry several events; the wire format is a list under `events`.
    """
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise Rejected("payload carried no events")

    parsed: list[Event] = []
    for raw in events:
        kind = raw.get("type")
        if kind not in KNOWN:
            log.info("ignoring unknown webhook type %r", kind)
            continue

        stamp = raw.get("timestamp")
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError) as exc:
            raise Rejected(f"event timestamp {stamp!r} is not a timestamp") from exc

        activity = raw.get("activity") if isinstance(raw.get("activity"), dict) else None

        # CALENDAR_UPDATED does not carry a flat `activity`. It nests an `events`
        # array and a `deleted_events` array inside the event object, so reading
        # it with the activity shape yields an event with no identity at all.
        calendar = _calendar_payload(raw) if kind == "CALENDAR_UPDATED" else None

        parsed.append(
            Event(
                type=kind,
                athlete_id=raw.get("athlete_id"),
                external_ref=(activity or {}).get("id"),
                timestamp=when,
                activity=activity,
                calendar=calendar,
                raw=raw,
            )
        )

    if not parsed:
        raise Rejected("no recognised events in payload")
    return parsed


def _calendar_payload(raw: dict[str, Any]) -> CalendarChange:
    """The nested shape CALENDAR_UPDATED uses.

    `oauth_client_id` is carried through because PLAN-06 has to tell the
    athlete's edits from the coach's own writes echoing back. Note that a delete
    can race an update upstream, so the same event id can appear in both arrays
    across two deliveries and appear to resurrect; whatever consumes this must
    reconcile against a fresh read rather than trusting the arrays as truth.
    """

    def ids(key: str) -> list[str]:
        rows = raw.get(key)
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            if isinstance(row, dict):
                value = row.get("id") or row.get("external_id")
                if value is not None:
                    out.append(str(value))
            elif row is not None:
                out.append(str(row))
        return out

    return CalendarChange(
        updated=ids("events"),
        deleted=ids("deleted_events"),
        oauth_client_id=raw.get("oauth_client_id"),
        external_id=raw.get("external_id"),
    )


def enqueue(conn: psycopg.Connection, event: Event) -> int | None:
    """Queue a delivery for processing. Returns None when it is a replay.

    FIT-02's replay safety and PERF-03's budget are the same mechanism now. The
    row is written `pending` rather than `done`, so the record of having seen a
    delivery is not also a claim to have handled it. That distinction is what was
    wrong before: a delivery marked accepted up front could never be retried,
    because the upstream's redelivery collided with the record and was discarded
    as a replay while the work had actually failed.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into webhook_deliveries
                (delivery_key, event_type, athlete_id, external_ref, event_timestamp,
                 accepted, status, payload)
            values (%s, %s, %s, %s, %s, true, 'pending', %s)
            on conflict (delivery_key) do nothing
            returning id
            """,
            (
                event.delivery_key,
                event.type,
                event.athlete_id,
                event.external_ref,
                event.timestamp,
                Jsonb(event.raw or {}),
            ),
        )
        row = cur.fetchone()
    return row["id"] if row else None


def claim(conn: psycopg.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """Take the next pending deliveries, oldest first.

    `for update skip locked` so a second worker never picks up a row already in
    flight. There is one worker today; the lock costs nothing and removes the
    class of bug that appears the moment there are two.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            update webhook_deliveries set status = 'running', attempts = attempts + 1
            where id in (
                select id from webhook_deliveries
                where status = 'pending'
                order by received_at
                limit %s
                for update skip locked
            )
            returning id, event_type, athlete_id, external_ref, event_timestamp, payload, attempts
            """,
            (limit,),
        )
        return cur.fetchall()


def finish(
    conn: psycopg.Connection, delivery_id: int, ok: bool, reason: str = "", max_attempts: int = 5
) -> str:
    """Close out a claimed delivery. Returns the status it landed in.

    A failure goes back to `pending` so the next tick retries it, until the
    attempt ceiling. Past that it is `failed` and stays there: something is
    wrong that retrying will not fix, and the six hourly reconcile is the
    backstop that stops a poisoned delivery from losing the ride.
    """
    with conn.transaction(), conn.cursor() as cur:
        if ok:
            status = "done"
        else:
            cur.execute("select attempts from webhook_deliveries where id = %s", (delivery_id,))
            row = cur.fetchone()
            status = "failed" if row and row["attempts"] >= max_attempts else "pending"
        cur.execute(
            "update webhook_deliveries set status = %s, reason = %s, last_error = %s, "
            "processed_at = case when %s then now() else processed_at end where id = %s",
            (status, reason[:500], None if ok else reason[:500], status != "pending", delivery_id),
        )
    return status


def accept(
    conn: psycopg.Connection, payload: dict[str, Any], secret: str | None = None
) -> list[Event]:
    """Verify, parse and enqueue a payload. Returns the events queued.

    Raises :class:`Rejected` for anything unverified. Replayed events are dropped
    silently, because a redelivery is normal operation rather than an error: the
    upstream retries when our endpoint is slow, and on any non-2xx it retries
    with exponential backoff.
    """
    verify(payload, secret)
    fresh: list[Event] = []
    for event in parse(payload):
        if enqueue(conn, event) is None:
            log.info("ignoring replayed %s for %s", event.type, event.external_ref)
            continue
        fresh.append(event)
    return fresh
