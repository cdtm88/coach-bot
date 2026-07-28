"""Recovery deviation, computed against the athlete and nobody else.

RECOV-04: "Recovery deviation is computed against the athlete's own 28 day
baseline, not against platform derived scores." Both halves of that sentence do
work, and the second is the one that is easy to lose.

**The platform's score has nowhere to enter from.** `readiness` is Whoop's
recovery percentage arriving through intervals.icu. It is stored, it is shown to
the coach beside the local figure, and it is not an input to the deviation. That
is the FIT-03 rule applied to wellness — derived values sit alongside parsed ones
and are never substituted for them — and it matters here because a composite that
included readiness would be a rebadged Whoop score wearing the word "local".

So the deviation is built from measured signals only:

    hrv           higher is better
    restingHR     lower is better
    sleepSecs     lower is worse; the measurement, not the sleep *score*
    respiration   lower is better
    spO2          higher is better

Each is standardised against that athlete's own trailing 28 days of the same
field, oriented so positive always means better than his baseline, and averaged
over whichever of them the feed carried. RECOV-02 requires exactly that last
part: a field the feed does not carry is dropped from the calculation rather than
failing it. On the live account `hrvSDNN` is null on every day and is dropped
without anything special being written for it, because "dropped" is the default
and being carried is the exception.

**The arithmetic is SQL** (MEM-08). The long-format shape below — one row per
metric per day rather than one column per metric — is what keeps it to a single
readable window function instead of five copies of the same expression.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

# RECOV-04 names the window. The same 28 days as the weight trend, and for the
# same reason: it is long enough to cover the athlete's own rhythm and short
# enough that a fitness change six weeks ago is not still setting the baseline.
BASELINE_DAYS = 28

# How many prior observations of a field before its baseline means anything. A
# standard deviation over two readings is a number, not a baseline, and a
# deviation computed from one would swamp the average of the fields that do have
# history.
MIN_BASELINE_N = 7

# Measured signals only, with the sign that makes positive mean "better than this
# athlete's own baseline". Deliberately not a configuration value: adding
# `readiness` here would quietly undo RECOV-04, so the list is code and a test
# asserts what is absent from it.
COMPONENTS: dict[str, int] = {
    "hrv": 1,
    "resting_hr": -1,
    "sleep_secs": 1,
    "respiration": -1,
    "spo2": 1,
}

# Stored and shown, never summed into the deviation.
PLATFORM_SCORES = ("readiness", "sleep_score", "sleep_quality")


def _long_format() -> str:
    """One `select` per component, unioned into (day, metric, orientation, value)."""
    return "\n    union all\n    ".join(
        f"select local_date, {column!r} as metric, {orientation} as orientation, "
        f"{column}::numeric as value from window_rows"
        for column, orientation in COMPONENTS.items()
    )


_DEVIATION_SQL = f"""
with window_rows as (
    select * from wellness
     where local_date <= %(as_of)s::date
       and local_date > %(as_of)s::date - %(horizon)s::int
),
long as (
    {_long_format()}
),
scored as (
    select local_date, metric, orientation, value,
           avg(value)          over baseline as mean,
           stddev_samp(value)  over baseline as sd,
           count(value)        over baseline as n
      from long
    window baseline as (
        partition by metric order by local_date
        range between %(baseline)s * interval '1 day' preceding
                  and interval '1 day' preceding
    )
),
z as (
    -- A field the feed did not carry, or one without enough history behind it,
    -- simply produces no row here. That is RECOV-02's "dropped from the
    -- deviation" expressed as an absence rather than as a branch.
    select local_date, metric,
           orientation * (value - mean) / sd as z,
           n
      from scored
     where value is not null and n >= %(min_n)s and sd > 0
)
select local_date,
       avg(z)                                       as deviation,
       count(*)::int                                as fields_used,
       min(n)::int                                  as baseline_n,
       jsonb_object_agg(metric, round(z, 3)::float8) as components
  from z
 group by local_date
 order by local_date
"""


@dataclass(frozen=True)
class Deviation:
    """One day's recovery, relative to that athlete's own history."""

    local_date: date
    deviation: Decimal | None
    fields_used: int
    baseline_n: int
    components: dict[str, Any]
    platform_readiness: Decimal | None = None
    day_load: Decimal | None = None

    @property
    def usable(self) -> bool:
        """RECOV-02: a degraded deviation is still a deviation; an empty one is not."""
        return self.deviation is not None and self.fields_used > 0


def deviations(
    conn: psycopg.Connection,
    as_of: date,
    horizon_days: int = 120,
) -> list[Deviation]:
    """Every day in the horizon that has enough history to be scored."""
    with conn.cursor() as cur:
        cur.execute(
            _DEVIATION_SQL,
            {
                "as_of": as_of,
                "horizon": horizon_days,
                "baseline": BASELINE_DAYS,
                "min_n": MIN_BASELINE_N,
            },
        )
        rows = cur.fetchall()

    return [
        Deviation(
            local_date=row["local_date"],
            deviation=row["deviation"],
            fields_used=row["fields_used"],
            baseline_n=row["baseline_n"],
            components=row["components"] or {},
        )
        for row in rows
    ]


def for_day(conn: psycopg.Connection, day: date) -> Deviation | None:
    """One day's deviation, with the platform's own score alongside it."""
    # The horizon has to cover the baseline, not just the day. Asking for one
    # day leaves the window function with no history to standardise against and
    # every field drops out — a silent empty result rather than an error.
    found = [
        d for d in deviations(conn, day, horizon_days=BASELINE_DAYS + 1) if d.local_date == day
    ]
    if not found:
        return None

    with conn.cursor() as cur:
        cur.execute(
            "select readiness, atl_load from wellness where local_date = %s",
            (day,),
        )
        row = cur.fetchone() or {}

    base = found[0]
    return Deviation(
        local_date=base.local_date,
        deviation=base.deviation,
        fields_used=base.fields_used,
        baseline_n=base.baseline_n,
        components=base.components,
        platform_readiness=row.get("readiness"),
        day_load=row.get("atl_load"),
    )


def recompute(conn: psycopg.Connection, as_of: date, horizon_days: int = 120) -> int:
    """MEM-08: write the deviation to `rollups`, computed in SQL.

    Only the recovery columns are touched. The load figures belong to
    :func:`coach.ingest.reconcile.recompute_rollups` and the weight figures to
    :func:`coach.health.bodymass.recompute`; three writers, disjoint columns,
    none waiting on another.
    """
    scored = {d.local_date: d for d in deviations(conn, as_of, horizon_days)}

    # Every day the feed carried, not only the days that scored. A day with no
    # usable deviation still records the platform's number and the day's load,
    # because RECOV-06 reads the load whether or not recovery could be computed.
    with conn.cursor() as cur:
        cur.execute(
            """
            select local_date, readiness, atl_load from wellness
             where local_date <= %s::date and local_date > %s::date - %s::int
            """,
            (as_of, as_of, horizon_days),
        )
        days = cur.fetchall()

    written = 0
    for row in days:
        found = scored.get(row["local_date"])
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                insert into rollups (as_of, recovery_deviation, recovery_fields_used,
                                     recovery_components, recovery_baseline_n,
                                     platform_readiness, day_load, computed_at)
                values (%s, %s, %s, %s, %s, %s, %s, now())
                on conflict (as_of) do update set
                    recovery_deviation = excluded.recovery_deviation,
                    recovery_fields_used = excluded.recovery_fields_used,
                    recovery_components = excluded.recovery_components,
                    recovery_baseline_n = excluded.recovery_baseline_n,
                    platform_readiness = excluded.platform_readiness,
                    day_load = excluded.day_load,
                    computed_at = now()
                """,
                (
                    row["local_date"],
                    found.deviation if found else None,
                    found.fields_used if found else 0,
                    Jsonb(found.components if found else {}),
                    found.baseline_n if found else None,
                    row["readiness"],
                    row["atl_load"],
                ),
            )
        written += 1
    return written


def load_recorded_on(conn: psycopg.Connection, day: date) -> bool | None:
    """RECOV-06: did the platform record training load on this day?

    Three answers, and the third is the one that matters. True means load was
    recorded. False means the feed covered the day and recorded none. **None
    means the feed has nothing for that day at all**, which is not the same as
    zero and must not be read as one — an absent wellness row is the coach not
    knowing, and the design's hard rule is that absence of data is never evidence
    of absence of activity.
    """
    with conn.cursor() as cur:
        cur.execute("select atl_load from wellness where local_date = %s", (day,))
        row = cur.fetchone()
    if row is None or row["atl_load"] is None:
        return None
    return row["atl_load"] > 0


def context(conn: psycopg.Connection, as_of: date) -> str:
    """The recovery block for the prompt.

    The local deviation leads and the platform's score follows it, labelled as
    the platform's. Ordering is not decoration: the coach reasons from whichever
    number it reads as authoritative, and RECOV-04 decides which that is.
    """
    found = for_day(conn, as_of)
    if found is None or not found.usable:
        return ""

    lines = ["RECOVERY"]
    value = found.deviation
    if value is None:  # pragma: no cover - `usable` already excludes this
        return ""

    if value >= Decimal("0.5"):
        reading = "better than his own baseline"
    elif value <= Decimal("-0.5"):
        reading = "worse than his own baseline"
    else:
        reading = "in line with his own baseline"

    lines.append(
        f"Today is {reading} ({value:+.2f} standard deviations, "
        f"from {found.fields_used} measured signal(s))."
    )
    if found.fields_used < len(COMPONENTS):
        lines.append(
            "Some signals were not in the feed, so this is a partial reading. "
            "Treat it as softer evidence, not as a worse score."
        )
    if found.platform_readiness is not None:
        lines.append(
            f"The platform's own readiness score is {found.platform_readiness:.0f}. "
            "That is its opinion, not a measurement. Where the two disagree, the "
            "deviation above is computed from his own history and wins."
        )
    return "\n".join(lines)
