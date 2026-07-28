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
    """CONS-07: unconfirmed facts lose confidence by category half life."""
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


def consolidation_job(propose: Callable[..., Any]) -> Callable[[psycopg.Connection, date], Any]:
    """CONS-01, bound to a proposer.

    A factory because the pass needs a model and this module holds none — the
    same separation `coach.consolidation.pipeline` already makes, kept rather
    than collapsed.
    """

    def job(conn: psycopg.Connection, on: date) -> Any:
        from coach.consolidation import pipeline

        result = pipeline.run(conn, on, propose)
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

    # The consolidation proposer is the one piece still to be written: CONS-02's
    # strict-JSON diff prompt is P02's remaining half, and it belongs with the
    # pipeline rather than here. Until it exists the scheduler runs the two jobs
    # that need no model, which is honest — decay and the export are real work
    # and they have never run either.
    jobs: dict[str, Callable[[psycopg.Connection, date], Any]] = {
        "decay": decay_job,
        "export": export_job,
    }
    log.warning(
        "consolidation is not scheduled: no proposer is wired. Decay and the export run. "
        "See docs/state-of-build.md."
    )
    del client  # constructed to fail fast on a missing key; unused until the proposer exists

    serve(stop, jobs)
    log.info("scheduler stopped")
