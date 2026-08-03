"""System prompt assembly.

SAFE-01: safety constrained keys load verbatim at the top of every prompt.
Removing all other context still leaves the constraints present.

MEM-10 loads standing memory in full; MEM-11 caps the assembled context; MEM-13
sheds in a fixed order and never touches constraints. The shedding itself lives
in :mod:`coach.memory.context` — this module renders the pieces it sheds.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from coach import clock
from coach.agent import persona
from coach.calendars import availability as calmod
from coach.health import bodymass, recovery
from coach.memory import context as ctxmod
from coach.memory import facts as factmod
from coach.memory import keys as keymod
from coach.memory import state as statemod
from coach.plans import agenda as agendamod


def render_constraints(conn: psycopg.Connection) -> str:
    """SAFE-01: verbatim, never summarised, always first.

    Rendered from the safety keys alone so that no change elsewhere in memory
    can displace or dilute them.
    """
    vocabulary = keymod.load_all(conn)
    safety = [f for f in factmod.active(conn) if vocabulary[f.key].safety]
    if not safety:
        return "CONSTRAINTS\nNone recorded."

    lines = ["CONSTRAINTS", "These are absolute. Never program against them."]
    for fact in sorted(safety, key=lambda f: f.key):
        lines.append(f"- {fact.key}: {fact.value}")
    return "\n".join(lines)


def render_facts(conn: psycopg.Connection) -> str:
    """Every active non-safety fact, with confidence shown to the model."""
    vocabulary = keymod.load_all(conn)
    rows = [f for f in factmod.active(conn) if not vocabulary[f.key].safety]
    if not rows:
        return ""

    lines = ["WHAT YOU KNOW"]
    for fact in sorted(rows, key=lambda f: f.key):
        confidence = "" if fact.confidence >= 1 else f" (confidence {fact.confidence})"
        lines.append(f"- {fact.key}: {fact.value}{confidence} [{fact.provenance}]")
    return "\n".join(lines)


def render_continuity(conn: psycopg.Connection) -> str:
    """CHAT-05: the coach opens from the last open thread, not cold."""
    state = statemod.get(conn)
    if not state.rolling_summary and not state.open_threads:
        return ""
    lines = ["WHERE YOU LEFT OFF"]
    if state.rolling_summary:
        lines.append(state.rolling_summary)
    if state.open_threads:
        for thread in state.open_threads:
            lines.append(f"- open: {thread}")
    return "\n".join(lines)


def render_capabilities() -> str:
    """What the coach can look up, and the rule that it must look before refusing.

    The prompt described the athlete in twelve blocks and never once described
    the system. Eleven tools were offered and the model was left to infer what it
    could reach from whatever happened to be in its context, which it did, and
    got wrong: asked to confirm rides it held, it answered "calendar and
    activities feeds have never returned successfully for this account" and made
    no tool call at all. Every feed had returned successfully that hour.

    That is the same failure TRUST-05 names for numbers, pointed the other way. A
    model with a strong prohibition and no orientation invents; the invention
    here was a limitation rather than a figure, and a scanner watching for
    fabricated watts would never see it.

    Static, and in the cached prefix with the persona: it describes the system,
    which changes when the code changes and not when the athlete does.
    """
    return (
        "WHAT YOU CAN LOOK UP\n"
        "Ask for it rather than working from what happens to be in this prompt.\n"
        "- get_sessions: rides, gym sessions and golf rounds, with power, heart "
        "rate, duration and distance. This is how you answer anything about what "
        "he actually did.\n"
        "- get_plan: prescriptions and their status.\n"
        "- get_calendar: his own diary, the same feed as HIS DIARY below.\n"
        "- get_context, search_memory: what you hold on a topic, and past "
        "conversations.\n"
        "\n"
        "**Never tell him you cannot see something until you have called the tool "
        'that would show it.** "I do not have access to that" is a claim about '
        "this system, and you are not the one holding the evidence for it. If a "
        "tool returns nothing, say what you looked at and what came back. If "
        "there is genuinely no tool for what he asked, call log_capability_gap "
        "and say the one sentence it gives you.\n"
        "\n"
        "You do not read raw files and you do not need to: what a .fit contains "
        "is already parsed into the sessions get_sessions returns. Saying you "
        "cannot open a file is true and useless if you did not then look at the "
        "data that came out of it."
    )


def render_staleness(conn: psycopg.Connection, now: datetime) -> str:
    """CHAT-09: a stale feed is surfaced so the agent asks rather than infers.

    This is context, never an interruption — CHAT-11 is explicit that feed
    staleness shapes reasoning and does not consume the budget.

    `body_mass` is excluded, and its exclusion is a requirement rather than a
    tidy-up. HLTH-15 says the weigh in mention is the only one in the system and
    that "the generic feed staleness mechanism never emits a body mass mention of
    its own". A block that lists the feed and invites the coach to ask about it is
    exactly such a mention. The feed row is still maintained for OBS-05; what
    changes is who is allowed to speak about it, which is
    :func:`render_body_mass` and nothing else.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select name, last_success_at, stale_after_hours from feeds
            where name <> 'body_mass'
              and (last_success_at is null
                   or last_success_at < %s - (stale_after_hours * interval '1 hour'))
            order by name
            """,
            (now,),
        )
        stale = cur.fetchall()
    if not stale:
        # Silence used to mean healthy, and silence is exactly what the coach
        # filled in for itself. On 3 August 2026 it told the athlete "calendar
        # and activities feeds have never returned successfully for this
        # account" with all five stamped inside the hour, and consolidation
        # wrote that into the rolling summary, where it was read back the next
        # turn as established and said again.
        #
        # Worded narrowly on purpose. CHAT-09's whole point is that absence of
        # data is not evidence of absence of activity, so this must not become
        # "you have everything". It says the feeds answered, and forbids the one
        # inference that was actually drawn.
        return (
            "FEEDS\n"
            "Every feed has returned successfully inside its window. A feed that is "
            "working and quiet is not a feed that is broken: if you hold no data on "
            "something, look it up before saying you cannot see it, and never tell "
            "him a sync is down without evidence from this block."
        )

    lines = [
        "STALE FEEDS",
        "Absence of data is not evidence of absence of activity. Do not assert a",
        "missed session or a stalled trend from a feed listed here — ask.",
    ]
    for feed in stale:
        seen = feed["last_success_at"].strftime("%Y-%m-%d") if feed["last_success_at"] else "never"
        lines.append(f"- {feed['name']}: last success {seen}")
    return "\n".join(lines)


UNREADABLE_WINDOW_DAYS = 14


def render_unreadable(conn: psycopg.Connection, now: datetime, tz: ZoneInfo) -> str:
    """Activities the platform holds and will not serve, against the day's plan.

    intervals.icu returns a placeholder for anything synced from Strava, and on
    this account those are the gym and golf sessions Whoop writes there. The row
    says the athlete did something; nothing in it says what. So the one question
    worth asking is whether it was the session that was planned, and that is a
    question rather than an inference — the discipline is unknown, so the
    ordinary date-and-discipline match in `review.match` cannot claim it and must
    not be made to.

    Scoped to days that also carry an unmatched prescription. An unreadable
    activity on a day with nothing planned needs no conversation: it is a golf
    round the coach neither prescribed nor has anything to say about, and listing
    it would be the nagging CHAT-11 exists to prevent.

    Context and not an interruption, on CHAT-09's precedent. This shapes what the
    coach concludes about adherence; it does not spend the one thing per
    conversation the coach may raise unbidden.
    """
    since = clock.local_day(now, tz) - timedelta(days=UNREADABLE_WINDOW_DAYS)
    with conn.cursor() as cur:
        cur.execute(
            """
            select s.local_date, s.started_at, s.name,
                   p.discipline as planned_discipline, p.planned_for
              from sessions s
              join prescriptions p
                on (p.planned_for at time zone 'UTC')::date = s.local_date
               and p.session_id is null
               and p.status in ('planned', 'adjusted', 'missed')
             where s.data_unavailable
               and s.local_date >= %s
             order by s.local_date, s.started_at
            """,
            (since,),
        )
        rows = cur.fetchall()
    if not rows:
        return ""

    lines = [
        "ACTIVITY YOU CANNOT SEE",
        "The platform recorded an activity on these days and will not serve it —",
        "on this account that is usually a gym or golf session written to Strava by",
        "Whoop. Something was done. What it was is not knowable from here, so a",
        "planned session on the same day is neither completed nor missed until he",
        "says which. Ask; never score it either way on your own.",
    ]
    for row in rows:
        when = row["started_at"].astimezone(tz).strftime("%H:%M")
        lines.append(
            f"- {row['local_date']} at {when}: unreadable activity, "
            f"against a planned {row['planned_discipline']}"
        )
    return "\n".join(lines)


def render_body_mass(conn: psycopg.Connection, now: datetime, tz: ZoneInfo) -> str:
    """The weight trend, rendered as permissions rather than as numbers.

    This is the load bearing half of P04. The HLTH requirements are almost all
    statements about what the coach may say — a direction needs three readings, a
    rate needs six across three weeks, a plateau needs four weeks with weekly
    coverage — and a model handed a list of readings will honour none of them,
    because the arithmetic is trivial and the restraint is not.

    So the readings never enter the context. What enters is a fitted slope, a
    range computed in SQL, and an explicit statement of which claims the current
    evidence supports. HLTH-09 then costs nothing to obey: there is no pair of
    readings in the prompt to compare.
    """
    return bodymass.context(conn, clock.local_day(now, tz))


def render_recovery(conn: psycopg.Connection, now: datetime, tz: ZoneInfo) -> str:
    """RECOV-04's local deviation, with the platform's score labelled as theirs.

    Empty when the feed has not carried enough history to standardise anything,
    which is the honest state rather than a zero.
    """
    return recovery.context(conn, clock.local_day(now, tz))


def render_calendar(conn: psycopg.Connection, now: datetime, tz: ZoneInfo) -> str:
    """CALR-05: the week ahead as the feed published it, never as fact.

    Every line of this block is hedged on purpose. Google serves secret iCal
    feeds from a cache, so a commitment added an hour ago is invisible, and a
    coach that reads an empty calendar as a free evening will confidently plan
    into a meeting. The block says what was published and asks for confirmation
    rather than asserting availability.
    """
    return calmod.context(conn, clock.local_day(now, tz), tz)


def render_today(conn: psycopg.Connection, now: datetime, tz: ZoneInfo) -> str:
    """What day it is, where in the block, and what is prescribed.

    The prompt carried none of this. It passed `now` into half a dozen renderers
    so they could cut their windows correctly and never told the model what the
    date or the time was, and no block anywhere held the plan — the agent could
    write a prescription and had no way to read one back.

    The visible cost was a coach that answered "what session?" out of the
    athlete's own diary, because THE WEEK AHEAD was the only thing in the prompt
    shaped like a schedule, and reported a session six hours in the future as
    having already happened.
    """
    return agendamod.context(conn, clock.local_day(now, tz), tz, now)


def render_interruption(claimed: Any | None) -> str:
    """The one item the coach may raise this conversation, if any (CHAT-11)."""
    if claimed is None:
        return ""
    return (
        "ONE THING TO RAISE\n"
        f"kind: {claimed.kind}"
        + (f", about: {claimed.ref}" if claimed.ref else "")
        + "\nFold it into a message you were sending anyway, as an aside. Never a "
        "standalone message, never a question, and only once."
    )


def assemble(
    conn: psycopg.Connection,
    now: datetime,
    claimed_interruption: Any | None = None,
    episodic: str = "",
    block_detail: str = "",
    counter: Any = None,
    tz: ZoneInfo | None = None,
) -> ctxmod.AssembledContext:
    """Build the turn's system prompt within the MEM-11 budget.

    Ordering is load-bearing: persona and constraints come first because SAFE-01
    requires the constraints at the top of every prompt, and because a stable
    prefix is what makes prompt caching work — the persona rarely changes, the
    facts change nightly.
    """
    parts = {
        "persona": persona.load(),
        "constraints": render_constraints(conn),
        # Describes the system rather than the athlete, so it caches with the
        # persona. Ordered after the constraints because SAFE-01 requires those
        # at the top of every prompt and nothing displaces them.
        "capabilities": render_capabilities(),
        # First of the volatile blocks, and first for a reason: everything below
        # is evidence about the athlete, and this is where he is standing. It
        # changes every turn, so it sits after the cache breakpoint.
        "today": render_today(conn, now, tz or clock.configured_tz()),
        "facts": render_facts(conn),
        "unreadable": render_unreadable(conn, now, tz or clock.configured_tz()),
        "body_mass": render_body_mass(conn, now, tz or clock.configured_tz()),
        "recovery": render_recovery(conn, now, tz or clock.configured_tz()),
        "calendar": render_calendar(conn, now, tz or clock.configured_tz()),
        "block_detail": block_detail,
        "continuity_note": render_continuity(conn),
        "staleness": render_staleness(conn, now),
        "interruption": render_interruption(claimed_interruption),
        "episodic_recall": episodic,
    }
    parts = {name: body for name, body in parts.items() if body}

    kwargs: dict[str, Any] = {}
    if counter is not None:
        kwargs["counter"] = counter
    return ctxmod.assemble(parts, **kwargs)


# The cached prefix: everything that describes the coach and the system rather
# than the athlete. Named once so the block list and the cache boundary cannot
# disagree about which is which.
STABLE = ("persona", "constraints", "capabilities")


def as_system_blocks(assembled: ctxmod.AssembledContext) -> list[dict[str, Any]]:
    """Render the assembled context as Anthropic system blocks.

    The cache breakpoint sits on the persona and constraints, which change
    rarely. Facts and continuity fall after it and change nightly, so they are
    re-read each day rather than invalidating the stable prefix.
    """
    blocks: list[dict[str, Any]] = []
    stable = [c for c in assembled.components if c.name in STABLE]
    volatile = [c for c in assembled.components if c.name not in STABLE]

    if stable:
        blocks.append(
            {
                "type": "text",
                "text": "\n\n".join(c.body for c in stable),
                "cache_control": {"type": "ephemeral"},
            }
        )
    if volatile:
        blocks.append({"type": "text", "text": "\n\n".join(c.body for c in volatile)})
    return blocks
