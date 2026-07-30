"""ADJ-01: the fixed set of trigger rules, evaluated on FIT ingest.

"FIT ingest evaluates a fixed set of trigger rules against the remaining week.
Each rule fires deterministically on seeded input."

**Fixed** is the load-bearing word. The rules are the six rows of design section
10's trigger table and nothing else, they are pure functions of the session and
its context, and a model is never asked which one applies. A coach that decided
case by case what a hard session meant would be unpredictable in the one place
the athlete needs to be able to trust it.

Each rule returns a :class:`Proposal` or nothing. A proposal is a *suggestion* —
it says what the rule wants and what it saw, and it carries no opinion on whether
it is allowed. :mod:`coach.adjust.authority` decides that, and the separation is
what stops a new rule quietly widening the coach's authority.

**Nothing here reads the clock or writes anything.** Everything is derived from
what is already stored, so a rule can be run twice on the same session and
produce the same answer, which is what "deterministically" requires.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from coach.blocks import load as loadmod
from coach.health import recovery as recoverymod

log = logging.getLogger(__name__)

# --- thresholds --------------------------------------------------------------
#
# Every one of these is a coaching judgement rather than a discovered constant.
# They live here, named, so a change is a change to a number in one place and
# shows up in a diff — not spread through the conditions that read them.

# "Session well over prescribed intensity or duration." Over by a quarter on
# duration, or a tenth on intensity. Intensity is tighter because a 10% miss on
# power is a different session, while 10% long is often just traffic lights.
OVER_DURATION_RATIO = Decimal("1.25")
OVER_INTENSITY_RATIO = Decimal("1.10")

# "Abandoned early." Half the prescribed duration or less. Deliberately far from
# `SHORT_RATIO` below: there is a real difference between cutting a session short
# and stopping, and only the second says something about the athlete's state.
ABANDONED_RATIO = Decimal("0.50")

# "Power fade against prescription." Completed the time but well under the power
# it was set at, which is what fading looks like in the numbers.
FADE_INTENSITY_RATIO = Decimal("0.85")
FADE_MIN_DURATION_RATIO = Decimal("0.80")

# "Session completed short or easy" — noted, no compensatory loading. The band
# between this and abandonment.
SHORT_RATIO = Decimal("0.90")
EASY_RATIO = Decimal("0.90")

# A recovery flag. The deviation is standardised against the athlete's own
# trailing 28 days, so this is in standard deviations below their own normal.
RECOVERY_FLAG_DEVIATION = Decimal("-1.0")

# "Sustained overperformance" — how many sessions over prescription, inside the
# lookback, before it is a pattern rather than a good day.
OVERPERFORMANCE_SESSIONS = 3
OVERPERFORMANCE_DAYS = 14


# --- what a rule produces ----------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """One rule's suggestion. Carries no authority and changes nothing.

    `action` is the vocabulary :mod:`coach.adjust.apply` knows how to perform, and
    it is deliberately small: every member of it reduces load or leaves it alone.
    A rule cannot propose an increase because there is no word for one — ADJ-02
    enforced by the type rather than by a check that could be forgotten.
    """

    trigger: str
    action: str
    target_prescription_id: int | None
    session_id: int | None
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    # Set by rules whose whole point is that they defer (ADJ-03). Authority
    # honours it rather than re-deriving it, so a rule that knows it is proposing
    # an upgrade says so once.
    review_only: bool = False


# The actions a rule may ask for. Each either reduces the combined load figure or
# leaves it untouched; `note` exists so "leave the week alone" is a decision the
# record shows rather than an absence of one.
ACTIONS = ("shorten", "ease", "move_later", "convert_to_rest", "note", "propose_progression")

# How much a downgrade takes off. One step, not a computed optimum: the point is
# to reduce load, and a rule that tuned the exact amount would be doing the
# coaching that the Sunday review and a conversation do better.
SHORTEN_TO = Decimal("0.75")
EASE_INTENSITY_TO = Decimal("0.90")


# --- the rules ---------------------------------------------------------------


def _next_hard_session(
    conn: psycopg.Connection, after: datetime, week_of: date
) -> dict[str, Any] | None:
    """The next unridden session in the same week that is worth downgrading.

    "Hard" is the highest planned load remaining rather than the soonest. Easing
    tomorrow's recovery spin would satisfy the letter of a downgrade and none of
    its purpose; the session that costs the most is the one whose reduction the
    athlete will feel.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, planned_for, discipline, spec, planned_load
              from prescriptions
             where session_id is null
               and status in ('planned', 'adjusted')
               and planned_for > %s
               and date_trunc('week', planned_for)::date = %s
             order by planned_load desc nulls last, planned_for
             limit 1
            """,
            (after, week_of),
        )
        return cur.fetchone()


def _ratios(compliance: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    duration = compliance.get("duration_ratio")
    intensity = compliance.get("intensity_ratio")
    return (
        Decimal(str(duration)) if duration is not None else None,
        Decimal(str(intensity)) if intensity is not None else None,
    )


def over_prescription(
    conn: psycopg.Connection, session: dict[str, Any], compliance: dict[str, Any]
) -> Proposal | None:
    """Row 1: well over prescribed intensity or duration → ease the next hard session.

    The athlete going harder than asked is not a problem to be corrected; it is
    load that was not planned for, and the week has to absorb it somewhere. Easing
    what comes next is how, and it is a reduction, so it needs nobody's permission.
    """
    duration, intensity = _ratios(compliance)
    over_duration = duration is not None and duration >= OVER_DURATION_RATIO
    over_intensity = intensity is not None and intensity >= OVER_INTENSITY_RATIO
    if not (over_duration or over_intensity):
        return None

    target = _next_hard_session(conn, session["started_at"], loadmod.week_of(session["local_date"]))
    if target is None:
        # Nothing left this week. ADJ-04 forbids reaching into the next one, so
        # there is genuinely nothing to do — and saying so is not a proposal.
        return None

    which = []
    if over_duration:
        which.append(f"{duration:.2f}x the prescribed duration")
    if over_intensity:
        which.append(f"{intensity:.2f}x the prescribed intensity")

    return Proposal(
        trigger="over_prescription",
        action="ease",
        target_prescription_id=int(target["id"]),
        session_id=int(session["id"]),
        reason=f"rode {' and '.join(which)}; easing the next hard session",
        evidence={
            "duration_ratio": float(duration) if duration is not None else None,
            "intensity_ratio": float(intensity) if intensity is not None else None,
            "source_session": int(session["id"]),
        },
    )


def abandoned_or_faded(
    conn: psycopg.Connection, session: dict[str, Any], compliance: dict[str, Any]
) -> Proposal | None:
    """Row 2: abandoned early, or power fade → downgrade the next hard session.

    Both are the same signal read two ways: the athlete could not hold what was
    asked. Fading needs the duration to be nearly complete, because a short ride
    at low power is abandonment and would otherwise match both rules.
    """
    duration, intensity = _ratios(compliance)

    abandoned = duration is not None and duration <= ABANDONED_RATIO
    faded = (
        intensity is not None
        and duration is not None
        and intensity <= FADE_INTENSITY_RATIO
        and duration >= FADE_MIN_DURATION_RATIO
    )
    if not (abandoned or faded):
        return None

    target = _next_hard_session(conn, session["started_at"], loadmod.week_of(session["local_date"]))
    if target is None:
        return None

    if abandoned:
        why = f"stopped at {duration:.0%} of the prescribed duration"
    else:
        why = f"held {intensity:.0%} of the prescribed power over a full session"

    return Proposal(
        trigger="abandoned_or_faded",
        action="ease",
        target_prescription_id=int(target["id"]),
        session_id=int(session["id"]),
        reason=f"{why}; downgrading the next hard session to endurance",
        evidence={
            "duration_ratio": float(duration) if duration is not None else None,
            "intensity_ratio": float(intensity) if intensity is not None else None,
            "kind": "abandoned" if abandoned else "power_fade",
            "source_session": int(session["id"]),
        },
    )


def poor_session_on_low_recovery(
    conn: psycopg.Connection, session: dict[str, Any], compliance: dict[str, Any]
) -> Proposal | None:
    """Row 3: a recovery flag plus a poor session → convert the next to rest.

    The strongest of the automatic rules and the only one that converts rather
    than eases, because it is the only one with two independent signals agreeing:
    the athlete's own body said something was wrong *and* the session bore it out.

    Requires a usable deviation. RECOV-02 degrades gracefully when fields are
    withheld, but an unusable deviation is the coach not knowing, and not knowing
    is never grounds for acting — the same reasoning as ADJ-08.
    """
    duration, intensity = _ratios(compliance)
    poor = (duration is not None and duration < SHORT_RATIO) or (
        intensity is not None and intensity < EASY_RATIO
    )
    if not poor:
        return None

    deviation = recoverymod.for_day(conn, session["local_date"])
    if deviation is None or not deviation.usable:
        return None
    if deviation.deviation is None or deviation.deviation > RECOVERY_FLAG_DEVIATION:
        return None

    target = _next_hard_session(conn, session["started_at"], loadmod.week_of(session["local_date"]))
    if target is None:
        return None

    return Proposal(
        trigger="poor_session_on_low_recovery",
        action="convert_to_rest",
        target_prescription_id=int(target["id"]),
        session_id=int(session["id"]),
        reason=(
            f"recovery {deviation.deviation:.2f} standard deviations below your own "
            "normal, and the session went badly; converting the next hard session to rest"
        ),
        evidence={
            "recovery_deviation": float(deviation.deviation),
            "recovery_fields_used": deviation.fields_used,
            "recovery_baseline_n": deviation.baseline_n,
            "duration_ratio": float(duration) if duration is not None else None,
            "intensity_ratio": float(intensity) if intensity is not None else None,
            "source_session": int(session["id"]),
        },
    )


def completed_short(
    conn: psycopg.Connection, session: dict[str, Any], compliance: dict[str, Any]
) -> Proposal | None:
    """Row 4: completed short or easy → note it, leave the week alone.

    "No compensatory loading", and this rule exists to make that a recorded
    decision rather than a gap in the log. Adding the missed work back is exactly
    the kind of well-meant increase ADJ-02 forbids, and the reason it is forbidden
    is that the week was already the plan.

    Fires only when nothing sharper did — the caller drops it if another rule
    matched, because "we shortened Thursday" is the more useful record.
    """
    duration, intensity = _ratios(compliance)
    short = duration is not None and ABANDONED_RATIO < duration < SHORT_RATIO
    easy = intensity is not None and intensity < EASY_RATIO
    if not (short or easy):
        return None

    return Proposal(
        trigger="completed_short",
        action="note",
        target_prescription_id=None,
        session_id=int(session["id"]),
        reason="came in under prescription; noted, and the week is unchanged",
        evidence={
            "duration_ratio": float(duration) if duration is not None else None,
            "intensity_ratio": float(intensity) if intensity is not None else None,
            "no_compensatory_loading": True,
        },
    )


def sustained_overperformance(
    conn: psycopg.Connection, session: dict[str, Any], _compliance: dict[str, Any]
) -> Proposal | None:
    """Row 5: sustained overperformance → propose a block progression. Review only.

    ADJ-03: "Overperformance produces a review proposal, never an immediate
    change." So this returns a proposal marked `review_only`, and the authority
    gate files it for the Sunday review without touching a prescription. The
    coach noticing is useful; the coach acting on it alone is the thing the
    asymmetry exists to prevent.
    """
    since = session["local_date"] - timedelta(days=OVERPERFORMANCE_DAYS)
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) as n
              from prescriptions p
              join sessions s on s.id = p.session_id
             where s.local_date between %s and %s
               and (p.compliance->>'duration_ratio')::numeric >= %s
            """,
            (since, session["local_date"], OVER_DURATION_RATIO),
        )
        over = int((cur.fetchone() or {}).get("n") or 0)

    if over < OVERPERFORMANCE_SESSIONS:
        return None

    return Proposal(
        trigger="sustained_overperformance",
        action="propose_progression",
        target_prescription_id=None,
        session_id=int(session["id"]),
        reason=(
            f"{over} sessions over prescription in {OVERPERFORMANCE_DAYS} days; "
            "the block may be too easy"
        ),
        evidence={"sessions_over": over, "window_days": OVERPERFORMANCE_DAYS},
        review_only=True,
    )


# Evaluated in order, and the order is a priority. The two-signal recovery rule
# comes first because it is the best-evidenced; `completed_short` comes last
# because it is what is true when nothing sharper is.
RULES = (
    poor_session_on_low_recovery,
    abandoned_or_faded,
    over_prescription,
    sustained_overperformance,
    completed_short,
)

# Rules that change the week. `completed_short` and the progression proposal do
# not, so they are allowed to accompany one rather than compete with it.
RESTRUCTURING = frozenset(
    {"poor_session_on_low_recovery", "abandoned_or_faded", "over_prescription"}
)


def evaluate(conn: psycopg.Connection, session_id: int) -> list[Proposal]:
    """ADJ-01: run every rule against one session. Writes nothing.

    Returns at most one restructuring proposal — the first that matched, by the
    priority in :data:`RULES` — plus any non-restructuring proposal that also
    applies. Two rules both wanting to downgrade the same week would be the same
    conclusion twice, and ADJ-05 caps the week at one restructure anyway; picking
    here rather than letting authority reject the loser keeps the reason the
    athlete is eventually told the *best* reason rather than the earliest.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select s.id, s.started_at, s.local_date, s.discipline, p.compliance
              from sessions s
              left join prescriptions p on p.session_id = s.id
             where s.id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()

    if row is None:
        return []
    compliance = row.get("compliance") or {}
    if not compliance:
        # Nothing was prescribed, so there is nothing to compare against. An
        # unprescribed ride is not evidence about a plan it was never part of.
        return []

    proposals: list[Proposal] = []
    restructured = False
    for rule in RULES:
        proposal = rule(conn, row, compliance)
        if proposal is None:
            continue
        if proposal.trigger in RESTRUCTURING:
            if restructured:
                continue
            restructured = True
        proposals.append(proposal)

    # "Noted, no change" is only worth recording when nothing else happened.
    if restructured:
        proposals = [p for p in proposals if p.trigger != "completed_short"]

    if proposals:
        log.info("session %s triggered %s", session_id, ", ".join(p.trigger for p in proposals))
    return proposals
