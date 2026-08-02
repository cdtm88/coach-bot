"""The reconcile backstop and the bulk backfill.

FIT-09 (history loads silently), FIT-11 (upstream activities missing locally are
backfilled), FIT-13 (rollups recompute after a bulk load rather than waiting for
the night).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from coach import feeds as feedmod
from coach.ingest import activities as actmod
from coach.ingest import archive as archivemod
from coach.ingest import client as clientmod
from coach.ingest import review as reviewmod

log = logging.getLogger(__name__)

# FIT-01 originally made this a six hourly backstop behind the webhook. Without a
# registered app there is no webhook, so polling is the primary path for anything
# that does not arrive through the watched folder, and the interval has to be
# short enough to keep PERF-03's five minute budget.
#
# Both are environment tunable because the right value depends on the rate limit,
# which is only knowable from the response headers on a live key. Changing the
# cadence must not need a deploy.
DEFAULT_POLL_INTERVAL_S = 120
DEFAULT_SWEEP_INTERVAL_S = 6 * 3600

# Kept for the sweep, which is still the six hourly job FIT-01 described.
INTERVAL_HOURS = 6


def env_interval(name: str, default: int, floor: int) -> int:
    """Read an interval from the environment, refusing values that would hurt.

    A floor rather than a free number: polling every second would exhaust the
    daily rate limit before lunch and the failure would look like intervals.icu
    being broken rather than like a configuration mistake.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %ds", name, raw, default)
        return default
    if value < floor:
        log.warning("%s=%ds is below the %ds floor; using the floor", name, value, floor)
        return floor
    return value


def poll_interval_s() -> int:
    """COACH_POLL_INTERVAL_S. Floored at 30s to stay clear of the rate limit."""
    return env_interval("COACH_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S, 30)


def sweep_interval_s() -> int:
    """COACH_SWEEP_INTERVAL_S. Floored at 5 minutes; it has nothing to do faster."""
    return env_interval("COACH_SWEEP_INTERVAL_S", DEFAULT_SWEEP_INTERVAL_S, 300)


@dataclass
class Outcome:
    examined: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.created + self.updated


def local_refs(conn: psycopg.Connection, oldest: date, newest: date | None = None) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "select external_ref from sessions "
            "where external_ref is not null and local_date >= %s "
            "  and (%s::date is null or local_date <= %s)",
            (oldest, newest, newest),
        )
        return {r["external_ref"] for r in cur.fetchall()}


def fetch_parsed_inputs(
    client: clientmod.Intervals, activity_id: str
) -> tuple[bytes | None, list[dict[str, Any]] | None]:
    """The original file if there is one, streams otherwise (FIT-03).

    Never falls back to the platform's aggregates: if both are unavailable the
    session simply has no parsed values, which is honest.
    """
    data = client.original_file(activity_id)
    if data:
        return data, None
    try:
        return None, client.streams(activity_id)
    except clientmod.IntervalsError as exc:
        log.info("no streams for %s either: %s", activity_id, exc)
        return None, None


def run(
    conn: psycopg.Connection,
    client: clientmod.Intervals,
    tz: ZoneInfo,
    oldest: date,
    newest: date | None = None,
    backfill: bool = False,
    fetch_files: bool = True,
) -> Outcome:
    """Pull a date range and reconcile it against what is stored.

    `backfill=True` is FIT-09's silent mode: rows are marked backfilled, which is
    what stops the review path and therefore the Telegram messages. It is a flag
    on the row rather than a separate code path so the two cannot drift.
    """
    outcome = Outcome()
    try:
        upstream = client.activities(oldest, newest)
    except clientmod.IntervalsError as exc:
        outcome.errors.append(str(exc))
        feedmod.record_error(conn, feedmod.ACTIVITIES, str(exc))
        return outcome

    # OBS-05. The call returning is the whole of the claim: an empty window is a
    # rest week and not a broken feed, and CHAT-09 exists to stop the coach
    # conflating the two.
    feedmod.record_success(conn, feedmod.ACTIVITIES)

    known = local_refs(conn, oldest, newest)

    for activity in upstream:
        outcome.examined += 1
        activity_id = activity.get("id")
        if not activity_id:
            outcome.skipped += 1
            continue

        # FIT-11 is about what is missing. An activity already stored is left
        # alone unless we are backfilling, so reconcile stays cheap.
        if activity_id in known and not backfill:
            outcome.skipped += 1
            continue

        file_bytes, streams = (None, None)
        if fetch_files:
            file_bytes, streams = fetch_parsed_inputs(client, activity_id)

        try:
            result = actmod.ingest(
                conn, activity, tz, file_bytes=file_bytes, streams=streams, backfilled=backfill
            )
        except Exception as exc:  # noqa: BLE001 - one bad activity must not stop the run
            outcome.errors.append(f"{activity_id}: {exc}")
            continue

        # FIT-15. The bytes were already paid for above; throwing them away after
        # parsing left the permanent archive holding only what the watched folder
        # happened to see, on the path that ingests almost everything.
        if file_bytes:
            archivemod.keep_original(conn, activity_id, file_bytes, result.session_id)

        if result.created:
            outcome.created += 1
        else:
            outcome.updated += 1

        if client.last_limit.exhausted:
            outcome.errors.append("rate limit exhausted; stopping early")
            break

    if backfill and outcome.changed:
        recompute_rollups(conn)  # FIT-13

    return outcome


def backfill_all(
    conn: psycopg.Connection,
    client: clientmod.Intervals,
    tz: ZoneInfo,
    since: date,
    chunk_days: int = 90,
) -> Outcome:
    """FIT-09: load history silently, in chunks the rate limiter can survive."""
    total = Outcome()
    start = since
    today = date.today()

    while start <= today:
        end = min(start + timedelta(days=chunk_days), today)
        chunk = run(conn, client, tz, start, end, backfill=True)
        total.examined += chunk.examined
        total.created += chunk.created
        total.updated += chunk.updated
        total.skipped += chunk.skipped
        total.errors.extend(chunk.errors)
        if any("rate limit" in e for e in chunk.errors):
            break
        start = end + timedelta(days=1)

    recompute_rollups(conn)
    return total


def recompute_rollups(conn: psycopg.Connection) -> int:
    """FIT-13: rebuild derived rollups now rather than waiting for the night.

    MEM-08 requires these to be SQL rather than model arithmetic, so this is one
    statement. Weight trend and recovery deviation arrive with their own feeds.

    **The gym count is the whole gym group**, taken from `review.equivalents`
    rather than written out again here. It was a literal `('weighttraining',
    'workout')` beside a group that also knows `gym` and `strength` — the same
    drift that made a Zwift ride unable to close a ride prescription (#27) and a
    gravel ride lose its power (#29), a third time. Latent rather than live,
    because nothing writes those two spellings today; it would have stopped being
    latent the first time anything did, silently, in a number GYM-08 puts in
    front of the athlete.

    **Adherence excludes break days** (BREAK-02). A prescription suspended by a
    break is not counted in either the numerator or the denominator, so a
    fortnight in Italy leaves the rate where it was rather than at zero. That is
    a stronger statement than "suspended sessions are not misses": counting them
    as offered-and-not-taken would be just as wrong, and it is the denominator
    that makes a rate lie convincingly.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into rollups
                (as_of, load_7d, load_28d, gym_session_count, adherence_rate, computed_at)
            select d.day,
                   (select coalesce(sum((s.derived->>'icu_training_load')::numeric), 0)
                      from sessions s
                     where s.local_date > d.day - interval '7 days'
                       and s.local_date <= d.day),
                   (select coalesce(sum((s.derived->>'icu_training_load')::numeric), 0)
                      from sessions s
                     where s.local_date > d.day - interval '28 days'
                       and s.local_date <= d.day),
                   (select count(*) from sessions s
                     where s.discipline = any(%(gym)s)
                       and s.local_date > d.day - interval '7 days'
                       and s.local_date <= d.day),
                   (select case when count(*) = 0 then null
                                else count(*) filter (where p.status = 'completed')::numeric
                                     / count(*)
                           end
                      from prescriptions p
                     where p.status in ('completed', 'missed')
                       and (p.planned_for at time zone 'UTC')::date
                             > d.day - interval '28 days'
                       and (p.planned_for at time zone 'UTC')::date <= d.day),
                   now()
              from (select distinct local_date as day from sessions) d
            on conflict (as_of) do update set
                load_7d = excluded.load_7d,
                load_28d = excluded.load_28d,
                gym_session_count = excluded.gym_session_count,
                adherence_rate = excluded.adherence_rate,
                computed_at = excluded.computed_at
            """,
            {"gym": reviewmod.equivalents("gym")},
        )
        return cur.rowcount
