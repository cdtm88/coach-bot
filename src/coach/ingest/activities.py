"""Turning an upstream activity into a session row.

FIT-03 (parsed and derived kept apart), FIT-04 (deduplication), FIT-07 and FIT-08
(discipline decides analysis, every device uses one path), FIT-10 (dated from the
data), FIT-17 (coach authored activities match rather than duplicate).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from coach import clock
from coach.ingest import parse

log = logging.getLogger(__name__)

# FIT-07: power based analysis applies to riding. Everything else is logged as
# activity only, so a golf round never acquires a compliance calculation.
POWER_DISCIPLINES = frozenset({"ride", "virtualride"})

# FIT-17: activities this coach wrote upstream carry this in their external_id,
# so the one that comes back through the webhook matches the local row instead of
# creating a second.
COACH_MARKER = "coach-bot:"

# Everything intervals.icu computed is icu_ prefixed, bar a handful. Kept whole
# in `derived` and never read as a substitute for a parsed value (FIT-03).
EXTRA_DERIVED = ("hr_load", "hr_load_type", "power_load", "trainer", "commute", "device_watts")


@dataclass
class Ingested:
    session_id: int
    created: bool
    reason: str = ""


def discipline_of(activity: dict[str, Any]) -> str:
    """Normalise the upstream activity type. FIT-08: one path for every source."""
    raw = (activity.get("type") or "other").strip().lower()
    return raw.replace(" ", "")


def uses_power_analysis(discipline: str) -> bool:
    return discipline in POWER_DISCIPLINES


def derived_fields(activity: dict[str, Any]) -> dict[str, Any]:
    """The platform's own numbers, segregated by prefix."""
    return {
        k: v
        for k, v in activity.items()
        if (k.startswith("icu_") or k in EXTRA_DERIVED) and v is not None
    }


def is_coach_authored(activity: dict[str, Any]) -> bool:
    external = activity.get("external_id") or ""
    return isinstance(external, str) and external.startswith(COACH_MARKER)


def analyzed_at_of(activity: dict[str, Any]) -> datetime | None:
    """When the platform finished consolidating, or None if it has not.

    Null here means every `icu_` field on the same read is provisional. The
    webhook trigger fires on upload, which is before analysis completes, so this
    being null at ingest time is the normal case rather than the exception.
    """
    raw = activity.get("analyzed")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        log.warning("activity %s has an unreadable analyzed stamp %r", activity.get("id"), raw)
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def started_at_of(activity: dict[str, Any], tz: ZoneInfo) -> datetime:
    """FIT-10: from the activity data, never from ingest time.

    `start_date_local` is wall clock at the activity, with no offset attached.
    Reading it as UTC would move a 23:30 ride onto the wrong day, which is
    exactly what TZ-01 exists to prevent, so it is localised to the configured
    zone before anything else touches it.
    """
    raw = activity.get("start_date_local") or activity.get("start_date")
    if not raw:
        raise ValueError(f"activity {activity.get('id')} carries no start date")
    moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return clock.to_utc(moment)


def existing(conn: psycopg.Connection, external_ref: str | None, hash_: str | None) -> int | None:
    """FIT-04: match on either half of the deduplication key."""
    with conn.cursor() as cur:
        if external_ref:
            cur.execute("select id from sessions where external_ref = %s", (external_ref,))
            row = cur.fetchone()
            if row:
                return row["id"]
        if hash_:
            cur.execute("select id from sessions where content_hash = %s", (hash_,))
            row = cur.fetchone()
            if row:
                return row["id"]
    return None


def match_coach_authored(
    conn: psycopg.Connection, activity: dict[str, Any], started_at: datetime
) -> int | None:
    """FIT-17: find the local session this returning activity already represents.

    Matched on the marker in external_id where present, and otherwise on same
    discipline within the same local day, because a session logged from chat has
    no upstream id until the moment it comes back.
    """
    external = activity.get("external_id") or ""
    if isinstance(external, str) and external.startswith(COACH_MARKER):
        local_id = external[len(COACH_MARKER) :]
        if local_id.isdigit():
            with conn.cursor() as cur:
                cur.execute("select id from sessions where id = %s", (int(local_id),))
                row = cur.fetchone()
                if row:
                    return row["id"]

    with conn.cursor() as cur:
        cur.execute(
            """
            select id from sessions
            where source = 'chat' and external_ref is null
              and discipline = %s and local_date = %s
            order by id limit 1
            """,
            (discipline_of(activity), started_at.date()),
        )
        row = cur.fetchone()
    return row["id"] if row else None


def ingest(
    conn: psycopg.Connection,
    activity: dict[str, Any],
    tz: ZoneInfo,
    file_bytes: bytes | None = None,
    streams: list[dict[str, Any]] | None = None,
    backfilled: bool = False,
    source: str = "intervals",
) -> Ingested:
    """Create or update one session row.

    Parsed values come from the file if there is one, from streams otherwise, and
    are absent rather than borrowed from `icu_` fields when neither is available
    (FIT-03).
    """
    external_ref = activity.get("id")
    started_at = started_at_of(activity, tz)
    local_date = clock.local_day(started_at, tz)
    discipline = discipline_of(activity)

    hash_ = parse.content_hash(file_bytes) if file_bytes else None
    # Null until the platform finishes; see analyzed_at_of. Recorded so the
    # ACTIVITY_ANALYZED refresh knows which rows still carry provisional numbers.
    analyzed = analyzed_at_of(activity)

    parsed = parse.Parsed()
    if file_bytes:
        try:
            parsed = parse.from_fit(file_bytes)
        except parse.UnparseableActivity as exc:
            log.warning("activity %s: %s", external_ref, exc)
    elif streams:
        try:
            parsed = parse.from_streams(streams, started_at)
        except parse.UnparseableActivity as exc:
            log.info("activity %s: %s", external_ref, exc)

    # FIT-07: a golf round or a gym session gets no power figures even if the
    # device recorded some.
    if not uses_power_analysis(discipline):
        parsed.avg_power_w = parsed.np_power_w = parsed.max_power_w = None

    session_id = existing(conn, external_ref, hash_)
    if session_id is None and is_coach_authored(activity):
        session_id = match_coach_authored(conn, activity, started_at)

    values = (
        external_ref,
        hash_,
        source,
        discipline,
        activity.get("type"),
        activity.get("name"),
        started_at,
        local_date,
        parsed.duration_s or activity.get("moving_time") or activity.get("elapsed_time"),
        parsed.distance_m if parsed.distance_m is not None else activity.get("distance"),
        parsed.elevation_m
        if parsed.elevation_m is not None
        else activity.get("total_elevation_gain"),
        parsed.avg_power_w,
        parsed.np_power_w,
        parsed.max_power_w,
        parsed.avg_hr,
        parsed.max_hr,
        parsed.avg_cadence,
        parsed.sample_count,
        Jsonb(derived_fields(activity)),
        analyzed,
        analyzed is None,
        is_coach_authored(activity),
        backfilled,
    )

    with conn.transaction(), conn.cursor() as cur:
        if session_id is not None:
            cur.execute(
                """
                update sessions set
                    external_ref = coalesce(%s, external_ref),
                    content_hash = coalesce(%s, content_hash),
                    source = %s, discipline = %s, activity_type = %s, name = %s,
                    started_at = %s, local_date = %s, duration_s = %s, distance_m = %s,
                    elevation_m = %s, avg_power_w = %s, np_power_w = %s, max_power_w = %s,
                    avg_hr = %s, max_hr = %s, avg_cadence = %s, sample_count = %s,
                    derived = %s, analyzed_at = %s, derived_provisional = %s,
                    coach_authored = %s, backfilled = %s
                where id = %s
                """,
                (*values, session_id),
            )
            return Ingested(session_id, created=False, reason="matched an existing session")

        cur.execute(
            """
            insert into sessions
                (external_ref, content_hash, source, discipline, activity_type, name,
                 started_at, local_date, duration_s, distance_m, elevation_m,
                 avg_power_w, np_power_w, max_power_w, avg_hr, max_hr, avg_cadence,
                 sample_count, derived, analyzed_at, derived_provisional,
                 coach_authored, backfilled)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s)
            returning id
            """,
            values,
        )
        return Ingested(cur.fetchone()["id"], created=True)
