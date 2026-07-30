"""Performing an adjustment, recording it, and deciding whether to say so.

ADJ-06 (the 12 hour notice rule) and ADJ-07 (every change is recorded). This is
also where each action's meaning lives — what `ease` actually does to a spec —
because :mod:`coach.adjust.authority` has to be able to price a change without
holding any opinion about training.

**ADJ-06 is a rule about interruption, not about logging.** "A change inside 12
hours of the original start sends a Telegram message; otherwise the reason is
written to the calendar event only." A session moved next Thursday is something
the athlete will see when they look; a session eased two hours before they get on
the bike is something they need told. The silent path is not a lesser path — it is
the normal one, and CHAT-04's one-question rule and the interruption budget exist
because unnecessary messages are a real cost.

**ADJ-07 is unconditional.** Every automatic change writes an `adjustment_events`
row with trigger, evidence, before and after, whether or not anybody was told.
"Asking why a session moved returns the stored reason, not a reconstruction" —
which is only true if the row is written at the moment of the change, from what
was actually known then.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from coach.adjust import authority as authmod
from coach.adjust import triggers as trigmod
from coach.blocks import load as loadmod

log = logging.getLogger(__name__)

# ADJ-06's threshold. Twelve hours, so an evening session changed that morning
# gets a message and one changed the previous week does not.
NOTICE_HOURS = 12

# Where `move_later` puts a session: the last slot the evening window allows,
# which is the most rest it can buy without moving to another day (ADJ-04 and
# PLAN-04 both forbid that).
LATEST_START = time(20, 0)


@dataclass
class Applied:
    """What one adjustment did. Every field is something a test asserts on."""

    prescription_id: int
    trigger: str
    action: str
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    notified: bool = False
    message: str = ""
    moved_to: datetime | None = None


def project(
    spec: dict[str, Any], discipline: str, action: str, planned_for: datetime
) -> tuple[dict[str, Any], datetime]:
    """What the target looks like after `action`. Pure, so authority can price it.

    Each action reduces the load figure or leaves it alone, and none of them can
    raise it — that is ADJ-02 made structural rather than checked. `move_later`
    keeps the same session and changes only when, which is why it is a downgrade
    at all: it buys recovery time without touching the work.
    """
    after = dict(spec)
    when = planned_for

    if action == "shorten":
        after["duration_s"] = int(int(spec.get("duration_s") or 0) * trigmod.SHORTEN_TO)

    elif action == "ease":
        # Cycling eases by intensity; gym eases by RPE. Both drop the target
        # rather than the duration, because the point is to take the sting out
        # while keeping the habit — a shorter hard session is still hard.
        if discipline.lower() in loadmod.GYM_DISCIPLINES:
            if spec.get("rpe_target"):
                after["rpe_target"] = round(
                    float(spec["rpe_target"]) * float(trigmod.EASE_INTENSITY_TO), 1
                )
        else:
            if spec.get("intensity_factor"):
                factor = float(spec["intensity_factor"]) * float(trigmod.EASE_INTENSITY_TO)
                after["intensity_factor"] = round(factor, 3)
                if spec.get("ftp_watts"):
                    after["target_watts"] = round(float(spec["ftp_watts"]) * factor)
            # A structured session eased to endurance loses its intervals. Design
            # section 10 says "downgrade the next hard session to endurance", and
            # leaving the step list on would publish intervals at endurance power,
            # which is neither one thing nor the other.
            after.pop("steps", None)

    elif action == "convert_to_rest":
        # Not a deletion. The session becomes an easy spin at a third of the time,
        # because a rest day the athlete can see is a decision and an empty slot
        # is an absence. `planned_load` falls out of the spec, so this is a large
        # reduction rather than a special case.
        after["duration_s"] = max(600, int(int(spec.get("duration_s") or 0) * 0.33))
        after["intensity_factor"] = 0.55
        after.pop("target_watts", None)
        after.pop("steps", None)
        after["purpose"] = "Easy spin — converted from the planned session"

    elif action == "move_later":
        when = planned_for.replace(
            hour=LATEST_START.hour, minute=LATEST_START.minute, second=0, microsecond=0
        )

    return after, when


def projected_load(spec: dict[str, Any], discipline: str) -> Decimal:
    """The combined-scale cost of a projected spec. GYM-08's unit throughout."""
    return loadmod.of_spec(discipline, spec)


def needs_notice(planned_for: datetime, now: datetime) -> bool:
    """ADJ-06: is the original start inside twelve hours?

    Measured against the *original* start rather than the new one, which is the
    requirement's wording and also the only reading that makes sense: the athlete
    is about to do the thing that is changing.
    """
    return planned_for - now <= timedelta(hours=NOTICE_HOURS)


def execute(
    conn: psycopg.Connection,
    proposal: trigmod.Proposal,
    decision: authmod.Decision,
    now: datetime,
    send: Any = None,
) -> Applied | None:
    """Perform an approved adjustment. ADJ-07's row is written here, always.

    `send` is a callable taking one string, or None. Injected rather than reached
    for so the notice rule is testable without a transport, and so this module
    never has to know Telegram exists.
    """
    if not decision.applies:
        return None
    if proposal.action == "note":
        # ADJ-01's "note it, leave the week alone". Recorded against no
        # prescription, because nothing was changed and pretending otherwise would
        # put a no-op in the athlete's adjustment history.
        _record_note(conn, proposal)
        return Applied(prescription_id=0, trigger=proposal.trigger, action="note", notified=False)

    target_id = proposal.target_prescription_id
    if target_id is None:
        return None

    with conn.cursor() as cur:
        cur.execute(
            "select id, planned_for, discipline, spec, planned_load, external_id "
            "from prescriptions where id = %s",
            (target_id,),
        )
        target = cur.fetchone()
    if target is None:
        log.warning("prescription %s vanished before the adjustment landed", target_id)
        return None

    before_spec = dict(target["spec"] or {})
    after_spec, when = project(
        before_spec, target["discipline"], proposal.action, target["planned_for"]
    )
    after_load = projected_load(after_spec, target["discipline"])

    result = Applied(
        prescription_id=target_id,
        trigger=proposal.trigger,
        action=proposal.action,
        before=before_spec,
        after=after_spec,
        moved_to=when if when != target["planned_for"] else None,
    )

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update prescriptions set spec = %s, planned_load = %s, planned_for = %s, "
            "status = 'adjusted' where id = %s",
            (Jsonb(after_spec), after_load, when, target_id),
        )
        # ADJ-07, unconditionally and inside the same transaction as the change.
        # A row written afterwards could be lost to a crash and leave a session
        # quietly different with no explanation, which is the exact failure the
        # requirement is about.
        cur.execute(
            """
            insert into adjustment_events
                (prescription_id, trigger, evidence, before_spec, after_spec, announced, authority)
            values (%s, %s, %s, %s, %s, %s, 'automatic')
            """,
            (
                target_id,
                proposal.trigger,
                Jsonb(
                    {
                        **proposal.evidence,
                        "action": proposal.action,
                        "reason": proposal.reason,
                        "decided_by": decision.requirement,
                        "load_before": float(target["planned_load"] or 0),
                        "load_after": float(after_load),
                    }
                ),
                Jsonb(before_spec),
                Jsonb(after_spec),
                False,
            ),
        )

    # ADJ-06. The message is composed here and sent by the caller's transport;
    # the silent path writes nothing extra, because the reason is already on the
    # calendar event — P08 republishes the description from the spec, and the
    # adjustment_events row is the durable record either way.
    if needs_notice(target["planned_for"], now):
        result.message = _notice(proposal, target, after_spec, when)
        if send is not None:
            send(result.message)
            result.notified = True
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "update adjustment_events set announced = true where prescription_id = %s "
                    "and trigger = %s and announced = false",
                    (target_id, proposal.trigger),
                )
        else:
            log.warning(
                "prescription %s changed inside %dh and no transport was given; "
                "ADJ-06 wanted a message",
                target_id,
                NOTICE_HOURS,
            )
    else:
        log.info(
            "prescription %s adjusted silently (%s); the reason is on the calendar event",
            target_id,
            proposal.trigger,
        )

    return result


def _notice(
    proposal: trigmod.Proposal,
    target: dict[str, Any],
    after_spec: dict[str, Any],
    when: datetime,
) -> str:
    """ADJ-06's message. An assertion with the reason, not a question.

    CHAT-04 allows one question per message and this is not the place to spend it:
    the change has already happened and asking permission after the fact is worse
    than either asking first or not asking. Design section 8's phrasing rule
    applies — state it so it invites correction.
    """
    minutes = int(after_spec.get("duration_s", 0)) // 60
    moved = when != target["planned_for"]
    what = {
        "ease": "eased it",
        "shorten": f"cut it to {minutes} minutes",
        "convert_to_rest": "turned it into an easy spin",
        "move_later": f"pushed it to {when:%H:%M}",
    }.get(proposal.action, "changed it")

    when_text = (
        f" and moved it to {when:%H:%M}" if moved and proposal.action != "move_later" else ""
    )
    return (
        f"I have {what}{when_text} for {target['planned_for']:%A}. {proposal.reason.capitalize()}."
    )


def _record_note(conn: psycopg.Connection, proposal: trigmod.Proposal) -> None:
    """The "noted, no change" record. Not an adjustment_events row.

    That table is one row per *change* to a prescription, and its foreign key says
    so. A note about a session that changed nothing belongs in the episodic
    archive, where the Sunday review reads the week's story from.
    """
    from coach.memory import notes as notemod

    with conn.cursor() as cur:
        cur.execute("select local_date from sessions where id = %s", (proposal.session_id,))
        row = cur.fetchone()
    if row is None:
        return
    notemod.add(
        conn,
        "observation",
        f"{proposal.reason} ({proposal.trigger})",
        row["local_date"],
        refs={"session_id": proposal.session_id, "requirement": "ADJ-01"},
    )


def defer(
    conn: psycopg.Connection,
    proposal: trigmod.Proposal,
    decision: authmod.Decision,
    for_week: date,
) -> int | None:
    """ADJ-03 and ADJ-05: file it for the Sunday review instead. REV-04 reads it.

    Idempotent on (prescription, trigger) while pending, so a rule that fires
    again next ingest does not give the review the same proposal twice. Returns
    None when it was already there, which is a normal outcome and not a failure.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into deferred_adjustments
                (prescription_id, session_id, trigger, deferred_by, proposal, evidence, for_week)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict do nothing
            returning id
            """,
            (
                proposal.target_prescription_id,
                proposal.session_id,
                proposal.trigger,
                decision.requirement,
                Jsonb(
                    {
                        "action": proposal.action,
                        "reason": proposal.reason,
                        "decision": decision.reason,
                    }
                ),
                Jsonb(proposal.evidence),
                for_week,
            ),
        )
        row = cur.fetchone()
    if row:
        log.info(
            "deferred %s to the review (%s): %s",
            proposal.trigger,
            decision.requirement,
            decision.reason,
        )
    return int(row["id"]) if row else None


def pending_for_review(conn: psycopg.Connection, for_week: date) -> list[dict[str, Any]]:
    """What the Sunday review has waiting. REV-04's input.

    Here rather than in P10 because this module owns the table, and a reader that
    lived with the review would have to know its shape from the outside.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select id, prescription_id, session_id, trigger, deferred_by, proposal, evidence "
            "from deferred_adjustments where for_week = %s and status = 'pending' order by id",
            (for_week,),
        )
        return cur.fetchall()
