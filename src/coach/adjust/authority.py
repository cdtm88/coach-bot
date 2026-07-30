"""The bounds on an automatic change. ADJ-02, ADJ-03, ADJ-04, ADJ-05, ADJ-08.

One function decides whether a proposal may happen autonomously, and it is
deliberately the only one. Design section 10: "Authority is bounded, and the
boundary is asymmetric" — downgrades fail safe, upgrades wait for a conversation,
"given a de-trained starting point and a spinal history".

**Why this is separate from the rules.** A rule that approved its own change
would make every mistake in a proposal a mistake in the athlete's training, and
every new rule a fresh chance to get the bound wrong. Here the checks run once,
in code that knows nothing about cycling, against the combined load figure GYM-08
defines.

**The order of the checks is the design.** Cheapest and most absolute first:

1. Is it an upgrade, or a rule that knows it defers?      ADJ-03
2. Is the target inside the current week?                 ADJ-04
3. Has the week's one restructure already been spent,
   or has this prescription already been adjusted?        ADJ-05
4. Is it safe to act at all on what we know?              ADJ-08
5. Does the change actually reduce weekly load?           ADJ-02

ADJ-02 is last because it is the only one that has to model the change to answer,
and there is no point pricing a change that was never allowed.

**Deferring is not failing.** ADJ-03 and ADJ-05 both say the proposal goes to the
Sunday review, and REV-04 reads it there. So the outcomes are apply, defer and
reject, and only the third means nobody will ever look at it — reserved for a
proposal that would *increase* load, which is the one thing that must not survive
in any form.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import psycopg

from coach.adjust import triggers as trigmod
from coach.blocks import load as loadmod

log = logging.getLogger(__name__)

# ADJ-05: "At most one autonomous restructure per week."
MAX_AUTONOMOUS_PER_WEEK = 1


class Outcome(Enum):
    APPLY = "apply"
    DEFER = "defer"
    REJECT = "reject"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str
    # Which requirement decided. Written to the record, so "why was this not
    # applied" has an answer that names a rule rather than describing a mood.
    requirement: str = ""

    @property
    def applies(self) -> bool:
        return self.outcome is Outcome.APPLY

    @property
    def defers(self) -> bool:
        return self.outcome is Outcome.DEFER


def week_of_prescription(conn: psycopg.Connection, prescription_id: int) -> date | None:
    with conn.cursor() as cur:
        cur.execute("select planned_for from prescriptions where id = %s", (prescription_id,))
        row = cur.fetchone()
    return loadmod.week_of(row["planned_for"].date()) if row else None


def autonomous_this_week(conn: psycopg.Connection, week_start: date) -> int:
    """How much of ADJ-05's budget the week has already spent.

    Counts on `authority = 'automatic'`, which is why that column exists: a
    PLAN-04 calendar placement and an athlete's own upstream edit both write to
    `adjustment_events` too, and neither is the coach spending its authority.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) as n
              from adjustment_events a
              join prescriptions p on p.id = a.prescription_id
             where a.authority = 'automatic'
               and date_trunc('week', p.planned_for)::date = %s
            """,
            (week_start,),
        )
        return int((cur.fetchone() or {}).get("n") or 0)


def already_adjusted(conn: psycopg.Connection, prescription_id: int) -> bool:
    """ADJ-05's second clause: "never the same prescription twice".

    Separate from the weekly count, and both are needed. The count stops a bad
    week turning into five changes; this stops one session being whittled down by
    a rule that fires again the next day.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from adjustment_events where prescription_id = %s and authority = 'automatic'"
            " limit 1",
            (prescription_id,),
        )
        return cur.fetchone() is not None


def _reduces_load(
    conn: psycopg.Connection, proposal: trigmod.Proposal, projected: Decimal
) -> tuple[bool, str]:
    """ADJ-02, priced on the combined scale.

    "Any generated change that increases computed weekly load is rejected and
    logged." Compared against the week's current planned total rather than against
    the target session alone, because the requirement is about the *week* — and a
    change that shortened Thursday while somehow adding to Saturday would pass a
    per-session check and fail the actual rule.
    """
    target_id = proposal.target_prescription_id
    if target_id is None:
        return True, "no prescription is modified"

    week_start = week_of_prescription(conn, target_id)
    if week_start is None:
        return False, "the target prescription no longer exists"

    weeks = {w.starts_on: w for w in loadmod.planned_weeks(conn)}
    current = weeks.get(week_start)
    before = current.total if current else Decimal(0)

    with conn.cursor() as cur:
        cur.execute("select planned_load from prescriptions where id = %s", (target_id,))
        row = cur.fetchone() or {}
    was = Decimal(str(row.get("planned_load") or 0))

    after = before - was + projected
    if after > before:
        return False, f"would take the week from {before} to {after} on the combined scale"
    return True, f"reduces the week from {before} to {after} on the combined scale"


def decide(
    conn: psycopg.Connection,
    proposal: trigmod.Proposal,
    now: datetime,
    tz: Any,
    projected_load: Decimal | None = None,
    safe_to_act: bool = True,
) -> Decision:
    """May this proposal happen autonomously, right now?

    `projected_load` is what the target would cost after the change, computed by
    :mod:`coach.adjust.apply` because it owns what each action does. Passed in
    rather than computed here so this module stays free of any opinion about
    training, which is what makes the bound trustworthy.

    `safe_to_act` is ADJ-08's, from `coach.ingest.review.missed`. False means the
    wellness feed had nothing for the day, so the coach does not know whether the
    session was skipped or the upload failed.
    """
    # A `note` changes nothing and needs no authority. Checked first so a decision
    # to leave the week alone is never reported as a rejection.
    if proposal.action == "note":
        return Decision(Outcome.APPLY, "records what happened and changes nothing", "ADJ-01")

    # 1. ADJ-03: upgrades, added sessions and intensity rises wait for the review.
    if proposal.review_only or proposal.action == "propose_progression":
        return Decision(
            Outcome.DEFER,
            "an increase in load or intensity is the Sunday review's to decide",
            "ADJ-03",
        )

    if proposal.target_prescription_id is None:
        return Decision(Outcome.REJECT, "nothing to change", "ADJ-01")

    # 2. ADJ-04: the current week only.
    target_week = week_of_prescription(conn, proposal.target_prescription_id)
    if target_week is None:
        return Decision(Outcome.REJECT, "the target prescription no longer exists", "ADJ-04")
    this_week = loadmod.week_of(now.astimezone(tz).date())
    if target_week != this_week:
        return Decision(
            Outcome.DEFER,
            f"the target is in the week of {target_week}, not the current week",
            "ADJ-04",
        )

    # 3. ADJ-05: one restructure per week, and never the same prescription twice.
    if already_adjusted(conn, proposal.target_prescription_id):
        return Decision(
            Outcome.DEFER,
            "this prescription has already been adjusted automatically once",
            "ADJ-05",
        )
    spent = autonomous_this_week(conn, this_week)
    if spent >= MAX_AUTONOMOUS_PER_WEEK:
        # Design section 10: "Repeated triggering means the block is wrong, which
        # is a conversation, not a rule." Deferring is how that conversation gets
        # started rather than suppressed.
        return Decision(
            Outcome.DEFER,
            f"the week has already had {spent} autonomous restructure(s); "
            "repeated triggering means the block is wrong, which is a conversation",
            "ADJ-05",
        )

    # 4. ADJ-08: never restructure on an ambiguous absence.
    if not safe_to_act:
        return Decision(
            Outcome.DEFER,
            "the recovery and load signal was unavailable, so what happened is not known",
            "ADJ-08",
        )

    # 5. ADJ-02: and only ever downward.
    if projected_load is None:
        return Decision(
            Outcome.REJECT,
            "the change was not priced, so it cannot be shown to reduce load",
            "ADJ-02",
        )
    ok, detail = _reduces_load(conn, proposal, projected_load)
    if not ok:
        # "rejected and logged", and logged at warning: a rule that proposed an
        # increase is a bug in the rule, not a normal outcome.
        log.warning(
            "ADJ-02 rejected %s on prescription %s: %s",
            proposal.trigger,
            proposal.target_prescription_id,
            detail,
        )
        return Decision(Outcome.REJECT, detail, "ADJ-02")

    return Decision(Outcome.APPLY, detail, "ADJ-02")
