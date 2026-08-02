"""The nightly process. `coach-scheduler`.

CONS-01: a nightly job at 03:00 in the athlete's configured timezone.
MEM-12: a nightly export of the active fact set alongside a pg_dump.
CONS-07: unconfirmed facts lose confidence by category half life.

Jobs, one clock, and a ledger so the clock can be crude. The loop wakes every
few minutes, asks which jobs are due, and runs the ones that have not run. Each
job carries its own `Schedule`, because P10's do not share an hour: the morning
message and the evening follow-up are about today, and consolidation at 03:00 is
about yesterday. That is deliberately not cron: a process that was down at
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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Schedule:
    """When a job is due, and which local date it is *about*.

    Those are two different questions, and conflating them is what made this a
    dataclass rather than an hour. Consolidation fires at 03:00 on Tuesday and is
    about Monday. The morning message fires at 06:00 on Tuesday and is about
    Tuesday. Both are once-a-day jobs on the same ledger, so the date they key on
    has to be the date they are about, or a job about today would collide with
    yesterday's row.

    `weekday` is the day the job *fires*, Monday 0 through Sunday 6, so a weekly
    review keeps a normal `covers` — the Sunday review is about the week ending
    that day, and its ledger key is that Sunday.
    """

    hour: int
    minute: int = 0
    weekday: int | None = None
    covers: str = "yesterday"

    def due(self, now: datetime, tz: Any) -> date | None:
        """The local date this job is about, if its time has passed today."""
        local = now.astimezone(tz)
        if self.weekday is not None and local.date().weekday() != self.weekday:
            return None
        if local.time() < time(self.hour, self.minute):
            return None
        if self.covers == "today":
            return local.date()
        return local.date() - timedelta(days=1)


# CONS-01's slot, and the default for anything that does not say otherwise.
NIGHTLY = Schedule(hour=CONSOLIDATION_HOUR)


@dataclass(frozen=True)
class Job:
    run: Callable[[psycopg.Connection, date], Any]
    schedule: Schedule = NIGHTLY


def _as_job(spec: Job | Callable[[psycopg.Connection, date], Any], schedule: Schedule) -> Job:
    """Accept a bare callable, which is what every caller before P10 passes."""
    return spec if isinstance(spec, Job) else Job(run=spec, schedule=schedule)


# NOTIF-05: the times move without a code change. Read at call time rather than
# at import, so a test can set them and a container restart is the only thing
# needed to change them in a deployment.
def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    """An out-of-range or unparseable value falls back rather than raising.

    A typo in a notification time must not stop the scheduler from
    consolidating. The cost of the wrong hour is a message at the wrong time;
    the cost of refusing to start is the whole nightly pass.
    """
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    if not lo <= value <= hi:
        log.warning("%s=%s out of range %d-%d; using %d", name, value, lo, hi, default)
        return default
    return value


def morning_schedule() -> Schedule:
    """NOTIF-01, and about today rather than yesterday — it names today's session."""
    return Schedule(hour=_int_env("COACH_MORNING_HOUR", 6, 0, 23), covers="today")


def follow_up_schedule() -> Schedule:
    """NOTIF-02's 21:00. Also about today: the session it is asking after is today's."""
    return Schedule(hour=_int_env("COACH_FOLLOW_UP_HOUR", 21, 0, 23), covers="today")


def review_schedule() -> Schedule:
    """REV-01. Sunday by default, and about the Sunday it fires on."""
    return Schedule(
        hour=_int_env("COACH_REVIEW_HOUR", 18, 0, 23),
        weekday=_int_env("COACH_REVIEW_WEEKDAY", 6, 0, 6),
        covers="today",
    )


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
    return Schedule(hour=hour).due(now, tz)


def run_due(
    conn: psycopg.Connection,
    now: datetime,
    tz: Any,
    jobs: dict[str, Job | Callable[[psycopg.Connection, date], Any]],
    hour: int = CONSOLIDATION_HOUR,
) -> dict[str, str]:
    """Run whichever jobs are due and unclaimed. Returns what happened to each.

    Each job is asked for its own due date rather than sharing one, because
    P10's jobs do not share an hour: the morning message at 06:00 and the
    follow-up at 21:00 are both about today, and consolidation at 03:00 is about
    yesterday. A job whose time has not come is simply absent from the result.
    """
    default = Schedule(hour=hour)
    outcomes: dict[str, str] = {}
    for name, spec in jobs.items():
        job = _as_job(spec, default)
        target = job.schedule.due(now, tz)
        if target is None:
            continue
        if not claim(conn, name, target):
            continue
        try:
            job.run(conn, target)
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


def sweep_job(api: Any, tz: Any = None) -> Callable[[psycopg.Connection, date], Any]:
    """PLAN-05: "orphan planned events ... are removed on the nightly pass."

    A factory for the same reason `consolidation_job` is one: this module holds no
    upstream client, and giving it one would make the scheduler the second place
    that knows how to talk to intervals.icu.

    Nightly rather than on the ingest loop, and the requirement says so. It is the
    one job here that deletes something the athlete can see, and once a day is
    both what is asked for and the cadence at which a mistake is survivable.
    """

    def job(conn: psycopg.Connection, _on: date) -> Any:
        from coach.plans import sweep

        result = sweep.run(conn, api, datetime.now(UTC), tz or clock.configured_tz())
        log.info(
            "swept %d orphan event(s), left %d past, ignored %d not ours",
            result.count,
            len(result.kept_past),
            result.foreign,
        )
        return result

    return job


def publish_job(api: Any, tz: Any = None) -> Callable[[psycopg.Connection, date], Any]:
    """PLAN-01: put whatever the coach has planned onto the calendar.

    This had no caller at all until it was noticed on a live deployment. The
    publish path, PLAN-04's placement and the workout text were all built and
    tested in P08, and nothing ever invoked them, so a prescription written in
    conversation stayed in the database and never reached intervals.icu.

    Nightly rather than in the turn, for LOG-08's reason: a conversation must
    not wait on a network call, and a failed publish must not lose a plan the
    athlete has just agreed to.
    """

    def job(conn: psycopg.Connection, _on: date) -> Any:
        from coach.plans import publish as publishmod

        zone = tz or clock.configured_tz()
        result = publishmod.publish(conn, api, zone)
        if result.published or result.unplaceable:
            log.info(
                "published %d prescription(s), %d unplaceable",
                len(result.published),
                len(result.unplaceable),
            )
        return result

    return job


def _publish_or_none(tz: Any) -> Callable[[psycopg.Connection, date], Any] | None:
    """Its own client, for the same reason the sweep has one."""
    try:
        from coach.ingest import client as clientmod

        return publish_job(clientmod.Intervals(), tz)
    except Exception as exc:  # noqa: BLE001 - a missing key must not stop the night
        log.warning("PLAN-01 publish not scheduled: %s", exc)
        return None


def _sweep_or_none(tz: Any) -> Callable[[psycopg.Connection, date], Any] | None:
    """PLAN-05's job with its own upstream client, or nothing plus a warning.

    The client is constructed here rather than passed in because this is the only
    process that runs the sweep. It is allowed to fail: an absent or rejected
    `INTERVALS_API_KEY` should cost the sweep and not the two jobs that need no
    network — a night that consolidates and decays but leaves a stale calendar
    entry is a much smaller problem than a night that does nothing.
    """
    try:
        from coach.ingest import client as clientmod

        return sweep_job(clientmod.Intervals(), tz)
    except Exception as exc:  # noqa: BLE001 - a missing key must not stop the night
        log.warning("PLAN-05 sweep not scheduled: %s", exc)
        return None


def _send_or_none() -> Callable[[str], None] | None:
    """A one-argument sender bound to the allowlisted chat, or nothing.

    Constructed here for the same reason the sweep's client is: this process
    should still consolidate when the Telegram token is missing. A scheduler
    that refused to start because it could not send a good-morning message would
    take the nightly memory pass down with it.
    """
    try:
        from coach.runtime import transport
        from coach.telegram import bot as botmod

        allowlist = botmod.Allowlist()
        telegram = transport.Telegram()
    except Exception as exc:  # noqa: BLE001 - the night matters more than the message
        log.warning("P10 notifications not scheduled: %s", exc)
        return None

    def send(text: str) -> None:
        telegram.send(allowlist.chat_id, text)

    return send


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
    jobs: dict[str, Job | Callable[[psycopg.Connection, date], Any]],
    connect=db.connect,
    tz=None,
    tick_s: int = TICK_S,
) -> None:
    """Wake, ask what is due, run it, sleep. Until `stop`."""
    zone = tz or clock.configured_tz()
    for name, spec in sorted(jobs.items()):
        schedule = _as_job(spec, NIGHTLY).schedule
        log.info(
            "scheduled %s at %02d:%02d local%s",
            name,
            schedule.hour,
            schedule.minute,
            "" if schedule.weekday is None else f" on weekday {schedule.weekday}",
        )

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
    #
    # PLAN-05's sweep goes after consolidation and before the export, because
    # consolidation can cancel a prescription and the sweep should remove that
    # session's calendar entry the same night rather than leaving the athlete
    # looking at a session the coach has already withdrawn.
    #
    # Its own client, constructed here: an INTERVALS_API_KEY that is absent or
    # wrong should stop the sweep, not the two jobs that need no network.
    sweep = _sweep_or_none(zone)
    publish = _publish_or_none(zone)

    # P10's three, each on its own hour. The two nudges are sentences and need
    # only a transport, so an absent Telegram token costs the messages and not
    # the night — same reasoning as the sweep.
    #
    # The review takes the model as well. It is still assembled deterministically
    # and still stored that way; the client only voices what the assembly
    # produced, and `voice.say` falls back to the assembled message on any
    # failure. So this is a better message when the model is reachable, not a
    # dependency on it being reachable.
    send = _send_or_none()

    jobs: dict[str, Job | Callable[[psycopg.Connection, date], Any]] = {
        "consolidation": consolidate,
        # PLAN-01 before PLAN-05: an event published tonight must not be swept
        # as an orphan on the same pass.
        **({"publish": publish} if publish else {}),
        **({"sweep": sweep} if sweep else {}),
        "decay": decay_job,
        "export": export_job,
    }
    if send is not None:
        from coach.notify import daily as notifymod
        from coach.review import weekly as reviewmod

        jobs["morning"] = Job(run=notifymod.morning_job(send), schedule=morning_schedule())
        jobs["follow_up"] = Job(run=notifymod.follow_up_job(send), schedule=follow_up_schedule())
        jobs["review"] = Job(
            run=lambda conn, on: reviewmod.run(conn, on, send=send, client=client),
            schedule=review_schedule(),
        )

    serve(stop, jobs, tz=zone)
    log.info("scheduler stopped")
