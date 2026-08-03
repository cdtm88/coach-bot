"""The ingest path from a new activity to a written review.

Three callers reach this, and FIT-01 no longer prefers one of them on principle:

* :func:`poll` — the fast loop. Asks intervals.icu what is new and scans the
  watched folder. This is the primary path, because webhooks need a registered
  app and that dependency was not worth blocking ingest on.
* :func:`drain` — the webhook queue, idle unless an app is ever registered. Built
  and tested, so switching it on is configuration rather than code.
* :func:`sweep` — the slow loop, for the one job that has nothing to gain from
  running often.

Keeping the pipeline here rather than in the HTTP handler is deliberate. PERF-03
gives five minutes from file arrival to review, and almost all of that budget is
upstream round trips and one model call, so the ordering of those calls is the
thing worth testing. A handler that could only be exercised by standing up a
socket would not get tested at that level.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from coach import feeds as feedmod
from coach.ingest import activities as actmod
from coach.ingest import archive as archivemod
from coach.ingest import client as clientmod
from coach.ingest import reconcile as reconcilemod
from coach.ingest import review as reviewmod
from coach.ingest import webhook as webhookmod

log = logging.getLogger(__name__)

# How far back the fast poll asks for. Short on purpose: running every couple of
# minutes, anything older than this is either already stored or is the wide
# sweep's problem. Widening it makes every poll carry more rows for no gain.
POLL_LOOKBACK_DAYS = 2


def no_review(_context: dict[str, Any]) -> str:
    """A note writer that declines.

    The default, so that calling into this module never costs a model call by
    accident. Only the callers that want a review pass one in.
    """
    return ""


@dataclass
class Handled:
    """What one event produced. Every field is something a test can assert on."""

    session_id: int | None = None
    created: bool = False
    prescription_id: int | None = None
    compliance: dict[str, Any] = field(default_factory=dict)
    review: str | None = None
    skipped: str = ""
    # P09. Empty unless `adjust` was asked for and a rule fired.
    adjusted: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)


def on_activity(
    conn: psycopg.Connection,
    client: clientmod.Intervals,
    activity: dict[str, Any],
    tz: ZoneInfo,
    write_note: Callable[[dict[str, Any]], str] = no_review,
    backfilled: bool = False,
    adjust: bool = False,
    now: datetime | None = None,
    send: Callable[[str], None] | None = None,
) -> Handled:
    """One activity, all the way through: parse, store, match, review, adjust.

    The order matters for PERF-03. The file fetch is the slow call, so it happens
    once and its result is reused; nothing here re-reads the activity it was
    handed.

    `adjust` is off by default and P09's trigger rules only run when it is on.
    That is not timidity: a backfill replaying two years of rides must not
    restructure anything. ADJ-04 would reject each one for being outside the
    current week, but the rules would still have run over history, and a backfill
    is the one path where "would have been rejected anyway" is not good enough —
    `backfilled` is passed separately and is checked here too.
    """
    activity_id = activity.get("id")
    file_bytes, streams = (None, None)
    if activity_id:
        file_bytes, streams = reconcilemod.fetch_parsed_inputs(client, activity_id)

    ingested = actmod.ingest(
        conn, activity, tz, file_bytes=file_bytes, streams=streams, backfilled=backfilled
    )
    result = Handled(session_id=ingested.session_id, created=ingested.created)

    # OBS-05: an activity read and stored is the activities feed working, and the
    # webhook drain never goes through reconcile, which is where the poll stamps
    # it. Without this the feed reads as never-successful on a deployment that
    # has a registered app.
    feedmod.record_success(conn, feedmod.ACTIVITIES)

    # FIT-15: the archive keeps the bytes even though upstream has them too,
    # because upstream deleting them is the case the archive exists for.
    if file_bytes and activity_id:
        archivemod.keep_original(conn, activity_id, file_bytes, ingested.session_id)

    return finish(
        conn, result, tz, write_note, backfilled=backfilled, adjust=adjust, now=now, send=send
    )


def finish(
    conn: psycopg.Connection,
    result: Handled,
    tz: ZoneInfo,
    write_note: Callable[[dict[str, Any]], str] = no_review,
    backfilled: bool = False,
    adjust: bool = False,
    now: datetime | None = None,
    send: Callable[[str], None] | None = None,
) -> Handled:
    """Everything that happens once a session row exists: match, freeze, review, adjust.

    **This is shared because it was not, and the poll never did any of it.** Both
    ingest paths produce a session row and then owe it the same four steps.
    `on_activity` did all four and its only caller is the webhook drain, which is
    idle; `poll` is the live path and it did the third one alone. So on the
    running deployment no ride was ever matched to a prescription: sessions kept
    a null `prescription_id`, prescriptions stayed 'planned' for ever, compliance
    was never frozen, and every ADJ rule read a figure that did not exist.

    The FIT-12 sweep hid it rather than surfacing it, and correctly: a
    prescription with a session on the same day is reported "unmatched rather
    than missed", so nothing was ever wrongly called a miss. It simply stayed
    open, which looks like a plan nobody is following.

    One function rather than two call sites in step, because two sites that must
    agree about the order of four operations is exactly the seam this project
    keeps finding defects in.
    """
    session_id = result.session_id
    if session_id is None:  # pragma: no cover - a Handled without a session
        return result

    # FIT-05, and skipped when the row already carries one: `match` claims an
    # unmatched prescription, so calling it twice for one session would take a
    # second prescription for a ride that already has one.
    with conn.cursor() as cur:
        cur.execute("select prescription_id from sessions where id = %s", (session_id,))
        row = cur.fetchone()
    already = row and row["prescription_id"]

    prescription_id = already or reviewmod.match(conn, session_id)
    if prescription_id is not None:
        result.prescription_id = prescription_id
        if not already:
            result.compliance = reviewmod.attach(conn, session_id, prescription_id).as_dict()

    # review() returns None by itself for a backfilled session (FIT-09); the
    # check is not repeated here so the two cannot disagree.
    result.review = reviewmod.review(conn, session_id, write_note)

    # ADJ-01: the rules run on ingest, after compliance is frozen — they read it,
    # so the order is a dependency and not a preference.
    if adjust and not backfilled and result.prescription_id is not None:
        from coach.adjust import pass_ as adjustmod

        outcome = adjustmod.run(conn, session_id, now or datetime.now(UTC), tz, send=send)
        result.adjusted = [f"{a.trigger}:{a.action}" for a in outcome.applied]
        result.deferred = list(outcome.deferred)

    return result


def watch_folder() -> Path:
    return Path(os.environ.get("COACH_FIT_WATCH", "var/fit-inbox"))


def receive(conn: psycopg.Connection, payload: dict[str, Any], secret: str | None = None) -> int:
    """SEC-02 and PERF-03: verify and queue. Does no work and touches no network.

    This is everything the HTTP handler is allowed to do before answering.
    intervals.icu retries any non-2xx with exponential backoff, and it treats a
    slow response as a failure — the developer found a `204` was being retried
    until he fixed it — so the response has to be immediate and the work has to
    happen after it.
    """
    return len(webhookmod.accept(conn, payload, secret))


def drain(
    conn: psycopg.Connection,
    client: clientmod.Intervals,
    tz: ZoneInfo,
    write_note: Callable[[dict[str, Any]], str] = no_review,
    limit: int = 10,
) -> list[Handled]:
    """Process queued deliveries. The work half of what `receive` accepted.

    A delivery that throws goes back to pending and is retried on the next pass
    rather than being lost, which is the whole reason the queue exists.
    """
    handled: list[Handled] = []
    for delivery in webhookmod.claim(conn, limit):
        try:
            result = _handle_delivery(conn, client, delivery, tz, write_note)
        except Exception as exc:  # noqa: BLE001 - one bad delivery must not stop the drain
            log.exception("delivery %s failed", delivery["id"])
            status = webhookmod.finish(conn, delivery["id"], ok=False, reason=str(exc))
            handled.append(Handled(skipped=f"failed ({status}): {exc}"))
            continue
        webhookmod.finish(conn, delivery["id"], ok=True, reason=result.skipped)
        handled.append(result)
    return handled


def _handle_delivery(
    conn: psycopg.Connection,
    client: clientmod.Intervals,
    delivery: dict[str, Any],
    tz: ZoneInfo,
    write_note: Callable[[dict[str, Any]], str],
) -> Handled:
    """One queued delivery, dispatched on its event type."""
    kind = delivery["event_type"]
    external_ref = delivery["external_ref"]

    if kind == webhookmod.TRIGGER:
        if not external_ref:
            return Handled(skipped="trigger carried no activity id")
        # The webhook body carries the activity, but only some of it. Re-reading
        # gets the icu_ fields that FIT-03 stores as the platform's opinion.
        activity = client.activity(external_ref)
        return on_activity(conn, client, activity, tz, write_note)

    if kind == "ACTIVITY_ANALYZED":
        return refresh_derived(conn, client, external_ref)

    return Handled(skipped=f"{kind} recorded; no handler in this phase")


def refresh_derived(
    conn: psycopg.Connection, client: clientmod.Intervals, external_ref: str | None
) -> Handled:
    """Update the platform's numbers on a session that already exists.

    ACTIVITY_UPLOADED fires before the platform has finished consolidating, so
    the icu_ fields read at trigger time are provisional and `analyzed` is null.
    ACTIVITY_ANALYZED is the signal they are final. Only the derived block and
    the analysis stamp are touched: FIT-03's parsed columns came from samples and
    have nothing to learn from a later read, and no review is generated, because
    the ride was already reviewed when it landed.
    """
    if not external_ref:
        return Handled(skipped="analysis event carried no activity id")

    with conn.cursor() as cur:
        cur.execute("select id from sessions where external_ref = %s", (external_ref,))
        row = cur.fetchone()
    if row is None:
        return Handled(skipped="no local session for this activity yet")

    activity = client.activity(external_ref)
    analyzed_at = actmod.analyzed_at_of(activity)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update sessions set derived = %s, analyzed_at = %s, derived_provisional = %s "
            "where id = %s",
            (
                Jsonb(actmod.derived_fields(activity)),
                analyzed_at,
                analyzed_at is None,
                row["id"],
            ),
        )
    return Handled(session_id=row["id"], created=False, skipped="derived fields refreshed")


def tick(
    conn: psycopg.Connection,
    client: clientmod.Intervals,
    tz: ZoneInfo,
    now: datetime | None = None,
    write_note: Callable[[dict[str, Any]], str] = no_review,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """A poll and a sweep in one call. Kept for callers that want both.

    The running process does not use this — it runs the two on different clocks,
    which is the whole point of separating them.
    """
    return {**poll(conn, client, tz, write_note, lookback_days), **sweep(conn, tz, now)}


def poll(
    conn: psycopg.Connection,
    client: clientmod.Intervals,
    tz: ZoneInfo,
    write_note: Callable[[dict[str, Any]], str] = no_review,
    lookback_days: int = POLL_LOOKBACK_DAYS,
    adjust: bool = False,
    now: datetime | None = None,
    send: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Find new work and process it. The fast loop.

    Without a webhook this is the primary ingest path rather than a backstop, so
    it runs on the order of minutes. Two sources, both cheap:

    FIT-11's reconcile costs exactly one API call to list the window, and only
    pays for a file download when it finds something it has not seen. A window of
    a couple of days is plenty when this runs every few minutes; the wide sweep
    catches anything older that went missing.

    FIT-14's watched folder costs nothing at all — no network, no credential. For
    a Zwift ride synced from the local Activities directory this sees the file
    before intervals.icu does, and it keeps working when intervals.icu does not.
    """
    outcome = reconcilemod.run(
        conn, client, tz, oldest=date.today() - timedelta(days=lookback_days)
    )
    scanned = archivemod.scan(conn, watch_folder(), tz)

    # Match, freeze, review and adjust anything either path just created. This
    # called `review` alone until 3 August 2026, which meant the live path never
    # matched a ride to its prescription: see `finish`.
    reviews: list[int] = []
    adjusted: list[str] = []
    deferred: list[str] = []
    for session_id in _unreviewed(conn):
        handled = finish(
            conn,
            Handled(session_id=session_id),
            tz,
            write_note,
            adjust=adjust,
            now=now,
            send=send,
        )
        if handled.review:
            reviews.append(session_id)
        adjusted.extend(handled.adjusted)
        deferred.extend(handled.deferred)

    return {
        "reconciled": outcome.created + outcome.updated,
        "errors": outcome.errors,
        "scanned": scanned,
        "reviewed": reviews,
        "adjusted": adjusted,
        "deferred": deferred,
        # Files in the permanent archive that yield no session. A standing count
        # rather than this pass's, because that is the question worth asking:
        # the log says it once and then never again, so without this the only
        # record of a file nobody can read is a row nothing reads either.
        "unreadable_files": len(archivemod.unreadable(conn)),
    }


def sweep(conn: psycopg.Connection, tz: ZoneInfo, now: datetime | None = None) -> dict[str, Any]:
    """Age out prescriptions nothing satisfied. The slow loop.

    Separated from the poll because FIT-12's grace window is 18 hours past the
    local day end. Asking every two minutes whether an 18 hour deadline has passed
    is eighteen hours of identical answers, and the check reads every open
    prescription to produce them.

    It has to run after the poll has had its chance to produce a session, which
    is why the missed verdict cross checks the day's sessions rather than trusting
    the absence of a match.
    """
    moment = now or datetime.now(UTC)
    verdicts = reviewmod.missed(conn, moment, tz)
    to_mark = [v["prescription_id"] for v in verdicts if v["missed"]]
    return {"missed": reviewmod.mark_missed(conn, to_mark)}


def _unreviewed(conn: psycopg.Connection) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            "select id from sessions "
            "where reviewed_at is null and not backfilled order by started_at"
        )
        return [r["id"] for r in cur.fetchall()]
