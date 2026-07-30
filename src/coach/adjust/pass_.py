"""One session in, whatever the week needed out. The P09 entry point.

ADJ-01 says the rules are evaluated "on FIT ingest", so this is what
`coach.ingest.service.on_activity` calls. It is three lines of real logic and a
lot of care about ordering, which is why it is here rather than inlined there:
ingest should not have to know that authority precedes application.

**It is optional at the call site.** `on_activity` takes an `adjust` flag that
defaults off, the same way it takes `write_note`. A backfill replaying two years
of rides must not restructure anything — the weeks are long gone, ADJ-04 would
reject it anyway, and running the rules over history would burn a week's ADJ-05
budget on a ride from 2024.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg

from coach.adjust import apply as applymod
from coach.adjust import authority as authmod
from coach.adjust import triggers as trigmod
from coach.blocks import load as loadmod

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    """What one evaluation did, in full. OBS-04 reads this shape from the logs."""

    applied: list[applymod.Applied] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(a.prescription_id for a in self.applied)


def run(
    conn: psycopg.Connection,
    session_id: int,
    now: datetime,
    tz: Any,
    send: Any = None,
    safe_to_act: bool = True,
) -> Outcome:
    """Evaluate, decide, act. In that order, once per session.

    The pricing happens between the decision's other checks and ADJ-02's, which is
    why `apply.project` is called here rather than inside either module: the
    action's meaning belongs to :mod:`apply`, the bound belongs to
    :mod:`authority`, and neither should import the other to find out.
    """
    result = Outcome()

    for proposal in trigmod.evaluate(conn, session_id):
        projected = _price(conn, proposal)
        decision = authmod.decide(
            conn, proposal, now, tz, projected_load=projected, safe_to_act=safe_to_act
        )

        if decision.applies:
            applied = applymod.execute(conn, proposal, decision, now, send=send)
            if applied is not None:
                result.applied.append(applied)
            continue

        if decision.defers:
            week = loadmod.week_of(now.astimezone(tz).date())
            applymod.defer(conn, proposal, decision, week)
            result.deferred.append(f"{proposal.trigger}: {decision.requirement}")
            continue

        result.rejected.append(f"{proposal.trigger}: {decision.reason}")

    if result.applied or result.deferred or result.rejected:
        log.info(
            "session %s: applied %s, deferred %s, rejected %s",
            session_id,
            [a.action for a in result.applied],
            result.deferred,
            result.rejected,
        )
    return result


def _price(conn: psycopg.Connection, proposal: trigmod.Proposal) -> Any:
    """What the target would cost after the change, on GYM-08's scale.

    None for a proposal that changes no prescription, which authority reads as
    "nothing to price" rather than as a failure to price it.
    """
    if proposal.target_prescription_id is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "select planned_for, discipline, spec from prescriptions where id = %s",
            (proposal.target_prescription_id,),
        )
        target = cur.fetchone()
    if target is None:
        return None

    after_spec, _ = applymod.project(
        dict(target["spec"] or {}), target["discipline"], proposal.action, target["planned_for"]
    )
    return applymod.projected_load(after_spec, target["discipline"])
