"""The intervals.icu wellness feed.

HLTH-04: body mass is read from here and never from HealthKit. RECOV-01 and
RECOV-02 read the same rows for the recovery fields in P05, which is why every
field the feed carries is stored now rather than only the one P04 uses — a second
pass over the same endpoint to pick up columns we deliberately dropped would cost
a day and answer nothing.

**What the live feed actually carries.** Read across 21 days on 28 July 2026:

    sleepSecs, sleepScore, restingHR, hrv, readiness, respiration, spO2
                                          13 of 22 days populated
    hrvSDNN                                0 of 22 days, always null
    weight                                 0 of 22 days, always null

So `hrvSDNN` is dropped from RECOV-04's deviation exactly as RECOV-02 provides
for, and body mass has no source at all until MacroLog's HealthBridge writes it.
That resolved open items 1 and 3. It also quietly resolved a risk: nothing
upstream writes `weight`, so there is no connected provider to resync over an API
written value, and the `locked: true` one way door in docs/intervals-api.md is
less necessary than it looked.

RECOV-05: reads are idempotent across overlapping ranges. The upsert below is
keyed on the date, so re-reading a fortnight rewrites fourteen rows rather than
adding fourteen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from coach.health import bodymass, recovery
from coach.ingest import client as clientmod

log = logging.getLogger(__name__)

# How far back a routine sync asks for. Wide enough that a provider filling in
# yesterday's sleep after the fact is picked up, narrow enough to stay one cheap
# call. RECOV-05 makes the overlap free.
DEFAULT_LOOKBACK_DAYS = 21

# The wellness properties we store, mapped to their columns. Written out rather
# than derived from the payload keys, so a field the platform adds does not
# silently become a column and, more to the point, so `bodyFat` cannot: HLTH-14
# excludes body fat from v1 and the exclusion has to be visible somewhere a
# reviewer will look.
#
# HLTH-04's field and RECOV-02's six. Absence here is reported, because these are
# the ones a requirement expects to find.
RECOVERY_FIELDS: dict[str, str] = {
    "weight": "weight_kg",
    "sleepSecs": "sleep_secs",
    "sleepScore": "sleep_score",
    "sleepQuality": "sleep_quality",
    "restingHR": "resting_hr",
    "hrv": "hrv",
    "hrvSDNN": "hrv_sdnn",
    "readiness": "readiness",
    "respiration": "respiration",
    "spO2": "spo2",
}

# The platform's training load curves. RECOV-06 needs `atlLoad` — the day's load
# — to tell a missed session from a missing upload. `ctl` and `atl` are the
# fitness and fatigue curves that make a day's load readable; both are the
# platform's arithmetic and neither is ever substituted for something computed
# locally, per FIT-03.
LOAD_FIELDS: dict[str, str] = {
    "ctl": "ctl",
    "atl": "atl",
    "ctlLoad": "ctl_load",
    "atlLoad": "atl_load",
    "rampRate": "ramp_rate",
}

FIELDS: dict[str, str] = {**RECOVERY_FIELDS, **LOAD_FIELDS}

INTEGER_COLUMNS = frozenset({"sleep_secs", "resting_hr"})

# Read on 28 July 2026 and deliberately not stored. `tempWeight` is populated on
# every day, which makes it look like the body mass source HLTH-04 is missing —
# and it is not. Across 22 days it carried **two distinct values one kilogram
# apart**, alternating between them. That is a carried-forward or rounded stand-in
# the platform keeps for its power-to-weight arithmetic, not a measurement
# series, and `tempRestingHR` behaves the same way beside a `restingHR` that
# carries real values on 13 days.
#
# This is the harmful case open item 1 was written to catch, arriving under a
# field name the item did not anticipate. Fitting a 28 day trend on it would
# produce a confident line through two numbers and look exactly like data. Named
# here so that the next person to notice the weight trend is empty finds the
# reason rather than the workaround.
NEVER_STORED = ("tempWeight", "tempRestingHR")


@dataclass
class Synced:
    """What one sync did."""

    days: int = 0
    readings: int = 0
    held: int = 0
    fields_absent: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


def _number(value: Any) -> Decimal | None:
    """Coerce a JSON number, treating anything unparseable as absent.

    RECOV-02 records an absent field as absent rather than as a zero. A string
    that will not parse is the same situation as a null: the feed did not carry
    a usable value, and inventing one would put a fabricated reading into a
    weight trend.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def store(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> Synced:
    """Upsert wellness days. RECOV-05: idempotent across overlapping ranges."""
    result = Synced()
    populated: set[str] = set()

    for row in rows:
        day = _day_of(row)
        if day is None:
            result.errors.append(f"wellness row without a usable date: {sorted(row)[:6]}")
            continue

        values: dict[str, Decimal | int | None] = {}
        for source, column in FIELDS.items():
            number = _number(row.get(source))
            if number is not None:
                populated.add(source)
            values[column] = (
                int(number) if number is not None and column in INTEGER_COLUMNS else number
            )

        columns = list(values)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                f"""
                insert into wellness (local_date, {", ".join(columns)}, locked, raw, fetched_at)
                values (%s, {", ".join(["%s"] * len(columns))}, %s, %s, now())
                on conflict (local_date) do update set
                    {", ".join(f"{c} = excluded.{c}" for c in columns)},
                    locked = excluded.locked,
                    raw = excluded.raw,
                    fetched_at = now()
                """,
                (day, *values.values(), bool(row.get("locked")), Jsonb(row)),
            )
        result.days += 1

        # HLTH-04: the weight on a wellness day is a body mass reading. It goes
        # through record() rather than straight into the table so HLTH-11's
        # outlier gate applies to it on the way in.
        weight = values["weight_kg"]
        if weight is not None and weight > 0:
            recorded = bodymass.record(conn, day, weight, source="wellness")
            result.readings += 1
            if recorded.held_for_confirmation:
                result.held += 1

    # Only the fields a requirement expects. A load curve the platform did not
    # publish is not a RECOV-02 absence and reporting it as one would bury the
    # ones that matter.
    result.fields_absent = set(RECOVERY_FIELDS) - populated
    return result


def _day_of(row: dict[str, Any]) -> date | None:
    """The date of a wellness row.

    The feed keys these on `id`, which is the local date as a string, and also
    carries an explicit `date` on some responses. Both are read because the two
    have differed between versions of the spec and a row landing on the wrong
    date is a reading attributed to the wrong day.
    """
    for key in ("id", "date", "local_date"):
        raw = row.get(key)
        if isinstance(raw, str):
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                continue
        if isinstance(raw, date):
            return raw
    return None


def sync(
    conn: psycopg.Connection,
    client: clientmod.Intervals,
    today: date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Synced:
    """Read a window of wellness and store it. One API call.

    Errors are collected rather than raised: this runs on a loop beside ingest,
    and a wellness feed that is down must not take activity ingest with it.
    """
    try:
        rows = client.wellness(today - timedelta(days=lookback_days), today)
    except clientmod.IntervalsError as exc:
        result = Synced(errors=[str(exc)])
        _record_feed(conn, "wellness", ok=False, error=str(exc))
        return result

    result = store(conn, rows)
    if result.fields_absent:
        # RECOV-02: a field the feed does not carry is "noted once in the phase
        # notes". This is that note, and it is a log line rather than a message
        # because it is a fact about the integration, not about the athlete.
        log.info("wellness fields absent across this window: %s", sorted(result.fields_absent))

    bodymass.recompute(conn, today)
    recovery.recompute(conn, today)  # RECOV-04
    _record_feed(conn, "wellness", ok=True)
    _record_body_mass_feed(conn)
    return result


def _record_feed(conn: psycopg.Connection, name: str, ok: bool, error: str | None = None) -> None:
    """OBS-05: last success per inbound feed, surfaced as staleness by CHAT-09."""
    with conn.transaction(), conn.cursor() as cur:
        if ok:
            cur.execute(
                "update feeds set last_success_at = now(), last_error = null where name = %s",
                (name,),
            )
        else:
            cur.execute("update feeds set last_error = %s where name = %s", (error, name))


def _record_body_mass_feed(conn: psycopg.Connection) -> None:
    """The body mass feed is stale 12 days after a *reading*, not after a fetch.

    Two feeds run off one call and they mean different things. `wellness` is
    healthy when the endpoint answered; `body_mass` is healthy when a reading
    arrived. Collapsing them would make a working wellness feed hide a weight
    pipeline that has been dead for a month — which, on the live account as of
    28 July 2026, is exactly the situation.
    """
    last = bodymass.latest_reading_at(conn)
    if last is None:
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update feeds set last_success_at = greatest(coalesce(last_success_at, %s), %s) "
            "where name = 'body_mass'",
            (last, last),
        )


def local_today(tz: ZoneInfo) -> date:
    """TZ-01: the athlete's local day, whatever the server thinks."""
    return datetime.now(tz).date()
