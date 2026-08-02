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


def paired_event_id_of(activity: dict[str, Any]) -> str | None:
    """PLAN-07: the platform's link from this activity to the planned event it met.

    Kept as text rather than an int. Upstream event ids are numeric today, and
    every other upstream identifier in this schema is text for the same reason —
    an id is a name, arithmetic is never done on it, and a type change upstream
    should not need a migration here.

    Absent on most activities, and that is not a failure: nothing pairs a ride
    that had no planned workout, and a file arriving through the watched folder
    never went past the platform to be paired.
    """
    raw = activity.get("paired_event_id")
    return str(raw) if raw not in (None, "", 0) else None


def _blank_is_absent(value: str | None) -> str | None:
    return (value or "").strip() or None


def name_of(activity: dict[str, Any]) -> str | None:
    """The title upstream gave the ride, or None when it gave none.

    Blank is None rather than a name. intervals.icu serves `"name": ""` for an
    activity the athlete never titled, and an empty string stored is still a
    value: it would win a coalesce against a perfectly good existing name.
    """
    raw = activity.get("name")
    return _blank_is_absent(raw) if isinstance(raw, str) else None


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


def stored_discipline(conn: psycopg.Connection, session_id: int) -> str | None:
    """What the row already says it is. FIT-07 is decided on this, not on a guess."""
    with conn.cursor() as cur:
        cur.execute("select discipline from sessions where id = %s", (session_id,))
        row = cur.fetchone()
    return row["discipline"] if row else None


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
    fallback_name: str | None = None,
) -> Ingested:
    """Create or update one session row.

    Parsed values come from the file if there is one, from streams otherwise, and
    are absent rather than borrowed from `icu_` fields when neither is available
    (FIT-03).

    `fallback_name` is for callers that have no name to offer, only something
    name-shaped — the watched folder path has a file stem and nothing else. It
    names a row that would otherwise have no name and never replaces one that
    already does.

    The ordering there is load bearing, because the two ingest paths meet on one
    row. A ride comes back from the API as "Zwift - Race: Stage 3" and sits on
    disk as `2026-07-27-181000.fit`; the poll's reconcile stores it, then the
    watched folder scan finds the identical bytes, matches them on content hash
    (FIT-04) and updates the row it just found. The stem is the only name that
    path has, so before this it was the name that survived, and the coach
    discussed "2026-07-27-181000" with the athlete.
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

    session_id = existing(conn, external_ref, hash_)
    if session_id is None and is_coach_authored(activity):
        session_id = match_coach_authored(conn, activity, started_at)

    # Whether this call carries the platform's own view of the activity. FIT-14's
    # watched folder does not: it synthesises a dict out of a file and a
    # timestamp, so its `type` is a default rather than a reading and its
    # `source` describes the reader rather than the row. Those are claims only an
    # upstream read can make, and this path routinely lands on a row the poll
    # already created — relabelling a Zwift ride as an outdoor `Ride` off
    # `local_file` is the same lie as renaming it to the filename.
    upstream_read = external_ref is not None

    # FIT-07 follows the discipline the row will actually have, which is not this
    # call's when this call is not the one allowed to set it. Reading the stored
    # value rather than assuming: a golf round matched on content hash must not
    # acquire power figures from a scan that thinks everything is a ride.
    effective = discipline
    if session_id is not None and not upstream_read:
        effective = stored_discipline(conn, session_id) or discipline

    # FIT-07: a golf round or a gym session gets no power figures even if the
    # device recorded some.
    if not uses_power_analysis(effective):
        parsed.avg_power_w = parsed.np_power_w = parsed.max_power_w = None

    derived = derived_fields(activity)

    values: dict[str, Any] = {
        "external_ref": external_ref,
        "content_hash": hash_,
        "source": source,
        "discipline": discipline,
        "activity_type": activity.get("type"),
        # The insert takes the best name available; the update prefers upstream's
        # and keeps what is already there rather than falling to the stem.
        "name": name_of(activity) or _blank_is_absent(fallback_name),
        "upstream_name": name_of(activity),
        "stem": _blank_is_absent(fallback_name),
        "started_at": started_at,
        "local_date": local_date,
        "duration_s": parsed.duration_s
        or activity.get("moving_time")
        or activity.get("elapsed_time"),
        "distance_m": parsed.distance_m
        if parsed.distance_m is not None
        else activity.get("distance"),
        "elevation_m": parsed.elevation_m
        if parsed.elevation_m is not None
        else activity.get("total_elevation_gain"),
        "avg_power_w": parsed.avg_power_w,
        "np_power_w": parsed.np_power_w,
        "max_power_w": parsed.max_power_w,
        "avg_hr": parsed.avg_hr,
        "max_hr": parsed.max_hr,
        "avg_cadence": parsed.avg_cadence,
        "sample_count": parsed.sample_count,
        "derived": Jsonb(derived),
        "analyzed_at": analyzed,
        "derived_provisional": analyzed is None,
        "coach_authored": is_coach_authored(activity),
        "backfilled": backfilled,
        # PLAN-07: the platform's own link from this activity to the planned event
        # it satisfied. Better evidence than date and discipline when present, and
        # absent for anything that had no planned workout — which is most rides.
        "paired_event_id": paired_event_id_of(activity),
        "session_id": session_id,
    }

    with conn.transaction(), conn.cursor() as cur:
        if session_id is not None:
            cur.execute(update_statement(parsed, effective, derived, upstream_read), values)
            return Ingested(session_id, created=False, reason="matched an existing session")

        cur.execute(INSERT, values)
        return Ingested(cur.fetchone()["id"], created=True)


# Computed from samples this call read, and from nothing else. `duration_s`,
# `distance_m` and `elevation_m` are not here: they fall back to the activity's
# own fields, so a call with no file still has something true to say about them.
SAMPLE_COLUMNS = (
    "avg_power_w",
    "np_power_w",
    "max_power_w",
    "avg_hr",
    "max_hr",
    "avg_cadence",
    "sample_count",
)

POWER_COLUMNS = ("avg_power_w", "np_power_w", "max_power_w")

INSERT = """
    insert into sessions
        (external_ref, content_hash, source, discipline, activity_type, name,
         started_at, local_date, duration_s, distance_m, elevation_m,
         avg_power_w, np_power_w, max_power_w, avg_hr, max_hr, avg_cadence,
         sample_count, derived, analyzed_at, derived_provisional,
         coach_authored, backfilled, paired_event_id)
    values
        (%(external_ref)s, %(content_hash)s, %(source)s, %(discipline)s,
         %(activity_type)s, %(name)s, %(started_at)s, %(local_date)s, %(duration_s)s,
         %(distance_m)s, %(elevation_m)s, %(avg_power_w)s, %(np_power_w)s,
         %(max_power_w)s, %(avg_hr)s, %(max_hr)s, %(avg_cadence)s, %(sample_count)s,
         %(derived)s, %(analyzed_at)s, %(derived_provisional)s, %(coach_authored)s,
         %(backfilled)s, %(paired_event_id)s)
    returning id
"""


def update_statement(
    parsed: parse.Parsed, discipline: str, derived: dict[str, Any], upstream_read: bool
) -> str:
    """The update, with each block written only if this call has one.

    A call with no file and no streams has nothing to say about the values FIT-03
    computes from samples, and saying it anyway means writing null over numbers
    that came from real data. `reconcile.run(fetch_files=False)` did exactly that,
    and so does any re-ingest that arrives while the original is briefly
    unavailable: the ride keeps its row and quietly loses its power, heart rate
    and cadence. The coach then reads a ride with no numbers and either says so or
    works around it, and either way it is describing our write, not the ride.

    When samples *were* read the columns are written unconditionally, nulls
    included. That direction is not symmetrical and must not be: a file with no
    power meter is a positive statement that this ride had no power, and
    coalescing it would leave yesterday's figure standing.

    The `derived` block is gated the same way and for the same reason. A file
    from the watched folder carries no `icu_` fields at all, so writing its empty
    block over an upstream read erases `icu_training_load` — which is what the
    load rollups sum. The coach would then quote a seven day load that is short
    by a ride nothing appears to be missing from. `analyzed_at` and
    `derived_provisional` describe that block, so they move with it.

    What the row *is* — its source, its type, whether the coach wrote it — is
    left to a call that read the platform. `discipline` is the one that bites:
    the watched folder labels every file a ride, so a golf round matched on
    content hash used to come back a ride, take power figures FIT-07 exists to
    withhold, and start being scored for compliance.
    """
    sets = [
        "external_ref = coalesce(%(external_ref)s, external_ref)",
        "content_hash = coalesce(%(content_hash)s, content_hash)",
        "name = coalesce(%(upstream_name)s, name, %(stem)s)",
        "started_at = %(started_at)s",
        "local_date = %(local_date)s",
        # Same reasoning as the sample columns, one step weaker: these fall back
        # to the activity's own fields, so null here means neither the file nor
        # the platform knew — which is not a reason to forget what we knew.
        "duration_s = coalesce(%(duration_s)s, duration_s)",
        "distance_m = coalesce(%(distance_m)s, distance_m)",
        "elevation_m = coalesce(%(elevation_m)s, elevation_m)",
        "backfilled = %(backfilled)s",
        "paired_event_id = coalesce(%(paired_event_id)s, paired_event_id)",
    ]
    if upstream_read:
        sets += [
            "source = %(source)s",
            "discipline = %(discipline)s",
            "activity_type = %(activity_type)s",
            "coach_authored = %(coach_authored)s",
        ]
    if derived:
        sets += [
            "derived = %(derived)s",
            "analyzed_at = %(analyzed_at)s",
            "derived_provisional = %(derived_provisional)s",
        ]

    powered = uses_power_analysis(discipline)
    if parsed.sample_count:
        written = (
            SAMPLE_COLUMNS
            if powered
            else tuple(c for c in SAMPLE_COLUMNS if c not in POWER_COLUMNS)
        )
        sets += [f"{column} = %({column})s" for column in written]
    if not powered:
        # FIT-07 holds whatever this call read, so a discipline corrected upstream
        # to golf drops the power figures it should never have carried. Kept out
        # of the loop above because Postgres refuses two assignments to one column.
        sets += [f"{column} = null" for column in POWER_COLUMNS]

    return f"update sessions set {', '.join(sets)} where id = %(session_id)s"
