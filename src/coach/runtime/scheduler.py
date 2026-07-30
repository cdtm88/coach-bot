"""The nightly process. `coach-scheduler`.

CONS-01: a nightly job at 03:00 in the athlete's configured timezone.
MEM-12: a nightly export of the active fact set alongside a pg_dump.
CONS-07: unconfirmed facts lose confidence by category half life.

Three jobs, one clock, and a ledger so the clock can be crude. The loop wakes
every few minutes, asks which jobs are due for today's local date, and runs the
ones that have not run. That is deliberately not cron: a process that was down at
03:00 must still consolidate when it comes back, and OBS-08 caps the retries
rather than the schedule.

**The date is the idempotency key.** CONS-10 already makes a re-run for the same
date produce nothing new, so the ledger here is an optimisation and a record
rather than the guarantee. Two mechanisms for one property is usually a smell;
here the ledger is what makes "did last night run?" answerable, which OBS-04
needs and CONS-10 does not provide.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import psycopg

from coach import clock, db
from coach.memory import export as exportmod
from coach.memory import facts as factmod

log = logging.getLogger(__name__)

# CONS-01 names the hour. Local, per TZ-01, which is the whole reason this
# cannot be a UTC cron entry.
CONSOLIDATION_HOUR = 3

# How often the loop wakes to ask whether anything is due. Fine enough that a job
# runs within a few minutes of its hour, coarse enough to be free.
TICK_S = 300

# OBS-08: at most one run per date and at most one retry on failure.
MAX_ATTEMPTS = 2


def _ensure_ledger(conn: psycopg.Connection) -> None:
    """The job ledger, created on first use rather than in a migration.

    A migration would be the usual home. This is not schema the requirements
    describe — it is bookkeeping for a process — and putting it in `migrations/`
    would make it look like part of the memory design that a reviewer should map
    to `docs/memory-design.md`. It is not; it is the scheduler's own notebook.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            create table if not exists scheduled_runs (
              job          text not null,
              local_date   date not null,
              attempts     int not null default 0,
              status       text not null default 'pending'
                           check (status in ('pending', 'succeeded', 'failed')),
              last_error   text,
              started_at   timestamptz not null default now(),
              finished_at  timestamptz,
              primary key (job, local_date)
            )
            """
        )


def claim(conn: psycopg.Connection, job: str, local_date: date) -> bool:
    """Take today's slot for a job, or report that it is taken.

    False when the job has already succeeded for this date, or has failed its
    way through the attempt ceiling. OBS-08: "a failing run cannot loop; the
    second failure logs and waits for the next night."
    """
    _ensure_ledger(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into scheduled_runs (job, local_date, attempts, status)
            values (%s, %s, 1, 'pending')
            on conflict (job, local_date) do update
                set attempts = scheduled_runs.attempts + 1,
                    started_at = now()
             where scheduled_runs.status = 'failed'
               and scheduled_runs.attempts < %s
            returning attempts
            """,
            (job, local_date, MAX_ATTEMPTS),
        )
        return cur.fetchone() is not None


def finish(conn: psycopg.Connection, job: str, local_date: date, ok: bool, error: str = "") -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update scheduled_runs set status = %s, last_error = %s, finished_at = now() "
            "where job = %s and local_date = %s",
            ("succeeded" if ok else "failed", error[:500] or None, job, local_date),
        )


def due(now: datetime, tz: Any, hour: int = CONSOLIDATION_HOUR) -> date | None:
    """The local date to consolidate, if the hour has passed. TZ-01.

    Returns the *previous* local day: at 03:00 on Tuesday the pass consolidates
    Monday, which is the day that has finished. Returns None before the hour, so
    a process started at midnight waits rather than consolidating a day that is
    still happening.
    """
    local = now.astimezone(tz)
    if local.time() < time(hour):
        return None
    return local.date() - timedelta(days=1)


def run_due(
    conn: psycopg.Connection,
    now: datetime,
    tz: Any,
    jobs: dict[str, Callable[[psycopg.Connection, date], Any]],
    hour: int = CONSOLIDATION_HOUR,
) -> dict[str, str]:
    """Run whichever jobs are due and unclaimed. Returns what happened to each."""
    target = due(now, tz, hour)
    if target is None:
        return {}

    outcomes: dict[str, str] = {}
    for name, job in jobs.items():
        if not claim(conn, name, target):
            continue
        try:
            job(conn, target)
        except Exception as exc:  # noqa: BLE001 - one failing job must not stop the rest
            log.exception("%s failed for %s", name, target)
            finish(conn, name, target, ok=False, error=str(exc))
            outcomes[name] = f"failed: {exc}"
            continue
        finish(conn, name, target, ok=True)
        outcomes[name] = "succeeded"
    return outcomes


# --- the jobs ----------------------------------------------------------------


def decay_job(conn: psycopg.Connection, _on: date) -> None:
    """CONS-07: unconfirmed facts lose confidence by category half life.

    Consolidation's step 9 decays too, so on most nights this finds nothing to
    do. It is not redundant: `pipeline.run` returns before step 9 on a day with
    no messages and no telemetry, and a fact does not stop ageing because the
    athlete was quiet. `apply_decay` recomputes from the curve rather than
    stepping down from the stored value, so running both is idempotent — which is
    what makes covering the gap this cheap.
    """
    changed = factmod.apply_decay(conn)
    log.info("decayed %d facts", changed)


def export_job(conn: psycopg.Connection, _on: date) -> None:
    """MEM-12: the human readable fact export.

    The pg_dump half of MEM-12 is the deployment's job — it needs credentials
    and a destination this process has no business holding — and `docs/setup.md`
    owns it. This is the markdown half, which is the one a person reads.
    """
    path = exportmod.write(conn, Path(os.environ.get("COACH_EXPORT_DIR", "backups")))
    log.info("exported facts to %s", path)


def consolidation_job(
    propose: Callable[..., Any], tz: Any = None
) -> Callable[[psycopg.Connection, date], Any]:
    """CONS-01, bound to a proposer.

    A factory because the pass needs a model and this module holds none — the
    same separation `coach.consolidation.pipeline` already makes, kept rather
    than collapsed.

    The offset is TZ-01 and it is not cosmetic. `pipeline.gather` windows on
    `local midnight - tz_offset`, and its default of zero windows on a UTC day.
    In Asia/Dubai that misses everything the athlete sent between local midnight
    and 04:00 and pulls in the next day's small hours instead — a message at
    01:00 would be consolidated into the wrong day, or twice, or not at all.
    """

    def job(conn: psycopg.Connection, on: date) -> Any:
        from coach.consolidation import pipeline

        zone = tz or clock.configured_tz()
        # The offset *on the day being consolidated*, not today's. They differ
        # across a DST boundary, and the window has to match the day it claims.
        offset = datetime.combine(on, time(12)).replace(tzinfo=zone).utcoffset() or timedelta(0)
        result = pipeline.run(conn, on, propose, tz_offset=offset)
        log.info("consolidated %s: %s", on, result)
        return result

    return job


def serve(
    stop: threading.Event,
    jobs: dict[str, Callable[[psycopg.Connection, date], Any]],
    connect=db.connect,
    tz=None,
    tick_s: int = TICK_S,
) -> None:
    """Wake, ask what is due, run it, sleep. Until `stop`."""
    zone = tz or clock.configured_tz()
    log.info("scheduler running %s at %02d:00 local", sorted(jobs), CONSOLIDATION_HOUR)

    while not stop.is_set():
        try:
            with connect() as conn:
                outcomes = run_due(conn, datetime.now(UTC), zone, jobs)
            if outcomes:
                log.info("nightly: %s", outcomes)
        except Exception:
            log.exception("scheduler tick failed; retrying next interval")
        stop.wait(tick_s)


def main() -> None:
    logging.basicConfig(level=os.environ.get("COACH_LOG_LEVEL", "INFO"))

    from coach.runtime import models

    client = models.build_client()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    # Resolved once and handed to both, rather than left to each to read for
    # itself. TZ-03 turns on the whole process agreeing on one answer, and the
    # hour that decides what is due has to be the hour the window is cut on.
    zone = clock.configured_tz()

    # Consolidation needs a connection to bind its proposer to, and this process
    # holds none between ticks — `serve` opens one per wake. So the job is built
    # per run, from whatever connection the tick is using.
    def consolidate(conn: psycopg.Connection, on: date) -> Any:
        from coach.consolidation import propose

        return consolidation_job(propose.build(client, conn), zone)(conn, on)

    # CONS-01 first: consolidation writes the day's facts, and decay should run
    # against what it wrote rather than against yesterday's picture. Python keeps
    # insertion order and `run_due` iterates in it, so this ordering is the
    # schedule. The export runs last so the file reflects both.
    jobs: dict[str, Callable[[psycopg.Connection, date], Any]] = {
        "consolidation": consolidate,
        "decay": decay_job,
        "export": export_job,
    }

    serve(stop, jobs, tz=zone)
    log.info("scheduler stopped")
