"""The ingest path from a webhook event to a written review.

FIT-01 names two triggers and this module is what both of them call: the
`ACTIVITY_UPLOADED` webhook, and the six hourly reconcile that catches whatever
the webhook dropped. FIT-14's watched folder is the third caller, on the same
tick as the reconcile.

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

from coach.ingest import activities as actmod
from coach.ingest import archive as archivemod
from coach.ingest import client as clientmod
from coach.ingest import parse
from coach.ingest import reconcile as reconcilemod
from coach.ingest import review as reviewmod
from coach.ingest import webhook as webhookmod

log = logging.getLogger(__name__)


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


def on_activity(
    conn: psycopg.Connection,
    client: clientmod.Intervals,
    activity: dict[str, Any],
    tz: ZoneInfo,
    write_note: Callable[[dict[str, Any]], str] = no_review,
    backfilled: bool = False,
) -> Handled:
    """One activity, all the way through: parse, store, match, review.

    The order matters for PERF-03. The file fetch is the slow call, so it happens
    once and its result is reused; nothing here re-reads the activity it was
    handed.
    """
    activity_id = activity.get("id")
    file_bytes, streams = (None, None)
    if activity_id:
        file_bytes, streams = reconcilemod.fetch_parsed_inputs(client, activity_id)

    ingested = actmod.ingest(
        conn, activity, tz, file_bytes=file_bytes, streams=streams, backfilled=backfilled
    )
    result = Handled(session_id=ingested.session_id, created=ingested.created)

    # FIT-15: the archive keeps the bytes even though upstream has them too,
    # because upstream deleting them is the case the archive exists for.
    if file_bytes and activity_id:
        _archive(conn, activity_id, file_bytes, ingested.session_id)

    prescription_id = reviewmod.match(conn, ingested.session_id)
    if prescription_id is not None:
        result.prescription_id = prescription_id
        result.compliance = reviewmod.attach(conn, ingested.session_id, prescription_id).as_dict()

    # review() returns None by itself for a backfilled session (FIT-09); the
    # check is not repeated here so the two cannot disagree.
    result.review = reviewmod.review(conn, ingested.session_id, write_note)
    return result


def _archive(
    conn: psycopg.Connection, activity_id: str, data: bytes, session_id: int | None
) -> None:
    """Keep the downloaded original locally, keyed to the upstream activity."""
    folder = archive_folder()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{activity_id}.fit"
    if not path.exists():
        path.write_bytes(data)
    archivemod.register(conn, path, data)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update fit_archive set session_id = coalesce(session_id, %s), "
            "external_ref = coalesce(external_ref, %s) where sha256 = %s",
            (session_id, activity_id, parse.content_hash(data)),
        )


def archive_folder() -> Path:
    return Path(os.environ.get("COACH_FIT_ARCHIVE", "var/fit-archive"))


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
    """The periodic pass: reconcile, scan the folder, then age out prescriptions.

    FIT-11 for the reconcile, FIT-14 for the folder, FIT-12 for the missed check.
    The order is not arbitrary — the missed check reads sessions, so it has to run
    after both ingest paths have had their chance to produce one.
    """
    moment = now or datetime.now(UTC)
    outcome = reconcilemod.run(
        conn, client, tz, oldest=date.today() - timedelta(days=lookback_days)
    )

    scanned = archivemod.scan(conn, watch_folder(), tz)

    verdicts = reviewmod.missed(conn, moment, tz)
    to_mark = [v["prescription_id"] for v in verdicts if v["missed"]]
    marked = reviewmod.mark_missed(conn, to_mark)

    # Reviews for anything the reconcile or the folder just created. The webhook
    # path reviews inline; this covers the rides it never heard about.
    reviews = []
    for session_id in _unreviewed(conn):
        body = reviewmod.review(conn, session_id, write_note)
        if body:
            reviews.append(session_id)

    return {
        "reconciled": outcome.created + outcome.updated,
        "errors": outcome.errors,
        "scanned": scanned,
        "missed": marked,
        "reviewed": reviews,
    }


def _unreviewed(conn: psycopg.Connection) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            "select id from sessions "
            "where reviewed_at is null and not backfilled order by started_at"
        )
        return [r["id"] for r in cur.fetchall()]
