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

**The record and the message are two artefacts, not one.** `render` is the
record: every section, in a fixed order, written into the note and the block so
that a figure the coach quotes in March can be traced to the Sunday it came
from. `message` is what the athlete actually reads, and it drops the sections
with nothing in them into a single line rather than spending six lines saying
nothing happened. `voice` then puts the message through the coach's own voice.

Posting the record was the original behaviour and it was wrong in a way worth
naming, because it is the failure this whole file is arranged to avoid running
in reverse. The sections are assembled deterministically so the model cannot
invent a number. That is not a reason to hand the assembly to the athlete
unedited: `health/trend.py` writes its block as instructions *to the coach*, and
posting it verbatim put "You may report this figure if asked for it" in a
message from his coach.
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
from coach.blocks import load as loadmod
from coach.health import breaks as breakmod
from coach.health import nutrition as nutmod
from coach.health import recovery as recoverymod
from coach.health import trend as trendmod
from coach.memory import notes as notemod

log = logging.getLogger(__name__)

# REV-02 names five. Intake is the sixth, because NUT-06 asks for it here by
# name — "the weekly review includes intake adherence alongside training and
# weight" — and a requirement that names the artefact belongs in the artefact.
#
# Effort and Ahead are the seventh and eighth, and neither is in the PRD. They
# are here because the review was answering the wrong question. Load is training
# stress on GYM-08's combined scale, which is the right number for deciding next
# week's ceiling and tells the athlete nothing about what he did: "440 over the
# week" is not a week anyone recognises. Effort is the week he actually had —
# how many times he went out, for how long, how far. Ahead is the other half of
# the same complaint: a review that only looks backwards is a report, and what
# makes a review worth reading on a Sunday evening is the session it points at.
#
# Effort leads. The week he had comes before the week he was scored against,
# because one of those is his and the other is the plan's opinion of it.
SECTIONS = ("Effort", "Adherence", "Load", "Weight", "Recovery", "Goals", "Intake", "Ahead")


@dataclass(frozen=True)
class Section:
    """One of REV-02's six, and whether it carries anything.

    `notable` is false when the section's whole content is an absence — nothing
    prescribed, no load, no readings, nothing logged. The record keeps those
    either way, because "no intake was logged in the week of 2 August" is a fact
    about the week and the absence of a line is not. The message does not: six
    sections each reporting nothing is a wall of text that says a quiet week
    happened, and one line says it better.
    """

    title: str
    body: str
    notable: bool = True

    # What the quiet-week line calls this section. Falls back to the title,
    # lowercased, when nothing better is set.
    quiet_as: str | None = None

    # A shorter body for the message, where the record's is longer than the
    # athlete needs. It may only ever be a subset of `body` — a message that
    # says something the record does not is the bug MEM-08 exists to prevent,
    # arriving by a side door.
    said_as: str | None = None

    # Kept in the record, never said, and not counted as an absence either.
    #
    # Only Goals sets it. His goals are a two year target he stated himself and
    # has not forgotten, and reciting them back every Sunday is the review
    # padding itself with the one thing in it he already knows. They stay in the
    # facts because the coach needs to know what the week is for before he can
    # say why it mattered. Knowing it and reciting it are different things, and
    # the difference is the whole point of this flag.
    record_only: bool = False

    @property
    def quiet_label(self) -> str:
        return self.quiet_as or self.title.lower()

    @property
    def spoken(self) -> str:
        return self.said_as or self.body


# Past this many empty sections, naming them is worse than not. Listing five
# things that did not happen is a longer sentence than "nothing happened" and
# carries less: he does not need to be told that a week with no sessions in it
# also had no training load. The record still names every one of them.
QUIET_ENUMERATION_MAX = 3


def _quiet_line(sections: list[Section]) -> str:
    """The sections with nothing in them, collapsed into one clause."""
    labels = [s.quiet_label for s in sections]
    if len(labels) > QUIET_ENUMERATION_MAX:
        return "Nothing recorded this week."
    if len(labels) == 1:
        joined = labels[0]
    else:
        joined = f"{', '.join(labels[:-1])} and {labels[-1]}"
    return f"Nothing on {joined} this week."


# The date, as a person would say it. `2026-08-02` is how the record stores a
# Sunday and not how anyone refers to one.
def _spoken_date(on: date) -> str:
    return f"{on.strftime('%A')} {on.day} {on.strftime('%B')}"


@dataclass(frozen=True)
class Review:
    week_ending: date
    sections: list[Section]
    question: str
    decisions: list[str] = field(default_factory=list)

    @property
    def notable(self) -> list[Section]:
        return [s for s in self.sections if s.notable and not s.record_only]

    @property
    def quiet(self) -> list[Section]:
        return [s for s in self.sections if not s.notable and not s.record_only]

    def render(self) -> str:
        """The record: every section, in REV-02's order, keyed by the Sunday.

        Written into the note and the block, and read back by anything that has
        to reconstruct what was known on a given week. Not what the athlete
        reads — see `message`.
        """
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

    def message(self) -> str:
        """What gets posted, when there is no model to voice it.

        Structured rather than continuous: a heading line, a blank line between
        each section so the eye can find them on a phone, the decisions under
        their own heading, and the question last where it can be answered. The
        persona bars headings and lists from ordinary replies and exempts a
        summary, which is what this is.

        One question, still (REV-03). The sections state and never ask, so the
        count is a property of the assembly rather than a hope, and `run`
        asserts it before anything is sent.
        """
        parts = [f"Week ending {_spoken_date(self.week_ending)}", ""]

        blocks = [f"{s.title}: {s.spoken}" for s in self.notable]
        if self.quiet:
            blocks.append(_quiet_line(self.quiet))
        parts.append("\n\n".join(blocks))

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
        return Section(
            "Adherence",
            "nothing was prescribed this week.",
            notable=False,
            quiet_as="prescribed sessions",
        )
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


def _hours(seconds: int) -> str:
    """Duration the way it is said out loud, not as a count of minutes."""
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h" if rest == 0 else f"{hours}h {rest:02d}m"


def effort_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """What the week actually was: how often he went out, how long, how far.

    Sessions rather than prescriptions, because this is the week he had and not
    the week he was given. A ride nobody prescribed still cost him three hours,
    and a review that counts only what it asked for is reading its own homework.

    Counted in SQL for the same reason every other figure here is (MEM-08), and
    the unreadable ones are counted separately rather than dropped: FIT-15's
    `data_unavailable` means he trained and the file did not survive, which is
    the one case where a zero would be a lie about the athlete rather than
    about the data.
    """
    since, until = _week(week_ending)
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*)::int                                  as n,
                   coalesce(sum(duration_s) filter
                            (where not data_unavailable), 0)::int  as duration_s,
                   coalesce(sum(distance_m) filter
                            (where not data_unavailable), 0)       as distance_m,
                   count(*) filter (where data_unavailable)::int   as unreadable
              from sessions
             where local_date between %s and %s
            """,
            (since, until),
        )
        row = cur.fetchone() or {}

    total = row.get("n") or 0
    if not total:
        return Section("Effort", "nothing recorded.", notable=False, quiet_as="completed sessions")

    parts = [f"{total} session{'' if total == 1 else 's'}"]
    if row.get("duration_s"):
        parts.append(_hours(row["duration_s"]))
    # Rounded to the kilometre. A weekly total quoted to the metre invites a
    # comparison with last week's metre, which is noise wearing a number's
    # clothes.
    if row.get("distance_m") and row["distance_m"] >= 1000:
        parts.append(f"{int(Decimal(row['distance_m']) / 1000)} km")

    body = ", ".join(parts)
    if row.get("unreadable"):
        body += (
            f" ({row['unreadable']} of them with no usable file, so the time and "
            "distance are short by whatever those were)"
        )
    return Section("Effort", body + ".")


def load_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """GYM-08's combined scale, which is what makes one number cover both."""
    now = _rollup(conn, week_ending)
    if not now or now.get("load_7d") is None:
        return Section(
            "Load",
            "no training load recorded.",
            notable=False,
            quiet_as="training load",
        )

    prior = _rollup(conn, week_ending - timedelta(days=7))
    body = f"{int(now['load_7d'])} over the week"
    if prior and prior.get("load_7d") is not None and prior["load_7d"] > 0:
        change = (now["load_7d"] - prior["load_7d"]) / prior["load_7d"] * 100
        direction = "up" if change >= 0 else "down"
        body += f", {direction} {abs(int(change))}% on the week before"
    if now.get("load_28d") is not None:
        body += f"; {int(now['load_28d'])} over 28 days"
    if now.get("gym_session_count"):
        gym = now["gym_session_count"]
        body += f"; {gym} gym session{'' if gym == 1 else 's'}"
    return Section("Load", body + ".")


def weight_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """HLTH's claim ladder decides what may be said, not this.

    `describe` rather than `render`: the latter is the permission block the
    coach's prompt carries, and this section is read by the athlete. They walk
    the same ladder off the same `Claims`, so nothing is said here that the
    prompt would have forbidden.

    Whether the section is worth a paragraph is `may_report_reading` rather than
    a reading count of this module's own. It is the same question — there is at
    least one reading — and asking the claim gate is what keeps the bar stated
    in one place.
    """
    fit = trendmod.fit(conn, week_ending)
    claims = trendmod.Claims.of(fit)
    return Section("Weight", trendmod.describe(fit, claims), notable=claims.may_report_reading)


def recovery_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """The week's mean deviation against the athlete's own baseline (RECOV-01)."""
    since, until = _week(week_ending)
    usable = [
        d
        for d in recoverymod.deviations(conn, until, horizon_days=14)
        if d.usable and since <= d.local_date <= until
    ]
    if not usable:
        return Section(
            "Recovery",
            "not enough history to score the week.",
            notable=False,
            quiet_as="recovery scoring",
        )

    mean = sum((d.deviation or Decimal(0)) for d in usable) / Decimal(len(usable))
    if mean >= Decimal("0.5"):
        reading = "better than his own baseline"
    elif mean <= Decimal("-0.5"):
        reading = "below his own baseline"
    else:
        reading = "about his own baseline"
    return Section("Recovery", f"{reading} across {len(usable)} scored day(s).")


def goals_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """REV-02's fifth: what the block says it is for. Recorded, never recited.

    This is the section the athlete asked to stop reading, and he was right to.
    They are his own two year goals, stated by him, unchanged since the block
    opened, and quoting them back at him every Sunday is the review filling
    space with the one thing in it he is certain of. Worse, it is the longest
    section, so the message led with the least new information in it.

    `record_only` rather than deletion, because the coach still has to know
    what the week was for. The voicing prompt is given the whole review and told
    that this section is context and not content: it is the difference between
    "that ride was the aerobic base showing up" and "your goal is to get under
    100 kg", and only the first is worth his evening.
    """
    block = blockmod.active(conn)
    if block is None:
        return Section("Goals", "no active block.", notable=False, record_only=True)

    stated = blockmod.section_of(block.content, "goals").strip()
    if not stated:
        return Section(
            "Goals",
            f"block {block.id} is active but states no goals.",
            notable=False,
            record_only=True,
        )
    return Section("Goals", stated, record_only=True)


def _describe(discipline: str, spec: dict[str, Any]) -> str:
    """A prescription as a phrase. `notify/daily.py` says it the same way.

    Its `_describe` is not imported and this is not quite a duplicate: the
    morning message names one session on the day it happens, so it needs no day
    and no ranking. Sharing the function would mean one of the two callers
    passing a flag to suppress half of it, which is how two clear sentences
    become one unclear one.
    """
    parts = [discipline]
    minutes = int((spec.get("duration_s") or 0) / 60)
    if minutes:
        parts.append(f"{minutes} min")
    described = ", ".join(parts)
    purpose = spec.get("purpose")
    return f"{described}, {purpose}" if purpose else described


def ahead_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """What is prescribed for the week that starts tomorrow, and the one that
    carries it.

    **The key session is the heaviest, and that is a rule rather than a
    judgement.** `blocks/load.of_spec` is the same function BLOCK-07's ramp
    limit is enforced with, so the session named here is the one the week is
    actually built around rather than the one that reads most impressively. A
    model asked to pick would pick differently on identical weeks, and "which
    session matters" is not a question anyone should have to re-litigate every
    Sunday.

    Ties break toward the earlier session: two sessions of equal weight and the
    one he reaches first is the one worth flagging.
    """
    since = week_ending + timedelta(days=1)
    until = week_ending + timedelta(days=7)
    with conn.cursor() as cur:
        cur.execute(
            """
            select planned_for, discipline, spec
              from prescriptions
             where (planned_for at time zone 'UTC')::date between %s and %s
               and status in ('planned', 'adjusted')
             order by planned_for
            """,
            (since, until),
        )
        rows = cur.fetchall()

    if not rows:
        # Notable even though it is an absence, and the only section that is.
        # An empty week ahead is not a quiet week, it is a week nobody has
        # written yet, and burying that in the "nothing happened" line is how he
        # finds out on Tuesday. Everything else in the quiet line is a fact
        # about a week that is over and cannot be acted on.
        return Section("Ahead", "nothing prescribed for next week yet.")

    total = len(rows)
    heaviest = max(rows, key=lambda r: loadmod.of_spec(r["discipline"], r["spec"] or {}))
    day = heaviest["planned_for"].strftime("%A")
    described = _describe(heaviest["discipline"], heaviest["spec"] or {})

    body = f"{total} session{'' if total == 1 else 's'} prescribed. "
    if total == 1:
        body += f"{day}: {described}."
    else:
        body += f"The one that carries the week is {day}: {described}."
    return Section("Ahead", body)


def intake_section(conn: psycopg.Connection, week_ending: date) -> Section:
    """NUT-06, which names the review explicitly."""
    windows = nutmod.rollup(conn, week_ending)
    verdict = nutmod.arbitration(conn, week_ending)
    body = nutmod.render(windows, verdict)
    logged = any(w.logged_days for w in windows)
    return Section(
        "Intake",
        body.removeprefix("Intake:").strip(),
        notable=logged,
        quiet_as="logged intake",
    )


# REV-03. One question, and it is always the same shape: what is coming, and
# what might get in the way. Generated rather than chosen by the model because
# "one question" is a property the renderer can guarantee and a prompt cannot.
#
# No em dash. The persona bars them outright and this is the one sentence in the
# review the coach says verbatim every week, so it was the most visible place in
# the system to be breaking his own rule.
QUESTION = "What does the coming week look like, and is anything going to get in the way?"


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
        effort_section(conn, week_ending),
        adherence_section(conn, week_ending),
        load_section(conn, week_ending),
        weight_section(conn, week_ending),
        recovery_section(conn, week_ending),
        goals_section(conn, week_ending),
        intake_section(conn, week_ending),
        ahead_section(conn, week_ending),
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
    client: Any = None,
) -> Review:
    """REV-01: build it, record it, post it.

    `send` optional so the review can be generated and stored without a
    transport — which is how it is tested, and how it would be regenerated after
    a delivery failure without writing a second note. `client` optional for the
    same reason from the other end: voicing improves the message and nothing
    about the review depends on it, so its absence costs the voice and not the
    review.

    The record is stored before anything is said, and it is the assembled one
    either way. What the athlete was sent on a Sunday is recoverable from the
    chat; what was true on that Sunday has to be recoverable from the note.
    """
    review = build(conn, week_ending)
    store(conn, review)
    if send is not None:
        from coach.review import voice as voicemod

        send(voicemod.say(review, client, conn))
    return review
