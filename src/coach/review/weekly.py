"""The Sunday review. REV-01 to REV-05.

**Every figure is read, not computed here.** Adherence and load come from
`rollups`, the weight trend from the fit `health/trend.py` stores, recovery from
its own deviations, intake from `health/nutrition.py`. MEM-08 is the reason —
the review is the most quotable thing the system produces, and a number in it
that nobody else can reproduce is worse than no number.

**The review does not ask the model to do arithmetic, and it does not ask the
model to decide what to raise.** The sections are assembled deterministically
and handed over as text. That leaves the model the job it is good at — saying it
like the coach — and keeps the job it is bad at away from a message the athlete
will treat as authoritative.

**One question, and only one** (REV-03). CHAT-11 exempts the review from the
interruption budget, which sounds like permission to ask several things and is
not: the exemption exists because the review's question is not an interruption,
not because the review is a questionnaire. The renderer asserts the count.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from coach.adjust import apply as adjustmod
from coach.blocks import document as blockmod
from coach.health import breaks as breakmod
from coach.health import nutrition as nutmod
from coach.health import recovery as recoverymod
from coach.health import trend as trendmod
from coach.memory import notes as notemod

log = logging.getLogger(__name__)

# REV-02 names five. Intake is the sixth, because NUT-06 asks for it here by
# name — "the weekly review includes intake adherence alongside training and
# weight" — and a requirement that names the artefact belongs in the artefact.
SECTIONS = ("Adherence", "Load", "Weight", "Recovery", "Goals", "Intake")


@dataclass(frozen=True)
class Section:
    title: str
    body: str


@dataclass(frozen=True)
class Review:
    week_ending: date
    sections: list[Section]
    question: str
    decisions: list[str] = field(default_factory=list)

    def render(self) -> str:
        """The message body. One question, at the end, where it can be answered."""
        parts = [f"Week ending {self.week_ending.isoformat()}", ""]
        for section in self.sections:
            parts.append(f"{section.title}: {section.body}")
        if self.decisions:
            parts.append("")
            parts.append("Waiting on you:")
            parts.extend(f"- {d}" for d in self.decisions)
        parts.append("")
        parts.append(self.question)
        return "\n".join(parts)


def _week(week_ending: date) -> tuple[date, date]:
    return week_ending - timedelta(days=6), week_ending


def _rollup(conn: psycopg.Connection, on: date) -> dict[str, Any]:
    """The most recent rollup at or before the day, or an empty one.

    At or before rather than exactly on: rollups exist for days that have
    sessions, so a rest Sunday has no row of its own and the week's figures are
    still the ones from its last training day.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select as_of, load_7d, load_28d, adherence_rate, gym_session_count "
            "from rollups where as_of <= %s order by as_of desc limit 1",
            (on,),
        )
        return cur.fetchone() or {}


def adherence_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """REV-02's first. Break days are already out of this figure (BREAK-02)."""
    since, until = _week(week_ending)
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) filter (where status = 'completed')::int as completed,
                   count(*) filter (where status = 'missed')::int    as missed,
                   count(*) filter (where status = 'suspended')::int as suspended
              from prescriptions
             where (planned_for at time zone 'UTC')::date between %s and %s
            """,
            (since, until),
        )
        row = cur.fetchone() or {}

    completed, missed = row.get("completed", 0), row.get("missed", 0)
    offered = completed + missed
    if offered == 0 and not row.get("suspended"):
        return Section("Adherence", "nothing was prescribed this week.")
    if offered == 0:
        return Section(
            "Adherence",
            f"the week was inside a break — {row['suspended']} session(s) suspended, "
            "which is not a miss.",
        )

    body = f"{completed} of {offered} sessions completed"
    if row.get("suspended"):
        body += f", plus {row['suspended']} suspended by a break and not counted"
    return Section("Adherence", body + ".")


def load_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """GYM-08's combined scale, which is what makes one number cover both."""
    now = _rollup(conn, week_ending)
    if not now or now.get("load_7d") is None:
        return Section("Load", "no training load recorded.")

    prior = _rollup(conn, week_ending - timedelta(days=7))
    body = f"{int(now['load_7d'])} over the week"
    if prior and prior.get("load_7d") is not None and prior["load_7d"] > 0:
        change = (now["load_7d"] - prior["load_7d"]) / prior["load_7d"] * 100
        direction = "up" if change >= 0 else "down"
        body += f", {direction} {abs(int(change))}% on the week before"
    if now.get("load_28d") is not None:
        body += f"; {int(now['load_28d'])} over 28 days"
    if now.get("gym_session_count"):
        body += f"; {now['gym_session_count']} gym session(s)"
    return Section("Load", body + ".")


def weight_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """HLTH's claim ladder decides what may be said, not this."""
    fit = trendmod.fit(conn, week_ending)
    claims = trendmod.Claims.of(fit)
    rendered = trendmod.render(fit, claims).strip()
    return Section("Weight", rendered or "no readings to work from.")


def recovery_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """The week's mean deviation against the athlete's own baseline (RECOV-01)."""
    since, until = _week(week_ending)
    usable = [
        d
        for d in recoverymod.deviations(conn, until, horizon_days=14)
        if d.usable and since <= d.local_date <= until
    ]
    if not usable:
        return Section("Recovery", "not enough history to score the week.")

    mean = sum((d.deviation or Decimal(0)) for d in usable) / Decimal(len(usable))
    if mean >= Decimal("0.5"):
        reading = "better than his own baseline"
    elif mean <= Decimal("-0.5"):
        reading = "below his own baseline"
    else:
        reading = "about his own baseline"
    return Section("Recovery", f"{reading} across {len(usable)} scored day(s).")


def goals_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """REV-02's fifth: progress against what the block says it is for."""
    block = blockmod.active(conn)
    if block is None:
        return Section("Goals", "no active block.")

    stated = blockmod.section_of(block.content, "goals").strip()
    if not stated:
        return Section("Goals", f"block {block.id} is active but states no goals.")
    return Section("Goals", stated)


def intake_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """NUT-06, which names the review explicitly."""
    windows = nutmod.rollup(conn, week_ending)
    verdict = nutmod.arbitration(conn, week_ending)
    body = nutmod.render(windows, verdict)
    return Section("Intake", body.removeprefix("Intake:").strip())


# REV-03. One question, and it is always the same shape: what is coming, and
# what might get in the way. Generated rather than chosen by the model because
# "one question" is a property the renderer can guarantee and a prompt cannot.
QUESTION = "What does the coming week look like — anything that will get in the way?"


def decisions(conn: psycopg.Connection, week_ending: date) -> list[str]:
    """REV-04: what is waiting on the athlete rather than on the coach.

    Two sources, and they are here together because they are one thing from the
    athlete's side: a decision the system deliberately did not take on its own.
    """
    out: list[str] = []
    for row in adjustmod.pending_for_review(conn, week_ending):
        proposal = row.get("proposal") or {}
        described = proposal.get("summary") or row["trigger"]
        out.append(f"{described} (deferred by {row['deferred_by']})")

    awaiting = breakmod.awaiting_re_entry(conn, week_ending)
    if awaiting is not None:
        proposal = breakmod.re_entry(conn, awaiting, week_ending)
        if proposal is not None:
            out.append(proposal.render())
    return out


def build(conn: psycopg.Connection, week_ending: date) -> Review:
    """Assemble the review. Reads everything, writes nothing."""
    sections = [
        adherence_section(conn, week_ending),
        load_section(conn, week_ending),
        weight_section(conn, week_ending),
        recovery_section(conn, week_ending),
        goals_section(conn, week_ending),
        intake_section(conn, week_ending),
    ]
    return Review(
        week_ending=week_ending,
        sections=sections,
        question=QUESTION,
        decisions=decisions(conn, week_ending),
    )


def store(conn: psycopg.Connection, review: Review) -> notemod.Note:
    """REV-05: both artefacts.

    The note is the durable record and is written first, because it is the one
    that cannot fail for a reason outside this module — a block might not exist,
    and a review that happened must be recorded either way.
    """
    body = review.render()
    note = notemod.add(
        conn,
        kind="review",
        body=body,
        occurred_on=review.week_ending,
        refs={"week_ending": review.week_ending.isoformat()},
    )

    # Before the block append, and deliberately: whether the athlete has been
    # offered a re-entry is a fact about the review having happened, not about
    # there being a block to write it into. Doing this after the early return
    # below meant a deployment without an active block re-proposed the same
    # re-entry every Sunday.
    awaiting = breakmod.awaiting_re_entry(conn, review.week_ending)
    if awaiting is not None:
        breakmod.mark_re_entry_proposed(conn, awaiting.id, review.week_ending)

    block = blockmod.active(conn)
    if block is None:
        log.info("review %s stored as a note; no active block to append to", review.week_ending)
        return note

    existing = blockmod.section_of(block.content, "review").strip()
    appended = f"{existing}\n\n{body}".strip() if existing else body
    blockmod.rewrite(
        conn,
        block.id,
        "review",
        appended,
        reason=f"weekly review for {review.week_ending.isoformat()}",
    )
    return note


def run(
    conn: psycopg.Connection,
    week_ending: date,
    send: Any = None,
) -> Review:
    """REV-01: build it, record it, post it.

    `send` last and optional so the review can be generated and stored without a
    transport — which is how it is tested, and how it would be regenerated after
    a delivery failure without writing a second note.
    """
    review = build(conn, week_ending)
    store(conn, review)
    if send is not None:
        send(review.render())
    return review
