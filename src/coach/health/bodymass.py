"""Body mass readings: how they land, what holds them back, when they are missed.

HLTH-04 fixes the source: readings come from the intervals.icu wellness feed and
never from HealthKit. This module is what happens to one once it arrives —
the outlier gate of HLTH-11, the capture rate of HLTH-05, the gap mention of
HLTH-12 and HLTH-15, and the rollup that the trend is read from.

**The live account has no weight at all.** A 21 day wellness read on 28 July 2026
returned thirteen populated days and a null `weight` on every one of them, which
resolved open item 1: nothing feeds body mass today, and MacroLog's HealthBridge
has to write it before any of this has data. Everything here is therefore written
to be correct on an empty series first and a full one second. A coach that says
nothing about weight because there is no weight is behaving correctly; a coach
that says nothing because the code raised on an empty fit is not.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg

from coach.agent import interruptions
from coach.health import breaks, trend

log = logging.getLogger(__name__)

# HLTH-11: how far outside the established pattern a reading has to fall before
# it is held for confirmation.
#
# Two terms, and the larger wins. The scatter term adapts to the athlete: three
# times the residual spread about the fitted line means a tight series is held to
# a tight tolerance. The floor stops that from becoming absurd — a run of
# readings that happen to sit on a straight line would otherwise make an ordinary
# 700g fluctuation an outlier, and HLTH-11 is meant to fire on a reading that is
# probably a different person on the scale, not on an ordinary day's variation.
OUTLIER_FLOOR_KG = Decimal("1.5")
OUTLIER_SIGMAS = Decimal("3")

# Below this there is no established pattern to be outside of, so nothing is an
# outlier. Deliberately the same number as the direction threshold in HLTH-07:
# if the series cannot support a claim about direction, it cannot support a claim
# that a reading contradicts it either.
OUTLIER_MIN_BASELINE = trend.DIRECTION_MIN_READINGS

# HLTH-15: the only weigh in prompt in the system. Matches the `body_mass` row
# in `feeds` (288 hours), which is the same threshold expressed for OBS-05.
GAP_DAYS = 12

# HLTH-05: a target rate, not a schedule. Nothing in this file names a day of the
# week, and nothing may: "there is no fixed weigh in day and no requirement to
# hit the target in any given week".
DEFAULT_TARGET_PER_WEEK = Decimal("2.5")


def target_per_week() -> Decimal:
    raw = os.environ.get("COACH_WEIGH_IN_TARGET_PER_WEEK")
    if not raw:
        return DEFAULT_TARGET_PER_WEEK
    try:
        value = Decimal(raw)
    except Exception:  # noqa: BLE001 - any malformed value is the same mistake
        log.warning("COACH_WEIGH_IN_TARGET_PER_WEEK=%r is not a number; using default", raw)
        return DEFAULT_TARGET_PER_WEEK
    return value if value > 0 else DEFAULT_TARGET_PER_WEEK


def outlier_kg_floor() -> Decimal:
    raw = os.environ.get("COACH_BODY_MASS_OUTLIER_KG")
    if not raw:
        return OUTLIER_FLOOR_KG
    try:
        value = Decimal(raw)
    except Exception:  # noqa: BLE001
        log.warning("COACH_BODY_MASS_OUTLIER_KG=%r is not a number; using default", raw)
        return OUTLIER_FLOOR_KG
    return value if value > 0 else OUTLIER_FLOOR_KG


@dataclass(frozen=True)
class Recorded:
    """What one reading did. Every field is something a test asserts on."""

    reading_id: int
    local_date: date
    weight_kg: Decimal
    status: str
    created: bool
    outlier_delta: Decimal | None = None

    @property
    def held_for_confirmation(self) -> bool:
        return self.status == "pending_confirmation"


def record(
    conn: psycopg.Connection,
    on: date,
    weight_kg: Decimal | float,
    source: str = "wellness",
) -> Recorded:
    """Store a reading, holding it out of the trend if it is an outlier.

    The order is load bearing. The baseline is fitted *before* the reading is
    written and with its own date excluded, so a reading is never tested against
    a pattern it is already part of. Re-syncing the same date therefore gets the
    same verdict every time rather than gradually becoming its own normal.
    """
    value = Decimal(str(weight_kg))
    baseline = trend.fit(conn, as_of=on, exclude=on)
    status, delta = _verdict(baseline, on, value)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into body_mass_readings (local_date, weight_kg, source, status, outlier_delta)
            values (%s, %s, %s, %s, %s)
            on conflict (local_date) do update set
                weight_kg = excluded.weight_kg,
                source = excluded.source,
                -- HLTH-11 confirms a reading *once*, and RECOV-05 means the same
                -- day is re-read every hour for as long as it stays in the
                -- window. So an answered reading keeps its answer, whichever way
                -- it went — a rejected outlier that reverted to the fresh
                -- verdict would be re-offered on every sync, which is the
                -- interrogation the requirement forbids.
                --
                -- Unless the value itself changed. Upstream correcting a weight
                -- makes it a different reading, and an answer given about the
                -- old number says nothing about the new one.
                status = case when body_mass_readings.confirmed_at is not null
                                   and body_mass_readings.weight_kg = excluded.weight_kg
                              then body_mass_readings.status
                              else excluded.status end,
                confirmed_at = case when body_mass_readings.confirmed_at is not null
                                         and body_mass_readings.weight_kg = excluded.weight_kg
                                    then body_mass_readings.confirmed_at end,
                outlier_delta = excluded.outlier_delta
            returning id, local_date, weight_kg, status,
                      (xmax = 0) as created, outlier_delta
            """,
            (on, value, source, status, delta),
        )
        row = cur.fetchone()

    if row["status"] == "pending_confirmation":
        log.info("holding body mass reading for %s: %s kg off the trend", on, delta)
    return Recorded(
        reading_id=row["id"],
        local_date=row["local_date"],
        weight_kg=row["weight_kg"],
        status=row["status"],
        created=row["created"],
        outlier_delta=row["outlier_delta"],
    )


def _verdict(baseline: trend.Fit, on: date, value: Decimal) -> tuple[str, Decimal | None]:
    """HLTH-11: does this reading sit far enough outside to need confirming?"""
    predicted = baseline.predict(on)
    if baseline.n < OUTLIER_MIN_BASELINE or predicted is None:
        return "accepted", None

    delta = value - predicted
    scatter = (baseline.residual_sd or Decimal(0)) * OUTLIER_SIGMAS
    threshold = max(outlier_kg_floor(), scatter)
    if abs(delta) > threshold:
        return "pending_confirmation", delta
    return "accepted", None


def resolve(conn: psycopg.Connection, reading_id: int, accepted: bool) -> str:
    """Close out an HLTH-11 confirmation. Returns the status it landed in.

    `confirmed_at` is stamped on both outcomes, because it records that the
    question was asked and answered. Without it a rejected reading would be
    re-offered on the next resync and the athlete would be asked twice.
    """
    status = "accepted" if accepted else "rejected"
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update body_mass_readings set status = %s, confirmed_at = now() where id = %s",
            (status, reading_id),
        )
    return status


def pending_confirmation(conn: psycopg.Connection) -> dict | None:
    """The reading waiting on HLTH-11's single light question, if any."""
    with conn.cursor() as cur:
        cur.execute(
            "select id, local_date, weight_kg, outlier_delta from body_mass_readings "
            "where status = 'pending_confirmation' and confirmed_at is null "
            "order by local_date desc limit 1"
        )
        return cur.fetchone()


# --- the gap ---------------------------------------------------------------


@dataclass(frozen=True)
class Gap:
    last_on: date | None
    days: int | None
    mentioned: bool
    suppressed_by_break: bool

    @property
    def open(self) -> bool:
        return self.days is not None and self.days > GAP_DAYS


def gap(conn: psycopg.Connection, today: date) -> Gap:
    """How long since a reading, and whether this gap has already been raised.

    HLTH-15 says the mention happens once "and not again until a reading resets
    the counter", so the identity of a gap is the reading it started from. The
    interruption row records that date, which makes "have we mentioned this gap"
    an exact lookup rather than a window comparison that drifts.

    A reading arriving resets the counter whatever its status. A reading held for
    HLTH-11 confirmation is still the athlete having stood on the scale, and
    telling him he has not weighed in would be plainly wrong.
    """
    with conn.cursor() as cur:
        cur.execute("select max(local_date) as last_on from body_mass_readings")
        last_on = cur.fetchone()["last_on"]

    if last_on is None:
        # No reading has ever arrived, so there is no gap — there is a feed that
        # has never delivered. That is OBS-05 staleness, which reaches the prompt
        # through CHAT-09 and shapes reasoning without spending the CHAT-11
        # budget. Calling it a gap would have the coach ask the athlete why he
        # has stopped doing something he was never able to start.
        return Gap(None, None, mentioned=False, suppressed_by_break=False)

    with conn.cursor() as cur:
        cur.execute(
            "select 1 from interruptions where kind = 'body_mass_gap' and ref = %s limit 1",
            (last_on.isoformat(),),
        )
        mentioned = cur.fetchone() is not None

    return Gap(
        last_on=last_on,
        days=(today - last_on).days,
        mentioned=mentioned,
        # HLTH-13: a scheduled break suppresses weigh in prompts entirely, and
        # the trend resumes on return without commentary on the gap.
        suppressed_by_break=breaks.active_on(conn, today) is not None,
    )


def candidates(conn: psycopg.Connection, today: date) -> list[interruptions.Candidate]:
    """What body mass would like to raise, for CHAT-11 to arbitrate.

    Offered rather than emitted. Both of these are interruptions in CHAT-11's
    sense and neither gets to decide it is important enough — the outlier
    confirmation outranks the gap mention in that requirement's priority order,
    and the budget may already be spent on something that outranks both.
    """
    offered: list[interruptions.Candidate] = []

    outlier = pending_confirmation(conn)
    if outlier is not None:
        ref = str(outlier["id"])
        if not _already_raised(conn, "outlier_confirmation", ref):
            offered.append(interruptions.Candidate("outlier_confirmation", ref))

    window = gap(conn, today)
    if window.open and not window.mentioned and not window.suppressed_by_break:
        assert window.last_on is not None  # implied by window.open
        offered.append(interruptions.Candidate("body_mass_gap", window.last_on.isoformat()))

    return offered


def _already_raised(conn: psycopg.Connection, kind: str, ref: str) -> bool:
    """Has this exact thing already spent an interruption?

    HLTH-11 asks once. If the athlete does not answer, the reading stays out of
    the trend and the question is not repeated — asking again in every
    conversation until he replies is the interrogation the requirement rules
    out, and an unconfirmed outlier sitting outside the fit is the failure-safe
    direction anyway.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from interruptions where kind = %s and ref = %s limit 1",
            (kind, ref),
        )
        return cur.fetchone() is not None


# --- capture rate ----------------------------------------------------------


@dataclass(frozen=True)
class Capture:
    readings: int
    per_week: Decimal
    target_per_week: Decimal
    consecutive_pairs: int

    @property
    def at_target(self) -> bool:
        return self.per_week >= self.target_per_week


def capture_rate(conn: psycopg.Connection, as_of: date, window: int | None = None) -> Capture:
    """HLTH-05: the observed rate against the configured target.

    Reported, never enforced. HLTH-12 is explicit that missed readings are not a
    lapse and never count against adherence, so this number exists to widen
    confidence in the trend and for nothing else. Nothing in this codebase reads
    it into an adherence figure, and a test asserts that.
    """
    span = window if window is not None else trend.window_days()
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*)::int as n,
                   count(*) filter (where gap_days = 1)::int as consecutive
              from (
                select local_date - lag(local_date) over (order by local_date) as gap_days
                  from body_mass_readings
                 where local_date <= %s and local_date > %s::date - %s::int
              ) r
            """,
            (as_of, as_of, span),
        )
        row = cur.fetchone()

    weeks = Decimal(span) / Decimal(7)
    return Capture(
        readings=row["n"],
        per_week=(Decimal(row["n"]) / weeks) if weeks else Decimal(0),
        target_per_week=target_per_week(),
        # HLTH-05 prefers non consecutive days. Counted so the trend can be
        # treated as noisier when the readings clump, never as a failure.
        consecutive_pairs=row["consecutive"] or 0,
    )


# --- the rollup ------------------------------------------------------------


def recompute(conn: psycopg.Connection, as_of: date, horizon_days: int = 120) -> int:
    """MEM-08: write the fitted trend to `rollups`, in SQL, for the model to read.

    Recomputed for every date in the recent horizon that carries a reading, plus
    `as_of` itself, because HLTH-06 refits as each reading lands and a late
    arriving reading changes every day after it.

    Only the weight columns are touched. The load columns belong to
    :func:`coach.ingest.reconcile.recompute_rollups` and the two writers are
    disjoint by design, so either can run without waiting for the other.
    """
    dates = _dates_to_recompute(conn, as_of, horizon_days)
    if not dates:
        return 0

    written = 0
    for day in dates:
        fitted = trend.fit(conn, as_of=day)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                insert into rollups (as_of, weight_trend_slope, weight_reading_n,
                                     weight_trend_low, weight_trend_high, weight_span_days,
                                     weight_weekday_bias, weight_weeks_covered,
                                     weight_latest_kg, computed_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                on conflict (as_of) do update set
                    weight_trend_slope = excluded.weight_trend_slope,
                    weight_reading_n = excluded.weight_reading_n,
                    weight_trend_low = excluded.weight_trend_low,
                    weight_trend_high = excluded.weight_trend_high,
                    weight_span_days = excluded.weight_span_days,
                    weight_weekday_bias = excluded.weight_weekday_bias,
                    weight_weeks_covered = excluded.weight_weeks_covered,
                    weight_latest_kg = excluded.weight_latest_kg,
                    computed_at = now()
                """,
                (
                    day,
                    fitted.slope_kg_per_week,
                    fitted.n,
                    fitted.rate_low,
                    fitted.rate_high,
                    fitted.span_days,
                    fitted.weekday_bias,
                    fitted.weeks_covered,
                    fitted.latest_kg,
                ),
            )
        written += 1
    return written


def _dates_to_recompute(conn: psycopg.Connection, as_of: date, horizon_days: int) -> list[date]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct local_date as day from body_mass_readings
             where status = 'accepted'
               and local_date <= %s and local_date > %s::date - %s::int
            union
            select %s::date
             where exists (select 1 from body_mass_readings where status = 'accepted')
             order by day
            """,
            (as_of, as_of, horizon_days, as_of),
        )
        return [row["day"] for row in cur.fetchall()]


def context(conn: psycopg.Connection, as_of: date) -> str:
    """The body mass block for the prompt. Always present, even at zero readings.

    Omitting it when there is nothing to say was the first instinct and it was
    wrong. HLTH-15 bars body mass from the generic staleness block, so if this
    block is absent too then nothing in the prompt says the weight feed is
    silent — and silence reads as a stable weight. Two lines is a cheap price for
    the coach not inferring a trend from the absence of one.
    """
    fitted = trend.fit(conn, as_of=as_of)
    return trend.render(fitted, trend.Claims.of(fitted))


def latest_reading_at(conn: psycopg.Connection) -> datetime | None:
    """When the most recent reading dates from, for the OBS-05 feed row.

    Deliberately the reading's own date and not the moment the sync succeeded.
    The `body_mass` feed goes stale after 12 days without a *reading*, per
    HLTH-15; a wellness fetch that succeeds and returns no weight is a successful
    fetch of nothing and must not reset the clock.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select max(local_date) as last_on from body_mass_readings where source = 'wellness'"
        )
        last_on = cur.fetchone()["last_on"]
    if last_on is None:
        return None
    return datetime.combine(last_on, datetime.min.time()).replace(tzinfo=UTC)
