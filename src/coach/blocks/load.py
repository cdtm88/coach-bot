"""One scale for two disciplines, and the ceiling that sits on it.

GYM-08 is the unit of account: "session load is RPE multiplied by duration in
minutes, scaled by a configured coefficient into the cycling load unit. The
coefficient is configuration, not code. This is the unit of account for GYM-05,
BLOCK-07 and ADJ-02."

GYM-05: a gym session counts toward the weekly ceiling alongside cycling.
BLOCK-07: weekly planned load, computed across both disciplines on that scale,
never increases by more than a configured percentage against the prior week —
"including where the breach comes from added gym volume".

**Why one scale rather than two budgets.** Two budgets would let the athlete add
three gym sessions to a week already at the cycling ceiling and have every rule
report compliance. The requirement is explicit that the breach can come from the
gym side, which only means anything if the two are added together before the
ceiling is checked.

**The cycling side is the standard model, not an invention.** Intensity factor
squared, times duration in hours, times 100 — an hour at threshold is 100. That
is what makes the coefficient below expressible: it converts an RPE-minutes
product into the same unit rather than into a number of its own.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import psycopg

log = logging.getLogger(__name__)

# GYM-08's coefficient. A 45 minute session at RPE 7 is 7 * 45 * 0.20 = 63,
# which sits where it should against an hour of endurance riding at about 55.
# Configuration, so the trade between a gym session and a ride can be retuned
# without a deploy — that is the requirement's acceptance criterion.
DEFAULT_GYM_COEFFICIENT = Decimal("0.20")

# BLOCK-07's ramp limit. Ten percent is the conventional bound and it is on the
# permissive side for this athlete: de-trained, with an L5-S1 repair. It is
# configuration because the right number changes as he does, and a floor of zero
# is meaningful — a block that never increases load is a legitimate block.
DEFAULT_RAMP_PCT = Decimal("10")

# Below this a week is not a baseline. Comparing week two against a week that
# contained one twenty minute spin would make almost any second week a breach,
# so the ramp rule does not apply until the prior week is a real week.
MIN_BASELINE_LOAD = Decimal("20")


class LoadCeilingBreached(RuntimeError):
    """BLOCK-07: the generated week exceeds what the ramp limit allows."""


def gym_coefficient() -> Decimal:
    return _decimal("COACH_GYM_LOAD_COEFFICIENT", DEFAULT_GYM_COEFFICIENT, minimum=Decimal("0"))


def ramp_pct() -> Decimal:
    return _decimal("COACH_WEEKLY_LOAD_RAMP_PCT", DEFAULT_RAMP_PCT, minimum=Decimal("0"))


def _decimal(name: str, default: Decimal, minimum: Decimal) -> Decimal:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = Decimal(raw)
    except Exception:  # noqa: BLE001 - any malformed value is the same mistake
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value < minimum:
        log.warning("%s=%s is below %s; using the default", name, value, minimum)
        return default
    return value


def gym_load(rpe: float | Decimal, duration_minutes: int | Decimal) -> Decimal:
    """GYM-08: RPE times minutes times the coefficient."""
    return (Decimal(str(rpe)) * Decimal(str(duration_minutes)) * gym_coefficient()).quantize(
        Decimal("0.01")
    )


def cycling_load(intensity_factor: float | Decimal, duration_seconds: int) -> Decimal:
    """The standard model: IF squared, times hours, times 100.

    An hour at threshold is 100. Expressed here rather than taken from the
    platform because MEM-08 keeps derived figures ours, and because a *planned*
    session has no upstream number to take.
    """
    intensity = Decimal(str(intensity_factor))
    hours = Decimal(duration_seconds) / Decimal(3600)
    return (intensity * intensity * hours * Decimal(100)).quantize(Decimal("0.01"))


def of_spec(discipline: str, spec: dict) -> Decimal:
    """The planned load of one prescription, whichever discipline it is.

    Returns zero rather than raising when a spec carries nothing to compute
    from. A rest day has no load, and so does a session whose spec is still
    being written; neither should stop a week being costed.
    """
    duration_s = spec.get("duration_s") or 0
    if discipline in GYM_DISCIPLINES:
        rpe = spec.get("rpe_target")
        if rpe is None or not duration_s:
            return Decimal("0.00")
        return gym_load(rpe, Decimal(duration_s) / Decimal(60))

    intensity = spec.get("intensity_factor")
    if intensity is None:
        target = spec.get("target_watts")
        ftp = spec.get("ftp_watts")
        if not target or not ftp:
            return Decimal("0.00")
        intensity = Decimal(str(target)) / Decimal(str(ftp))
    if not duration_s:
        return Decimal("0.00")
    return cycling_load(intensity, duration_s)


# GYM-04: gym load is tracked as session count, RPE and duration rather than
# tonnage, so these are the disciplines that cost RPE-minutes instead of
# intensity-hours.
GYM_DISCIPLINES = frozenset({"gym", "weighttraining", "workout", "strength"})


@dataclass(frozen=True)
class Week:
    """One planned week on the combined scale."""

    starts_on: date
    total: Decimal
    cycling: Decimal
    gym: Decimal
    sessions: int

    @property
    def is_baseline(self) -> bool:
        return self.total >= MIN_BASELINE_LOAD


def week_of(day: date) -> date:
    """The Monday of the local training week containing a date."""
    return day - timedelta(days=day.weekday())


def planned_weeks(conn: psycopg.Connection, block_id: int | None = None) -> list[Week]:
    """Planned load per week, computed in SQL (MEM-08).

    Reads `planned_load` off the row rather than recomputing from the spec: a
    coefficient change must not retroactively rewrite what a past week was
    allowed to be.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select date_trunc('week', planned_for)::date as starts_on,
                   coalesce(sum(planned_load), 0)::numeric as total,
                   coalesce(sum(planned_load) filter (
                       where lower(discipline) <> all(%s)), 0)::numeric as cycling,
                   coalesce(sum(planned_load) filter (
                       where lower(discipline) = any(%s)), 0)::numeric as gym,
                   count(*)::int as sessions
              from prescriptions
             where status <> 'cancelled'
               and (%s::bigint is null or block_id = %s)
             group by 1
             order by 1
            """,
            (list(GYM_DISCIPLINES), list(GYM_DISCIPLINES), block_id, block_id),
        )
        return [Week(**row) for row in cur.fetchall()]


def ceiling_for(previous: Week | None, pct: Decimal | None = None) -> Decimal | None:
    """What BLOCK-07 allows a week to reach, given the week before it.

    None means unbounded: there is no prior week, or the prior week was too
    small to be a baseline. A first block has nothing to ramp from, and refusing
    to generate one would make the rule impossible to satisfy rather than safe.
    """
    if previous is None or not previous.is_baseline:
        return None
    limit = pct if pct is not None else ramp_pct()
    return (previous.total * (Decimal(100) + limit) / Decimal(100)).quantize(Decimal("0.01"))


def check_ramp(weeks: list[Week], pct: Decimal | None = None) -> list[str]:
    """Every BLOCK-07 breach across a sequence of weeks.

    Returns the breaches rather than raising, so a caller generating a whole
    block can report all of them at once instead of one per attempt.
    """
    breaches = []
    for previous, current in zip(weeks, weeks[1:], strict=False):
        ceiling = ceiling_for(previous, pct)
        if ceiling is not None and current.total > ceiling:
            breaches.append(
                f"week of {current.starts_on} plans {current.total} against a ceiling of "
                f"{ceiling} ({previous.total} the week before, +{pct or ramp_pct()}%). "
                f"cycling {current.cycling}, gym {current.gym}"
            )
    return breaches


def would_breach(
    conn: psycopg.Connection,
    planned_for: date,
    added_load: Decimal,
    block_id: int | None = None,
    pct: Decimal | None = None,
) -> str | None:
    """GYM-05: would adding this session put its week over the ceiling?

    The acceptance is "a week at the cycling ceiling cannot add a gym session
    without reducing elsewhere", and this is the function that says so. It reads
    the *combined* total, which is the whole reason GYM-08 exists.
    """
    weeks = {w.starts_on: w for w in planned_weeks(conn, block_id)}
    starts_on = week_of(planned_for)
    current = weeks.get(starts_on)
    previous = weeks.get(starts_on - timedelta(days=7))

    ceiling = ceiling_for(previous, pct)
    if ceiling is None:
        return None

    projected = (current.total if current else Decimal(0)) + added_load
    if projected <= ceiling:
        return None
    return (
        f"adding {added_load} to the week of {starts_on} would reach {projected}, "
        f"over the ceiling of {ceiling}"
    )
