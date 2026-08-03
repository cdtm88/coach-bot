"""One-off: match the rides that the live path never matched. `coach-reconcile`.

**This exists because of a specific defect and can be deleted once it has been
run.** Until 3 August 2026 the poll path called `review.review` alone, so no ride
ingested by the running deployment was ever matched to its prescription:
`sessions.prescription_id` stayed null, prescriptions stayed 'planned', and
compliance was never frozen. `docs/debug/resolved/` records the whole of it.

The fix in `ingest.service.finish` closes the loop for every ride from now on and
does nothing for the ones already past. It cannot: `poll` only considers sessions
with `reviewed_at is null`, and the affected sessions were all reviewed. That is
correct behaviour and it is why this is a separate, deliberate command rather
than something that quietly happens on the next pass.

**It is a dry run unless told otherwise.** This writes to the athlete's real
training history: prescriptions become 'completed', compliance is frozen against
them, and both feed the Sunday review's adherence. Printing the plan and
requiring `--apply` is the difference between a maintenance command and a
mistake nobody can see.

**What it deliberately does not do.**

*It does not review.* The affected sessions already have their reviews;
re-reviewing would write a second note per session and spend a model call for
each, to say again what was said at the time.

*It does not adjust.* Running P09's rules over history is the "backfill replaying
two years of rides" case that `service.finish` guards against by parameter.
ADJ-04 would reject each one for being outside the current week, but a backfill
is the one path where "would have been rejected anyway" is not good enough.

*It does not claim a suspended prescription.* `review.match` has two paths, and
the `paired_event_id` one filters on `session_id is null` without checking
status. Live that is nearly harmless, because a break cancels its events
upstream so nothing stays paired for long. Over months of history it is not: a
prescription suspended by a break is one the coach agreed did not happen, and
claiming it retroactively would erase the break from the record. So the status
is re-checked here rather than trusted.

**The dry run is the real thing, rolled back.** `review.match` is written for one
session at a time and answers from what is stored, so a dry run that only
*reads* would offer the same prescription to every ride on a day and then, on
apply, hand it to the first and drop the rest. The plan would promise something
the apply could not keep.

Simulating it in a transaction and rolling back solves that exactly rather than
approximately, and it does so without a second copy of the matching rules. A
batch-aware matcher written here would be the fourth place in this repository
where two implementations have to agree about the same thing, and the previous
three all turned out to disagree. It also removes any window between planning
and applying, because `--apply` runs the same pass and simply keeps it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from coach import db
from coach.ingest import review as reviewmod

log = logging.getLogger(__name__)

# The only statuses a past prescription may be moved out of. 'completed' and
# 'missed' are settled; 'cancelled' and 'suspended' are decisions.
OPEN_STATUSES = ("planned", "adjusted")


@dataclass(frozen=True)
class Match:
    """One ride and the prescription it should have closed."""

    session_id: int
    local_date: date
    discipline: str
    prescription_id: int
    planned_for: Any
    prescribed_discipline: str

    def describe(self) -> str:
        return (
            f"  session {self.session_id:>5}  {self.local_date}  {self.discipline:<14} "
            f"->  prescription {self.prescription_id:>5}  {self.prescribed_discipline}"
        )


def unmatched_sessions(conn: psycopg.Connection, since: date | None = None) -> list[dict[str, Any]]:
    """Rides that carry no prescription, oldest first.

    Backfilled sessions are excluded for FIT-09's reason: loading history
    produces session rows and rollups, and was never meant to close a plan.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, local_date, discipline
              from sessions
             where prescription_id is null
               and not backfilled
               and (%s::date is null or local_date >= %s)
             order by local_date, id
            """,
            (since, since),
        )
        return cur.fetchall()


def _status_of(conn: psycopg.Connection, prescription_id: int) -> tuple[str, Any, str] | None:
    with conn.cursor() as cur:
        cur.execute(
            "select status, planned_for, discipline from prescriptions where id = %s",
            (prescription_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row["status"], row["planned_for"], row["discipline"]


class _Rollback(Exception):
    """Ends the simulation. Never escapes :func:`plan`."""


def _reconcile(conn: psycopg.Connection, since: date | None) -> list[Match]:
    """Match and attach every eligible ride. Writes, unless the caller rolls back.

    The status re-check is the one rule this adds to `review.match`, and it is
    for the `paired_event_id` path, which filters on `session_id is null` without
    looking at status. See the module docstring.
    """
    written: list[Match] = []

    for session in unmatched_sessions(conn, since):
        prescription_id = reviewmod.match(conn, session["id"])
        if prescription_id is None:
            continue

        current = _status_of(conn, prescription_id)
        if current is None:  # pragma: no cover - match only returns real rows
            continue
        status, planned_for, prescribed = current
        if status not in OPEN_STATUSES:
            log.info(
                "session %s matches prescription %s, which is %s; leaving it alone",
                session["id"],
                prescription_id,
                status,
            )
            continue

        reviewmod.attach(conn, session["id"], prescription_id)
        written.append(
            Match(
                session_id=session["id"],
                local_date=session["local_date"],
                discipline=session["discipline"],
                prescription_id=prescription_id,
                planned_for=planned_for,
                prescribed_discipline=prescribed,
            )
        )
    return written


def plan(conn: psycopg.Connection, since: date | None = None) -> list[Match]:
    """What `apply` would do, simulated and rolled back.

    Really performed and then discarded, so `review.match` sees each attachment
    as it happens and the second ride on a day is offered the second
    prescription rather than the one already taken.
    """
    found: list[Match] = []
    try:
        with conn.transaction():
            found = _reconcile(conn, since)
            raise _Rollback
    except _Rollback:
        pass
    return found


def apply(conn: psycopg.Connection, since: date | None = None) -> list[Match]:
    """The same pass, kept.

    No re-check against a previously printed plan, because there is no window to
    re-check across: this does its own matching against the database as it is
    now.
    """
    return _reconcile(conn, since)


def render(matches: list[Match], applied: bool) -> str:
    if not matches:
        return (
            "Nothing to reconcile. Every ride that could be matched already is, "
            "which is what a healthy database looks like after the fix has been "
            "running for a while."
        )

    verb = "Matched" if applied else "Would match"
    lines = [f"{verb} {len(matches)} ride(s) to their prescriptions:", ""]
    lines.extend(m.describe() for m in matches)
    lines.append("")
    if applied:
        lines.append(
            "Those prescriptions are now 'completed' with compliance frozen against "
            "the ride. Past weeks' adherence in the Sunday review will change to "
            "match."
        )
    else:
        lines.append("Nothing was written. Re-run with --apply to do it.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coach-reconcile",
        description=(
            "Match rides that the live ingest path left unmatched before "
            "3 August 2026. Dry run unless --apply is given."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the matches. Without this, only prints what it would do.",
    )
    parser.add_argument(
        "--since",
        type=date.fromisoformat,
        help="only consider rides on or after this local date.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(message)s")

    with db.connect() as conn:
        matches = apply(conn, args.since) if args.apply else plan(conn, args.since)
        print(render(matches, applied=args.apply))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
