"""Intake rollups, and the one rule about what may be concluded from them.

NUT-01 to NUT-06. `macros.py` receives and stores meals; this reads them back as
the figures the weekly review and the coach's context are allowed to quote.

**Every number here is computed in SQL** (MEM-08). That is not a performance
choice. A 7 day protein average the model arrived at by adding up meals in its
context window is a number nobody can reproduce, and the requirement that
matters — NUT-03's exclusion of gap days — is exactly the kind of thing a model
does inconsistently and silently.

**NUT-03 falls out of the grouping rather than being enforced.** A day with no
meals produces no row in the per-day subquery, so `avg` never sees it. Counting
it as zero would take extra code, which is the right way round: the failure mode
the requirement guards against is not reachable by accident.

**NUT-04 is a rule about silence.** An energy balance estimate and a weight trend
that disagree is the normal state of affairs for the first three weeks, because
the trend has not earned the right to arbitrate yet. `trend.Claims` already
computes that right — `may_arbitrate_energy_balance`, the same weekly-coverage
bar HLTH-16 uses for a plateau — so this consumes it rather than restating it.
Below the bar the answer is "no claim", not a hedged claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import psycopg

from coach.health import trend as trendmod
from coach.memory import facts as factmod

# NUT-01 names both.
WINDOWS = (7, 28)

# The fact NUT-02 reads. Seeded in migration 002, so changing the target is a
# conversation rather than a deploy.
PROTEIN_TARGET_KEY = "goal.protein_target_g"


@dataclass(frozen=True)
class Window:
    """Averages over the days that have intake logged, and how many that was.

    `logged_days` is reported alongside every average deliberately. "2.1 g/kg of
    protein" over two logged days out of seven is a different statement from the
    same figure over seven, and the review has to be able to say which.
    """

    days: int
    logged_days: int
    kcal: Decimal | None
    protein_g: Decimal | None
    carbs_g: Decimal | None
    fat_g: Decimal | None
    fibre_g: Decimal | None
    target_g: Decimal | None
    days_meeting_target: int

    @property
    def coverage(self) -> Decimal:
        """Logged days as a share of the window. NUT-03's other half.

        Excluding gap days from the average is right and would be misleading on
        its own — it makes a badly logged week look like a well-fed one.
        """
        return Decimal(self.logged_days) / Decimal(self.days)

    @property
    def adherence(self) -> Decimal | None:
        """NUT-02: share of logged days at or above the protein target.

        Over logged days rather than over the window, for the same reason the
        averages are: a day with no data is not a day the athlete missed the
        target, it is a day nobody knows about.
        """
        if self.target_g is None or self.logged_days == 0:
            return None
        return Decimal(self.days_meeting_target) / Decimal(self.logged_days)


def protein_target(conn: psycopg.Connection) -> Decimal | None:
    """NUT-02's target, from facts. None when it has never been stated."""
    fact = factmod.active_for(conn, PROTEIN_TARGET_KEY)
    if fact is None:
        return None
    try:
        return Decimal(str(fact.value))
    except (ArithmeticError, ValueError):
        return None


_WINDOW_SQL = """
with per_day as (
  select local_date,
         sum(kcal)      as kcal,
         sum(protein_g) as protein_g,
         sum(carbs_g)   as carbs_g,
         sum(fat_g)     as fat_g,
         sum(fibre_g)   as fibre_g
    from meals
   where local_date between %(since)s and %(until)s
   group by local_date
)
select count(*)::int                                       as logged_days,
       avg(kcal)                                           as kcal,
       avg(protein_g)                                      as protein_g,
       avg(carbs_g)                                        as carbs_g,
       avg(fat_g)                                          as fat_g,
       avg(fibre_g)                                        as fibre_g,
       count(*) filter (where protein_g >= %(target)s)::int as days_meeting_target
  from per_day
"""


def window(
    conn: psycopg.Connection, as_of: date, days: int, target: Decimal | None = None
) -> Window:
    """NUT-01: one window's averages, over logged days only.

    `as_of` is inclusive, so a 7 day window is the week ending that day.
    """
    goal = target if target is not None else protein_target(conn)
    with conn.cursor() as cur:
        cur.execute(
            _WINDOW_SQL,
            {
                "since": as_of - timedelta(days=days - 1),
                "until": as_of,
                # A NULL target makes the FILTER match nothing, which is what
                # `days_meeting_target` should be when there is no target.
                "target": goal,
            },
        )
        row = cur.fetchone() or {}
    return Window(
        days=days,
        logged_days=row.get("logged_days") or 0,
        kcal=row.get("kcal"),
        protein_g=row.get("protein_g"),
        carbs_g=row.get("carbs_g"),
        fat_g=row.get("fat_g"),
        fibre_g=row.get("fibre_g"),
        target_g=goal,
        days_meeting_target=row.get("days_meeting_target") or 0,
    )


def rollup(
    conn: psycopg.Connection, as_of: date, windows: tuple[int, ...] = WINDOWS
) -> list[Window]:
    """Every window NUT-01 names, computed once so the target is read once."""
    goal = protein_target(conn)
    return [window(conn, as_of, days, target=goal) for days in windows]


@dataclass(frozen=True)
class Arbitration:
    """NUT-04: whether the weight trend is allowed to settle a disagreement.

    `may_arbitrate` is the whole requirement. When it is false the correct output
    is that neither source is right yet — not a softened version of one of them,
    which is what "present it as an estimate" degrades into if the gate is
    missing.
    """

    may_arbitrate: bool
    weeks_covered: int
    readings: int

    @property
    def verdict(self) -> str:
        if self.may_arbitrate:
            return "trend arbitrates"
        return "no claim"


def arbitration(conn: psycopg.Connection, as_of: date) -> Arbitration:
    """Ask the body mass trend whether it has earned the right to arbitrate.

    The bar is HLTH-16's, stated once in `trend.Claims` and read here. NUT-04 and
    HLTH-16 are the same condition seen from two directions, and the PRD's own
    table says so.
    """
    fit = trendmod.fit(conn, as_of)
    claims = trendmod.Claims.of(fit)
    return Arbitration(
        may_arbitrate=claims.may_arbitrate_energy_balance,
        weeks_covered=fit.weeks_covered,
        readings=fit.n,
    )


def render(windows: list[Window], verdict: Arbitration) -> str:
    """The review's intake section (NUT-06), and the coach's context.

    Prose rather than a table because it is read in Telegram, and every figure
    carries its logged-day count so no average can be mistaken for a full week.
    """
    if not windows or all(w.logged_days == 0 for w in windows):
        return "Intake: nothing logged."

    lines = []
    for w in windows:
        if w.logged_days == 0:
            lines.append(f"- last {w.days} days: nothing logged")
            continue
        parts = [f"{_round(w.kcal)} kcal", f"{_round(w.protein_g)} g protein"]
        if w.target_g is not None and w.adherence is not None:
            parts.append(
                f"{w.days_meeting_target}/{w.logged_days} days at or above {_round(w.target_g)} g"
            )
        lines.append(
            f"- last {w.days} days: {', '.join(parts)} "
            f"(averaged over {w.logged_days} logged day{'s' if w.logged_days != 1 else ''})"
        )

    if not verdict.may_arbitrate:
        lines.append(
            "- energy balance: an estimate only. The weight trend cannot arbitrate yet "
            f"({verdict.weeks_covered} of {trendmod.PLATEAU_MIN_WEEKS_COVERED} weeks covered), "
            "so a disagreement between intake and the scale settles nothing."
        )
    return "Intake:\n" + "\n".join(lines)


def _round(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return str(int(Decimal(value).to_integral_value()))
