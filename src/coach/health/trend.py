"""The body mass trend, and what it permits the coach to say.

HLTH-06: fitted over a 28 day window using a time weighted method that tolerates
irregular spacing and gaps, refitted as each reading lands. HLTH-07 to HLTH-10
and HLTH-16 then decide what may be said about the fit, from the PRD's weight
trend confidence table. That table is the only place a threshold is stated, so
it is transcribed once, here, and every caller asks this module rather than
carrying its own bar.

**The fit is SQL, not Python and not the model.** MEM-08 requires derived
rollups to be computed in SQL, and the reason is sharper for this one than for
the load figures: a coach that can do arithmetic over readings will compare two
of them, and HLTH-09 forbids exactly that. The model is handed a slope, a range
and a list of permissions. It is never handed the readings.

**Why weighted least squares rather than a moving average.** The athlete weighs
in two or three times a week on no fixed day (HLTH-05), so the series is
irregular by design. A moving average over an irregular series silently weights
whichever week happened to have four readings; regressing on the actual date
does not. Exponential recency weights then make a fortnight-old reading count
for less than this morning's without discarding it, which is what HLTH-06 means
by a gap degrading confidence rather than breaking the fit.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import psycopg

log = logging.getLogger(__name__)

# HLTH-06 fixes the window at 28 days. Environment tunable because the half life
# inside it is a judgement rather than a requirement, and changing it must not
# need a deploy.
DEFAULT_WINDOW_DAYS = 28
DEFAULT_HALF_LIFE_DAYS = 14

# HLTH-08 requires a rate to be stated as a range. The interval below is a
# regression standard error, which is honest about sampling noise and says
# nothing about scale noise: the design document puts close to a kilo on a single
# weigh in. This floor stops a run of readings that happen to fall on a straight
# line from producing a range so tight it reads as a promise.
MIN_RATE_HALF_WIDTH_KG_PER_WEEK = Decimal("0.05")

# Roughly a 95% interval. Not exactly, because the recency weights are not
# inverse-variance weights, so the standard error below is an approximation with
# the floor above compensating. HLTH-08 asks for a range, not a p-value.
INTERVAL_Z = Decimal("1.96")


def window_days() -> int:
    return _positive_int("COACH_WEIGHT_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)


def half_life_days() -> int:
    return _positive_int("COACH_WEIGHT_HALF_LIFE_DAYS", DEFAULT_HALF_LIFE_DAYS)


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %d", name, raw, default)
        return default
    if value < 1:
        log.warning("%s=%d is not positive; using %d", name, value, default)
        return default
    return value


# The whole fit in one statement. Read it as five steps: select the window,
# weight each reading by age, accumulate the weighted sums, solve for the line,
# then make a second pass for the residuals the interval needs.
_FIT_SQL = """
with readings as (
    select local_date, weight_kg
      from body_mass_readings
     where status = 'accepted'
       and local_date <= %(as_of)s::date
       and local_date > %(as_of)s::date - %(window)s::int
       -- HLTH-11 tests a new reading against the pattern the others establish,
       -- so the day under test has to be able to leave its own baseline.
       and (%(exclude)s::date is null or local_date <> %(exclude)s::date)
),
points as (
    select local_date,
           (local_date - %(as_of)s::date)::numeric as x,
           weight_kg::numeric as y,
           power(0.5::numeric,
                 (%(as_of)s::date - local_date)::numeric / %(half_life)s::numeric) as w
      from readings
),
sums as (
    select count(*)::int                                as n,
           sum(w)                                       as sw,
           sum(w * x)                                   as swx,
           sum(w * y)                                   as swy,
           sum(w * x * x)                               as swxx,
           sum(w * x * y)                               as swxy,
           min(local_date)                              as first_on,
           max(local_date)                              as last_on,
           count(distinct extract(dow from local_date)) as weekdays,
           count(distinct floor(
               (%(as_of)s::date - local_date)::numeric / 7))::int as weeks_covered
      from points
),
solved as (
    select s.*,
           (s.sw * s.swxx - s.swx * s.swx) as denom
      from sums s
),
line as (
    select s.*,
           case when s.n >= 2 and s.denom <> 0
                then (s.sw * s.swxy - s.swx * s.swy) / s.denom
           end as slope_per_day
      from solved s
),
placed as (
    select l.*,
           case when l.slope_per_day is not null
                then (l.swy - l.slope_per_day * l.swx) / l.sw
           end as intercept
      from line l
),
scatter as (
    -- Second pass: the weighted residual sum, which is what turns a slope into
    -- a range. Null when there are too few points to have residuals at all.
    select sum(p.w * power(p.y - (pl.intercept + pl.slope_per_day * p.x), 2)) as swee
      from points p, placed pl
     where pl.slope_per_day is not null
)
select pl.n,
       pl.first_on,
       pl.last_on,
       pl.weekdays,
       pl.weeks_covered,
       pl.slope_per_day,
       pl.intercept,
       case
           when pl.n >= 3 and pl.denom > 0 and sc.swee is not null
           then sqrt((sc.swee / pl.sw) * (pl.n::numeric / (pl.n - 2)) * pl.sw / pl.denom)
       end as slope_per_day_se,
       -- Weighted scatter about the fitted line. HLTH-11 sizes "far outside the
       -- established pattern" from this rather than from a constant, so an
       -- athlete whose readings are tight is not held to the same absolute
       -- tolerance as one whose readings are noisy.
       case when pl.n >= 3 and sc.swee is not null
            then sqrt(sc.swee / pl.sw)
       end as residual_sd,
       (select weight_kg from readings order by local_date desc limit 1) as latest_kg
  from placed pl left join scatter sc on true
"""


@dataclass(frozen=True)
class Fit:
    """The fitted trend, and everything the claim gate needs to read it.

    Every rate is kg per week. `slope` is never quoted on its own: HLTH-08 says
    a rate is stated as a range, and :attr:`rate_range` is that range.
    """

    n: int
    as_of: date
    first_on: date | None = None
    last_on: date | None = None
    weekdays: int = 0
    weeks_covered: int = 0
    slope_kg_per_week: Decimal | None = None
    rate_low: Decimal | None = None
    rate_high: Decimal | None = None
    latest_kg: Decimal | None = None
    intercept_kg: Decimal | None = None
    slope_per_day: Decimal | None = None
    residual_sd: Decimal | None = None

    @property
    def span_days(self) -> int:
        if self.first_on is None or self.last_on is None:
            return 0
        return (self.last_on - self.first_on).days

    @property
    def weekday_bias(self) -> bool:
        """HLTH-10: every reading in the window fell on the same weekday.

        Below three readings this is not a sampling pattern, it is a coincidence,
        and flagging it would fire on the athlete's first two weigh ins.
        """
        return self.n >= 3 and self.weekdays == 1

    def predict(self, on: date) -> Decimal | None:
        """The fitted value on a date. Used to test a new reading (HLTH-11)."""
        if self.slope_per_day is None or self.intercept_kg is None:
            return None
        offset = Decimal((on - self.as_of).days)
        return self.intercept_kg + self.slope_per_day * offset


def fit(
    conn: psycopg.Connection,
    as_of: date,
    window: int | None = None,
    half_life: int | None = None,
    exclude: date | None = None,
) -> Fit:
    """Fit the trend as of a date. Cheap enough to run on every reading.

    `exclude` drops one date from the window, which is how HLTH-11 tests a
    reading against the pattern the other readings establish rather than against
    a pattern it is already part of.
    """
    with conn.cursor() as cur:
        cur.execute(
            _FIT_SQL,
            {
                "as_of": as_of,
                "window": window if window is not None else window_days(),
                "half_life": half_life if half_life is not None else half_life_days(),
                "exclude": exclude,
            },
        )
        row = cur.fetchone()

    if row is None or not row["n"]:
        return Fit(n=0, as_of=as_of)

    slope_day = row["slope_per_day"]
    slope_week = slope_day * 7 if slope_day is not None else None

    low = high = None
    if slope_week is not None:
        se = row["slope_per_day_se"]
        half_width = (
            max(se * 7 * INTERVAL_Z, MIN_RATE_HALF_WIDTH_KG_PER_WEEK)
            if se is not None
            else MIN_RATE_HALF_WIDTH_KG_PER_WEEK
        )
        low, high = slope_week - half_width, slope_week + half_width

    return Fit(
        n=row["n"],
        as_of=as_of,
        first_on=row["first_on"],
        last_on=row["last_on"],
        weekdays=int(row["weekdays"] or 0),
        weeks_covered=int(row["weeks_covered"] or 0),
        slope_kg_per_week=slope_week,
        rate_low=low,
        rate_high=high,
        latest_kg=row["latest_kg"],
        intercept_kg=row["intercept"],
        slope_per_day=slope_day,
        residual_sd=row["residual_sd"],
    )


# --- the claim gate ---------------------------------------------------------
#
# The PRD's weight trend confidence table, transcribed once. "No other threshold
# is used anywhere in the system, and no requirement outside it may state its
# own bar" — so nothing else in this codebase may hard-code a reading count.

DIRECTION_MIN_READINGS = 3  # HLTH-07
RATE_MIN_READINGS = 6  # HLTH-08
RATE_MIN_SPAN_DAYS = 21  # HLTH-08: three weeks

# HLTH-16 and NUT-04 both read "weekly coverage" over "4 weeks". Those are one
# condition rather than two: a reading in each of the four weeks of a 28 day
# window *is* four weeks of readings.
#
# Stating them separately looked safer and was wrong. The window runs from
# as_of-27 to as_of inclusive, so the widest possible span inside it is 27 days
# and a separate `span >= 28` bar could never be met — a plateau would have been
# unreachable by construction, which is a rule that reads as strict and is
# actually just broken. Four covered weeks implies a span of at least 21 days, so
# that is what the span bar asserts: a guard against a wider configured window
# satisfying the coverage count with four old readings.
PLATEAU_MIN_WEEKS_COVERED = 4
PLATEAU_MIN_SPAN_DAYS = 21


@dataclass(frozen=True)
class Claims:
    """What the fit permits. Rendered into the prompt as permissions.

    Everything here is a ceiling, not an instruction: permission to state a
    direction is not a reason to mention weight at all.
    """

    may_report_reading: bool
    may_state_direction: bool
    may_quote_rate: bool
    may_call_plateau: bool
    may_arbitrate_energy_balance: bool
    weekday_bias: bool

    @classmethod
    def of(cls, trend: Fit) -> Claims:
        weekly_coverage = (
            trend.weeks_covered >= PLATEAU_MIN_WEEKS_COVERED
            and trend.span_days >= PLATEAU_MIN_SPAN_DAYS
        )
        return cls(
            may_report_reading=trend.n >= 1,
            may_state_direction=trend.n >= DIRECTION_MIN_READINGS,
            may_quote_rate=(
                trend.n >= RATE_MIN_READINGS
                and trend.span_days >= RATE_MIN_SPAN_DAYS
                and trend.rate_low is not None
            ),
            # HLTH-16: a programme change on weight evidence alone.
            may_call_plateau=weekly_coverage,
            # NUT-04: the same bar, stated in the same table.
            may_arbitrate_energy_balance=weekly_coverage,
            weekday_bias=trend.weekday_bias,
        )


# Below this the fitted slope is flat for coaching purposes. A trend that reads
# as "rising" at 4 grams a week is a true statement and a useless one.
FLAT_KG_PER_WEEK = Decimal("0.05")


def _direction_line(trend: Fit, claims: Claims) -> str:
    """What may be said about which way the trend is going.

    The uncertainty interval is allowed to straddle zero, and that case has to be
    said out loud rather than papered over. Rendering it as an unsigned range —
    "between 0.06 and 0.10 kg per week" for an interval running from -0.10 to
    +0.06 — would turn "we cannot tell whether he is gaining or losing" into a
    confident rate in the direction the point estimate happened to fall. That is
    the failure HLTH-08 exists to prevent, arriving through the requirement's own
    output.
    """
    slope = trend.slope_kg_per_week
    assert slope is not None  # the caller checks

    if abs(slope) < FLAT_KG_PER_WEEK:
        return "Trend is flat. Say that it is holding steady, and quote no rate."

    direction = "falling" if slope < 0 else "rising"

    if not claims.may_quote_rate or trend.rate_low is None or trend.rate_high is None:
        return (
            f"Trend is {direction}. You may say the direction and nothing about the rate: "
            "a rate needs six readings across three weeks."
        )

    low, high = sorted((trend.rate_low, trend.rate_high))
    if low <= 0 <= high:
        return (
            f"Trend points {direction} but the range spans zero, so the rate is not "
            "distinguishable from flat. Say the direction is not yet clear and quote no rate."
        )

    magnitude = sorted((abs(low), abs(high)))
    return (
        f"Trend is {direction}, between {magnitude[0]:.2f} and {magnitude[1]:.2f} kg per week. "
        "Quote the range, never a single figure."
    )


def render(trend: Fit, claims: Claims) -> str:
    """The body mass block for the prompt.

    Written as what may be said rather than as what is known, because the
    failure mode is a model that has a number and reaches for it. HLTH-09 is
    stated positively here — the readings themselves are not in the context at
    all, so there is nothing to compare.
    """
    if trend.n == 0:
        # Present rather than omitted, and it says do not raise it rather than
        # leaving that to be inferred. HLTH-15 keeps body mass out of the generic
        # staleness block, so this is the only line in the prompt that stops the
        # coach reading silence as a stable weight or as a missed weigh in.
        return (
            "BODY MASS\n"
            "No readings have arrived, so nothing is feeding the weight trend. That is a "
            "gap in the plumbing, not a change in the athlete. Make no claim about his "
            "weight in either direction, and do not raise the subject."
        )

    lines = ["BODY MASS", f"{trend.n} reading(s) over {trend.span_days} days."]

    if claims.may_report_reading and trend.latest_kg is not None:
        lines.append(
            f"Most recent reading {trend.latest_kg:.1f} kg on {trend.last_on}. "
            "You may report this figure if asked for it."
        )

    if claims.may_state_direction and trend.slope_kg_per_week is not None:
        lines.append(_direction_line(trend, claims))
    else:
        lines.append(
            "Not enough readings to state a direction. Report a reading if asked and "
            "make no claim about which way it is going."
        )

    if not claims.may_call_plateau:
        lines.append(
            "Do not call a plateau or change the programme on weight evidence: "
            "that needs four weeks with a reading in each."
        )

    if claims.weekday_bias:
        lines.append(
            "Every reading fell on the same weekday, so weekly rhythm is inside this "
            "trend. Treat it as noisier than it looks."
        )

    lines.append(
        "Never comment on one reading moving, and never compare two readings. "
        "The trend is the only thing that means anything."
    )
    return "\n".join(lines)
