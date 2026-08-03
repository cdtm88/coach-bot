"""The plan, read back. What is prescribed, when, and whether it has happened.

Everything in `plans/` before this module wrote *outwards* — a prescription to
an upstream calendar, an edit back into the plan, an orphan swept. Nothing read
the plan back for the coach, and it turned out nothing anywhere did.

**That gap was the whole bug.** The agent's tool surface could create
prescriptions (`write_session_events`) and had no way to see one. The prompt
carried facts, body mass, recovery, staleness and the athlete's *diary*, and not
one line of what he was supposed to be doing. So when he asked "what session?",
the coach had no session to answer with. What it did have was a block headed THE
WEEK AHEAD holding calendar entries from his own iCal feed, which is the only
thing in the prompt shaped like a schedule — and it answered out of that,
reporting a 16:45 commitment called "Zwift Ride Test" as the training session,
at half past seven in the morning, in the past tense.

Neither half of that was the model being careless. It was not told the time and
it was not told the plan.

So: one module that reads prescriptions, used by the prompt block and by the
tool, because "what is planned" should not have two implementations that can
disagree about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from coach.blocks import document as blockmod

# Statuses that describe a session still expected to happen. 'cancelled' and
# 'suspended' are deliberately absent: a session withdrawn by the coach or
# suspended by a break is not something he should be told is coming.
OPEN = ("planned", "adjusted")

# How far ahead the prompt's block looks. The training week, and no further —
# the coach reasons about this week and next Sunday reasons about the next one.
HORIZON_DAYS = 7


@dataclass(frozen=True)
class Scheduled:
    """One prescription, as something to be said rather than published."""

    id: int
    planned_for: datetime
    discipline: str
    spec: dict[str, Any]
    status: str
    done: bool

    @property
    def minutes(self) -> int:
        return int(self.spec.get("duration_s") or 0) // 60

    def describe(self) -> str:
        """The session in one line, with every number it actually carries.

        Deliberately not `notify.daily._describe`, which names a session on the
        morning of it and needs no target: this is the line the coach answers
        "what session?" with, and a duration alone is what he complained about
        getting.
        """
        parts = [self.discipline]
        if self.minutes:
            parts.append(f"{self.minutes} min")

        if self.spec.get("target_watts"):
            watts = int(self.spec["target_watts"])
            factor = self.spec.get("intensity_factor")
            parts.append(f"{watts} W" + (f" (IF {float(factor):.2f})" if factor else ""))
        elif self.spec.get("intensity_factor"):
            parts.append(f"IF {float(self.spec['intensity_factor']):.2f}")
        elif self.spec.get("rpe_target"):
            parts.append(f"RPE {float(self.spec['rpe_target']):g}")

        line = ", ".join(parts)
        if self.spec.get("purpose"):
            line += f". {self.spec['purpose']}"
        return line


_SQL = """
select p.id, p.planned_for, p.discipline, p.spec, p.status,
       -- Whether anything landed for the day. FIT-10 attributes a session to
       -- its local date, so this is the same question the follow-up asks
       -- before deciding the athlete has not trained.
       exists (
         select 1 from sessions s
          where s.local_date = (p.planned_for at time zone 'UTC')::date
       ) as done
  from prescriptions p
 where (p.planned_for at time zone 'UTC')::date between %s and %s
   and p.status <> 'cancelled'
 order by p.planned_for
"""


def between(conn: psycopg.Connection, since: date, until: date) -> list[Scheduled]:
    """Every prescription in a local date range, in the order they happen."""
    with conn.cursor() as cur:
        cur.execute(_SQL, (since, until))
        return [
            Scheduled(
                id=row["id"],
                planned_for=row["planned_for"],
                discipline=row["discipline"],
                spec=row["spec"] or {},
                status=row["status"],
                done=bool(row["done"]),
            )
            for row in cur.fetchall()
        ]


def on(conn: psycopg.Connection, day: date) -> list[Scheduled]:
    return between(conn, day, day)


def _block_position(block: blockmod.Block | None, today: date) -> str:
    """Where in the block today falls. "Week 2 of 8", not a row id.

    The athlete asked where they were in the plan and there was nowhere in the
    prompt that could have answered.
    """
    if block is None:
        return "No training block is active."
    week = max(1, (today - block.starts_on).days // 7 + 1)
    if block.weeks:
        return f'Week {week} of {block.weeks} in "{block.title}", which started {block.starts_on}.'
    return f'Week {week} of "{block.title}", which started {block.starts_on}.'


def _status_of(item: Scheduled) -> str:
    if item.status == "completed":
        return "done"
    if item.status == "missed":
        return "recorded as missed"
    if item.status == "suspended":
        return "suspended by a break"
    if item.done:
        # An activity landed on the day and nothing has matched it to this
        # prescription yet. Saying "not done" here is how the coach ends up
        # asking whether he trained on a day he has already uploaded.
        return "something was uploaded for this day, not yet matched to it"
    return "not done yet"


def context(conn: psycopg.Connection, today: date, tz: ZoneInfo, now: datetime) -> str:
    """The TODAY block: what day it is, where in the plan, and what is on.

    First block after the persona and constraints, and the one whose absence was
    most visible. It states the date and the local clock time outright — the
    coach was reporting a 16:45 session as having already happened at 07:32,
    which is not a reasoning failure but an unanswerable question.
    """
    local = now.astimezone(tz)
    lines = [
        "TODAY",
        f"{local:%A} {local.day} {local:%B %Y}, {local:%H:%M} local.",
        _block_position(blockmod.active(conn), today),
        "",
    ]

    scheduled = between(conn, today, today + timedelta(days=HORIZON_DAYS))
    todays = [s for s in scheduled if s.planned_for.date() == today]
    if not todays:
        lines.append("Nothing is prescribed for today.")
    else:
        for item in todays:
            when = item.planned_for.astimezone(tz).strftime("%H:%M")
            lines.append(f"Today at {when}: {item.describe()} [{_status_of(item)}]")

    ahead = [s for s in scheduled if s.planned_for.date() > today and s.status in OPEN]
    if ahead:
        lines.append("")
        lines.append("Still to come in the next seven days:")
        for item in ahead:
            when = item.planned_for.astimezone(tz)
            lines.append(f"- {when:%a %d %b} {when:%H:%M}: {item.describe()}")

    lines.append("")
    # The line that stops the confusion the whole module exists for. THE WEEK
    # AHEAD sits below this in the prompt and is the athlete's own diary; it was
    # being read as the training plan because it was the only thing here shaped
    # like one.
    lines.append(
        "This block is the training plan. Anything under HIS DIARY is his own calendar "
        "— meetings, travel, commitments he entered himself — and is never a session "
        "you prescribed, whatever it is called."
    )
    return "\n".join(lines)
