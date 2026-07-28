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

import hmac
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

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
class Event:
    type: str
    athlete_id: str | None
    external_ref: str | None
    timestamp: datetime
    activity: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_trigger(self) -> bool:
        return self.type == TRIGGER


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
        parsed.append(
            Event(
                type=kind,
                athlete_id=raw.get("athlete_id"),
                external_ref=(activity or {}).get("id"),
                timestamp=when,
                activity=activity,
                raw=raw,
            )
        )

    if not parsed:
        raise Rejected("no recognised events in payload")
    return parsed


def record(conn: psycopg.Connection, event: Event, accepted: bool, reason: str = "") -> bool:
    """Record a delivery. Returns False when this exact event was already seen.

    FIT-02's replay safety. The unique index covers (type, external_ref,
    timestamp), so a replayed body collides and this returns False without the
    caller having to check first.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into webhook_deliveries
                (event_type, athlete_id, external_ref, event_timestamp, accepted, reason)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (event_type, external_ref, event_timestamp)
                where external_ref is not null
            do nothing
            returning id
            """,
            (event.type, event.athlete_id, event.external_ref, event.timestamp, accepted, reason),
        )
        return cur.fetchone() is not None


def accept(
    conn: psycopg.Connection, payload: dict[str, Any], secret: str | None = None
) -> list[Event]:
    """Verify, parse and de-replay a payload. Returns the events to act on.

    Raises :class:`Rejected` for anything unverified. Replayed events are dropped
    silently, because a redelivery is normal operation rather than an error: the
    upstream retries when our endpoint is slow.
    """
    verify(payload, secret)
    fresh: list[Event] = []
    for event in parse(payload):
        if not record(conn, event, accepted=True):
            log.info("ignoring replayed %s for %s", event.type, event.external_ref)
            continue
        fresh.append(event)
    return fresh
